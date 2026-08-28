"""Deterministic PPT-native quality Oracles.

This baseline is the mandatory fallback for every scene.  It intentionally uses
only package/object evidence, so it remains available when source materials,
assets, a renderer, or model providers are unavailable.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from ppt_eval.adapters.pptx import PptxAdapter, PptxAdapterError, SlideObject
from ppt_eval.domain.enums import ScoreRole, Severity
from ppt_eval.domain.models import Evidence, OracleResult

from .base import AtomicOracle, CompositeOracle, case_metadata, clamp, evidence


class FileDeliverabilityOracle(AtomicOracle):
    oracle_id = "file_deliverability_oracle"
    metric_id = "file_deliverability"
    score_role = ScoreRole.BASE_MULTIPLIER

    def _evaluate(self, context: object) -> OracleResult:
        try:
            presentation = self.presentation(context)
        except PptxAdapterError as exc:
            report = getattr(exc, "report", None)
            findings = getattr(report, "findings", ())
            details = [
                evidence(
                    self.metric_id,
                    f"finding-{index}",
                    "security_finding",
                    finding.message,
                    payload={
                        "code": finding.code,
                        "severity": finding.severity,
                        "entry": finding.entry,
                        **dict(finding.payload),
                    },
                )
                for index, finding in enumerate(findings, start=1)
            ]
            if not details:
                details = [
                    evidence(
                        self.metric_id,
                        "unreadable",
                        "file_error",
                        f"The presentation cannot be opened: {exc}",
                        payload={"error_type": type(exc).__name__},
                    )
                ]
            return self.multiplied(
                0.0,
                details,
                raw_value="unreadable",
                confidence=1.0,
                severity=Severity.CRITICAL,
            )

        report = presentation.preflight
        return self.multiplied(
            1.0,
            (
                evidence(
                    self.metric_id,
                    "package-summary",
                    "package_summary",
                    f"PPTX opened successfully with {presentation.slide_count} slides.",
                    payload={
                        "sha256": presentation.source_sha256,
                        "parser_backend": presentation.parser_backend,
                        "entry_count": report.entry_count,
                        "archive_bytes": report.archive_bytes,
                        "has_macros": report.has_macros,
                        "has_external_relationships": report.has_external_relationships,
                    },
                ),
            ),
            raw_value="readable",
        )


class CriticalContentVisibilityOracle(AtomicOracle):
    oracle_id = "critical_content_visibility_oracle"
    metric_id = "critical_content_visibility"
    score_role = ScoreRole.BASE_MULTIPLIER

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        if not presentation.slides:
            return self.multiplied(
                0.0,
                (evidence(self.metric_id, "no-slides", "content", "Presentation has no slides."),),
                raw_value=0,
            )
        visible_counts = [
            sum(
                1
                for item in slide.visible_objects
                if item.visible_text or item.kind in {"picture", "chart", "table", "media"}
            )
            for slide in presentation.slides
        ]
        nonblank = sum(value > 0 for value in visible_counts)
        ratio = nonblank / len(visible_counts)
        blank_evidence = tuple(
            evidence(
                self.metric_id,
                f"blank-{slide.page_number}",
                "blank_slide",
                "Slide has no visible text or semantic media objects.",
                page_number=slide.page_number,
            )
            for slide, count in zip(presentation.slides, visible_counts)
            if count == 0
        )[:20]
        if nonblank == 0:
            multiplier = 0.0
        elif ratio < 0.5:
            multiplier = 0.5
        else:
            multiplier = 1.0
        return self.multiplied(
            multiplier,
            blank_evidence
            or (
                evidence(
                    self.metric_id,
                    "visible",
                    "content_summary",
                    "Every slide contains visible content.",
                    payload={"nonblank_slide_ratio": ratio},
                ),
            ),
            raw_value=ratio,
            metadata={"nonblank_slides": nonblank, "slide_count": len(visible_counts)},
        )


class InternalDataConsistencyOracle(AtomicOracle):
    oracle_id = "internal_data_consistency_oracle"
    metric_id = "internal_data_consistency"
    score_role = ScoreRole.BASE_MULTIPLIER

    _PAIR = re.compile(
        r"(?P<label>[A-Za-z\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff ]{1,24})\s*[:：]\s*"
        r"(?P<value>[+\-]?\d[\d,.]*(?:%|万|亿|k|m)?)",
        re.IGNORECASE,
    )

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        configured = case_metadata(context).get("critical_consistency_keys", ())
        keys = {str(item).strip().lower() for item in configured if str(item).strip()}
        observations: dict[str, list[tuple[str, int, SlideObject]]] = defaultdict(list)
        for slide in presentation.slides:
            for item in slide.visible_objects:
                for match in self._PAIR.finditer(item.text):
                    label = re.sub(r"\s+", "", match.group("label")).lower()
                    if keys and not any(key.replace(" ", "") in label for key in keys):
                        continue
                    observations[label].append((match.group("value").lower(), slide.page_number, item))

        # Only explicitly declared business-critical keys may activate a hard gate.
        if not keys:
            return self.multiplied(
                1.0,
                (
                    evidence(
                        self.metric_id,
                        "no-critical-keys",
                        "scope",
                        "No critical consistency keys were declared; no hard contradiction was inferred.",
                    ),
                ),
                raw_value="no_declared_keys",
            )

        conflicts = {
            label: values
            for label, values in observations.items()
            if len({value for value, _, _ in values}) > 1
        }
        details = []
        for label, values in conflicts.items():
            for value, page, item in values:
                details.append(
                    evidence(
                        self.metric_id,
                        f"{label}-{page}-{item.object_id}-{value}",
                        "data_conflict",
                        f"Critical field '{label}' has inconsistent value '{value}'.",
                        page_number=page,
                        object_id=item.object_id,
                        bbox=item.bbox.as_tuple(),
                        payload={"field": label, "value": value},
                    )
                )
        multiplier = 0.5 if conflicts else 1.0
        return self.multiplied(
            multiplier,
            details
            or (
                evidence(
                    self.metric_id,
                    "consistent",
                    "data_summary",
                    "No contradiction was found for declared critical fields.",
                    payload={"keys": sorted(keys)},
                ),
            ),
            raw_value=len(conflicts),
            confidence=0.98,
        )


class ContentClarityOracle(AtomicOracle):
    oracle_id = "content_clarity_oracle"
    metric_id = "content_clarity"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        if not presentation.slides:
            return self.scored(0.0, raw_value=0)
        dense: list[tuple[int, int]] = []
        blank = 0
        fragmented: list[tuple[int, int]] = []
        for slide in presentation.slides:
            text_length = len(slide.visible_text)
            if text_length == 0:
                blank += 1
            if text_length > 700:
                dense.append((slide.page_number, text_length))
            text_objects = sum(bool(item.visible_text) for item in slide.visible_objects)
            if text_objects > 18:
                fragmented.append((slide.page_number, text_objects))
        count = len(presentation.slides)
        score = 1.0 - 0.45 * blank / count - 0.35 * len(dense) / count - 0.20 * len(fragmented) / count
        details = [
            evidence(
                self.metric_id,
                f"dense-{page}",
                "dense_slide",
                f"Slide contains {length} visible characters.",
                page_number=page,
                payload={"visible_characters": length, "threshold": 700},
            )
            for page, length in dense
        ]
        details.extend(
            evidence(
                self.metric_id,
                f"fragmented-{page}",
                "fragmented_content",
                f"Slide contains {amount} separate text objects.",
                page_number=page,
                payload={"text_objects": amount, "threshold": 18},
            )
            for page, amount in fragmented
        )
        return self.scored(
            score,
            details[:20],
            raw_value=round(score, 4),
            metadata={"blank_slides": blank, "dense_slides": len(dense)},
        )


class TemplateResidueOracle(AtomicOracle):
    """Detect high-confidence, user-visible template residue.

    The rules deliberately avoid broad keyword matching.  For example, a real
    date, a named presenter, ``XXL`` and prose containing the word "date" are
    not findings.  This metric is additive because the deterministic patterns
    are strong delivery defects but are not sufficient to reject a deck as a
    non-compensable hard gate.
    """

    oracle_id = "template_residue_oracle"
    metric_id = "template_residue"
    score_role = ScoreRole.BASE_ADDITIVE
    version = "1.0.0"

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        findings: list[tuple[int, SlideObject, tuple[str, ...]]] = []
        reason_counts: dict[str, int] = defaultdict(int)
        affected_pages: set[int] = set()

        for slide in presentation.slides:
            for item in slide.visible_objects:
                if not item.visible_text:
                    continue
                reasons = _template_residue_reasons(item.visible_text)
                if not reasons:
                    continue
                findings.append((slide.page_number, item, reasons))
                affected_pages.add(slide.page_number)
                for reason in reasons:
                    reason_counts[reason] += 1

        score = template_residue_score(
            len(findings), len(affected_pages), presentation.slide_count
        )
        details = tuple(
            evidence(
                self.metric_id,
                f"residue-{page}-{item.object_id}",
                "template_residue",
                "Visible text contains a high-confidence unresolved template marker.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={
                    "reason_codes": reasons,
                    "matched_excerpt": _evidence_excerpt(item.visible_text),
                },
            )
            for page, item, reasons in findings[:20]
        )
        return self.scored(
            score,
            details,
            raw_value=len(findings),
            confidence=0.96,
            metadata={
                "residue_objects": len(findings),
                "affected_pages": len(affected_pages),
                "reason_counts": dict(sorted(reason_counts.items())),
            },
        )


class NarrativeOracle(AtomicOracle):
    oracle_id = "narrative_oracle"
    metric_id = "narrative"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        if not presentation.slides:
            return self.scored(0.0)
        titled = []
        missing = []
        for slide in presentation.slides:
            title = _title_object(slide.visible_objects)
            if title is None:
                missing.append(slide.page_number)
            else:
                titled.append(title.text)
        title_ratio = len(titled) / len(presentation.slides)
        duplicate_ratio = 0.0
        if titled:
            duplicate_ratio = 1.0 - len({value.strip().lower() for value in titled}) / len(titled)
        first_has_title = bool(_title_object(presentation.slides[0].visible_objects))
        score = 0.15 + 0.65 * title_ratio + 0.20 * float(first_has_title) - 0.20 * duplicate_ratio
        details = tuple(
            evidence(
                self.metric_id,
                f"missing-title-{page}",
                "structure",
                "No likely slide title was detected.",
                page_number=page,
            )
            for page in missing[:20]
        )
        return self.scored(
            score,
            details,
            raw_value=title_ratio,
            confidence=0.82,
            metadata={"title_coverage": title_ratio, "duplicate_title_ratio": duplicate_ratio},
        )


class BodyCompletenessOracle(AtomicOracle):
    """Diagnostic body-signal coverage for text-observable slides.

    This does not claim semantic correctness. It identifies repeated
    title-only or near-empty content pages while treating the cover, a
    conventional closing page, and substantial charts/media as valid bodies.
    Raster-only decks are N/A because pixels own their content observability.
    """

    oracle_id = "body_completeness_oracle"
    metric_id = "body_completeness"
    score_role = ScoreRole.DIAGNOSTIC
    version = "1.0.0"

    _CLOSING = re.compile(
        r"(?:thank(?:\s+you)?|questions?|q\s*&\s*a|contact|the\s+end|"
        r"谢谢|感谢|提问|联系我们)",
        re.IGNORECASE,
    )
    _SEMANTIC_VISUAL_KINDS = frozenset(
        {"chart", "table", "picture", "linked_picture", "media"}
    )

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        observable_pages = sum(
            bool(slide.visible_text.strip()) for slide in presentation.slides
        )
        text_page_ratio = observable_pages / max(1, presentation.slide_count)
        if text_page_ratio < 0.25:
            result = self.not_applicable(
                "Too few slides have extractable text for an object-tree body audit.",
                code="TEXT_OBSERVABILITY_INSUFFICIENT",
            )
            return replace(
                result,
                metadata={
                    **dict(result.metadata),
                    "observable_pages": observable_pages,
                    "slide_count": presentation.slide_count,
                    "text_page_ratio": text_page_ratio,
                },
            )

        assessed = 0
        complete = 0
        exempt_pages: list[int] = []
        incomplete: list[tuple[int, SlideObject, int]] = []
        last_page = presentation.slide_count
        for slide in presentation.slides:
            if slide.page_number == 1:
                exempt_pages.append(slide.page_number)
                continue
            if slide.page_number == last_page and self._CLOSING.search(
                slide.visible_text
            ):
                exempt_pages.append(slide.page_number)
                continue
            text_objects = [
                item for item in slide.visible_objects if item.visible_text.strip()
            ]
            if not text_objects:
                continue  # blank-page quality belongs to ContentClarityOracle
            title = _title_object(slide.visible_objects)
            body_objects = [
                item
                for item in text_objects
                if title is None or item.object_id != title.object_id
            ]
            body_characters = len(
                re.sub(r"\s+", "", "\n".join(item.visible_text for item in body_objects))
            )
            has_semantic_visual = any(
                item.kind in self._SEMANTIC_VISUAL_KINDS and item.bbox.area >= 0.04
                for item in slide.visible_objects
            )
            assessed += 1
            if body_characters >= 40 or has_semantic_visual:
                complete += 1
            else:
                anchor = title or text_objects[0]
                incomplete.append((slide.page_number, anchor, body_characters))

        if assessed == 0:
            return self.not_applicable(
                "No non-cover text-observable content slides were available.",
                code="NO_ASSESSABLE_BODY_SLIDES",
            )
        score = complete / assessed
        details = tuple(
            evidence(
                self.metric_id,
                f"body-{page}",
                "body_content_missing",
                "Slide has a title or short label but no substantial body text or visual body.",
                page_number=page,
                object_id=anchor.object_id,
                bbox=anchor.bbox.as_tuple(),
                payload={"body_characters": characters, "minimum_characters": 40},
            )
            for page, anchor, characters in incomplete[:20]
        )
        return self.scored(
            score,
            details,
            raw_value=score,
            confidence=0.78,
            metadata={
                "assessed_pages": assessed,
                "complete_pages": complete,
                "incomplete_pages": [page for page, _, _ in incomplete],
                "exempt_pages": exempt_pages,
                "text_page_ratio": text_page_ratio,
            },
        )


class VisualHierarchyOracle(AtomicOracle):
    oracle_id = "visual_hierarchy_oracle"
    metric_id = "visual_hierarchy"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        scores: list[float] = []
        details = []
        for slide in presentation.slides:
            title = _title_object(slide.visible_objects)
            text_objects = [item for item in slide.visible_objects if item.visible_text]
            if not text_objects:
                scores.append(0.3)
                continue
            all_sizes = [size for item in text_objects for size in item.font_sizes_pt]
            title_sizes = list(title.font_sizes_pt) if title else []
            if all_sizes and title_sizes:
                body = statistics.median(all_sizes)
                ratio = max(title_sizes) / max(1.0, body)
                local = clamp(0.45 + 0.35 * min(2.0, ratio - 0.5))
            else:
                local = 0.72 if title else 0.45
            if title is None:
                details.append(
                    evidence(
                        self.metric_id,
                        f"weak-{slide.page_number}",
                        "visual_hierarchy",
                        "No visually plausible title anchor was found.",
                        page_number=slide.page_number,
                    )
                )
            scores.append(local)
        score = statistics.fmean(scores) if scores else 0.0
        return self.scored(score, details[:20], confidence=0.72 if scores else 1.0)


class LayoutOracle(AtomicOracle):
    oracle_id = "layout_oracle"
    metric_id = "layout"
    score_role = ScoreRole.BASE_ADDITIVE
    version = "1.1.0"

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        considered = 0
        outside: list[tuple[int, SlideObject]] = []
        overlaps: list[tuple[int, SlideObject, SlideObject, float, str]] = []
        ignored_overlaps = 0
        ignored_outside_tolerance = 0
        ignored_intentional_outside = 0
        for slide in presentation.slides:
            objects = [
                item
                for item in slide.visible_objects
                if item.bbox.area > 1e-5 and item.kind not in {"group", "connector"}
            ]
            considered += len(objects)
            for item in objects:
                if not item.bbox.is_outside_slide:
                    continue
                if _is_intentional_outside(item):
                    ignored_intentional_outside += 1
                elif _meaningfully_outside_slide(item):
                    outside.append((slide.page_number, item))
                else:
                    ignored_outside_tolerance += 1
            for index, left in enumerate(objects):
                if left.bbox.area > 0.80:
                    continue
                for right in objects[index + 1 :]:
                    if right.bbox.area > 0.80:
                        continue
                    ratio = _overlap_ratio(left, right)
                    classification = _defective_overlap_class(left, right, ratio)
                    if classification is not None:
                        overlaps.append(
                            (slide.page_number, left, right, ratio, classification)
                        )
                    elif ratio > 0.35:
                        ignored_overlaps += 1
        denominator = max(1, considered)
        score = 1.0 - min(0.65, 2.5 * len(outside) / denominator) - min(
            0.45, 1.5 * len(overlaps) / denominator
        )
        details = [
            evidence(
                self.metric_id,
                f"outside-{page}-{item.object_id}",
                "out_of_bounds",
                "Object extends outside the slide canvas.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
            )
            for page, item in outside[:10]
        ]
        details.extend(
            evidence(
                self.metric_id,
                f"overlap-{page}-{left.object_id}-{right.object_id}",
                "overlap",
                "Peer objects substantially overlap.",
                page_number=page,
                object_id=left.object_id,
                bbox=left.bbox.as_tuple(),
                payload={
                    "other_object_id": right.object_id,
                    "overlap_ratio": round(ratio, 4),
                    "classification": classification,
                    "left_kind": left.kind,
                    "right_kind": right.kind,
                },
            )
            for page, left, right, ratio, classification in overlaps[:10]
        )
        return self.scored(
            score,
            details,
            metadata={
                "objects_considered": considered,
                "outside": len(outside),
                "overlaps": len(overlaps),
                "ignored_intentional_overlaps": ignored_overlaps,
                "ignored_outside_tolerance": ignored_outside_tolerance,
                "ignored_intentional_outside": ignored_intentional_outside,
            },
        )


class TypographyOracle(AtomicOracle):
    oracle_id = "typography_oracle"
    metric_id = "typography"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        observed: list[float] = []
        too_small: list[tuple[int, SlideObject, float]] = []
        for slide in presentation.slides:
            for item in slide.visible_objects:
                if not item.visible_text:
                    continue
                observed.extend(item.font_sizes_pt)
                if item.font_sizes_pt and min(item.font_sizes_pt) < 14.0:
                    too_small.append((slide.page_number, item, min(item.font_sizes_pt)))
        if observed:
            score = 1.0 - 0.75 * len(too_small) / max(1, sum(bool(item.visible_text) for slide in presentation.slides for item in slide.visible_objects))
            confidence = 0.92
        else:
            # Inherited theme sizes are not always materialized in runs.  Preserve
            # coverage without pretending the evidence is as strong as explicit sizes.
            dense_slides = sum(len(slide.visible_text) > 700 for slide in presentation.slides)
            score = 0.78 - 0.25 * dense_slides / max(1, presentation.slide_count)
            confidence = 0.55
        details = tuple(
            evidence(
                self.metric_id,
                f"small-{page}-{item.object_id}",
                "small_text",
                f"Text includes an explicit {size:g} pt run below the 14 pt floor.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={"minimum_font_pt": size},
            )
            for page, item, size in too_small[:20]
        )
        return self.scored(score, details, confidence=confidence, raw_value=len(too_small))


class StyleConsistencyOracle(AtomicOracle):
    oracle_id = "style_consistency_oracle"
    metric_id = "style_consistency"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        font_names = [
            name.lower()
            for slide in presentation.slides
            for item in slide.visible_objects
            for name in item.font_names
            if not name.startswith("+")
        ]
        title_positions = [
            title.bbox.y
            for slide in presentation.slides
            if (title := _title_object(slide.visible_objects)) is not None
        ]
        family_penalty = max(0, len(set(font_names)) - 3) * 0.08
        position_penalty = 0.0
        if len(title_positions) >= 3:
            position_penalty = min(0.30, statistics.pstdev(title_positions) * 2.0)
        score = 1.0 - min(0.45, family_penalty) - position_penalty
        confidence = 0.85 if font_names else 0.62
        details: tuple[Evidence, ...] = ()
        if len(set(font_names)) > 5:
            details = (
                evidence(
                    self.metric_id,
                    "font-diversity",
                    "style",
                    f"Presentation explicitly uses {len(set(font_names))} font families.",
                    payload={"font_families": sorted(set(font_names))[:20]},
                ),
            )
        return self.scored(
            score,
            details,
            confidence=confidence,
            metadata={"explicit_font_families": len(set(font_names))},
        )


class MultimediaQualityOracle(AtomicOracle):
    oracle_id = "multimedia_quality_oracle"
    metric_id = "multimedia_quality"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        media = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind in {"picture", "media", "linked_picture"}
        ]
        broken = [(page, item) for page, item in media if not item.media_sha256]
        tiny = [(page, item) for page, item in media if item.bbox.area < 0.0025]
        score = 1.0 if not media else 1.0 - 0.75 * len(broken) / len(media) - 0.25 * len(tiny) / len(media)
        details = tuple(
            evidence(
                self.metric_id,
                f"broken-{page}-{item.object_id}",
                "media_unavailable",
                "Media object has no readable embedded payload.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={"relationship_target": item.relationship_target},
            )
            for page, item in broken[:20]
        )
        return self.scored(
            score,
            details,
            raw_value=len(media) - len(broken),
            metadata={"media_objects": len(media), "broken_media": len(broken)},
        )


class EditabilityOracle(AtomicOracle):
    oracle_id = "editability_oracle"
    metric_id = "editability"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        semantic = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind not in {"group", "connector", "line"} and item.bbox.area > 1e-6
        ]
        if not semantic:
            return self.scored(0.0, raw_value=0)
        editable = sum(item.editable for _, item in semantic)
        raster_only_pages = []
        for slide in presentation.slides:
            visible = [item for item in slide.visible_objects if item.bbox.area > 1e-6]
            if visible and not any(item.editable and item.visible_text for item in visible):
                if any(item.kind == "picture" and item.bbox.area > 0.65 for item in visible):
                    raster_only_pages.append(slide.page_number)
        ratio = editable / len(semantic)
        score = 0.20 + 0.80 * ratio - 0.35 * len(raster_only_pages) / max(1, presentation.slide_count)
        details = tuple(
            evidence(
                self.metric_id,
                f"raster-{page}",
                "rasterized_slide",
                "Slide appears to be flattened into a large picture and has no editable text.",
                page_number=page,
            )
            for page in raster_only_pages
        )
        return self.scored(
            score,
            details,
            raw_value=ratio,
            metadata={"editable_ratio": ratio, "raster_only_pages": raster_only_pages},
        )


class CompatibilityOracle(AtomicOracle):
    oracle_id = "compatibility_oracle"
    metric_id = "compatibility"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        report = presentation.preflight
        unknown = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.objects
            if item.kind in {"unknown", "embedded_ole_object", "linked_ole_object"}
        ]
        score = 1.0
        if report.has_macros:
            score -= 0.25
        if report.has_external_relationships:
            score -= 0.20
        score -= min(0.35, 0.08 * len(unknown))
        details = tuple(
            evidence(
                self.metric_id,
                finding.code,
                "compatibility_risk",
                finding.message,
                payload={"code": finding.code, "entry": finding.entry},
            )
            for finding in report.findings
            if finding.severity == "WARN"
        )
        return self.scored(
            score,
            details,
            raw_value=len(details),
            metadata={"parser_backend": presentation.parser_backend, "warnings": presentation.warnings},
        )


class AccessibilityOracle(AtomicOracle):
    oracle_id = "accessibility_oracle"
    metric_id = "accessibility"
    score_role = ScoreRole.BASE_ADDITIVE

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        images = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind in {"picture", "media"}
        ]
        described = [
            (page, item)
            for page, item in images
            if _meaningful_alt_text(str(item.metadata.get("alt_text", "")), item.name)
        ]
        title_ratio = sum(_title_object(slide.visible_objects) is not None for slide in presentation.slides) / max(
            1, presentation.slide_count
        )
        image_ratio = 1.0 if not images else len(described) / len(images)
        score = 0.55 * title_ratio + 0.45 * image_ratio
        missing = [(page, item) for page, item in images if (page, item) not in described]
        details = tuple(
            evidence(
                self.metric_id,
                f"alt-{page}-{item.object_id}",
                "missing_alt_text",
                "Image has no meaningful alternative text in the OOXML metadata.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
            )
            for page, item in missing[:20]
        )
        return self.scored(
            score,
            details,
            confidence=0.82,
            metadata={"image_alt_coverage": image_ratio, "title_coverage": title_ratio},
        )


class BaselinePptQualityOracle(CompositeOracle):
    """Mandatory baseline subgraph.  The fixed id is a compiler invariant."""

    ORACLE_ID = "baseline_ppt_quality"
    oracle_id = ORACLE_ID
    metric_id = "baseline_ppt_quality"
    version = "8.0.0"

    def __init__(self, adapter: PptxAdapter | None = None) -> None:
        children = (
            FileDeliverabilityOracle(adapter),
            CriticalContentVisibilityOracle(adapter),
            InternalDataConsistencyOracle(adapter),
        )
        super().__init__(children)


def _title_object(objects: Iterable[SlideObject]) -> SlideObject | None:
    candidates = [
        item
        for item in objects
        if item.visible_text and item.bbox.y < 0.32 and 1 < len(item.visible_text) <= 120
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            item.bbox.y,
            -max(item.font_sizes_pt or (0.0,)),
            -item.bbox.width,
        ),
    )


def _overlap_ratio(left: SlideObject, right: SlideObject) -> float:
    x1 = max(left.bbox.x, right.bbox.x)
    y1 = max(left.bbox.y, right.bbox.y)
    x2 = min(left.bbox.x + left.bbox.width, right.bbox.x + right.bbox.width)
    y2 = min(left.bbox.y + left.bbox.height, right.bbox.y + right.bbox.height)
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return intersection / max(1e-9, min(left.bbox.area, right.bbox.area))


def _meaningfully_outside_slide(item: SlideObject, tolerance: float = 0.005) -> bool:
    """Ignore sub-percent OOXML/renderer rounding at slide edges."""

    bbox = item.bbox
    return (
        bbox.x < -tolerance
        or bbox.y < -tolerance
        or bbox.width < 0
        or bbox.height < 0
        or bbox.x + bbox.width > 1.0 + tolerance
        or bbox.y + bbox.height > 1.0 + tolerance
    )


def _is_intentional_outside(item: SlideObject) -> bool:
    """Treat non-semantic decoration bleed as composition, not clipping."""

    return (
        not item.visible_text
        and item.kind not in _SEMANTIC_MEDIA_KINDS
        and _is_decorative_object(item)
    )


_SEMANTIC_MEDIA_KINDS = {
    "chart",
    "linked_picture",
    "media",
    "picture",
    "table",
}


def _defective_overlap_class(
    left: SlideObject, right: SlideObject, ratio: float
) -> str | None:
    """Return a high-precision overlap class, or ``None`` for composition.

    Object-tree bounding boxes do not reveal z-order occlusion or actual glyph
    bounds.  As a result, cards behind labels, timeline connectors through
    nodes, decorative circles, and text laid over imagery must not be treated
    as defects.  Text-on-text overlap remains a useful high-confidence signal.
    Semantic media overlap is retained only at a stricter threshold.
    """

    left_has_text = bool(left.visible_text)
    right_has_text = bool(right.visible_text)
    if left_has_text and right_has_text:
        # Text boxes include internal margins and line-height padding.  The
        # real-deck calibration slice contains legitimate heading/body pairs
        # around 0.50, so deterministic evidence starts above 0.60.
        return "text_text" if ratio > 0.60 else None

    # A label inside a card, text over a hero image, and annotations on charts
    # are common intentional compositions.
    if left_has_text != right_has_text:
        return None

    if _is_decorative_object(left) or _is_decorative_object(right):
        return None

    if left.kind not in _SEMANTIC_MEDIA_KINDS or right.kind not in _SEMANTIC_MEDIA_KINDS:
        return None

    if {left.kind, right.kind} <= {"picture", "linked_picture", "media"}:
        return "media_media" if ratio > 0.75 else None
    return "semantic_media" if ratio > 0.50 else None


def _is_decorative_object(item: SlideObject) -> bool:
    if item.visible_text:
        return False
    if item.kind in {"connector", "group", "shape", "placeholder", "unknown"}:
        return True
    if item.bbox.area > 0.45 or item.bbox.area < 0.0025:
        return True
    if item.bbox.width < 0.012 or item.bbox.height < 0.012:
        return True
    name = re.sub(r"[ _-]+", " ", item.name.lower()).strip()
    return bool(
        re.search(
            r"\b(?:background|connector|decoration|decorative|line|node|"
            r"oval|rectangle|shape|accent|ornament|arrow)\b",
            name,
        )
    )


_TEMPLATE_RESIDUE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lorem_ipsum", re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE)),
    (
        "office_prompt",
        re.compile(
            r"(?:\bclick\s+to\s+add\s+(?:title|subtitle|text)\b|"
            r"(?:\u5355\u51fb|\u70b9\u51fb)(?:\u6b64\u5904)?(?:\u6dfb\u52a0|\u8f93\u5165)(?:\u6807\u9898|\u526f\u6807\u9898|\u6587\u672c|\u5185\u5bb9))",
            re.IGNORECASE,
        ),
    ),
    (
        "date_placeholder",
        re.compile(
            r"(?:(?:\u65e5\u671f|date)\s*[:\uff1a-]?\s*(?:"
            r"(?:20x{2}|y{2,4}|x{2,4})\s*[/.-]\s*(?:m{1,2}|x{1,2})\s*[/.-]\s*(?:d{1,2}|x{1,2})|"
            r"(?:m{1,2}|x{1,2})\s*[/.-]\s*(?:d{1,2}|x{1,2})\s*[/.-]\s*(?:y{2,4}|x{2,4})|"
            r"(?:y{2,4}|x{2,4})\s*\u5e74\s*(?:m{1,2}|x{1,2})\s*\u6708\s*(?:d{1,2}|x{1,2})\s*\u65e5|"
            r"\u5e74\s*\u6708\s*\u65e5|\u5f85\u586b(?:\u5199)?|\u5f85\u5b9a|\u586b\u5199\u65e5\u671f)|"
            r"^(?:20x{2}|y{2,4}|x{4})\s*[/.-]\s*(?:m{1,2}|x{1,2})\s*[/.-]\s*(?:d{1,2}|x{1,2})$)",
            re.IGNORECASE,
        ),
    ),
    (
        "presenter_placeholder",
        re.compile(
            r"(?:(?:\u6c47\u62a5\u4eba|\u62a5\u544a\u4eba|\u6f14\u8bb2\u8005|presenter|prepared\s+by)"
            r"\s*[:\uff1a-]\s*(?:\u59d3\u540d|\u540d\u5b57|name|your\s+name|x{2,}|\u5f85\u586b(?:\u5199)?|\u5f85\u5b9a)\s*[.!\uff01\u3002\u2026_-]*$|"
            r"^(?:\u6c47\u62a5\u4eba|\u62a5\u544a\u4eba|\u6f14\u8bb2\u8005)\s*(?:\u59d3\u540d|\u540d\u79f0)$|"
            r"^(?:presenter|speaker)\s+name$)",
            re.IGNORECASE,
        ),
    ),
    (
        "bracketed_template_field",
        re.compile(
            r"^(?:\{\{|\[|<)\s*(?:insert\s+)?(?:title|subtitle|company|client|"
            r"presenter|name|date|logo|\u6807\u9898|\u526f\u6807\u9898|\u516c\u53f8|\u5ba2\u6237|\u59d3\u540d|\u65e5\u671f|\u5f85\u586b\u5185\u5bb9)"
            r"\s*(?:\}\}|\]|>)$",
            re.IGNORECASE,
        ),
    ),
    (
        "explicit_todo_token",
        re.compile(
            r"^\s*(?:tbd|tbc|todo|fixme|x{2,}|\u5f85\u5b9a|\u5f85\u8865\u5145|\u5f85\u586b\u5199|\u5f85\u66f4\u65b0)"
            r"\s*[.!\uff01\u3002\u2026_-]*\s*$",
            re.IGNORECASE,
        ),
    ),
)


def _template_residue_reasons(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", " ", text).strip()
    return tuple(code for code, pattern in _TEMPLATE_RESIDUE_RULES if pattern.search(normalized))


def template_residue_score(
    finding_count: int, affected_page_count: int, slide_count: int
) -> float:
    """Monotonic score used by :class:`TemplateResidueOracle`."""

    if finding_count < 0 or affected_page_count < 0 or slide_count < 0:
        raise ValueError("template residue counts cannot be negative")
    if finding_count == 0:
        return 1.0
    affected_ratio = min(1.0, affected_page_count / max(1, slide_count))
    penalty = 0.30 + 0.15 * (finding_count - 1) + 0.20 * affected_ratio
    return clamp(1.0 - min(0.90, penalty))


def _evidence_excerpt(text: str, limit: int = 120) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "\u2026"


def _meaningful_alt_text(alt_text: str, name: str) -> bool:
    value = alt_text.strip()
    if len(value) < 3:
        return False
    automatic = re.compile(r"^(picture|image|photo|图片|图像)[ _-]*\d+$", re.IGNORECASE)
    return not automatic.match(value) and value.lower() != name.strip().lower()
