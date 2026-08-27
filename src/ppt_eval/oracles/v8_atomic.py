"""Scoped deterministic observation Oracles for the v8 evaluation candidate.

The existing v1-v7 Oracles return one score per metric.  This module deliberately
stops one layer earlier: each Oracle emits independently auditable observations at
the page, object, requirement, asset, chart or slide-pair scope.  A reducer owned by
the profile may later combine those observations without losing the lower tail.

Nothing in this module is registered automatically.  The old replay contracts stay
unchanged until a versioned v8 profile explicitly opts into these Oracles.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

from ppt_eval.adapters.pptx import (
    ParsedPresentation,
    ParsedSlide,
    PptxAdapter,
    SlideObject,
)
from ppt_eval.domain import (
    AtomicObservation,
    EvaluationScope,
    Evidence,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    Severity,
)

from .base import (
    case_metadata,
    clamp,
    evidence,
    load_presentation,
    normalize_text,
    text_tokens,
    token_recall,
)
from .baseline import (
    _defective_overlap_class,
    _is_intentional_outside,
    _meaningful_alt_text,
    _meaningfully_outside_slide,
    _overlap_ratio,
    _template_residue_reasons,
    _title_object,
)
from .scenarios import _asset_hash, _fact_strings, _requirements

V8_ATOMIC_VERSION = "2.1.0"
_MEDIA_KINDS = frozenset({"picture", "linked_picture", "media"})
_SEMANTIC_VISUAL_KINDS = frozenset({"chart", "table", *_MEDIA_KINDS})
_CLOSING_RE = re.compile(
    r"(?:thank(?:\s+you)?|questions?|q\s*&\s*a|contact|the\s+end|谢谢|感谢|提问|联系我们)",
    re.IGNORECASE,
)
_NEGATIVE_REQUIREMENT_RE = re.compile(
    r"(?:不得|禁止|不可|不要|严禁|never|do\s+not|must\s+not)", re.IGNORECASE
)
_CRITICAL_REQUIREMENT_RE = re.compile(
    r"(?:必须|务必|不得|禁止|不可|严禁|must|required|never|do\s+not)", re.IGNORECASE
)
_PAGE_SCOPE_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*页"),
    re.compile(r"(?:page|slide)\s*#?\s*(\d+)", re.IGNORECASE),
)
_EVERY_PAGE_RE = re.compile(
    r"(?:每(?:一)?页|所有页|each\s+(?:page|slide)|every\s+(?:page|slide))", re.IGNORECASE
)
_CONCRETE_ANCHOR_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?(?:%|万|亿|k|m)?|"
    r"https?://|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:20\d{2})[年/.-](?:0?[1-9]|1[0-2]))",
    re.IGNORECASE,
)
_GENERIC_PHRASES = (
    "核心优势",
    "解决方案",
    "未来展望",
    "持续赋能",
    "行业领先",
    "用户至上",
    "高质量发展",
    "key benefits",
    "our solution",
    "next steps",
    "best in class",
    "key takeaways",
    "the call to action is clear",
    "win or learn",
    "from insight to action",
    "turning insights into action",
)
_FORMULAIC_COPY_PATTERNS = (
    re.compile(r"\b(?:from|turn(?:ing)?)\s+[a-z][a-z -]{1,40}\s+(?:to|into)\s+[a-z]", re.IGNORECASE),
    re.compile(r"\b(?:rapid[- ]fire|unlock(?:ing)?|decode your|your strategic radar)\b", re.IGNORECASE),
    re.compile(r"(?:从.+到.+|赋能.+增长|方法论.+驱动.+结果)"),
)
_PLACEHOLDER_AUTHORSHIP_PATTERNS = (
    re.compile(r"\b(?:competitor|rival|company|client|customer)\s+[A-Z]\b"),
    re.compile(r"\?\s*/\s*10\b"),
    re.compile(
        r"\b(?:a|an)\s+(?:saas firm|consumer brand|industrial player|company|enterprise|client)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bthis slide is 100% editable\b", re.IGNORECASE),
)
_CJK_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_LANGUAGE_NEUTRAL_LATIN_TOKENS = frozenset(
    {
        "ai",
        "api",
        "arr",
        "crm",
        "esg",
        "kpi",
        "mvp",
        "ppt",
        "pptx",
        "roi",
        "saas",
        "sam",
        "som",
        "tam",
    }
)


def _case(context: object) -> object:
    return getattr(context, "case", context)


def _source_key(presentation: ParsedPresentation) -> str:
    return presentation.source_sha256 or presentation.source_name


def _observation_id(
    presentation: ParsedPresentation,
    oracle_id: str,
    metric_id: str,
    scope: EvaluationScope,
    unit_key: str,
) -> str:
    digest = hashlib.sha256(
        f"{_source_key(presentation)}|{oracle_id}|{metric_id}|{scope.value}|{unit_key}".encode()
    ).hexdigest()[:20]
    return f"obs-{digest}"


def _score_severity(score: float, *, critical_floor: float = -1.0) -> Severity:
    if score <= critical_floor:
        return Severity.CRITICAL
    if score < 0.35:
        return Severity.MAJOR
    if score < 0.70:
        return Severity.MINOR
    return Severity.INFO


def _importance(role: str) -> float:
    return {
        "cover": 1.25,
        "data": 1.20,
        "content": 1.00,
        "section": 0.75,
        "closing": 0.50,
    }.get(role, 1.0)


def _scored_observation(
    presentation: ParsedPresentation,
    *,
    oracle_id: str,
    metric_id: str,
    scope: EvaluationScope,
    unit_key: str,
    score: float,
    raw_value: float | str | bool | None,
    confidence: float,
    evidence_items: Sequence[Evidence] = (),
    importance: float = 1.0,
    key_unit: bool = False,
    critical: bool = False,
    severity: Severity | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AtomicObservation:
    local_score = clamp(score)
    return AtomicObservation(
        observation_id=_observation_id(presentation, oracle_id, metric_id, scope, unit_key),
        oracle_id=oracle_id,
        metric_id=metric_id,
        scope=scope,
        unit_key=unit_key,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        raw_value=raw_value,
        local_score=local_score,
        confidence=clamp(confidence),
        severity=severity or _score_severity(local_score),
        importance=importance,
        key_unit=key_unit,
        critical=critical,
        evidence=tuple(evidence_items),
        version=V8_ATOMIC_VERSION,
        metadata=metadata or {},
    )


def _pass_observation(
    presentation: ParsedPresentation,
    *,
    oracle_id: str,
    metric_id: str,
    scope: EvaluationScope,
    unit_key: str,
    raw_value: float | str | bool | None,
    evidence_items: Sequence[Evidence] = (),
    importance: float = 1.0,
    key_unit: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> AtomicObservation:
    return AtomicObservation(
        observation_id=_observation_id(presentation, oracle_id, metric_id, scope, unit_key),
        oracle_id=oracle_id,
        metric_id=metric_id,
        scope=scope,
        unit_key=unit_key,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.PASS,
        raw_value=raw_value,
        confidence=1.0,
        severity=Severity.INFO,
        importance=importance,
        key_unit=key_unit,
        evidence=tuple(evidence_items),
        version=V8_ATOMIC_VERSION,
        metadata=metadata or {},
    )


def _na_observation(
    presentation: ParsedPresentation,
    *,
    oracle_id: str,
    metric_id: str,
    scope: EvaluationScope,
    unit_key: str,
    reason: str,
    page_number: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AtomicObservation:
    return AtomicObservation(
        observation_id=_observation_id(presentation, oracle_id, metric_id, scope, unit_key),
        oracle_id=oracle_id,
        metric_id=metric_id,
        scope=scope,
        unit_key=unit_key,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.NA,
        confidence=1.0,
        severity=Severity.INFO,
        evidence=(
            evidence(
                metric_id,
                unit_key,
                "insufficient_evidence",
                reason,
                page_number=page_number,
            ),
        ),
        version=V8_ATOMIC_VERSION,
        metadata={"reason": reason, **dict(metadata or {})},
    )


def _error_observation(*, oracle_id: str, metric_id: str, error: Exception) -> AtomicObservation:
    digest = hashlib.sha256(f"{oracle_id}|{metric_id}|{type(error).__name__}|{error}".encode()).hexdigest()[
        :20
    ]
    return AtomicObservation(
        observation_id=f"obs-{digest}",
        oracle_id=oracle_id,
        metric_id=metric_id,
        scope=EvaluationScope.PACKAGE,
        unit_key="package",
        execution_status=ExecutionStatus.ERROR,
        metric_status=MetricStatus.ERROR,
        confidence=0.0,
        severity=Severity.MAJOR,
        evidence=(
            evidence(
                metric_id,
                "observation-error",
                "oracle_error",
                f"{type(error).__name__}: {error}",
            ),
        ),
        version=V8_ATOMIC_VERSION,
        metadata={"error_type": type(error).__name__},
    )


def classify_slide_role(slide: ParsedSlide, slide_count: int) -> str:
    """Return a conservative structural role used only for scoped reduction."""

    text_objects = [item for item in slide.visible_objects if item.visible_text]
    visible_text = slide.visible_text.strip()
    if slide.page_number == 1:
        return "cover"
    if slide.page_number == slide_count and _CLOSING_RE.search(visible_text):
        return "closing"
    if any(item.kind in {"chart", "table"} for item in slide.visible_objects):
        return "data"
    title = _title_object(slide.visible_objects)
    if title is not None and len(normalize_text(visible_text)) <= 90 and len(text_objects) <= 2:
        return "section"
    return "content"


class ScopedObservationOracle(ABC):
    """Template method for deterministic, non-aggregating v8 Oracles."""

    oracle_id: str
    metric_id: str
    expected_scope: EvaluationScope
    version = V8_ATOMIC_VERSION

    def __init__(self, adapter: PptxAdapter | None = None) -> None:
        self.adapter = adapter or PptxAdapter()

    def supports(self, context: object) -> bool:
        artifacts = getattr(context, "artifacts", {})
        return bool(getattr(_case(context), "pptx_path", "")) or (
            isinstance(artifacts, Mapping)
            and any(
                isinstance(artifacts.get(key), ParsedPresentation)
                for key in (
                    "ppt_eval.parsed_presentation",
                    "presentation",
                    "parsed_presentation",
                )
            )
        )

    def evaluate(self, context: object) -> ObservationBatch:
        started = time.perf_counter()
        try:
            presentation = load_presentation(context, self.adapter)
            observations = self._observe(context, presentation)
            metadata: Mapping[str, Any] = {
                "metric_id": self.metric_id,
                "expected_scope": self.expected_scope.value,
                "slide_count": presentation.slide_count,
                "source_sha256": presentation.source_sha256,
            }
        except Exception as exc:  # an observation failure is not a quality failure
            observations = (
                _error_observation(oracle_id=self.oracle_id, metric_id=self.metric_id, error=exc),
            )
            metadata = {"metric_id": self.metric_id, "expected_scope": self.expected_scope.value}
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return ObservationBatch(
            oracle_id=self.oracle_id,
            observations=tuple(observations),
            version=self.version,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    @abstractmethod
    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        raise NotImplementedError


class SlideRoleClassifierOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_role_classifier"
    metric_id = "slide_role"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            result.append(
                _pass_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    raw_value=role,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"role-{slide.page_number}",
                            "slide_role",
                            f"Slide was classified as {role} for reduction only.",
                            page_number=slide.page_number,
                            payload={"role": role},
                        ),
                    ),
                    metadata={"role": role, "classifier_kind": "deterministic_proxy"},
                )
            )
        return tuple(result)


class SlideContentPresenceOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_content_presence"
    metric_id = "slide_content_presence"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            semantic = [
                item
                for item in slide.visible_objects
                if item.visible_text or item.kind in _SEMANTIC_VISUAL_KINDS
            ]
            present = bool(semantic)
            finding = evidence(
                self.metric_id,
                f"presence-{slide.page_number}",
                "content_present" if present else "blank_slide",
                "Slide contains observable semantic content."
                if present
                else "Slide contains no observable semantic content.",
                page_number=slide.page_number,
                payload={"semantic_objects": len(semantic), "role": role},
            )
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=float(present),
                    raw_value=present,
                    confidence=0.96,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=not present and role in {"cover", "data"},
                    severity=Severity.INFO
                    if present
                    else Severity.CRITICAL
                    if role in {"cover", "data"}
                    else Severity.MAJOR,
                    evidence_items=(finding,),
                    metadata={"role": role, "semantic_objects": len(semantic)},
                )
            )
        return tuple(result)


class SlideReadingLoadOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_reading_load"
    metric_id = "slide_reading_load"
    expected_scope = EvaluationScope.PAGE

    _LIMITS = {
        "cover": (140, 320, 6, 12),
        "section": (180, 400, 8, 14),
        "closing": (180, 400, 8, 14),
        "content": (700, 1200, 18, 30),
        "data": (700, 1200, 18, 30),
    }

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            characters = len(re.sub(r"\s+", "", slide.visible_text))
            text_objects = sum(bool(item.visible_text) for item in slide.visible_objects)
            if not characters:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="No extractable text is available for a reading-load observation.",
                        page_number=slide.page_number,
                        metadata={"page_number": slide.page_number, "role": role},
                    )
                )
                continue
            char_soft, char_hard, object_soft, object_hard = self._LIMITS[role]
            char_load = clamp((characters - char_soft) / max(1, char_hard - char_soft))
            object_load = clamp((text_objects - object_soft) / max(1, object_hard - object_soft))
            score = 1.0 - 0.65 * char_load - 0.35 * object_load
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=characters,
                    confidence=0.82,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"load-{slide.page_number}",
                            "reading_load",
                            f"Slide contains {characters} non-whitespace characters in {text_objects} text objects.",
                            page_number=slide.page_number,
                            payload={
                                "role": role,
                                "characters": characters,
                                "text_objects": text_objects,
                                "character_soft_limit": char_soft,
                                "character_hard_limit": char_hard,
                                "object_soft_limit": object_soft,
                                "object_hard_limit": object_hard,
                            },
                        ),
                    ),
                    metadata={"role": role, "character_load": char_load, "object_load": object_load},
                )
            )
        return tuple(result)


class SlideGeometryIntegrityOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_geometry_integrity"
    metric_id = "slide_geometry_integrity"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            objects = [
                item
                for item in slide.visible_objects
                if item.bbox.area > 1e-5 and item.kind not in {"group", "connector"}
            ]
            outside = [
                item
                for item in objects
                if item.bbox.is_outside_slide
                and not _is_intentional_outside(item)
                and _meaningfully_outside_slide(item)
            ]
            overlaps: list[tuple[SlideObject, SlideObject, float, str]] = []
            for index, left in enumerate(objects):
                if left.bbox.area > 0.80:
                    continue
                for right in objects[index + 1 :]:
                    if right.bbox.area > 0.80:
                        continue
                    ratio = _overlap_ratio(left, right)
                    classification = _defective_overlap_class(left, right, ratio)
                    if classification is not None:
                        overlaps.append((left, right, ratio, classification))
            denominator = max(1, len(objects))
            score = (
                1.0
                - min(0.70, 2.5 * len(outside) / denominator)
                - min(0.50, 1.5 * len(overlaps) / denominator)
            )
            details = [
                evidence(
                    self.metric_id,
                    f"outside-{slide.page_number}-{item.object_id}",
                    "out_of_bounds",
                    "Object extends materially outside the slide canvas.",
                    page_number=slide.page_number,
                    object_id=item.object_id,
                    bbox=item.bbox.as_tuple(),
                )
                for item in outside
            ]
            details.extend(
                evidence(
                    self.metric_id,
                    f"overlap-{slide.page_number}-{left.object_id}-{right.object_id}",
                    "overlap",
                    "Peer objects have a high-confidence defective overlap.",
                    page_number=slide.page_number,
                    object_id=left.object_id,
                    bbox=left.bbox.as_tuple(),
                    payload={
                        "other_object_id": right.object_id,
                        "other_bbox": right.bbox.as_tuple(),
                        "overlap_ratio": round(ratio, 4),
                        "classification": classification,
                    },
                )
                for left, right, ratio, classification in overlaps
            )
            if not details:
                details.append(
                    evidence(
                        self.metric_id,
                        f"geometry-{slide.page_number}",
                        "geometry_summary",
                        "No high-confidence clipping or defective peer overlap was found.",
                        page_number=slide.page_number,
                        payload={"objects_considered": len(objects)},
                    )
                )
            critical = any(item.visible_text for item in outside)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=len(outside) + len(overlaps),
                    confidence=0.90,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=critical,
                    severity=Severity.CRITICAL if critical else _score_severity(score),
                    evidence_items=details,
                    metadata={
                        "role": role,
                        "objects_considered": len(objects),
                        "outside": len(outside),
                        "overlaps": len(overlaps),
                    },
                )
            )
        return tuple(result)


class SlideTypographyFunctionalOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_typography_functional"
    metric_id = "slide_typography_functional"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            text_objects = [item for item in slide.visible_objects if item.visible_text]
            observed = [item for item in text_objects if item.font_sizes_pt]
            if not text_objects or not observed:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="Explicit font sizes are unavailable for this page.",
                        page_number=slide.page_number,
                        metadata={
                            "page_number": slide.page_number,
                            "role": role,
                            "text_objects": len(text_objects),
                            "observed_text_objects": len(observed),
                        },
                    )
                )
                continue
            too_small = [item for item in observed if min(item.font_sizes_pt) < 14.0]
            critically_small = [item for item in observed if min(item.font_sizes_pt) < 10.0]
            score = 1.0 - 0.75 * len(too_small) / len(observed)
            observability = len(observed) / len(text_objects)
            details = [
                evidence(
                    self.metric_id,
                    f"small-{slide.page_number}-{item.object_id}",
                    "small_text",
                    f"Text includes an explicit {min(item.font_sizes_pt):g} pt run.",
                    page_number=slide.page_number,
                    object_id=item.object_id,
                    bbox=item.bbox.as_tuple(),
                    payload={
                        "minimum_font_pt": min(item.font_sizes_pt),
                        "functional_floor_pt": 14.0,
                        "critical_floor_pt": 10.0,
                    },
                )
                for item in too_small
            ]
            if not details:
                details.append(
                    evidence(
                        self.metric_id,
                        f"type-{slide.page_number}",
                        "typography_summary",
                        "All explicitly sized text runs meet the functional floor.",
                        page_number=slide.page_number,
                    )
                )
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=len(too_small),
                    confidence=0.55 + 0.40 * observability,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=bool(critically_small),
                    severity=Severity.CRITICAL if critically_small else _score_severity(score),
                    evidence_items=details,
                    metadata={
                        "role": role,
                        "text_objects": len(text_objects),
                        "observed_text_objects": len(observed),
                        "observability": observability,
                        "too_small_objects": len(too_small),
                        "critically_small_objects": len(critically_small),
                    },
                )
            )
        return tuple(result)


class SlideEditabilityOracle(ScopedObservationOracle):
    oracle_id = "v8.slide_editability"
    metric_id = "slide_editability"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            semantic = [
                item
                for item in slide.visible_objects
                if item.kind not in {"group", "connector", "line"} and item.bbox.area > 1e-6
            ]
            if not semantic:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="No semantic object is available for editability assessment.",
                        page_number=slide.page_number,
                    )
                )
                continue
            native = [item for item in semantic if item.editable or item.kind in {"chart", "table"}]
            large_flattened = [
                item for item in semantic if item.kind in _MEDIA_KINDS and item.bbox.area >= 0.65
            ]
            has_editable_text = any(item.editable and item.visible_text for item in semantic)
            raster_only = bool(
                large_flattened
                and not has_editable_text
                and not any(item.kind in {"chart", "table"} for item in semantic)
            )
            native_ratio = len(native) / len(semantic)
            score = 0.10 if raster_only else clamp(0.55 + 0.45 * native_ratio)
            finding = evidence(
                self.metric_id,
                f"editability-{slide.page_number}",
                "rasterized_slide" if raster_only else "editability_summary",
                "Slide appears flattened into a large raster image."
                if raster_only
                else f"{len(native)}/{len(semantic)} semantic objects are native/editable.",
                page_number=slide.page_number,
                object_id=large_flattened[0].object_id if raster_only else None,
                bbox=large_flattened[0].bbox.as_tuple() if raster_only else None,
                payload={"native_ratio": native_ratio, "raster_only": raster_only},
            )
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=native_ratio,
                    confidence=0.90,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=raster_only and role in {"cover", "data"},
                    severity=Severity.MAJOR if raster_only else _score_severity(score),
                    evidence_items=(finding,),
                    metadata={
                        "role": role,
                        "semantic_objects": len(semantic),
                        "native_objects": len(native),
                        "native_ratio": native_ratio,
                        "raster_only": raster_only,
                    },
                )
            )
        return tuple(result)


class MediaIntegrityOracle(ScopedObservationOracle):
    oracle_id = "v8.media_integrity"
    metric_id = "media_integrity"
    expected_scope = EvaluationScope.OBJECT

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        media = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind in _MEDIA_KINDS
        ]
        if not media:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="object:none",
                    reason="No media object is present.",
                ),
            )
        result = []
        for page, item in media:
            available = bool(item.media_sha256)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{page}/object:{item.object_id}",
                    score=float(available),
                    raw_value=available,
                    confidence=0.99,
                    critical=not available,
                    severity=Severity.INFO if available else Severity.CRITICAL,
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"media-{page}-{item.object_id}",
                            "media_available" if available else "media_unavailable",
                            "Media object has a readable payload."
                            if available
                            else "Media object has no readable payload.",
                            page_number=page,
                            object_id=item.object_id,
                            bbox=item.bbox.as_tuple(),
                            payload={"relationship_target": item.relationship_target},
                        ),
                    ),
                    metadata={"page_number": page, "object_kind": item.kind},
                )
            )
        return tuple(result)


class CropGeometryRiskOracle(ScopedObservationOracle):
    oracle_id = "v8.crop_geometry_risk"
    metric_id = "crop_geometry_risk"
    expected_scope = EvaluationScope.OBJECT

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        pictures = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind == "picture"
        ]
        if not pictures:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="object:none",
                    reason="No embedded picture is available for crop geometry inspection.",
                ),
            )
        result = []
        for page, item in pictures:
            crop = item.metadata.get("crop")
            crop_values = tuple(float(value) for value in crop) if isinstance(crop, (tuple, list)) else ()
            crop_amount = sum(max(0.0, value) for value in crop_values)
            aspect = item.bbox.width / max(1e-9, item.bbox.height)
            score = 1.0 - min(0.55, crop_amount * 0.8)
            if item.bbox.area < 0.0025 or aspect > 8.0 or aspect < 0.125:
                score -= 0.30
            score = clamp(score)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{page}/object:{item.object_id}",
                    score=score,
                    raw_value=crop_amount,
                    confidence=0.78,
                    importance=1.0 + min(0.5, item.bbox.area),
                    key_unit=item.bbox.area >= 0.20,
                    critical=score < 0.35 and item.bbox.area >= 0.20,
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"crop-{page}-{item.object_id}",
                            "crop_geometry",
                            "Picture crop and geometry were inspected without inferring subject loss.",
                            page_number=page,
                            object_id=item.object_id,
                            bbox=item.bbox.as_tuple(),
                            payload={
                                "crop": crop_values,
                                "crop_amount": crop_amount,
                                "aspect_ratio": aspect,
                                "slide_area_ratio": item.bbox.area,
                            },
                        ),
                    ),
                    metadata={"page_number": page, "semantic_crop_observed": False},
                )
            )
        return tuple(result)


class DocumentStructureOracle(ScopedObservationOracle):
    oracle_id = "v8.document_structure"
    metric_id = "document_structure"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            title = _title_object(slide.visible_objects)
            if role == "closing":
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="Closing slides are exempt from a title-anchor requirement.",
                        page_number=slide.page_number,
                        metadata={"role": role, "page_number": slide.page_number},
                    )
                )
                continue
            score = float(title is not None)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=title is not None,
                    confidence=0.82,
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=title is None and role == "cover",
                    severity=Severity.INFO if title else Severity.MAJOR,
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"title-{slide.page_number}",
                            "title_anchor" if title else "missing_title_anchor",
                            "A plausible title anchor was found."
                            if title
                            else "No plausible title anchor was found.",
                            page_number=slide.page_number,
                            object_id=title.object_id if title else None,
                            bbox=title.bbox.as_tuple() if title else None,
                            payload={"role": role},
                        ),
                    ),
                    metadata={"role": role, "title_object_id": title.object_id if title else None},
                )
            )
        return tuple(result)


class AltTextOracle(ScopedObservationOracle):
    oracle_id = "v8.alt_text"
    metric_id = "alt_text"
    expected_scope = EvaluationScope.OBJECT

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        images = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind in {"picture", "media"}
        ]
        if not images:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="object:none",
                    reason="No non-text media object requires alternative text.",
                ),
            )
        result = []
        for page, item in images:
            alt_text = str(item.metadata.get("alt_text", ""))
            meaningful = _meaningful_alt_text(alt_text, item.name)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{page}/object:{item.object_id}",
                    score=float(meaningful),
                    raw_value=meaningful,
                    confidence=0.98,
                    importance=1.0 + min(0.5, item.bbox.area),
                    key_unit=item.bbox.area >= 0.20,
                    severity=Severity.INFO if meaningful else Severity.MAJOR,
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"alt-{page}-{item.object_id}",
                            "meaningful_alt_text" if meaningful else "missing_alt_text",
                            "Media object has meaningful alternative text."
                            if meaningful
                            else "Media object has no meaningful alternative text.",
                            page_number=page,
                            object_id=item.object_id,
                            bbox=item.bbox.as_tuple(),
                            payload={"alt_text_length": len(alt_text.strip())},
                        ),
                    ),
                    metadata={"page_number": page, "object_kind": item.kind},
                )
            )
        return tuple(result)


class TitleBodyAlignmentOracle(ScopedObservationOracle):
    oracle_id = "v8.title_body_alignment"
    metric_id = "title_body_alignment"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            title = _title_object(slide.visible_objects)
            if role in {"cover", "section", "closing"} or title is None:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="This page has no assessable title/body pair.",
                        page_number=slide.page_number,
                        metadata={"role": role, "page_number": slide.page_number},
                    )
                )
                continue
            bodies = [
                item
                for item in slide.visible_objects
                if item.object_id != title.object_id
                and (item.visible_text or item.kind in _SEMANTIC_VISUAL_KINDS)
            ]
            if not bodies:
                score = 0.0
                horizontal_alignment = 0.0
                topical_overlap = 0.0
            else:
                anchor_x = statistics.median(item.bbox.x for item in bodies)
                horizontal_alignment = 1.0 - clamp(abs(title.bbox.x - anchor_x) / 0.25)
                body_text = "\n".join(item.visible_text for item in bodies if item.visible_text)
                title_tokens = text_tokens(title.visible_text)
                topical_overlap = (
                    len(title_tokens & text_tokens(body_text)) / len(title_tokens) if title_tokens else 0.0
                )
                body_signal = (
                    1.0
                    if len(normalize_text(body_text)) >= 40
                    or any(item.kind in _SEMANTIC_VISUAL_KINDS for item in bodies)
                    else 0.5
                )
                score = 0.70 * horizontal_alignment + 0.30 * body_signal
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=horizontal_alignment,
                    confidence=0.68,
                    importance=_importance(role),
                    key_unit=role == "data",
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"title-body-{slide.page_number}",
                            "title_body_proxy",
                            "Title/body alignment was measured from object anchors and body presence.",
                            page_number=slide.page_number,
                            object_id=title.object_id,
                            bbox=title.bbox.as_tuple(),
                            payload={
                                "body_objects": len(bodies),
                                "horizontal_alignment": horizontal_alignment,
                                "topical_overlap_diagnostic": topical_overlap,
                            },
                        ),
                    ),
                    metadata={
                        "role": role,
                        "body_objects": len(bodies),
                        "topical_overlap_is_score_affecting": False,
                        "topical_overlap": topical_overlap,
                    },
                )
            )
        return tuple(result)


def _slide_signature(slide: ParsedSlide) -> tuple[str, tuple[str, ...], tuple[int, ...]]:
    normalized = normalize_text(slide.visible_text)
    kinds = tuple(sorted(item.kind for item in slide.visible_objects if item.bbox.area > 1e-5))
    geometry = tuple(
        sorted(
            round(value * 20)
            for item in slide.visible_objects
            if item.bbox.area > 1e-5
            for value in item.bbox.as_tuple()
        )
    )
    return normalized, kinds, geometry


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / max(1, len(left | right))


class DuplicateSlideOracle(ScopedObservationOracle):
    oracle_id = "v8.duplicate_slide"
    metric_id = "duplicate_slide"
    expected_scope = EvaluationScope.SLIDE_PAIR

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        if len(presentation.slides) < 2:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="pair:none",
                    reason="At least two slides are required for duplicate-slide inspection.",
                ),
            )
        suspicious = []
        checked = 0
        signatures = {slide.page_number: _slide_signature(slide) for slide in presentation.slides}
        for left_index, left in enumerate(presentation.slides):
            left_text, left_kinds, left_geometry = signatures[left.page_number]
            for right in presentation.slides[left_index + 1 :]:
                checked += 1
                right_text, right_kinds, right_geometry = signatures[right.page_number]
                text_similarity = _jaccard(text_tokens(left.visible_text), text_tokens(right.visible_text))
                exact_text = bool(left_text) and left_text == right_text
                same_structure = left_kinds == right_kinds and left_geometry == right_geometry
                duplicate = exact_text and (same_structure or len(left_text) >= 12)
                near_duplicate = text_similarity >= 0.94 and left_kinds == right_kinds
                if not (duplicate or near_duplicate):
                    continue
                score = 0.0 if duplicate and same_structure else 0.20
                unit_key = f"pages:{left.page_number}-{right.page_number}"
                suspicious.append(
                    _scored_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=unit_key,
                        score=score,
                        raw_value=text_similarity,
                        confidence=0.90 if duplicate else 0.76,
                        key_unit=True,
                        critical=False,
                        severity=Severity.MAJOR,
                        evidence_items=(
                            evidence(
                                self.metric_id,
                                unit_key,
                                "duplicate_slide_pair",
                                "Two slides have duplicate or near-duplicate deterministic signatures.",
                                page_number=left.page_number,
                                payload={
                                    "other_page_number": right.page_number,
                                    "text_similarity": text_similarity,
                                    "exact_text": exact_text,
                                    "same_structure": same_structure,
                                },
                            ),
                        ),
                        metadata={"left_page": left.page_number, "right_page": right.page_number},
                    )
                )
        if suspicious:
            return tuple(suspicious)
        return (
            _scored_observation(
                presentation,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                scope=self.expected_scope,
                unit_key="pairs:none-suspicious",
                score=1.0,
                raw_value=0,
                confidence=0.84,
                evidence_items=(
                    evidence(
                        self.metric_id,
                        "no-duplicates",
                        "slide_pair_summary",
                        "No duplicate deterministic slide signature was found.",
                        payload={"pairs_checked": checked},
                    ),
                ),
                metadata={"pairs_checked": checked, "sentinel": True},
            ),
        )


class TransitionCoherenceProxyOracle(ScopedObservationOracle):
    oracle_id = "v8.transition_coherence_proxy"
    metric_id = "transition_coherence_proxy"
    expected_scope = EvaluationScope.SLIDE_PAIR

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        if len(presentation.slides) < 2:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="pair:none",
                    reason="At least two slides are required for a transition observation.",
                ),
            )
        result = []
        for left, right in zip(presentation.slides, presentation.slides[1:]):
            similarity = _jaccard(text_tokens(left.visible_text), text_tokens(right.visible_text))
            right_role = classify_slide_role(right, presentation.slide_count)
            if right_role in {"section", "closing"}:
                score = 0.85
            elif similarity >= 0.94:
                score = 0.35
            else:
                score = 0.55 + 0.45 * clamp(similarity / 0.20)
            unit_key = f"pages:{left.page_number}-{right.page_number}"
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=unit_key,
                    score=score,
                    raw_value=similarity,
                    confidence=0.45,
                    importance=max(
                        _importance(classify_slide_role(left, presentation.slide_count)),
                        _importance(right_role),
                    ),
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            unit_key,
                            "transition_proxy",
                            "Adjacent-slide lexical continuity was recorded as a low-confidence proxy.",
                            page_number=left.page_number,
                            payload={
                                "other_page_number": right.page_number,
                                "token_jaccard": similarity,
                                "next_slide_role": right_role,
                            },
                        ),
                    ),
                    metadata={
                        "left_page": left.page_number,
                        "right_page": right.page_number,
                        "proxy_only": True,
                    },
                )
            )
        return tuple(result)


def _language_counts(text: str) -> tuple[int, int]:
    latin_tokens = [
        token
        for token in _LATIN_WORD_RE.findall(text)
        if token.casefold() not in _LANGUAGE_NEUTRAL_LATIN_TOKENS
        and not (token.isupper() and len(token) <= 5)
    ]
    return len(_CJK_CHARACTER_RE.findall(text)), len(latin_tokens)


def _language_label(text: str) -> str:
    cjk_units, latin_units = _language_counts(text)
    if cjk_units >= 2 and latin_units >= 3:
        return "MIXED"
    if cjk_units >= 2:
        return "ZH"
    if latin_units >= 3:
        return "EN"
    return "UNKNOWN"


def _declared_language_policy(context: object) -> tuple[str, frozenset[str]]:
    metadata = case_metadata(context)
    raw_policy = str(metadata.get("language_policy") or "").strip().upper()
    raw_allowed = metadata.get("allowed_languages", ())
    if isinstance(raw_allowed, str):
        values: Sequence[object] = (raw_allowed,)
    elif isinstance(raw_allowed, Sequence):
        values = raw_allowed
    else:
        values = ()
    aliases = {
        "CHINESE": "ZH",
        "ZH-CN": "ZH",
        "ZH_CN": "ZH",
        "ENGLISH": "EN",
        "EN-US": "EN",
        "EN_US": "EN",
    }
    allowed = frozenset(
        aliases.get(str(value).strip().upper(), str(value).strip().upper())
        for value in values
        if str(value).strip()
    )
    request = str(getattr(_case(context), "request", "") or "")
    normalized_request = normalize_text(request)
    if any(marker in normalized_request for marker in ("bilingual", "双语", "中英双语")):
        raw_policy = "BILINGUAL"
        allowed = frozenset(("EN", "ZH"))
    return raw_policy, allowed


class LanguageConsistencyOracle(ScopedObservationOracle):
    """Detect undeclared deck-level language switching without judging content quality."""

    oracle_id = "v8.language_consistency"
    metric_id = "language_consistency"
    expected_scope = EvaluationScope.DECK

    def _observe(
        self,
        context: object,
        presentation: ParsedPresentation,
    ) -> tuple[AtomicObservation, ...]:
        page_text = {
            slide.page_number: slide.visible_text.strip()
            for slide in presentation.slides
            if _language_label(slide.visible_text) != "UNKNOWN"
        }
        if not page_text:
            return (
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key="deck",
                    reason="No extractable text is available for language-consistency inspection.",
                ),
            )
        cjk_units = sum(_language_counts(text)[0] for text in page_text.values())
        latin_units = sum(_language_counts(text)[1] for text in page_text.values())
        dominant = "ZH" if cjk_units > latin_units else "EN"
        labels = {page: _language_label(text) for page, text in page_text.items()}
        mixed_pages = tuple(page for page, label in labels.items() if label == "MIXED")
        minority_pages = tuple(
            page
            for page, label in labels.items()
            if label not in {"UNKNOWN", "MIXED", dominant}
        )
        policy, allowed = _declared_language_policy(context)
        explicit_bilingual = policy == "BILINGUAL" or {"EN", "ZH"} <= allowed
        systematic_bilingual = len(mixed_pages) / len(page_text) >= 0.75
        affected_weight = len(mixed_pages) + 0.75 * len(minority_pages)
        inconsistency_ratio = affected_weight / len(page_text)
        score = (
            1.0
            if explicit_bilingual
            else clamp(1.0 - 1.50 * inconsistency_ratio)
        )
        affected_pages = tuple(dict.fromkeys((*mixed_pages, *minority_pages)))
        details = tuple(
            evidence(
                self.metric_id,
                f"language-{page_number}",
                "undeclared_mixed_language"
                if page_number in mixed_pages
                else "minority_language_page",
                "Visible language use differs from the dominant undeclared deck language.",
                page_number=page_number,
                payload={
                    "page_language": labels[page_number],
                    "dominant_language": dominant,
                },
            )
            for page_number in affected_pages[:20]
        ) or (
            evidence(
                self.metric_id,
                "language-summary",
                "language_consistency_summary",
                "Deck language use is internally consistent or explicitly bilingual.",
                payload={"dominant_language": dominant},
            ),
        )
        return (
            _scored_observation(
                presentation,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                scope=self.expected_scope,
                unit_key="deck",
                score=score,
                raw_value=inconsistency_ratio,
                confidence=0.90,
                severity=Severity.MINOR if score < 0.90 else Severity.INFO,
                evidence_items=details,
                metadata={
                    "dominant_language": dominant,
                    "cjk_units": cjk_units,
                    "latin_units": latin_units,
                    "mixed_pages": list(mixed_pages),
                    "minority_language_pages": list(minority_pages),
                    "text_pages": len(page_text),
                    "inconsistency_ratio": inconsistency_ratio,
                    "language_policy": policy or "UNDECLARED",
                    "allowed_languages": sorted(allowed),
                    "explicit_bilingual": explicit_bilingual,
                    "systematic_bilingual": systematic_bilingual,
                    "primary_owner": "language_consistency",
                    "not_authorship_penalty": True,
                },
            ),
        )


class AuthorshipSpecificitySignalsOracle(ScopedObservationOracle):
    oracle_id = "v8.authorship_specificity_signals"
    metric_id = "authorship_specificity_signals"
    expected_scope = EvaluationScope.PAGE

    def _observe(self, context: object, presentation: ParsedPresentation) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        silhouettes: dict[tuple[tuple[str, ...], tuple[int, ...]], list[int]] = {}
        slide_silhouettes: dict[int, tuple[tuple[str, ...], tuple[int, ...]]] = {}
        for slide in presentation.slides:
            _, kinds, geometry = _slide_signature(slide)
            silhouette = (kinds, geometry)
            slide_silhouettes[slide.page_number] = silhouette
            silhouettes.setdefault(silhouette, []).append(slide.page_number)
        for slide in presentation.slides:
            text = slide.visible_text.strip()
            role = classify_slide_role(slide, presentation.slide_count)
            if not text:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=self.expected_scope,
                        unit_key=f"page:{slide.page_number}",
                        reason="No extractable text is available for authorship-specificity signals.",
                        page_number=slide.page_number,
                    )
                )
                continue
            residue_codes = _template_residue_reasons(text)
            normalized = normalize_text(text)
            generic_hits = sum(
                normalize_text(phrase) in normalized for phrase in _GENERIC_PHRASES
            ) + sum(bool(pattern.search(text)) for pattern in _FORMULAIC_COPY_PATTERNS)
            placeholder_hits = sum(
                len(pattern.findall(text)) for pattern in _PLACEHOLDER_AUTHORSHIP_PATTERNS
            )
            anchors = len(_CONCRETE_ANCHOR_RE.findall(text))
            tokens = text_tokens(text)
            lexical_signal = clamp(len(tokens) / 24.0)
            component_signatures = [
                (round(item.bbox.width, 2), round(item.bbox.height, 2))
                for item in slide.visible_objects
                if 0.015 <= item.bbox.area <= 0.25
            ]
            repeated_components = max(
                (component_signatures.count(signature) for signature in set(component_signatures)),
                default=0,
            )
            mechanical_grid = (
                repeated_components >= 4
                and repeated_components / max(1, len(component_signatures)) >= 0.50
            )
            icon_like = [
                item
                for item in slide.visible_objects
                if item.kind in {"freeform", "picture", "linked_picture"}
                and 0.001 <= item.bbox.area <= 0.04
            ]
            short_labels = [
                item
                for item in slide.visible_objects
                if item.visible_text
                and 1 <= len(normalize_text(item.visible_text)) <= 60
            ]
            icon_module_signal = (
                len(icon_like) >= 3
                and len(short_labels) >= 3
                and (repeated_components >= 3 or len(icon_like) >= 6)
            )
            silhouette_pages = silhouettes[slide_silhouettes[slide.page_number]]
            repeated_silhouette = (
                len(silhouette_pages) >= 3
                and len(silhouette_pages) / max(1, presentation.slide_count) >= 0.12
            )
            ai_style_signal_count = sum(
                (
                    bool(generic_hits),
                    bool(placeholder_hits),
                    bool(mechanical_grid),
                    bool(icon_module_signal),
                    bool(repeated_silhouette),
                )
            )
            score = clamp(
                0.35
                + 0.35 * lexical_signal
                + 0.10 * min(2, anchors)
                - 0.15 * min(2, generic_hits)
                - 0.55 * bool(residue_codes)
                - 0.15 * mechanical_grid
                - 0.16 * icon_module_signal
                - 0.20 * repeated_silhouette
                - 0.25 * min(2, placeholder_hits)
                - 0.08 * (ai_style_signal_count >= 2)
            )
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=self.expected_scope,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=anchors,
                    confidence=0.45,
                    severity=Severity.MINOR if score < 0.70 else Severity.INFO,
                    importance=_importance(role),
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"specificity-{slide.page_number}",
                            "authorship_rule_signals",
                            "Text specificity, placeholder risk, repeated silhouettes, icon modules and template residue were recorded.",
                            page_number=slide.page_number,
                            payload={
                                "concrete_anchors": anchors,
                                "distinct_tokens": len(tokens),
                                "generic_phrase_hits": generic_hits,
                                "placeholder_authorship_hits": placeholder_hits,
                                "template_residue_codes": residue_codes,
                                "repeated_component_count": repeated_components,
                                "mechanical_grid_signal": mechanical_grid,
                                "icon_like_object_count": len(icon_like),
                                "short_label_count": len(short_labels),
                                "icon_module_signal": icon_module_signal,
                                "repeated_silhouette_signal": repeated_silhouette,
                                "repeated_silhouette_pages": silhouette_pages,
                                "ai_style_signal_count": ai_style_signal_count,
                            },
                        ),
                    ),
                    metadata={
                        "role": role,
                        "proxy_only": True,
                        "visual_and_text_signals": True,
                        "not_semantic_authorship_proof": True,
                        "primary_owner": "authorship_specificity",
                        "excluded_from_functional_hard_gate": True,
                    },
                )
            )
        return tuple(result)


def _requirement_pages(requirement: str, presentation: ParsedPresentation) -> tuple[int, ...]:
    if _EVERY_PAGE_RE.search(requirement):
        return tuple(slide.page_number for slide in presentation.slides)
    pages: list[int] = []
    for pattern in _PAGE_SCOPE_PATTERNS:
        pages.extend(int(value) for value in pattern.findall(requirement))
    valid = {slide.page_number for slide in presentation.slides}
    return tuple(dict.fromkeys(page for page in pages if page in valid))


def _requirement_subject(requirement: str) -> str:
    subject = _EVERY_PAGE_RE.sub(" ", requirement)
    for pattern in _PAGE_SCOPE_PATTERNS:
        subject = pattern.sub(" ", subject)
    subject = re.sub(
        r"\b(?:must|required|never|do\s+not|include|show|use)\b",
        " ",
        subject,
        flags=re.IGNORECASE,
    )
    subject = re.sub(
        r"必须|务必|不得|禁止|不可|不要|严禁|应当|需要|请|包含|展示|使用|加入|提供",
        "",
        subject,
    )
    return subject.strip() or requirement


def _locate_requirement(
    presentation: ParsedPresentation, requirement: str, scoped_pages: Sequence[int]
) -> tuple[int | None, SlideObject | None]:
    allowed = set(scoped_pages)
    tokens = sorted(text_tokens(requirement), key=len, reverse=True)
    for slide in presentation.slides:
        if allowed and slide.page_number not in allowed:
            continue
        for item in slide.visible_objects:
            normalized = normalize_text(item.visible_text)
            if any(normalize_text(token) in normalized for token in tokens):
                return slide.page_number, item
    return None, None


def observe_requirements(
    context: object,
    *,
    adapter: PptxAdapter | None = None,
    requirements: Sequence[str] | None = None,
    critical_requirements: Sequence[str] | None = None,
    oracle_id: str = "v8.requirement_observations",
    metric_id: str = "requirement_satisfaction",
) -> ObservationBatch:
    """Create shared requirement atoms, expanding explicit page-scoped requirements."""

    started = time.perf_counter()
    presentation = load_presentation(context, adapter or PptxAdapter())
    request = str(getattr(_case(context), "request", "") or "")
    items = tuple(
        str(item)
        for item in (requirements if requirements is not None else _requirements(request))
        if str(item).strip()
    )
    declared_critical = {
        normalize_text(str(item)) for item in (critical_requirements or ()) if str(item).strip()
    }
    result = []
    for index, requirement in enumerate(items, start=1):
        critical = normalize_text(requirement) in declared_critical or bool(
            _CRITICAL_REQUIREMENT_RE.search(requirement)
        )
        pages = _requirement_pages(requirement, presentation)
        scopes: tuple[int | None, ...] = tuple(pages) if pages else (None,)
        for page in scopes:
            scoped_pages = (page,) if page is not None else ()
            deck_text = (
                next(slide.visible_text for slide in presentation.slides if slide.page_number == page)
                if page is not None
                else presentation.all_visible_text
            )
            subject = _requirement_subject(requirement)
            coverage = token_recall(subject, deck_text)
            negative = bool(_NEGATIVE_REQUIREMENT_RE.search(requirement))
            satisfaction = 1.0 - coverage if negative else coverage
            found_page, found_object = _locate_requirement(presentation, subject, scoped_pages)
            unit_key = f"requirement:{index}" + (f"/page:{page}" if page is not None else "")
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=oracle_id,
                    metric_id=metric_id,
                    scope=EvaluationScope.REQUIREMENT,
                    unit_key=unit_key,
                    score=satisfaction,
                    raw_value=coverage,
                    confidence=0.78,
                    importance=1.5 if critical else 1.0,
                    key_unit=critical,
                    critical=critical and satisfaction < 0.45,
                    severity=Severity.CRITICAL
                    if critical and satisfaction < 0.45
                    else _score_severity(satisfaction),
                    evidence_items=(
                        evidence(
                            metric_id,
                            unit_key,
                            "requirement_match" if satisfaction >= 0.45 else "requirement_gap",
                            f"Requirement satisfaction proxy is {satisfaction:.0%}.",
                            page_number=found_page or page,
                            object_id=found_object.object_id if found_object else None,
                            bbox=found_object.bbox.as_tuple() if found_object else None,
                            payload={
                                "requirement": requirement,
                                "coverage": coverage,
                                "negative": negative,
                                "critical": critical,
                                "declared_page_scope": page,
                            },
                        ),
                    ),
                    metadata={
                        "requirement_index": index,
                        "declared_page_scope": page,
                        "negative": negative,
                    },
                )
            )
    if not result:
        result.append(
            _na_observation(
                presentation,
                oracle_id=oracle_id,
                metric_id=metric_id,
                scope=EvaluationScope.REQUIREMENT,
                unit_key="requirement:none",
                reason="No requirement was supplied.",
            )
        )
    return ObservationBatch(
        oracle_id=oracle_id,
        observations=tuple(result),
        version=V8_ATOMIC_VERSION,
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
        metadata={"metric_id": metric_id, "requirements": len(items)},
    )


def _match_asset(asset: str, presentation: ParsedPresentation) -> tuple[int, SlideObject] | None:
    digest = _asset_hash(asset)
    basename = Path(asset).name.lower()
    for slide in presentation.slides:
        for item in slide.visible_objects:
            if item.kind not in _MEDIA_KINDS:
                continue
            target_name = Path(item.relationship_target or "").name.lower()
            if digest and item.media_sha256 == digest:
                return slide.page_number, item
            if basename and basename == target_name:
                return slide.page_number, item
    return None


def observe_assets(
    context: object,
    *,
    adapter: PptxAdapter | None = None,
    assets: Sequence[str] | None = None,
    required_assets: Sequence[str] | None = None,
    oracle_id: str = "v8.asset_observations",
    metric_id: str = "asset_presence",
) -> ObservationBatch:
    """Match each asset once; criticality is data, not a duplicate Oracle call."""

    started = time.perf_counter()
    presentation = load_presentation(context, adapter or PptxAdapter())
    case_assets = getattr(_case(context), "assets", ())
    manifest_items = tuple(str(item) for item in (assets if assets is not None else case_assets) if str(item))
    configured_required = case_metadata(context).get("required_assets", ())
    required_values = required_assets if required_assets is not None else configured_required
    required = (
        {str(item) for item in required_values} if isinstance(required_values, (list, tuple, set)) else set()
    )
    items = tuple(dict.fromkeys((*manifest_items, *required)))
    result = []
    for index, asset in enumerate(items, start=1):
        match = _match_asset(asset, presentation)
        critical = asset in required
        page, item = match if match is not None else (None, None)
        unit_key = f"asset:{index}:{Path(asset).name}"
        result.append(
            _scored_observation(
                presentation,
                oracle_id=oracle_id,
                metric_id=metric_id,
                scope=EvaluationScope.ASSET,
                unit_key=unit_key,
                score=float(match is not None),
                raw_value=match is not None,
                confidence=0.99,
                importance=1.5 if critical else 1.0,
                key_unit=critical,
                critical=critical and match is None,
                severity=Severity.CRITICAL
                if critical and match is None
                else Severity.MAJOR
                if match is None
                else Severity.INFO,
                evidence_items=(
                    evidence(
                        metric_id,
                        unit_key,
                        "matched_asset" if match else "missing_asset",
                        f"Asset '{Path(asset).name}' matched an embedded media object."
                        if match
                        else f"Asset '{Path(asset).name}' was not matched.",
                        page_number=page,
                        object_id=item.object_id if item else None,
                        bbox=item.bbox.as_tuple() if item else None,
                        source_uri=asset,
                        payload={"critical": critical, "match_method": "sha256_or_part_name"},
                    ),
                ),
                metadata={"asset_index": index, "asset_name": Path(asset).name},
            )
        )
    if not result:
        result.append(
            _na_observation(
                presentation,
                oracle_id=oracle_id,
                metric_id=metric_id,
                scope=EvaluationScope.ASSET,
                unit_key="asset:none",
                reason="No asset manifest was supplied.",
            )
        )
    return ObservationBatch(
        oracle_id=oracle_id,
        observations=tuple(result),
        version=V8_ATOMIC_VERSION,
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
        metadata={"metric_id": metric_id, "assets": len(items)},
    )


def _chart_values(item: SlideObject) -> tuple[str, ...]:
    value = item.metadata.get("chart_values", ())
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(str(part) for part in value)


def observe_chart_series(
    context: object,
    *,
    adapter: PptxAdapter | None = None,
    expectations: Sequence[str] | Mapping[str, object] | None = None,
    critical_expectations: Sequence[str] | None = None,
    oracle_id: str = "v8.chart_series_observations",
    metric_id: str = "chart_series_accuracy",
) -> ObservationBatch:
    """Bind expected chart facts to chart-object values instead of deck text."""

    started = time.perf_counter()
    presentation = load_presentation(context, adapter or PptxAdapter())
    configured = case_metadata(context).get("chart_expectations")
    configured_critical = case_metadata(context).get("critical_chart_values")
    explicit_values = _fact_strings(expectations if expectations is not None else configured)
    critical_items = (
        [str(item) for item in critical_expectations if str(item).strip()]
        if critical_expectations is not None
        else _fact_strings(configured_critical)
    )
    values = list(dict.fromkeys((*explicit_values, *critical_items)))
    critical_values = {normalize_text(item) for item in critical_items}
    charts = [
        (slide.page_number, item, _chart_values(item))
        for slide in presentation.slides
        for item in slide.visible_objects
        if item.kind == "chart"
    ]
    result = []
    for index, expectation in enumerate(values, start=1):
        candidates = [
            (token_recall(expectation, " ".join(chart_values)), page, item, chart_values)
            for page, item, chart_values in charts
        ]
        best = max(candidates, key=lambda row: row[0]) if candidates else None
        coverage, page, item, chart_values = best if best is not None else (0.0, None, None, ())
        critical = normalize_text(expectation) in critical_values
        unit_key = f"chart-expectation:{index}"
        result.append(
            _scored_observation(
                presentation,
                oracle_id=oracle_id,
                metric_id=metric_id,
                scope=EvaluationScope.CHART_SERIES,
                unit_key=unit_key,
                score=coverage,
                raw_value=coverage,
                confidence=0.86,
                importance=1.5 if critical else 1.0,
                key_unit=critical,
                critical=critical and coverage < 0.70,
                severity=Severity.CRITICAL if critical and coverage < 0.70 else _score_severity(coverage),
                evidence_items=(
                    evidence(
                        metric_id,
                        unit_key,
                        "chart_value_match" if coverage >= 0.70 else "chart_value_gap",
                        f"Best chart-object value coverage is {coverage:.0%}.",
                        page_number=page,
                        object_id=item.object_id if item else None,
                        bbox=item.bbox.as_tuple() if item else None,
                        payload={
                            "expectation": expectation,
                            "chart_values": chart_values,
                            "critical": critical,
                            "binding_scope": "chart_object_values",
                        },
                    ),
                ),
                metadata={"expectation_index": index, "charts_considered": len(charts)},
            )
        )
    if not result:
        result.append(
            _na_observation(
                presentation,
                oracle_id=oracle_id,
                metric_id=metric_id,
                scope=EvaluationScope.CHART_SERIES,
                unit_key="chart-expectation:none",
                reason="No chart ground-truth expectation was supplied.",
                metadata={"chart_objects": len(charts)},
            )
        )
    return ObservationBatch(
        oracle_id=oracle_id,
        observations=tuple(result),
        version=V8_ATOMIC_VERSION,
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
        metadata={
            "metric_id": metric_id,
            "expectations": len(values),
            "chart_objects": len(charts),
            "binding_scope": "chart_object_values",
        },
    )


class ReadingOrderProxyOracle(ScopedObservationOracle):
    """Compare XML text order with the visible top-to-bottom/left-to-right order."""

    oracle_id = "v8.reading_order_proxy"
    metric_id = "reading_order_proxy"
    expected_scope = EvaluationScope.PAGE

    def _observe(
        self, context: object, presentation: ParsedPresentation
    ) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            text_objects = [item for item in slide.visible_objects if item.visible_text.strip()]
            if len(text_objects) < 2:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=EvaluationScope.PAGE,
                        unit_key=f"page:{slide.page_number}",
                        reason="Fewer than two visible text objects; reading order is not observable.",
                    )
                )
                continue
            visual_order = sorted(
                range(len(text_objects)),
                key=lambda index: (
                    round(text_objects[index].bbox.y, 3),
                    round(text_objects[index].bbox.x, 3),
                ),
            )
            positions = {original_index: rank for rank, original_index in enumerate(visual_order)}
            inversions = sum(
                positions[left] > positions[right]
                for left in range(len(text_objects))
                for right in range(left + 1, len(text_objects))
            )
            pairs = len(text_objects) * (len(text_objects) - 1) / 2
            score = 1.0 - inversions / max(1.0, pairs)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=EvaluationScope.PAGE,
                    unit_key=f"page:{slide.page_number}",
                    score=score,
                    raw_value=inversions,
                    confidence=0.70,
                    severity=Severity.MAJOR if score < 0.50 else _score_severity(score),
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"reading-order-{slide.page_number}",
                            "reading_order_proxy",
                            f"XML/visual order contains {inversions} pair inversions.",
                            page_number=slide.page_number,
                            payload={"objects": len(text_objects), "pair_inversions": inversions},
                        ),
                    ),
                    metadata={"proxy_only": True, "text_objects": len(text_objects)},
                )
            )
        return tuple(result)


def _rendered_image_paths(context: object) -> Mapping[int, Path]:
    artifacts = getattr(context, "artifacts", {})
    value = artifacts.get("slide_images", ()) if isinstance(artifacts, Mapping) else ()
    if isinstance(value, (str, bytes, Path)) or not isinstance(value, Sequence):
        return {}
    result: dict[int, Path] = {}
    for index, item in enumerate(value, start=1):
        if isinstance(item, Mapping):
            page_number = int(item.get("page_number", index))
            uri = item.get("uri") or item.get("path")
        elif hasattr(item, "page_number") and hasattr(item, "uri"):
            page_number = int(getattr(item, "page_number"))
            uri = getattr(item, "uri")
        else:
            page_number = index
            uri = item
        if uri:
            result[page_number] = Path(str(uri))
    return result


def _histogram_percentile(histogram: Sequence[int], fraction: float) -> int:
    target = max(1, round(sum(histogram) * fraction))
    cumulative = 0
    for value, amount in enumerate(histogram):
        cumulative += amount
        if cumulative >= target:
            return value
    return 255


def _relative_luminance(value: int) -> float:
    channel = value / 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


class PixelContrastProxyOracle(ScopedObservationOracle):
    """Estimate text/background contrast from rendered text regions."""

    oracle_id = "v8.pixel_contrast_proxy"
    metric_id = "slide_pixel_contrast"
    expected_scope = EvaluationScope.PAGE

    def _observe(
        self, context: object, presentation: ParsedPresentation
    ) -> tuple[AtomicObservation, ...]:
        paths = _rendered_image_paths(context)
        result = []
        for slide in presentation.slides:
            path = paths.get(slide.page_number)
            text_objects = [item for item in slide.visible_objects if item.visible_text.strip()]
            if path is None or not path.is_file() or not text_objects:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=EvaluationScope.PAGE,
                        unit_key=f"page:{slide.page_number}",
                        reason="Rendered pixels or visible text regions are unavailable for contrast estimation.",
                        metadata={"page_number": slide.page_number},
                    )
                )
                continue
            try:
                with Image.open(path) as opened:
                    image = opened.convert("L")
                    object_scores: list[tuple[float, SlideObject, float, float]] = []
                    for item in text_objects:
                        left = max(0, round(item.bbox.x * image.width))
                        top = max(0, round(item.bbox.y * image.height))
                        right = min(image.width, round((item.bbox.x + item.bbox.width) * image.width))
                        bottom = min(image.height, round((item.bbox.y + item.bbox.height) * image.height))
                        if right <= left or bottom <= top:
                            continue
                        histogram = image.crop((left, top, right, bottom)).histogram()
                        dark = _relative_luminance(_histogram_percentile(histogram, 0.10))
                        light = _relative_luminance(_histogram_percentile(histogram, 0.90))
                        ratio = (light + 0.05) / (dark + 0.05)
                        largest_font = max(item.font_sizes_pt or (0.0,))
                        target = 3.0 if largest_font >= 24.0 else 4.5
                        object_scores.append((clamp(ratio / target), item, ratio, target))
            except OSError:
                object_scores = []
            if not object_scores:
                result.append(
                    _na_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=EvaluationScope.PAGE,
                        unit_key=f"page:{slide.page_number}",
                        reason="Rendered text regions could not be decoded for contrast estimation.",
                    )
                )
                continue
            page_score = statistics.fmean(item[0] for item in object_scores)
            worst = min(object_scores, key=lambda item: item[0])
            role = classify_slide_role(slide, presentation.slide_count)
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=EvaluationScope.PAGE,
                    unit_key=f"page:{slide.page_number}",
                    score=page_score,
                    raw_value=worst[2],
                    confidence=0.72,
                    severity=Severity.CRITICAL if page_score < 0.35 else _score_severity(page_score),
                    importance=_importance(role),
                    key_unit=role in {"cover", "data"},
                    critical=page_score < 0.35 and role in {"cover", "data"},
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"contrast-{slide.page_number}-{worst[1].object_id}",
                            "pixel_contrast_proxy",
                            f"Worst rendered-region luminance ratio is {worst[2]:.2f}:1.",
                            page_number=slide.page_number,
                            object_id=worst[1].object_id,
                            bbox=worst[1].bbox.as_tuple(),
                            payload={"ratio": worst[2], "target": worst[3]},
                        ),
                    ),
                    metadata={"proxy_only": True, "text_regions": len(object_scores)},
                )
            )
        return tuple(result)


class EffectiveImageResolutionOracle(ScopedObservationOracle):
    """Measure embedded image pixels against their displayed slide area."""

    oracle_id = "v8.effective_image_resolution"
    metric_id = "effective_image_resolution"
    expected_scope = EvaluationScope.OBJECT

    def _observe(
        self, context: object, presentation: ParsedPresentation
    ) -> tuple[AtomicObservation, ...]:
        del context
        result = []
        for slide in presentation.slides:
            role = classify_slide_role(slide, presentation.slide_count)
            for item in slide.visible_objects:
                if item.kind not in {"picture", "linked_picture"}:
                    continue
                raw_size = item.metadata.get("image_size_px")
                unit_key = f"page:{slide.page_number}/object:{item.object_id}"
                if (
                    not isinstance(raw_size, (tuple, list))
                    or len(raw_size) != 2
                    or int(raw_size[0]) <= 0
                    or int(raw_size[1]) <= 0
                ):
                    result.append(
                        _na_observation(
                            presentation,
                            oracle_id=self.oracle_id,
                            metric_id=self.metric_id,
                            scope=EvaluationScope.OBJECT,
                            unit_key=unit_key,
                            reason="Embedded image pixel dimensions are unavailable.",
                            metadata={"page_number": slide.page_number},
                        )
                    )
                    continue
                width_px, height_px = int(raw_size[0]), int(raw_size[1])
                required_width = max(1.0, item.bbox.width * 1600.0)
                required_height = max(1.0, item.bbox.height * 900.0)
                ratio = min(width_px / required_width, height_px / required_height)
                score = clamp(ratio)
                key_unit = role in {"cover", "data"} or item.bbox.area >= 0.25
                result.append(
                    _scored_observation(
                        presentation,
                        oracle_id=self.oracle_id,
                        metric_id=self.metric_id,
                        scope=EvaluationScope.OBJECT,
                        unit_key=unit_key,
                        score=score,
                        raw_value=ratio,
                        confidence=0.96,
                        severity=Severity.CRITICAL if score < 0.25 else _score_severity(score),
                        importance=1.5 if key_unit else 1.0,
                        key_unit=key_unit,
                        critical=key_unit and score < 0.25,
                        evidence_items=(
                            evidence(
                                self.metric_id,
                                f"resolution-{slide.page_number}-{item.object_id}",
                                "effective_image_resolution",
                                f"Embedded/display pixel ratio is {ratio:.2f}.",
                                page_number=slide.page_number,
                                object_id=item.object_id,
                                bbox=item.bbox.as_tuple(),
                                payload={
                                    "image_size_px": (width_px, height_px),
                                    "required_size_px": (required_width, required_height),
                                },
                            ),
                        ),
                    )
                )
        if not result:
            result.append(
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=EvaluationScope.OBJECT,
                    unit_key="image:none",
                    reason="No embedded picture object is available for resolution evaluation.",
                )
            )
        return tuple(result)


class RenderAvailabilityParityOracle(ScopedObservationOracle):
    """Verify that every object-tree page has a corresponding immutable render."""

    oracle_id = "v8.render_availability_parity"
    metric_id = "render_availability_parity"
    expected_scope = EvaluationScope.PAGE

    def _observe(
        self, context: object, presentation: ParsedPresentation
    ) -> tuple[AtomicObservation, ...]:
        artifacts = getattr(context, "artifacts", {})
        value = artifacts.get("slide_images", ()) if isinstance(artifacts, Mapping) else ()
        rendered_pages: set[int] = set()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Path)):
            for index, item in enumerate(value, start=1):
                if isinstance(item, Mapping):
                    rendered_pages.add(int(item.get("page_number", index)))
                elif hasattr(item, "page_number"):
                    rendered_pages.add(int(getattr(item, "page_number")))
                else:
                    rendered_pages.add(index)
        if not rendered_pages:
            return tuple(
                _na_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=EvaluationScope.PAGE,
                    unit_key=f"page:{slide.page_number}",
                    reason="No rendered slide artifact was supplied for parity validation.",
                    metadata={"page_number": slide.page_number},
                )
                for slide in presentation.slides
            )
        result = []
        for slide in presentation.slides:
            available = slide.page_number in rendered_pages
            result.append(
                _scored_observation(
                    presentation,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    scope=EvaluationScope.PAGE,
                    unit_key=f"page:{slide.page_number}",
                    score=float(available),
                    raw_value=available,
                    confidence=1.0,
                    severity=Severity.INFO if available else Severity.CRITICAL,
                    importance=_importance(
                        classify_slide_role(slide, presentation.slide_count)
                    ),
                    key_unit=slide.page_number in {1, presentation.slide_count},
                    critical=not available,
                    evidence_items=(
                        evidence(
                            self.metric_id,
                            f"render-{slide.page_number}",
                            "render_available" if available else "render_missing",
                            "Rendered page exists for this object-tree slide."
                            if available
                            else "No rendered page was supplied for this object-tree slide.",
                            page_number=slide.page_number,
                        ),
                    ),
                )
            )
        return tuple(result)


V8_ATOMIC_ORACLE_TYPES = (
    SlideRoleClassifierOracle,
    SlideContentPresenceOracle,
    SlideReadingLoadOracle,
    SlideGeometryIntegrityOracle,
    SlideTypographyFunctionalOracle,
    SlideEditabilityOracle,
    MediaIntegrityOracle,
    CropGeometryRiskOracle,
    DocumentStructureOracle,
    AltTextOracle,
    TitleBodyAlignmentOracle,
    DuplicateSlideOracle,
    TransitionCoherenceProxyOracle,
    LanguageConsistencyOracle,
    AuthorshipSpecificitySignalsOracle,
    ReadingOrderProxyOracle,
    RenderAvailabilityParityOracle,
    PixelContrastProxyOracle,
    EffectiveImageResolutionOracle,
)
