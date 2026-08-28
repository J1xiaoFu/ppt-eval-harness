"""v8 observation acquisition and quality-attribute reduction composites."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from ppt_eval.adapters.model_audits import ModelAuditProvider
from ppt_eval.adapters.pptx import PptxAdapter
from ppt_eval.application.oracle import (
    EvaluationContext,
    MetricDefinition,
    OracleDescriptor,
    OracleExecutionOutput,
)
from ppt_eval.domain import (
    AtomicObservation,
    EvaluationScope,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    OracleResult,
    ReducerSpec,
    SceneType,
    ScoreRole,
    Severity,
)
from ppt_eval.scoring import (
    IMPORTANCE_COVERAGE,
    PAGE_QUALITY,
    PAIR_QUALITY,
    ReducerEngine,
)

from .base import clamp, evidence, load_presentation, normalize_text, read_materials, text_tokens
from .model_audits import (
    V8_GROUNDED_VISUAL_CRITERION_IDS,
    V8_RASTER_TEXT_CRITERION_IDS,
    GroundedSingleCriterionVlmOracle,
)
from .scenarios import (
    AudienceFitOracle,
    FactQualityOracle,
    compression_quality_score,
)
from .v8_atomic import (
    V8_ATOMIC_ORACLE_TYPES,
    observe_assets,
    observe_chart_series,
    observe_requirements,
)

V8_OBSERVATION_COMPOSITE_ID = "v8.atomic_observations"
V8_REDUCER_ORACLE_ID = "v8.quality_reducers"
V8_QUALITY_VERSION = "8.3.0"
V8_VISUAL_CRITERION_IDS = V8_GROUNDED_VISUAL_CRITERION_IDS
V8_RASTER_TEXT_OBSERVATION_METRICS: Mapping[str, str] = {
    "raster_content_structure": "raster_content_structure_vlm",
    "raster_language_consistency": "raster_language_consistency_vlm",
}

V8_BASE_ADDITIVE_METRICS = (
    "content_structure",
    "language_consistency",
    "composition_craft",
    "typography_craft",
    "palette_craft",
    "visual_communication",
    "visual_system_sequence",
    "authorship_specificity_v2",
)
V8_SCENE_METRICS: Mapping[SceneType, tuple[str, ...]] = {
    SceneType.READY_MADE: (),
    SceneType.TEXT_TO_PPT: ("instruction", "audience", "fact_claim"),
    SceneType.PROJECT_SUMMARY: (
        "source_claim",
        "key_point",
        "numeric",
        "compression_richness",
        "traceability",
    ),
    SceneType.MULTIMODAL: (
        "asset_coverage",
        "asset_presentation",
        "crop_image_integrity",
        "chart_fidelity",
        "media_integrity",
    ),
}

_NUMBER = re.compile(r"(?<![\w])[-+]?\d[\d,.]*(?:%|万|亿|k|m)?", re.IGNORECASE)

_CRITERION_RULE_METRICS: Mapping[str, tuple[str, ...]] = {
    "composition_layout": ("slide_geometry_integrity",),
    "typography_legibility": (
        "slide_typography_functional",
        "slide_reading_load",
        "slide_pixel_contrast",
    ),
    "color_contrast": ("slide_pixel_contrast",),
    "imagery_data_visualization": (
        "media_integrity",
        "crop_geometry_risk",
        "effective_image_resolution",
    ),
    "cross_slide_consistency": ("transition_coherence_proxy", "duplicate_slide"),
    "render_integrity": ("render_availability_parity",),
    "authorship_specificity": ("authorship_specificity_signals",),
}

_CONTESTABLE_GATE_MODEL_METRIC_IDS: Mapping[str, str] = {
    "slide_geometry_integrity": "structured_vlm_composition_layout",
    "slide_typography_functional": "structured_vlm_typography_legibility",
    "slide_pixel_contrast": "structured_vlm_color_contrast",
    "effective_image_resolution": "structured_vlm_imagery_data_visualization",
}
_GATE_RULE_KIND_TO_MODEL_DEFECT_CODES: Mapping[str, frozenset[str]] = {
    "out_of_bounds": frozenset(
        {"content_overflow_or_cutoff", "unbalanced_space_distribution"}
    ),
    "overlap": frozenset({"occluded_content", "content_alignment_issue"}),
    "small_text": frozenset({"improper_font_sizing", "poor_text_hierarchy"}),
    "pixel_contrast_proxy": frozenset({"insufficient_color_contrast"}),
    "effective_image_resolution": frozenset(
        {"poor_image_quality_or_editing", "improper_image_sizing"}
    ),
}
_DIRECT_FUNCTIONAL_GATE_METRIC_IDS = frozenset(
    {
        "slide_content_presence",
        "media_integrity",
        "render_availability_parity",
        "requirement_satisfaction",
        "numeric_claim_alignment",
        "asset_presence",
        "chart_series_accuracy",
    }
)
_FUNCTIONAL_GATE_METRIC_IDS = frozenset(
    {
        *_DIRECT_FUNCTIONAL_GATE_METRIC_IDS,
        *_CONTESTABLE_GATE_MODEL_METRIC_IDS,
    }
)


def _scene(context: EvaluationContext) -> SceneType:
    return SceneType(context.case.scene)


def _observation_id(metric_id: str, unit_key: str, source_hash: str) -> str:
    digest = hashlib.sha256(
        f"{V8_OBSERVATION_COMPOSITE_ID}|{metric_id}|{unit_key}|{source_hash}".encode()
    ).hexdigest()[:20]
    return f"obs-{digest}"


def _observation(
    *,
    source_hash: str,
    metric_id: str,
    scope: EvaluationScope,
    unit_key: str,
    score: float | None,
    raw_value: float | str | bool | None,
    confidence: float,
    severity: Severity = Severity.INFO,
    importance: float = 1.0,
    key_unit: bool = False,
    critical: bool = False,
    evidence_items: Iterable[Any] = (),
    metadata: Mapping[str, Any] | None = None,
) -> AtomicObservation:
    status = MetricStatus.SCORED if score is not None else MetricStatus.NA
    return AtomicObservation(
        observation_id=_observation_id(metric_id, unit_key, source_hash),
        oracle_id=V8_OBSERVATION_COMPOSITE_ID,
        metric_id=metric_id,
        scope=scope,
        unit_key=unit_key,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=status,
        raw_value=raw_value,
        local_score=clamp(score) if score is not None else None,
        confidence=confidence,
        severity=severity,
        importance=importance,
        key_unit=key_unit,
        critical=critical,
        evidence=tuple(evidence_items),
        version=V8_QUALITY_VERSION,
        metadata=dict(metadata or {}),
    )


class V8AtomicObservationComposite:
    """Collect general and scene-specific observations without producing a score."""

    oracle_id = V8_OBSERVATION_COMPOSITE_ID
    version = V8_QUALITY_VERSION

    def __init__(self, adapter: PptxAdapter | None = None) -> None:
        self.adapter = adapter or PptxAdapter()
        self.children = tuple(oracle_type(self.adapter) for oracle_type in V8_ATOMIC_ORACLE_TYPES)

    def describe(self) -> OracleDescriptor:
        metrics = [
            MetricDefinition(child.metric_id, ScoreRole.DIAGNOSTIC)
            for child in self.children
        ]
        metrics.extend(
            MetricDefinition(metric_id, ScoreRole.DIAGNOSTIC)
            for metric_id in (
                "requirement_satisfaction",
                "audience_journey",
                "fact_claim_support",
                "source_claim_alignment",
                "key_point_coverage",
                "numeric_claim_alignment",
                "compression_richness_observation",
                "source_traceability",
                "asset_presence",
                "chart_series_accuracy",
            )
        )
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=tuple(metrics),
            deterministic=True,
            description="Scoped v8 observations for page, object, pair, claim, requirement, and asset units.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> ObservationBatch:
        observations: list[AtomicObservation] = []
        for child in self.children:
            if child.supports(context):
                observations.extend(child.evaluate(context).observations)

        scene = _scene(context)
        if scene == SceneType.TEXT_TO_PPT:
            observations.extend(
                observe_requirements(context, adapter=self.adapter).observations
            )
            observations.extend(self._legacy_text_observations(context))
        elif scene == SceneType.PROJECT_SUMMARY:
            observations.extend(self._project_observations(context))
        elif scene == SceneType.MULTIMODAL:
            observations.extend(observe_assets(context, adapter=self.adapter).observations)
            observations.extend(
                observe_chart_series(context, adapter=self.adapter).observations
            )

        normalized = tuple(observations)
        return ObservationBatch(
            oracle_id=self.oracle_id,
            observations=normalized,
            version=self.version,
            metadata={
                "scene": scene.value,
                "metrics": sorted({item.metric_id for item in normalized}),
            },
        )

    def _legacy_text_observations(
        self, context: EvaluationContext
    ) -> tuple[AtomicObservation, ...]:
        presentation = load_presentation(context, self.adapter)
        source_hash = presentation.source_sha256
        results = (
            ("audience_journey", AudienceFitOracle(self.adapter).evaluate(context)),
            ("fact_claim_support", FactQualityOracle(self.adapter).evaluate(context)),
        )
        observations = []
        for metric_id, result in results:
            score = result.normalized_score if result.metric_status == MetricStatus.SCORED else None
            observations.append(
                _observation(
                    source_hash=source_hash,
                    metric_id=metric_id,
                    scope=EvaluationScope.DECK,
                    unit_key="deck",
                    score=score,
                    raw_value=result.raw_value,
                    confidence=result.confidence,
                    severity=result.severity,
                    evidence_items=result.evidence,
                    metadata={"source_oracle_id": result.oracle_id},
                )
            )
        return tuple(observations)


    def _project_observations(
        self, context: EvaluationContext
    ) -> tuple[AtomicObservation, ...]:
        presentation = load_presentation(context, self.adapter)
        source_hash = presentation.source_sha256
        sources = tuple(context.case.source_materials)
        source_text = read_materials(sources)
        observations: list[AtomicObservation] = []
        source_tokens = text_tokens(source_text)

        for slide in presentation.slides:
            for item in slide.visible_objects:
                sentences = [
                    value.strip()
                    for value in re.split(r"[\n。！？!?]+", item.visible_text)
                    if 8 <= len(value.strip()) <= 240
                ]
                for index, claim in enumerate(sentences, start=1):
                    claim_tokens = text_tokens(claim)
                    score = (
                        len(claim_tokens & source_tokens) / len(claim_tokens)
                        if claim_tokens and source_tokens
                        else None
                    )
                    numeric = bool(_NUMBER.search(claim))
                    observations.append(
                        _observation(
                            source_hash=source_hash,
                            metric_id="source_claim_alignment",
                            scope=EvaluationScope.CLAIM,
                            unit_key=f"page:{slide.page_number}/object:{item.object_id}/claim:{index}",
                            score=score,
                            raw_value=claim,
                            confidence=0.72 if score is not None else 1.0,
                            severity=(
                                Severity.CRITICAL
                                if numeric and score is not None and score < 0.50
                                else Severity.MAJOR
                                if score is not None and score < 0.35
                                else Severity.INFO
                            ),
                            importance=1.5 if numeric else 1.0,
                            key_unit=numeric,
                            critical=numeric and score is not None and score < 0.50,
                            evidence_items=(
                                evidence(
                                    "source_claim_alignment",
                                    f"claim-{slide.page_number}-{item.object_id}-{index}",
                                    "claim_source_alignment",
                                    "Claim token support was measured against supplied source material.",
                                    page_number=slide.page_number,
                                    object_id=item.object_id,
                                    bbox=item.bbox.as_tuple(),
                                    payload={"claim": claim, "source_available": bool(source_text)},
                                ),
                            ),
                        )
                    )
                for index, value in enumerate(_NUMBER.findall(item.visible_text), start=1):
                    supported = normalize_text(value) in normalize_text(source_text)
                    observations.append(
                        _observation(
                            source_hash=source_hash,
                            metric_id="numeric_claim_alignment",
                            scope=EvaluationScope.CLAIM,
                            unit_key=f"page:{slide.page_number}/object:{item.object_id}/number:{index}",
                            score=float(supported) if source_text else None,
                            raw_value=value,
                            confidence=0.92 if source_text else 1.0,
                            severity=Severity.CRITICAL if source_text and not supported else Severity.INFO,
                            importance=1.5,
                            key_unit=True,
                            critical=bool(source_text and not supported),
                            evidence_items=(
                                evidence(
                                    "numeric_claim_alignment",
                                    f"number-{slide.page_number}-{item.object_id}-{index}",
                                    "numeric_claim_support",
                                    "Numeric occurrence was checked against supplied source material.",
                                    page_number=slide.page_number,
                                    object_id=item.object_id,
                                    bbox=item.bbox.as_tuple(),
                                    payload={"value": value, "supported": supported},
                                ),
                            ),
                        )
                    )

        configured_points = context.case.metadata.get("key_points")
        points = (
            [str(item) for item in configured_points]
            if isinstance(configured_points, (list, tuple))
            else [
                value.strip()
                for value in re.split(r"[\n。！？!?]+", source_text)
                if 8 <= len(value.strip()) <= 180
            ][:20]
        )
        deck_text = presentation.all_visible_text
        for index, point in enumerate(points, start=1):
            point_tokens = text_tokens(point)
            deck_tokens = text_tokens(deck_text)
            score = (
                len(point_tokens & deck_tokens) / len(point_tokens)
                if point_tokens
                else None
            )
            observations.append(
                _observation(
                    source_hash=source_hash,
                    metric_id="key_point_coverage",
                    scope=EvaluationScope.REQUIREMENT,
                    unit_key=f"key-point:{index}",
                    score=score,
                    raw_value=point,
                    confidence=0.75,
                    importance=1.0,
                )
            )

        ratio = len(normalize_text(deck_text)) / max(1, len(normalize_text(source_text)))
        observations.append(
            _observation(
                source_hash=source_hash,
                metric_id="compression_richness_observation",
                scope=EvaluationScope.DECK,
                unit_key="deck",
                score=compression_quality_score(ratio) if source_text else None,
                raw_value=ratio,
                confidence=0.70,
            )
        )
        cited_deck = (deck_text + "\n" + "\n".join(slide.notes_text for slide in presentation.slides)).lower()
        for index, source in enumerate(sources, start=1):
            label = Path(source).name.lower()
            hit = bool(label and label in cited_deck)
            observations.append(
                _observation(
                    source_hash=source_hash,
                    metric_id="source_traceability",
                    scope=EvaluationScope.ASSET,
                    unit_key=f"source:{index}",
                    score=float(hit),
                    raw_value=hit,
                    confidence=0.88,
                    evidence_items=(
                        evidence(
                            "source_traceability",
                            f"source-{index}",
                            "source_reference" if hit else "missing_source_reference",
                            "Source reference presence was checked in deck text and notes.",
                            source_uri=source,
                        ),
                    ),
                )
            )
        return tuple(observations)


class V8TieredVisualCriterionOracle:
    """Flash-first, criterion-isomorphic advanced fallback for one visual dimension."""

    version = V8_QUALITY_VERSION

    def __init__(
        self,
        criterion_id: str,
        flash_provider: ModelAuditProvider | None,
        advanced_provider: ModelAuditProvider | None,
        adapter: PptxAdapter | None = None,
    ) -> None:
        if criterion_id not in V8_VISUAL_CRITERION_IDS:
            raise ValueError(f"unknown v8 visual criterion {criterion_id!r}")
        self.criterion_id = criterion_id
        self.oracle_id = f"v8.visual.{criterion_id}"
        self.metric_id = f"structured_vlm_{criterion_id}"
        self._flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            adapter,
        )
        self._advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                adapter,
            )
            if advanced_provider is not None
            else None
        )

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.BASE_ADDITIVE),),
            deterministic=False,
            description=f"Atomic Flash/Advanced audit for {self.criterion_id}.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return self._flash.supports(context)

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        flash = self._flash.evaluate(context)
        attempted: list[tuple[str, GroundedSingleCriterionVlmOracle, OracleResult]] = [
            ("FLASH", self._flash, flash)
        ]
        reason: str | None = None
        if flash.metadata.get("reason_code") == (
            "CRITERION_CONFIDENCE_BELOW_PROFILE_FLOOR"
        ):
            reason = "FLASH_LOW_CONFIDENCE"
        elif flash.metric_status != MetricStatus.SCORED:
            reason = "FLASH_UNRESOLVED"
        elif flash.confidence < 0.60:
            reason = "FLASH_LOW_CONFIDENCE"
        elif self._rule_disagreement(context, flash):
            reason = "RULE_MODEL_DISAGREEMENT"
        chosen = flash
        tier = "FLASH"
        selected_attempt_tier: str | None = "FLASH" if reason is None else None
        advanced_rule_disagreement = False
        if reason is not None and self._advanced is not None:
            advanced = self._advanced.evaluate(context)
            attempted.append(("ADVANCED", self._advanced, advanced))
            if (
                advanced.metric_status == MetricStatus.SCORED
                and advanced.confidence >= 0.60
            ):
                selected_attempt_tier = "ADVANCED"
                advanced_rule_disagreement = (
                    self.criterion_id == "authorship_specificity"
                    and self._rule_disagreement(
                        context,
                        advanced,
                    )
                )
                if advanced_rule_disagreement:
                    chosen = replace(
                        advanced,
                        metric_status=MetricStatus.NA,
                        raw_value=None,
                        normalized_score=None,
                        confidence=min(flash.confidence, advanced.confidence),
                        severity=Severity.INFO,
                    )
                    tier = "ADVANCED_RULE_DISAGREEMENT_REVIEW"
                else:
                    chosen = advanced
                    tier = "ADVANCED"
            else:
                chosen = _unresolved_model_result(flash)
                tier = "FLASH_UNRESOLVED_ADVANCED_FAILED"
        elif reason is not None:
            chosen = _unresolved_model_result(flash)
            tier = "FLASH_UNRESOLVED_ADVANCED_UNCONFIGURED"
        routing_attempts = [
            _model_routing_attempt(
                attempt_tier,
                oracle,
                result,
                selected=(
                    selected_attempt_tier is not None
                    and attempt_tier == selected_attempt_tier
                ),
            )
            for attempt_tier, oracle, result in attempted
        ]
        return replace(
            chosen,
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            version=self.version,
            duration_ms=sum(item.duration_ms for _, _, item in attempted),
            cost=sum(item.cost for _, _, item in attempted),
            metadata={
                **dict(chosen.metadata),
                "routing_mode": "ATOMIC_FLASH_ADVANCED_HUMAN",
                "selected_tier": tier,
                "escalation_reason": reason,
                "advanced_rule_disagreement": advanced_rule_disagreement,
                "criterion_id": self.criterion_id,
                "routing_attempts": routing_attempts,
                "routing_usage": _model_routing_usage(routing_attempts),
            },
        )

    def _rule_disagreement(
        self, context: EvaluationContext, result: OracleResult
    ) -> bool:
        if result.normalized_score is None:
            return False
        rule_metrics = set(_CRITERION_RULE_METRICS[self.criterion_id])
        observations = context.memo.get("ppt_eval.atomic_observations", ())
        rule_scores = [
            float(item.local_score)
            for item in observations
            if isinstance(item, AtomicObservation)
            and item.metric_id in rule_metrics
            and item.local_score is not None
        ]
        if not rule_scores:
            return False
        model_high_rule_low = result.normalized_score > 0.75 and any(
            isinstance(item, AtomicObservation)
            and item.metric_id in rule_metrics
            and item.local_score is not None
            and item.local_score < 0.50
            for item in observations
        )
        rule_mean = sum(rule_scores) / len(rule_scores)
        model_low_rule_high = (
            self.criterion_id == "authorship_specificity"
            and result.normalized_score < 0.50
            and rule_mean > 0.75
        )
        return model_high_rule_low or model_low_rule_high


class V8RasterTextObservationOracle:
    """Recover page-scoped text semantics only for fully flattened decks.

    The model result is diagnostic provenance.  Score-affecting data enters the
    reducer exclusively as page-level ``AtomicObservation`` objects so the
    complete evidence, routing, page sample, and missingness remain auditable.
    Editable decks do not spend a model call and retain their deterministic
    content/language owners.
    """

    version = V8_QUALITY_VERSION

    def __init__(
        self,
        criterion_id: str,
        flash_provider: ModelAuditProvider | None,
        advanced_provider: ModelAuditProvider | None,
        adapter: PptxAdapter | None = None,
    ) -> None:
        if criterion_id not in V8_RASTER_TEXT_CRITERION_IDS:
            raise ValueError(f"unknown v8 raster text criterion {criterion_id!r}")
        self.criterion_id = criterion_id
        self.oracle_id = f"v8.raster_text.{criterion_id}"
        self.metric_id = f"structured_vlm_{criterion_id}"
        self.observation_metric_id = V8_RASTER_TEXT_OBSERVATION_METRICS[
            criterion_id
        ]
        self.adapter = adapter or PptxAdapter()
        self._flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            self.adapter,
        )
        self._advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                self.adapter,
            )
            if advanced_provider is not None
            else None
        )

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.DIAGNOSTIC),),
            deterministic=False,
            description=(
                "Raster-only VLM/OCR recovery emitted as page-scoped atomic observations."
            ),
        )

    def supports(self, context: EvaluationContext) -> bool:
        return self._flash.supports(context)

    def evaluate(self, context: EvaluationContext) -> OracleExecutionOutput:
        if not self._is_fully_rasterized(context):
            unavailable = self._flash.not_applicable(
                "Raster text recovery is unnecessary for a deck with editable semantic content.",
                code="RASTER_TEXT_RECOVERY_NOT_REQUIRED",
            )
            result = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                score_role=ScoreRole.DIAGNOSTIC,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "raster_only": False,
                    "score_affecting": False,
                },
            )
            return OracleExecutionOutput(results=(result,))

        flash = self._flash.evaluate(context)
        attempted: list[
            tuple[str, GroundedSingleCriterionVlmOracle, OracleResult]
        ] = [("FLASH", self._flash, flash)]
        reason: str | None = None
        if flash.metadata.get("reason_code") == (
            "CRITERION_CONFIDENCE_BELOW_PROFILE_FLOOR"
        ):
            reason = "FLASH_LOW_CONFIDENCE"
        elif flash.metric_status != MetricStatus.SCORED:
            reason = "FLASH_UNRESOLVED"
        elif flash.confidence < 0.60:
            reason = "FLASH_LOW_CONFIDENCE"

        chosen = flash
        tier = "FLASH"
        selected_attempt_tier: str | None = "FLASH" if reason is None else None
        if reason is not None and self._advanced is not None:
            advanced = self._advanced.evaluate(context)
            attempted.append(("ADVANCED", self._advanced, advanced))
            if (
                advanced.metric_status == MetricStatus.SCORED
                and advanced.confidence >= 0.60
            ):
                chosen = advanced
                tier = "ADVANCED"
                selected_attempt_tier = "ADVANCED"
            else:
                chosen = _unresolved_model_result(flash)
                tier = "FLASH_UNRESOLVED_ADVANCED_FAILED"
        elif reason is not None:
            chosen = _unresolved_model_result(flash)
            tier = "FLASH_UNRESOLVED_ADVANCED_UNCONFIGURED"

        routing_attempts = [
            _model_routing_attempt(
                attempt_tier,
                oracle,
                result,
                selected=(
                    selected_attempt_tier is not None
                    and attempt_tier == selected_attempt_tier
                ),
            )
            for attempt_tier, oracle, result in attempted
        ]
        routed = replace(
            chosen,
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            score_role=ScoreRole.DIAGNOSTIC,
            version=self.version,
            duration_ms=sum(item.duration_ms for _, _, item in attempted),
            cost=sum(item.cost for _, _, item in attempted),
            metadata={
                **dict(chosen.metadata),
                "routing_mode": "RASTER_ATOMIC_FLASH_ADVANCED_HUMAN",
                "selected_tier": tier,
                "escalation_reason": reason,
                "criterion_id": self.criterion_id,
                "routing_attempts": routing_attempts,
                "routing_usage": _model_routing_usage(routing_attempts),
                "raster_only": True,
                "score_affecting": False,
                "observation_metric_id": self.observation_metric_id,
            },
        )
        observations = self._to_observations(context, routed)
        return OracleExecutionOutput(results=(routed,), observations=observations)

    def _is_fully_rasterized(self, context: EvaluationContext) -> bool:
        presentation = load_presentation(context, self.adapter)
        raster_pages = {
            int(item.unit_key.split(":", 1)[1])
            for item in context.memo.get("ppt_eval.atomic_observations", ())
            if isinstance(item, AtomicObservation)
            and item.metric_id == "slide_editability"
            and item.metadata.get("raster_only") is True
            and item.unit_key.startswith("page:")
        }
        return raster_pages == set(range(1, presentation.slide_count + 1))

    def _to_observations(
        self,
        context: EvaluationContext,
        result: OracleResult,
    ) -> tuple[AtomicObservation, ...]:
        if (
            result.metric_status != MetricStatus.SCORED
            or result.normalized_score is None
        ):
            return ()
        raw_page_scores = result.metadata.get("page_scores")
        if not isinstance(raw_page_scores, Mapping):
            return ()
        presentation = load_presentation(context, self.adapter)
        evidence_by_page = {
            int(item.page_number): item
            for item in result.evidence
            if item.page_number is not None
        }
        roles = {
            int(item.unit_key.split(":", 1)[1]): item
            for item in context.memo.get("ppt_eval.atomic_observations", ())
            if isinstance(item, AtomicObservation)
            and item.metric_id == "slide_role"
            and item.unit_key.startswith("page:")
        }
        valid_scores: list[tuple[int, float]] = []
        for raw_page, raw_score in raw_page_scores.items():
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                continue
            try:
                page_number = int(raw_page)
            except (TypeError, ValueError):
                continue
            if page_number not in evidence_by_page or not 0.0 <= float(raw_score) <= 1.0:
                continue
            valid_scores.append((page_number, float(raw_score)))
        if not valid_scores:
            return ()
        score_by_page = dict(valid_scores)
        allocated_cost = result.cost / len(valid_scores)
        sampling_coverage = len(valid_scores) / presentation.slide_count
        observations: list[AtomicObservation] = []
        for page_number in range(1, presentation.slide_count + 1):
            sampled = page_number in score_by_page
            page_score = score_by_page.get(page_number)
            finding = evidence_by_page.get(page_number)
            payload = finding.payload if finding is not None else {}
            raw_confidence = payload.get("criterion_confidence", result.confidence)
            confidence = (
                float(raw_confidence)
                if sampled
                and not isinstance(raw_confidence, bool)
                and isinstance(raw_confidence, (int, float))
                and 0.0 <= float(raw_confidence) <= 1.0
                else 0.0
            )
            raw_severity = str(payload.get("severity") or "NONE")
            severity = (
                {
                    "CRITICAL": Severity.CRITICAL,
                    "MAJOR": Severity.MAJOR,
                    "MINOR": Severity.MINOR,
                }.get(raw_severity, Severity.INFO)
                if sampled
                else Severity.INFO
            )
            role = roles.get(page_number)
            unit_key = f"page:{page_number}"
            digest = hashlib.sha256(
                (
                    f"{self.oracle_id}|{self.observation_metric_id}|{unit_key}|"
                    f"{presentation.source_sha256}|{result.metadata.get('response_fingerprint')}"
                ).encode()
            ).hexdigest()[:20]
            observations.append(
                AtomicObservation(
                    observation_id=f"obs-{digest}",
                    oracle_id=self.oracle_id,
                    metric_id=self.observation_metric_id,
                    scope=EvaluationScope.PAGE,
                    unit_key=unit_key,
                    execution_status=ExecutionStatus.SUCCESS,
                    metric_status=(
                        MetricStatus.SCORED if sampled else MetricStatus.NA
                    ),
                    raw_value=page_score if sampled else "NOT_SAMPLED",
                    local_score=page_score if sampled else None,
                    confidence=confidence,
                    severity=severity,
                    importance=role.importance if role is not None else 1.0,
                    key_unit=role.key_unit if role is not None else False,
                    critical=False,
                    evidence=(
                        (finding,)
                        if finding is not None
                        else (
                            evidence(
                                self.observation_metric_id,
                                f"not-sampled-{page_number}",
                                "raster_page_not_sampled",
                                "Page was outside the bounded raster-text model sample.",
                                page_number=page_number,
                                payload={
                                    "sampled_pages": sorted(score_by_page),
                                    "total_pages": presentation.slide_count,
                                },
                            ),
                        )
                    ),
                    version=self.version,
                    cost=0.0,
                    metadata={
                        "criterion_id": self.criterion_id,
                        "source_model_metric_id": self.metric_id,
                        "selected_tier": result.metadata.get("selected_tier"),
                        "sampled_pages": list(result.metadata.get("sampled_pages", ())),
                        "model_input_mode": "RASTER_RENDERED_TEXT_RECOVERY",
                        "primary_owner": self.observation_metric_id,
                        "sampled": sampled,
                        "sample_count": len(valid_scores),
                        "total_pages": presentation.slide_count,
                        "deck_page_coverage": sampling_coverage,
                        "allocated_model_cost": allocated_cost if sampled else 0.0,
                        "cost_accounted_by_result": True,
                    },
                )
            )
        return tuple(observations)


def _unresolved_model_result(result: OracleResult) -> OracleResult:
    """Project a failed/uncertain attempt to legal N/A without erasing telemetry."""

    return replace(
        result,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.NA,
        raw_value=None,
        normalized_score=None,
        multiplier=None,
        confidence=max(0.0, min(1.0, result.confidence)),
        severity=Severity.INFO,
        error_code=None,
        error_message=None,
    )


def _model_routing_attempt(
    tier: str,
    oracle: GroundedSingleCriterionVlmOracle,
    result: OracleResult,
    *,
    selected: bool,
) -> Mapping[str, Any]:
    metadata = dict(result.metadata)
    actual_model = metadata.get("model")
    usage = metadata.get("usage")
    provider = oracle.provider
    adapter_usage_complete = all(
        item.payload.get("adapter_usage_complete") is not False
        for item in result.evidence
    )
    provider_usage_complete = metadata.get("provider_usage_complete") is not False
    criterion_usage_complete = (
        metadata.get("criterion_retry_usage_complete") is not False
    )
    usage_complete = (
        isinstance(usage, Mapping)
        and adapter_usage_complete
        and provider_usage_complete
        and criterion_usage_complete
    )
    cost_markers = tuple(
        item.payload.get("adapter_cost_known")
        for item in result.evidence
        if "adapter_cost_known" in item.payload
    )
    cost_known = (
        all(marker is True for marker in cost_markers)
        if cost_markers
        else result.cost > 0.0
    )
    return {
        "tier": tier,
        "selected": selected,
        "configured_provider": type(provider).__name__ if provider is not None else None,
        "configured_model": (
            str(getattr(provider, "model", "")).strip() or None
            if provider is not None
            else None
        ),
        "execution_status": result.execution_status.value,
        "metric_status": result.metric_status.value,
        "score": result.normalized_score,
        "confidence": result.confidence,
        "cost": result.cost,
        "cost_known": cost_known,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "model": dict(actual_model) if isinstance(actual_model, Mapping) else None,
        "usage": dict(usage) if isinstance(usage, Mapping) else None,
        "usage_complete": usage_complete,
        "adapter_usage_complete": adapter_usage_complete,
        "provider_usage_complete": provider_usage_complete,
        "criterion_retry_usage_complete": criterion_usage_complete,
        "request_fingerprint": metadata.get("request_fingerprint"),
        "response_fingerprint": metadata.get("response_fingerprint"),
        "evidence": [_routing_evidence(item) for item in result.evidence],
    }


def _routing_evidence(item: Any) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "message": item.message,
        "payload": dict(item.payload),
    }
    for field in ("page_number", "object_id", "bbox", "source_uri"):
        value = getattr(item, field)
        if value is not None:
            payload[field] = list(value) if field == "bbox" else value
    return payload


def _model_routing_usage(
    attempts: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    items = tuple(attempts)
    usages = tuple(item.get("usage") for item in items)
    complete = all(item.get("usage_complete") is True for item in items)
    return {
        "input_tokens": sum(
            int(usage.get("input_tokens", 0))
            for usage in usages
            if isinstance(usage, Mapping)
        ),
        "output_tokens": sum(
            int(usage.get("output_tokens", 0))
            for usage in usages
            if isinstance(usage, Mapping)
        ),
        "total_tokens": sum(
            int(usage.get("input_tokens", 0))
            + int(usage.get("output_tokens", 0))
            for usage in usages
            if isinstance(usage, Mapping)
        ),
        "reported_cost": sum(float(item.get("cost", 0.0)) for item in items),
        "cost_known": all(item.get("cost_known") is True for item in items),
        "attempt_count": len(items),
        "usage_complete": complete,
    }


class V8QualityReducerOracle:
    """Reduce observations by quality attribute; deterministic rules cap model scores."""

    oracle_id = V8_REDUCER_ORACLE_ID
    version = V8_QUALITY_VERSION

    def describe(self) -> OracleDescriptor:
        metrics = [
            MetricDefinition(metric_id, ScoreRole.BASE_ADDITIVE)
            for metric_id in V8_BASE_ADDITIVE_METRICS
        ]
        metrics.append(
            MetricDefinition("v8_functional_integrity", ScoreRole.BASE_MULTIPLIER)
        )
        metrics.append(
            MetricDefinition("authorship_specificity", ScoreRole.DIAGNOSTIC)
        )
        for metric_ids in V8_SCENE_METRICS.values():
            metrics.extend(
                MetricDefinition(metric_id, ScoreRole.SCENE_ADDITIVE)
                for metric_id in metric_ids
            )
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=tuple(dict.fromkeys(metrics)),
            deterministic=True,
            description="Versioned reduction and sensor fusion for v8 quality attributes.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> tuple[OracleResult, ...]:
        observations = tuple(context.memo.get("ppt_eval.atomic_observations", ()))
        prior_results = tuple(context.memo.get("ppt_eval.oracle_results", ()))
        if any(not isinstance(item, AtomicObservation) for item in observations):
            raise TypeError("v8 reducer received an invalid observation store")
        if any(not isinstance(item, OracleResult) for item in prior_results):
            raise TypeError("v8 reducer received an invalid result store")
        batch = ObservationBatch(
            oracle_id=self.oracle_id,
            observations=observations,
            version=self.version,
        )
        model_scores = {
            item.metric_id: item.normalized_score
            for item in prior_results
            if item.metric_status == MetricStatus.SCORED
            and item.normalized_score is not None
        }
        model_confidences = {
            item.metric_id: item.confidence
            for item in prior_results
            if item.metric_status == MetricStatus.SCORED
            and item.normalized_score is not None
        }
        results: list[OracleResult] = [self._integrity_gate(observations, prior_results)]

        content = self._reduce(
            batch,
            ("slide_content_presence", "slide_reading_load", "document_structure", "title_body_alignment"),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "content_structure",
            ScoreRole.BASE_ADDITIVE,
        )
        language = self._reduce(
            batch,
            ("language_consistency",),
            EvaluationScope.DECK,
            IMPORTANCE_COVERAGE,
            "language_consistency",
            ScoreRole.BASE_ADDITIVE,
        )
        content = self._prefer_raster_recovery(
            content,
            self._reduce(
                batch,
                ("raster_content_structure_vlm",),
                EvaluationScope.PAGE,
                PAGE_QUALITY,
                "raster_content_structure_recovery",
                ScoreRole.DIAGNOSTIC,
                required=False,
                minimum_observability=self._bounded_sample_minimum(
                    batch,
                    "raster_content_structure_vlm",
                    maximum_sample_pages=4,
                ),
            ),
            output_metric_id="content_structure",
        )
        language = self._prefer_raster_recovery(
            language,
            self._reduce(
                batch,
                ("raster_language_consistency_vlm",),
                EvaluationScope.PAGE,
                IMPORTANCE_COVERAGE,
                "raster_language_consistency_recovery",
                ScoreRole.DIAGNOSTIC,
                required=False,
                minimum_observability=self._bounded_sample_minimum(
                    batch,
                    "raster_language_consistency_vlm",
                    maximum_sample_pages=8,
                ),
            ),
            output_metric_id="language_consistency",
        )
        composition_rule = self._reduce(
            batch,
            ("slide_geometry_integrity",),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "composition_rule",
            ScoreRole.DIAGNOSTIC,
        )
        typography_rule = self._reduce(
            batch,
            ("slide_typography_functional",),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "typography_rule",
            ScoreRole.DIAGNOSTIC,
        )
        contrast_rule = self._reduce(
            batch,
            ("slide_pixel_contrast",),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "contrast_rule",
            ScoreRole.DIAGNOSTIC,
            required=False,
        )
        media_rule = self._reduce(
            batch,
            ("media_integrity", "crop_geometry_risk", "effective_image_resolution"),
            EvaluationScope.OBJECT,
            IMPORTANCE_COVERAGE,
            "visual_communication_rule",
            ScoreRole.DIAGNOSTIC,
            required=False,
        )
        sequence_rule = self._reduce(
            batch,
            ("transition_coherence_proxy", "duplicate_slide"),
            EvaluationScope.SLIDE_PAIR,
            PAIR_QUALITY,
            "visual_system_rule",
            ScoreRole.DIAGNOSTIC,
            required=False,
        )
        authorship_rule = self._reduce(
            batch,
            ("authorship_specificity_signals",),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "authorship_specificity_rule",
            ScoreRole.DIAGNOSTIC,
        )
        authorship = self._fuse_authorship(
            authorship_rule,
            model_scores.get("structured_vlm_authorship_specificity"),
            model_confidences.get("structured_vlm_authorship_specificity"),
        )
        authorship_alias = replace(
            authorship,
            metric_id="authorship_specificity",
            score_role=ScoreRole.DIAGNOSTIC,
            metadata={
                **dict(authorship.metadata),
                "compatibility_alias_for": "authorship_specificity_v2",
                "score_affecting": False,
            },
        )
        results.extend(
            (
                content,
                language,
                self._fuse(
                    "composition_craft",
                    composition_rule,
                    model_scores.get("structured_vlm_composition_layout"),
                ),
                self._fuse(
                    "typography_craft",
                    typography_rule,
                    model_scores.get("structured_vlm_typography_legibility"),
                ),
                self._fuse(
                    "palette_craft",
                    contrast_rule,
                    model_scores.get("structured_vlm_color_contrast"),
                ),
                self._fuse(
                    "visual_communication",
                    media_rule,
                    model_scores.get("structured_vlm_imagery_data_visualization"),
                ),
                self._fuse(
                    "visual_system_sequence",
                    sequence_rule,
                    model_scores.get("structured_vlm_cross_slide_consistency"),
                ),
                authorship,
                authorship_alias,
            )
        )
        results.extend(self._scene_results(context, batch))
        return tuple(results)

    def _reduce(
        self,
        batch: ObservationBatch,
        metric_ids: tuple[str, ...],
        scope: EvaluationScope,
        kind: str,
        output_metric: str,
        role: ScoreRole,
        *,
        required: bool = True,
        minimum_observability: float = 0.60,
    ) -> OracleResult:
        spec = ReducerSpec(
            reducer_id=f"v8.{output_metric}.reducer",
            version=self.version,
            input_metric_ids=metric_ids,
            expected_scope=scope,
            reducer_kind=kind,
            output_oracle_id=self.oracle_id,
            output_metric_id=output_metric,
            output_score_role=role,
            critical_cap=0.34,
            minimum_observability=minimum_observability,
            required=required,
        )
        return ReducerEngine().reduce(batch, spec)

    @staticmethod
    def _bounded_sample_minimum(
        batch: ObservationBatch,
        metric_id: str,
        *,
        maximum_sample_pages: int,
    ) -> float:
        expected_units = tuple(
            item for item in batch.observations if item.metric_id == metric_id
        )
        if not expected_units:
            return 0.60
        scored_units = tuple(
            item
            for item in expected_units
            if item.metric_status == MetricStatus.SCORED
            and item.local_score is not None
        )
        complete_sample_size = min(maximum_sample_pages, len(expected_units))
        if len(scored_units) < complete_sample_size:
            return 0.60
        total_importance = sum(item.importance for item in expected_units)
        sampled_importance = sum(item.importance for item in scored_units)
        if total_importance <= 0.0:
            return 0.60
        return min(0.60, sampled_importance / total_importance)

    def _fuse(
        self,
        metric_id: str,
        rule_result: OracleResult,
        model_score: float | None,
    ) -> OracleResult:
        if model_score is None:
            return self._na(metric_id, "MODEL_AESTHETIC_EVIDENCE_MISSING")
        rule_score = rule_result.normalized_score
        cap = 1.0 if rule_score is None else 0.34 + 0.66 * rule_score
        score = min(model_score, cap)
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.SCORED,
            score_role=ScoreRole.BASE_ADDITIVE,
            raw_value=score,
            normalized_score=score,
            confidence=min(0.90, rule_result.confidence),
            severity=Severity.INFO if score >= 0.70 else Severity.MINOR,
            version=self.version,
            metadata={
                "fusion_mode": "MODEL_POSITIVE_SIGNAL_WITH_DETERMINISTIC_CAP",
                "model_score": model_score,
                "rule_score": rule_score,
                "deterministic_cap": cap,
                "source_reducer": rule_result.metric_id,
            },
        )

    def _prefer_raster_recovery(
        self,
        deterministic: OracleResult,
        recovery: OracleResult,
        *,
        output_metric_id: str,
    ) -> OracleResult:
        """Use model pixels only when the deterministic owner abstained.

        The two sensors never average or penalize the same construct twice.
        A successful recovery becomes the sole score owner; otherwise the
        original deterministic N/A and its missingness lineage are preserved.
        """

        if deterministic.metric_status != MetricStatus.NA:
            return deterministic
        if (
            recovery.metric_status != MetricStatus.SCORED
            or recovery.normalized_score is None
        ):
            return replace(
                deterministic,
                metadata={
                    **dict(deterministic.metadata),
                    "raster_recovery_attempted": True,
                    "raster_recovery_status": recovery.metric_status.value,
                    "raster_recovery_metric_id": recovery.metric_id,
                },
            )
        score = recovery.normalized_score
        lineage = recovery.metadata.get("lineage")
        lineage = lineage if isinstance(lineage, Mapping) else {}
        observation_ids = tuple(lineage.get("observation_ids", ()))
        applicable_ids = tuple(lineage.get("applicable_observation_ids", ()))
        deck_page_coverage = (
            len(applicable_ids) / len(observation_ids)
            if observation_ids
            else None
        )
        return replace(
            recovery,
            metric_id=output_metric_id,
            score_role=ScoreRole.BASE_ADDITIVE,
            severity=Severity.INFO if score >= 0.70 else Severity.MINOR,
            metadata={
                **dict(recovery.metadata),
                "fusion_mode": "RASTER_VLM_ATOMIC_FALLBACK",
                "primary_owner": output_metric_id,
                "deterministic_metric_status": deterministic.metric_status.value,
                "deterministic_observability": deterministic.metadata.get(
                    "observability"
                ),
                "observability_basis": (
                    "DECK_PAGE_COVERAGE_WITH_BOUNDED_SAMPLE_MINIMUM"
                ),
                "deck_page_coverage": deck_page_coverage,
                "no_double_score": True,
            },
        )

    def _model_only(self, metric_id: str, score: float | None) -> OracleResult:
        if score is None:
            return self._na(metric_id, "MODEL_AESTHETIC_EVIDENCE_MISSING")
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.SCORED,
            score_role=ScoreRole.BASE_ADDITIVE,
            raw_value=score,
            normalized_score=score,
            confidence=0.85,
            severity=Severity.INFO if score >= 0.70 else Severity.MINOR,
            version=self.version,
            metadata={"fusion_mode": "MODEL_POSITIVE_SIGNAL"},
        )

    def _fuse_authorship(
        self,
        rule_result: OracleResult,
        model_score: float | None,
        model_confidence: float | None,
    ) -> OracleResult:
        rule_score = rule_result.normalized_score
        if model_score is None:
            return self._na(
                "authorship_specificity_v2",
                "MODEL_AUTHORSHIP_EVIDENCE_MISSING",
            )
        if rule_score is None:
            score = model_score
            mode = "MODEL_ONLY_AUTHORSHIP"
            confidence = model_confidence if model_confidence is not None else 0.0
        else:
            score = 0.30 * rule_score + 0.70 * model_score
            mode = "SINGLE_CONSTRUCT_RULE_MODEL_FUSION"
            confidence = 0.30 * rule_result.confidence + 0.70 * (
                model_confidence if model_confidence is not None else 0.0
            )
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id="authorship_specificity_v2",
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.SCORED,
            score_role=ScoreRole.BASE_ADDITIVE,
            raw_value=score,
            normalized_score=score,
            confidence=min(0.85, confidence),
            severity=Severity.INFO if score >= 0.70 else Severity.MINOR,
            version=self.version,
            metadata={
                "fusion_mode": mode,
                "rule_score": rule_score,
                "model_score": model_score,
                "model_confidence": model_confidence,
                "rule_confidence": rule_result.confidence,
                "rule_weight": 0.30 if model_score is not None else 1.0,
                "model_weight": 0.70 if model_score is not None else 0.0,
                "primary_owner": "authorship_specificity_v2",
                "score_affecting": True,
                "excluded_from_functional_hard_gate": True,
            },
        )

    def _integrity_gate(
        self,
        observations: tuple[AtomicObservation, ...],
        results: tuple[OracleResult, ...],
    ) -> OracleResult:
        file_result = next(
            (item for item in results if item.metric_id == "file_deliverability"),
            None,
        )
        scored = [
            item
            for item in observations
            if item.metric_id in _FUNCTIONAL_GATE_METRIC_IDS
            and item.metric_status == MetricStatus.SCORED
        ]
        by_metric = {
            metric_id: tuple(item for item in scored if item.metric_id == metric_id)
            for metric_id in sorted({item.metric_id for item in scored})
        }
        major_prevalence_by_metric = {
            metric_id: sum(item.severity == Severity.MAJOR for item in items)
            / len(items)
            for metric_id, items in by_metric.items()
            if items
        }
        gate_candidates: list[tuple[str, str, tuple[AtomicObservation, ...]]] = []
        for metric_id, items in by_metric.items():
            critical_items = tuple(
                item
                for item in items
                if item.severity == Severity.CRITICAL
                and (item.critical or item.key_unit)
            )
            major_items = tuple(item for item in items if item.severity == Severity.MAJOR)
            key_major_count = sum(item.key_unit for item in major_items)
            if critical_items:
                gate_candidates.append((metric_id, "CRITICAL", critical_items))
            elif (
                major_prevalence_by_metric.get(metric_id, 0.0) >= 0.20
                or key_major_count >= 2
            ):
                gate_candidates.append((metric_id, "MAJOR", major_items))

        verdicts: list[Mapping[str, Any]] = []
        confirmed: list[tuple[str, str, tuple[AtomicObservation, ...]]] = []
        unresolved: list[tuple[str, str, tuple[AtomicObservation, ...]]] = []
        for metric_id, severity, items in gate_candidates:
            model_severity: str | None
            if metric_id in _DIRECT_FUNCTIONAL_GATE_METRIC_IDS:
                verdict = "CONFIRMED"
                model_metric_id = None
                model_severity = severity
            else:
                model_metric_id = _CONTESTABLE_GATE_MODEL_METRIC_IDS.get(metric_id)
                verdict, model_severity = self._gate_model_verdict(
                    model_metric_id,
                    items,
                    results,
                )
            verdicts.append(
                {
                    "metric_id": metric_id,
                    "rule_severity": severity,
                    "verdict": verdict,
                    "model_metric_id": model_metric_id,
                    "model_severity": model_severity,
                    "observation_ids": [item.observation_id for item in items],
                }
            )
            if verdict == "CONFIRMED":
                confirmed.append((metric_id, model_severity or severity, items))
            elif verdict == "UNRESOLVED":
                unresolved.append((metric_id, severity, items))

        multiplier = 1.0
        reason = "PASS"
        if file_result is not None and file_result.multiplier == 0.0:
            multiplier = 0.0
            reason = "FILE_DELIVERABILITY_FAILED"
        elif any(severity == "CRITICAL" for _, severity, _ in confirmed):
            multiplier = 0.0
            reason = "VLM_CONFIRMED_KEY_UNIT_CRITICAL"
        elif confirmed:
            multiplier = 0.5
            reason = "CONFIRMED_FUNCTIONAL_DEFECT_PREVALENCE"
        elif unresolved:
            unresolved_items = tuple(
                item for _, _, items in unresolved for item in items
            )
            return OracleResult(
                oracle_id=self.oracle_id,
                metric_id="v8_functional_integrity",
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.NA,
                score_role=ScoreRole.BASE_MULTIPLIER,
                raw_value="GATE_AUDIT_UNRESOLVED",
                confidence=0.0,
                severity=Severity.INFO,
                evidence=tuple(
                    evidence_item
                    for item in unresolved_items[:20]
                    for evidence_item in item.evidence[:1]
                ),
                version=self.version,
                metadata={
                    "reason_code": "GATE_AUDIT_UNRESOLVED",
                    "gate_verdicts": verdicts,
                    "major_prevalence_by_metric": major_prevalence_by_metric,
                    "gate_owner_policy": "SCOPED_OWNER_WITH_VLM_CONFIRMATION",
                },
            )
        confirmed_items = tuple(item for _, _, items in confirmed for item in items)
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id="v8_functional_integrity",
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.PASS if multiplier == 1.0 else MetricStatus.FAIL,
            score_role=ScoreRole.BASE_MULTIPLIER,
            raw_value=reason,
            multiplier=multiplier,
            confidence=1.0,
            severity=Severity.INFO if multiplier == 1.0 else Severity.CRITICAL,
            evidence=tuple(
                evidence_item
                for item in confirmed_items[:20]
                for evidence_item in item.evidence[:1]
            ),
            version=self.version,
            metadata={
                "reason_code": reason,
                "critical_observation_ids": [
                    item.observation_id
                    for _, severity, items in confirmed
                    if severity == "CRITICAL"
                    for item in items
                ],
                "major_prevalence": max(
                    major_prevalence_by_metric.values(),
                    default=0.0,
                ),
                "major_prevalence_by_metric": major_prevalence_by_metric,
                "gate_verdicts": verdicts,
                "gate_eligible_metric_ids": sorted(_FUNCTIONAL_GATE_METRIC_IDS),
                "gate_eligible_observation_count": len(scored),
                "gate_owner_policy": "SCOPED_OWNER_WITH_VLM_CONFIRMATION",
            },
        )

    @staticmethod
    def _gate_model_verdict(
        model_metric_id: str | None,
        candidates: tuple[AtomicObservation, ...],
        results: tuple[OracleResult, ...],
    ) -> tuple[str, str | None]:
        if not model_metric_id:
            return "UNRESOLVED", None
        model = next(
            (item for item in results if item.metric_id == model_metric_id),
            None,
        )
        if (
            model is None
            or model.metric_status != MetricStatus.SCORED
            or model.confidence < 0.60
        ):
            return "UNRESOLVED", None
        metadata = model.metadata
        sampled_pages = {
            int(item) for item in metadata.get("sampled_pages", ())
        }
        model_severity = str(metadata.get("defect_severity") or "NONE")
        expected_by_page: dict[int, set[str]] = {}
        candidate_by_page: dict[int, AtomicObservation] = {}
        for candidate in candidates:
            for evidence_item in candidate.evidence:
                if evidence_item.page_number is None:
                    continue
                page_number = int(evidence_item.page_number)
                candidate_by_page[page_number] = candidate
                expected_by_page.setdefault(page_number, set()).update(
                    _GATE_RULE_KIND_TO_MODEL_DEFECT_CODES.get(
                        evidence_item.kind,
                        (),
                    )
                )
        candidate_pages = set(candidate_by_page)
        if not candidate_pages:
            return "UNRESOLVED", model_severity

        matching_pages: set[int] = set()
        matching_severities: list[str] = []
        for finding in model.evidence:
            if finding.page_number is None:
                continue
            page_number = int(finding.page_number)
            if page_number not in expected_by_page:
                continue
            payload = finding.payload
            affected_pages = {
                int(item) for item in payload.get("affected_page_numbers", ())
            }
            defect_codes = {
                str(item) for item in payload.get("defect_codes", ())
            }
            severity = str(payload.get("severity") or "NONE")
            if (
                page_number in affected_pages
                and severity in {"MAJOR", "CRITICAL"}
                and bool(expected_by_page[page_number] & defect_codes)
            ):
                matching_pages.add(page_number)
                matching_severities.append(severity)

        critical_candidates = any(
            item.severity == Severity.CRITICAL for item in candidates
        )
        if critical_candidates and matching_pages:
            severity = (
                "CRITICAL" if "CRITICAL" in matching_severities else "MAJOR"
            )
            return "CONFIRMED", severity

        if not critical_candidates and candidate_pages <= sampled_pages:
            total_pages_value = metadata.get("total_pages")
            total_pages = (
                int(total_pages_value)
                if isinstance(total_pages_value, int)
                and not isinstance(total_pages_value, bool)
                and total_pages_value > 0
                else max(sampled_pages, default=1)
            )
            matching_key_units = sum(
                candidate_by_page[page].key_unit for page in matching_pages
            )
            if (
                len(matching_pages) / total_pages >= 0.20
                or matching_key_units >= 2
            ):
                severity = (
                    "CRITICAL"
                    if "CRITICAL" in matching_severities
                    else "MAJOR"
                )
                return "CONFIRMED", severity

        if candidate_pages <= sampled_pages:
            return "REJECTED", model_severity
        return "UNRESOLVED", model_severity

    def _scene_results(
        self,
        context: EvaluationContext,
        batch: ObservationBatch,
    ) -> tuple[OracleResult, ...]:
        scene = _scene(context)
        if scene == SceneType.READY_MADE:
            return ()
        if scene == SceneType.TEXT_TO_PPT:
            return (
                self._reduce(batch, ("requirement_satisfaction",), EvaluationScope.REQUIREMENT, IMPORTANCE_COVERAGE, "instruction", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("audience_journey",), EvaluationScope.DECK, IMPORTANCE_COVERAGE, "audience", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("fact_claim_support",), EvaluationScope.DECK, IMPORTANCE_COVERAGE, "fact_claim", ScoreRole.SCENE_ADDITIVE),
            )
        if scene == SceneType.PROJECT_SUMMARY:
            return (
                self._reduce(batch, ("source_claim_alignment",), EvaluationScope.CLAIM, IMPORTANCE_COVERAGE, "source_claim", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("key_point_coverage",), EvaluationScope.REQUIREMENT, IMPORTANCE_COVERAGE, "key_point", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("numeric_claim_alignment",), EvaluationScope.CLAIM, IMPORTANCE_COVERAGE, "numeric", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("compression_richness_observation",), EvaluationScope.DECK, IMPORTANCE_COVERAGE, "compression_richness", ScoreRole.SCENE_ADDITIVE),
                self._reduce(batch, ("source_traceability",), EvaluationScope.ASSET, IMPORTANCE_COVERAGE, "traceability", ScoreRole.SCENE_ADDITIVE),
            )
        return (
            self._reduce(batch, ("asset_presence",), EvaluationScope.ASSET, IMPORTANCE_COVERAGE, "asset_coverage", ScoreRole.SCENE_ADDITIVE),
            self._reduce(batch, ("asset_presence",), EvaluationScope.ASSET, IMPORTANCE_COVERAGE, "asset_presentation", ScoreRole.SCENE_ADDITIVE),
            self._reduce(batch, ("crop_geometry_risk",), EvaluationScope.OBJECT, IMPORTANCE_COVERAGE, "crop_image_integrity", ScoreRole.SCENE_ADDITIVE, required=False),
            self._reduce(batch, ("chart_series_accuracy",), EvaluationScope.CHART_SERIES, IMPORTANCE_COVERAGE, "chart_fidelity", ScoreRole.SCENE_ADDITIVE),
            self._reduce(batch, ("media_integrity",), EvaluationScope.OBJECT, IMPORTANCE_COVERAGE, "media_integrity", ScoreRole.SCENE_ADDITIVE, required=False),
        )

    def _na(self, metric_id: str, reason: str) -> OracleResult:
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.NA,
            score_role=ScoreRole.BASE_ADDITIVE,
            confidence=1.0,
            severity=Severity.INFO,
            version=self.version,
            metadata={"reason_code": reason},
        )


__all__ = [
    "V8AtomicObservationComposite",
    "V8QualityReducerOracle",
    "V8TieredVisualCriterionOracle",
    "V8_BASE_ADDITIVE_METRICS",
    "V8_OBSERVATION_COMPOSITE_ID",
    "V8_QUALITY_VERSION",
    "V8_VISUAL_CRITERION_IDS",
    "V8_REDUCER_ORACLE_ID",
    "V8_SCENE_METRICS",
]
