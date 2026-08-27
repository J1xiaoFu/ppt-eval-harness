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
    STRUCTURED_VLM_VISUAL_CRITERION_IDS,
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
V8_QUALITY_VERSION = "8.0.0"

V8_BASE_ADDITIVE_METRICS = (
    "content_structure",
    "composition_craft",
    "typography_craft",
    "palette_craft",
    "visual_communication",
    "visual_system_sequence",
    "authorship_specificity",
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
    "typography_legibility": ("slide_typography_functional", "slide_reading_load"),
    "color_contrast": (),
    "imagery_data_visualization": ("media_integrity", "crop_geometry_risk"),
    "cross_slide_consistency": ("transition_coherence_proxy", "duplicate_slide"),
    "render_integrity": ("render_availability_parity",),
}


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

        normalized = tuple(
            replace(item, oracle_id=self.oracle_id) for item in observations
        )
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
        *,
        source_access_policy: Any = None,
    ) -> None:
        if criterion_id not in STRUCTURED_VLM_VISUAL_CRITERION_IDS:
            raise ValueError(f"unknown v8 visual criterion {criterion_id!r}")
        self.criterion_id = criterion_id
        self.oracle_id = f"v8.visual.{criterion_id}"
        self.metric_id = f"structured_vlm_{criterion_id}"
        self._flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            adapter,
            source_access_policy=source_access_policy,
        )
        self._advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                adapter,
                source_access_policy=source_access_policy,
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
        reason: str | None = None
        if flash.metric_status != MetricStatus.SCORED:
            reason = "FLASH_UNRESOLVED"
        elif flash.confidence < 0.60:
            reason = "FLASH_LOW_CONFIDENCE"
        elif self._rule_disagreement(context, flash):
            reason = "RULE_MODEL_DISAGREEMENT"
        chosen = flash
        tier = "FLASH"
        if reason is not None and self._advanced is not None:
            advanced = self._advanced.evaluate(context)
            if advanced.metric_status == MetricStatus.SCORED:
                chosen = advanced
                tier = "ADVANCED"
            else:
                tier = "FLASH_UNRESOLVED_ADVANCED_FAILED"
        return replace(
            chosen,
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            version=self.version,
            metadata={
                **dict(chosen.metadata),
                "routing_mode": "ATOMIC_FLASH_ADVANCED_HUMAN",
                "selected_tier": tier,
                "escalation_reason": reason,
                "criterion_id": self.criterion_id,
            },
        )

    def _rule_disagreement(
        self, context: EvaluationContext, result: OracleResult
    ) -> bool:
        if result.normalized_score is None or result.normalized_score <= 0.75:
            return False
        rule_metrics = set(_CRITERION_RULE_METRICS[self.criterion_id])
        observations = context.memo.get("ppt_eval.atomic_observations", ())
        return any(
            isinstance(item, AtomicObservation)
            and item.metric_id in rule_metrics
            and item.local_score is not None
            and item.local_score < 0.50
            for item in observations
        )


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
        results: list[OracleResult] = [self._integrity_gate(observations, prior_results)]

        content = self._reduce(
            batch,
            ("slide_content_presence", "slide_reading_load", "document_structure", "title_body_alignment"),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "content_structure",
            ScoreRole.BASE_ADDITIVE,
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
        media_rule = self._reduce(
            batch,
            ("media_integrity", "crop_geometry_risk"),
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
        authorship = self._reduce(
            batch,
            ("authorship_specificity_signals",),
            EvaluationScope.PAGE,
            PAGE_QUALITY,
            "authorship_specificity",
            ScoreRole.BASE_ADDITIVE,
        )
        results.extend(
            (
                content,
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
                self._model_only(
                    "palette_craft",
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
            minimum_observability=0.60,
            required=required,
        )
        return ReducerEngine().reduce(batch, spec)

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

    def _integrity_gate(
        self,
        observations: tuple[AtomicObservation, ...],
        results: tuple[OracleResult, ...],
    ) -> OracleResult:
        file_result = next(
            (item for item in results if item.metric_id == "file_deliverability"),
            None,
        )
        critical = [
            item
            for item in observations
            if item.severity == Severity.CRITICAL and (item.critical or item.key_unit)
        ]
        scored = [item for item in observations if item.metric_status == MetricStatus.SCORED]
        major = [item for item in scored if item.severity == Severity.MAJOR]
        multiplier = 1.0
        reason = "PASS"
        if file_result is not None and file_result.multiplier == 0.0:
            multiplier = 0.0
            reason = "FILE_DELIVERABILITY_FAILED"
        elif critical:
            multiplier = 0.0
            reason = "KEY_UNIT_CRITICAL"
        elif scored and len(major) / len(scored) >= 0.20:
            multiplier = 0.5
            reason = "MAJOR_DEFECT_PREVALENCE"
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
                for item in critical[:20]
                for evidence_item in item.evidence[:1]
            ),
            version=self.version,
            metadata={
                "reason_code": reason,
                "critical_observation_ids": [item.observation_id for item in critical],
                "major_prevalence": len(major) / max(1, len(scored)),
            },
        )

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
    "V8_REDUCER_ORACLE_ID",
    "V8_SCENE_METRICS",
]
