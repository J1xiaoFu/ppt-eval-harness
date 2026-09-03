"""v8 observation acquisition and quality-attribute reduction composites."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from ppt_eval.adapters.model_audits import ModelAuditProvider
from ppt_eval.adapters.pptx import PptxAdapter
from ppt_eval.application.model_request_budget import ModelRequestBudgetLedger
from ppt_eval.application.oracle import (
    EvaluationContext,
    MetricDefinition,
    OracleDescriptor,
    OracleExecutionOutput,
)
from ppt_eval.application.visual_selection import (
    assess_visual_criterion_progress,
    criterion_page_order,
    expand_visual_selection_plan_for_asset_context,
)
from ppt_eval.domain import (
    AtlasScoutResult,
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
    VisualPageIndex,
    VisualSelectionPlan,
)
from ppt_eval.scoring import (
    IMPORTANCE_COVERAGE,
    PAGE_QUALITY,
    PAIR_QUALITY,
    PptPdmsAggregator,
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
V8_QUALITY_VERSION = "8.4.0"
V83_QUALITY_VERSION = "8.3.0"
V83_ATOMIC_OBSERVATION_VERSION = "2.1.0"
V8_VISUAL_CRITERION_IDS = V8_GROUNDED_VISUAL_CRITERION_IDS
_VISUAL_INITIAL_RESULTS_MEMO_KEY = "ppt_eval.visual_initial_results"
_IMAGERY_CONTEXT_DEFECT_CODES = frozenset(
    {
        "placeholder_or_stock_visual",
        "visible_stock_watermark",
        "image_semantics_mismatch",
        "embedded_text_unreadable",
    }
)
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

    @staticmethod
    def version_for_profile(profile: Any) -> str:
        return (
            V83_QUALITY_VERSION
            if getattr(profile, "version", None) == "8.3"
            else V8_QUALITY_VERSION
        )

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

        contract_version = (
            V83_QUALITY_VERSION
            if context.profile.version == "8.3"
            else self.version
        )
        atomic_version = (
            V83_ATOMIC_OBSERVATION_VERSION
            if context.profile.version == "8.3"
            else None
        )
        normalized = tuple(
            replace(item, version=atomic_version)
            if atomic_version is not None
            else item
            for item in observations
        )
        return ObservationBatch(
            oracle_id=self.oracle_id,
            observations=normalized,
            version=contract_version,
            metadata={
                "scene": scene.value,
                "metrics": sorted({item.metric_id for item in normalized}),
                "profile_contract_version": context.profile.version,
                "atomic_observation_version": (
                    atomic_version or normalized[0].version
                    if normalized
                    else atomic_version
                ),
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
        self.initial_oracle_id = f"v8.visual.initial.{criterion_id}"
        self.initial_metric_id = f"provisional_structured_vlm_{criterion_id}"
        self._flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            adapter,
            profile_contract_version="8.4",
        )
        self._advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                adapter,
                profile_contract_version="8.4",
            )
            if advanced_provider is not None
            else None
        )
        self._v83_flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            adapter,
            profile_contract_version="8.3",
        )
        self._v83_advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                adapter,
                profile_contract_version="8.3",
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

    @staticmethod
    def version_for_profile(profile: Any) -> str:
        return (
            V83_QUALITY_VERSION
            if getattr(profile, "version", None) == "8.3"
            else V8_QUALITY_VERSION
        )

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        if context.profile.version == "8.4":
            return self._evaluate_adaptive(context)
        return self._evaluate_once(context)

    def _evaluate_once(
        self,
        context: EvaluationContext,
        *,
        model_request_budget_remaining: int | None = None,
    ) -> OracleResult:
        legacy_replay = context.profile.version == "8.3"
        flash_oracle = self._v83_flash if legacy_replay else self._flash
        advanced_oracle = (
            self._v83_advanced if legacy_replay else self._advanced
        )
        result_version = V83_QUALITY_VERSION if legacy_replay else self.version
        flash_reservation = _criterion_request_reservation(flash_oracle)
        if (
            not legacy_replay
            and model_request_budget_remaining is not None
            and model_request_budget_remaining < flash_reservation
        ):
            return self._request_budget_unavailable(
                maximum_reservation=flash_reservation,
                remaining=model_request_budget_remaining,
            )
        flash = flash_oracle.evaluate(context)
        attempted: list[tuple[str, GroundedSingleCriterionVlmOracle, OracleResult]] = [
            ("FLASH", flash_oracle, flash)
        ]
        reason: str | None = None
        budget_denied = flash.metadata.get("reason_code") == (
            "MODEL_REQUEST_BUDGET_EXHAUSTED"
        )
        if budget_denied:
            reason = "FLASH_REQUEST_BUDGET_EXHAUSTED"
        elif flash.metadata.get("reason_code") == (
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
        flash_request_count = (
            _oracle_result_model_request_count(flash)
            if flash_oracle.provider is not None
            else 0
        )
        advanced_reservation = (
            _criterion_request_reservation(advanced_oracle)
            if advanced_oracle is not None
            else 0
        )
        advanced_budget_available = (
            not budget_denied
            and (
                model_request_budget_remaining is None
                or model_request_budget_remaining - flash_request_count
                >= advanced_reservation
            )
        )
        if (
            reason is not None
            and advanced_oracle is not None
            and advanced_budget_available
        ):
            advanced = advanced_oracle.evaluate(context)
            attempted.append(("ADVANCED", advanced_oracle, advanced))
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
            tier = (
                "FLASH_UNRESOLVED_ADVANCED_BUDGET_EXHAUSTED"
                if advanced_oracle is not None and not advanced_budget_available
                else "FLASH_UNRESOLVED_ADVANCED_UNCONFIGURED"
            )
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
            version=result_version,
            duration_ms=sum(item.duration_ms for _, _, item in attempted),
            cost=sum(item.cost for _, _, item in attempted),
            metadata={
                **dict(chosen.metadata),
                "routing_mode": "ATOMIC_FLASH_ADVANCED_HUMAN",
                "selected_tier": tier,
                "escalation_reason": reason,
                "advanced_rule_disagreement": advanced_rule_disagreement,
                "criterion_id": self.criterion_id,
                **(
                    {
                        "request_budget": {
                            "remaining_before_call": (
                                model_request_budget_remaining
                            ),
                            "flash_reservation": flash_reservation,
                            "advanced_reservation": advanced_reservation,
                            "advanced_suppressed": (
                                reason is not None
                                and advanced_oracle is not None
                                and not advanced_budget_available
                            ),
                            "reservation_policy": (
                                "PROVIDER_HTTP_MAX_X_CRITERION_REPAIR_MAX"
                            ),
                        }
                    }
                    if not legacy_replay
                    and model_request_budget_remaining is not None
                    else {}
                ),
                "routing_attempts": routing_attempts,
                "routing_usage": _model_routing_usage(routing_attempts),
            },
        )

    def _request_budget_unavailable(
        self,
        *,
        maximum_reservation: int,
        remaining: int,
    ) -> OracleResult:
        unavailable = self._flash.not_applicable(
            "The visual model request budget cannot safely reserve another atomic audit.",
            code="VISUAL_MODEL_REQUEST_BUDGET_EXHAUSTED",
        )
        return replace(
            unavailable,
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            version=self.version,
            metadata={
                **dict(unavailable.metadata),
                "criterion_id": self.criterion_id,
                "adaptive_visual": True,
                "coverage_complete_for_criterion": False,
                "stopping_reason": "MODEL_REQUEST_BUDGET_EXHAUSTED_REVIEW",
                "adaptive_attempted_pages": [],
                "adaptive_audited_pages": [],
                "routing_attempts": [],
                "routing_usage": _model_routing_usage(()),
                "request_budget": {
                    "remaining_before_call": remaining,
                    "required_reservation": maximum_reservation,
                    "reservation_policy": (
                        "PROVIDER_HTTP_MAX_X_CRITERION_REPAIR_MAX"
                    ),
                },
            },
        )

    def evaluate_initial(self, context: EvaluationContext) -> OracleResult:
        """Acquire the shared-cohort seed without finalizing this criterion.

        Profile 8.4 runs every criterion seed before any criterion starts its
        risk-page refinement.  This makes a complete provisional visual vector
        available for PDMS bounds while preserving one model call owner and one
        timeout boundary per DAG node.
        """

        plan = context.memo.get("ppt_eval.visual_selection_plan")
        page_index = context.memo.get("ppt_eval.visual_page_index")
        scout = context.memo.get("ppt_eval.atlas_scout_result")
        if context.profile.version != "8.4" or not isinstance(
            plan, VisualSelectionPlan
        ) or not isinstance(page_index, VisualPageIndex) or not isinstance(
            scout, AtlasScoutResult
        ):
            unavailable = self._flash.not_applicable(
                "Profile 8.4 visual selection artifacts are unavailable.",
                code="VISUAL_SELECTION_PLAN_UNAVAILABLE",
            )
            seed = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "adaptive_phase": "INITIAL",
                    "coverage_complete_for_criterion": False,
                },
            )
            self._store_initial_seed(context, seed)
            return self._as_initial_diagnostic(seed)

        if self.criterion_id == "render_integrity" and not _render_audit_required(
            context
        ):
            skipped = self._flash.not_applicable(
                "No renderer, parity, pixel-difference, or Scout render risk triggered this audit.",
                code="CONDITIONAL_RENDER_INTEGRITY_NOT_TRIGGERED",
            )
            seed = replace(
                skipped,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                score_role=ScoreRole.DIAGNOSTIC,
                version=self.version,
                metadata={
                    **dict(skipped.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "adaptive_phase": "INITIAL",
                    "conditional_call": True,
                    "triggered": False,
                    "coverage_required": False,
                    "score_affecting": False,
                },
            )
            self._store_initial_seed(context, seed)
            return self._as_initial_diagnostic(seed)

        if self.criterion_id == "authorship_specificity" and len(
            page_index.pages
        ) < 2:
            unavailable = self._flash.not_applicable(
                "At least two rendered pages are required for systemic authorship inspection.",
                code="AUTHORSHIP_SYSTEMIC_SCOPE_UNOBSERVABLE",
            )
            seed = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                score_role=ScoreRole.DIAGNOSTIC,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "adaptive_phase": "INITIAL",
                    "coverage_required": False,
                    "coverage_complete_for_criterion": True,
                    "score_affecting": False,
                },
            )
            self._store_initial_seed(context, seed)
            return self._as_initial_diagnostic(seed)

        ordered_pages = criterion_page_order(plan, self.criterion_id)
        common_pages = _visual_common_pages(
            plan,
            self.criterion_id,
            ordered_pages,
        )
        if not common_pages:
            unavailable = self._flash.not_applicable(
                "The visual selection plan contains no page for this criterion.",
                code="VISUAL_CRITERION_NOT_SELECTED",
            )
            seed = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "adaptive_phase": "INITIAL",
                    "coverage_complete_for_criterion": True,
                    "score_affecting": False,
                },
            )
            self._store_initial_seed(context, seed)
            return self._as_initial_diagnostic(seed)

        remaining = _visual_request_budget_remaining(context, ())
        reservation = _criterion_request_reservation(self._flash)
        if remaining < reservation:
            seed = self._request_budget_unavailable(
                maximum_reservation=reservation,
                remaining=remaining,
            )
            self._store_initial_seed(context, seed)
            return self._as_initial_diagnostic(seed)

        active_store = context.memo.setdefault("ppt_eval.visual_active_pages", {})
        if not isinstance(active_store, MutableMapping):
            raise TypeError("visual active-page memo must be a mutable mapping")
        active_store[self.criterion_id] = common_pages
        try:
            first = self._evaluate_once(
                context,
                model_request_budget_remaining=remaining,
            )
        finally:
            active_store.pop(self.criterion_id, None)
        seed = replace(
            first,
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            version=self.version,
            metadata={
                **dict(first.metadata),
                "criterion_id": self.criterion_id,
                "adaptive_visual": True,
                "adaptive_phase": "INITIAL",
                "adaptive_call_count": 1,
                "adaptive_calls": [
                    _adaptive_call_record(
                        first,
                        call_index=1,
                        phase="INITIAL",
                        active_pages=common_pages,
                        new_pages=common_pages,
                        cache_prefix_pages=common_pages,
                        usage_accounted_by=self.initial_oracle_id,
                    )
                ],
                "adaptive_audited_pages": (
                    list(common_pages)
                    if first.metric_status == MetricStatus.SCORED
                    else []
                ),
                "adaptive_attempted_pages": list(common_pages),
                "sampled_pages": (
                    list(common_pages)
                    if first.metric_status == MetricStatus.SCORED
                    else []
                ),
                "cache_prefix_pages": list(common_pages),
                "criterion_page_order": list(ordered_pages),
                "coverage_complete_for_criterion": False,
                "score_affecting": False,
            },
        )
        self._store_initial_seed(context, seed)
        return self._as_initial_diagnostic(seed)

    def _store_initial_seed(
        self,
        context: EvaluationContext,
        result: OracleResult,
    ) -> None:
        store = context.memo.setdefault(_VISUAL_INITIAL_RESULTS_MEMO_KEY, {})
        if not isinstance(store, MutableMapping):
            raise TypeError("visual initial-result memo must be a mutable mapping")
        store[self.criterion_id] = result

    def _as_initial_diagnostic(self, result: OracleResult) -> OracleResult:
        return replace(
            result,
            oracle_id=self.initial_oracle_id,
            metric_id=self.initial_metric_id,
            score_role=ScoreRole.DIAGNOSTIC,
            metadata={
                **dict(result.metadata),
                "final_oracle_id": self.oracle_id,
                "final_metric_id": self.metric_id,
                "score_affecting": False,
                "usage_rollup_owner": self.initial_oracle_id,
            },
        )

    def _evaluate_adaptive(self, context: EvaluationContext) -> OracleResult:
        plan = context.memo.get("ppt_eval.visual_selection_plan")
        page_index = context.memo.get("ppt_eval.visual_page_index")
        scout = context.memo.get("ppt_eval.atlas_scout_result")
        if (
            not isinstance(plan, VisualSelectionPlan)
            or not isinstance(page_index, VisualPageIndex)
            or not isinstance(scout, AtlasScoutResult)
        ):
            unavailable = self._flash.not_applicable(
                "Profile 8.4 visual selection artifacts are unavailable.",
                code="VISUAL_SELECTION_PLAN_UNAVAILABLE",
            )
            return replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "coverage_complete_for_criterion": False,
                },
            )
        ordered_pages = criterion_page_order(plan, self.criterion_id)
        common_pages = _visual_common_pages(
            plan,
            self.criterion_id,
            ordered_pages,
        )
        if not common_pages:
            unavailable = self._flash.not_applicable(
                "The visual selection plan contains no page for this criterion.",
                code="VISUAL_CRITERION_NOT_SELECTED",
            )
            _store_criterion_pages(context, self.criterion_id, ())
            return replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "coverage_complete_for_criterion": True,
                    "score_affecting": False,
                },
            )

        raw_initial_store = context.memo.get(_VISUAL_INITIAL_RESULTS_MEMO_KEY, {})
        initial_store = (
            raw_initial_store if isinstance(raw_initial_store, Mapping) else {}
        )
        raw_stored_seed = initial_store.get(self.criterion_id)
        stored_seed = (
            raw_stored_seed if isinstance(raw_stored_seed, OracleResult) else None
        )
        seed_accounted_separately = stored_seed is not None
        initial_phase_result = next(
            (
                result
                for result in context.memo.get("ppt_eval.oracle_results", ())
                if isinstance(result, OracleResult)
                and result.oracle_id == self.initial_oracle_id
            ),
            None,
        )
        if stored_seed is None and initial_phase_result is not None:
            _store_criterion_pages(context, self.criterion_id, ())
            unavailable = self._flash.not_applicable(
                "The diagnostic initial visual phase did not produce a reusable seed.",
                code="INITIAL_VISUAL_SEED_UNAVAILABLE",
            )
            return replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                version=self.version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "adaptive_visual": True,
                    "adaptive_phase": "FINAL",
                    "coverage_complete_for_criterion": False,
                    "stopping_reason": "INITIAL_VISUAL_SEED_UNAVAILABLE_REVIEW",
                    "initial_phase_execution_status": (
                        initial_phase_result.execution_status.value
                    ),
                    "initial_phase_error_code": initial_phase_result.error_code,
                    "routing_attempts": [],
                    "routing_usage": None,
                },
            )
        first: OracleResult
        if stored_seed is not None:
            first = stored_seed
            if first.metadata.get("coverage_required") is False:
                _store_criterion_pages(context, self.criterion_id, ())
                return _final_result_from_non_scoring_seed(self, first)

        active_store = context.memo.setdefault("ppt_eval.visual_active_pages", {})
        if not isinstance(active_store, MutableMapping):
            raise TypeError("visual active-page memo must be a mutable mapping")
        calls: list[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]] = []
        billable_calls: list[
            tuple[OracleResult, tuple[int, ...], tuple[int, ...]]
        ] = []
        flash_reservation = _criterion_request_reservation(self._flash)
        attempted_pages: set[int] = set(common_pages)
        if not seed_accounted_separately:
            initial_remaining = _visual_request_budget_remaining(context, ())
            if initial_remaining < flash_reservation:
                _store_criterion_pages(context, self.criterion_id, ())
                return self._request_budget_unavailable(
                    maximum_reservation=flash_reservation,
                    remaining=initial_remaining,
                )
            active_store[self.criterion_id] = common_pages
            first = self._evaluate_once(
                context,
                model_request_budget_remaining=initial_remaining,
            )
            billable_calls.append((first, common_pages, common_pages))
        calls.append((first, common_pages, common_pages))
        successful_pages: set[int] = (
            set(common_pages)
            if first.metric_status == MetricStatus.SCORED
            else set()
        )
        current = first
        previous_affected: set[int] = set()
        stopping_reason = "BASE_COHORT_SUFFICIENT"
        raw_pending_context = context.memo.get(
            "ppt_eval.visual_pending_asset_context_pages", ()
        )
        pending_asset_context: set[int] = {
            int(page)
            for page in raw_pending_context
            if isinstance(page, int) and not isinstance(page, bool)
        } if isinstance(raw_pending_context, Sequence) and not isinstance(
            raw_pending_context, (str, bytes)
        ) else set()

        try:
            while True:
                affected = _result_affected_pages(current)
                new_affected = affected - previous_affected
                if self.criterion_id == "imagery_data_visualization" and (
                    _IMAGERY_CONTEXT_DEFECT_CODES
                    & {
                        str(code)
                        for code in current.metadata.get("defect_codes", ())
                    }
                ) and affected:
                    previously_audited = _all_visual_criterion_pages(context)
                    expanded_plan, newly_pending = (
                        expand_visual_selection_plan_for_asset_context(
                            page_index,
                            plan,
                            seed_page_numbers=affected,
                            audited_page_numbers=successful_pages,
                            protected_page_numbers=previously_audited,
                        )
                    )
                    pending_asset_context.update(newly_pending)
                    if expanded_plan.plan_id != plan.plan_id:
                        plan = expanded_plan
                        context.memo["ppt_eval.visual_selection_plan"] = plan
                        contracts = context.memo.get("ppt_eval.visual_contracts")
                        if isinstance(contracts, MutableMapping):
                            contracts["visual_selection_plan"] = plan
                        ordered_pages = criterion_page_order(
                            plan,
                            self.criterion_id,
                        )
                pending_asset_context.difference_update(successful_pages)
                context.memo["ppt_eval.visual_pending_asset_context_pages"] = tuple(
                    sorted(pending_asset_context)
                )
                severe = str(current.metadata.get("defect_severity") or "NONE")
                conflict = bool(current.metadata.get("advanced_rule_disagreement")) or (
                    current.metadata.get("escalation_reason")
                    == "RULE_MODEL_DISAGREEMENT"
                    and current.metadata.get("selected_tier") != "ADVANCED"
                )
                interim_score = _adaptive_visual_score(calls, self.criterion_id)
                criterion_interval = _visual_score_interval(
                    interim_score,
                    audited_count=len(successful_pages),
                    planned_count=len(ordered_pages),
                )
                pdms_interval = _profile_pdms_interval(
                    context,
                    criterion_id=self.criterion_id,
                    current_result=current,
                    criterion_interval=criterion_interval,
                )
                progress = assess_visual_criterion_progress(
                    page_index,
                    plan,
                    self.criterion_id,
                    audited_page_numbers=successful_pages,
                    metric_scored=current.metric_status == MetricStatus.SCORED,
                    confidence=current.confidence,
                    new_major_count=(
                        len(new_affected) if severe == "MAJOR" else 0
                    ),
                    new_critical_count=(
                        len(new_affected) if severe == "CRITICAL" else 0
                    ),
                    rule_conflict=conflict,
                    pending_asset_context_pages=pending_asset_context,
                    composite_lower_bound=(
                        pdms_interval[0] if pdms_interval else None
                    ),
                    composite_upper_bound=(
                        pdms_interval[1] if pdms_interval else None
                    ),
                    cost_exhausted=(
                        _visual_cost_exhausted(context, billable_calls)
                        or _visual_request_budget_exhausted(context, billable_calls)
                        or _visual_request_budget_remaining(context, billable_calls)
                        < flash_reservation
                    ),
                )
                if _evaluation_cancelled(context):
                    stopping_reason = "ORACLE_TIMEOUT_CANCELLED_REVIEW"
                    break
                if not progress.continue_audit:
                    stopping_reason = str(progress.stopping_reason)
                    break

                chunk = progress.next_page_numbers
                if _evaluation_cancelled(context):
                    stopping_reason = "ORACLE_TIMEOUT_CANCELLED_REVIEW"
                    break
                active_pages = tuple((*common_pages, *chunk))
                active_store[self.criterion_id] = active_pages
                next_result = self._evaluate_once(
                    context,
                    model_request_budget_remaining=(
                        _visual_request_budget_remaining(context, billable_calls)
                    ),
                )
                calls.append((next_result, active_pages, chunk))
                billable_calls.append((next_result, active_pages, chunk))
                attempted_pages.update(chunk)
                if next_result.metric_status == MetricStatus.SCORED:
                    successful_pages.update(chunk)
                previous_affected = affected
                current = next_result
        finally:
            active_store.pop(self.criterion_id, None)

        _store_criterion_pages(
            context,
            self.criterion_id,
            tuple(sorted(successful_pages)),
        )
        return _merge_adaptive_visual_results(
            self,
            calls,
            audited_pages=tuple(sorted(successful_pages)),
            attempted_pages=tuple(sorted(attempted_pages)),
            common_pages=common_pages,
            ordered_pages=ordered_pages,
            stopping_reason=stopping_reason,
            required_pages=progress.required_page_numbers,
            policy_coverage_complete=progress.coverage_complete_for_criterion,
            policy_unresolved_codes=progress.unresolved_codes,
            separately_accounted_call_count=(
                1 if seed_accounted_separately else 0
            ),
            pdms_interval=(
                (progress.composite_lower_bound, progress.composite_upper_bound)
                if progress.composite_lower_bound is not None
                and progress.composite_upper_bound is not None
                else None
            ),
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


class V8InitialVisualCriterionOracle:
    """Diagnostic first phase for one Profile 8.4 visual criterion."""

    version = V8_QUALITY_VERSION

    def __init__(self, owner: V8TieredVisualCriterionOracle) -> None:
        self.owner = owner
        self.criterion_id = owner.criterion_id
        self.oracle_id = owner.initial_oracle_id
        self.metric_id = owner.initial_metric_id

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.DIAGNOSTIC),),
            deterministic=False,
            description=(
                f"Shared-cohort provisional audit for {self.criterion_id}; "
                "never scores directly."
            ),
        )

    def supports(self, context: EvaluationContext) -> bool:
        return context.profile.version == "8.4" and self.owner.supports(context)

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        return self.owner.evaluate_initial(context)


def _visual_common_pages(
    plan: VisualSelectionPlan,
    criterion_id: str,
    ordered_pages: Sequence[int],
) -> tuple[int, ...]:
    common = (
        plan.common_cross_slide
        if criterion_id in {
            "cross_slide_consistency",
            "authorship_specificity",
        }
        else plan.common_page_local
    )
    pages = tuple(page for page in common if page in set(ordered_pages))
    if pages:
        return pages
    limit = 8 if criterion_id in {
        "cross_slide_consistency",
        "authorship_specificity",
    } else 4
    return tuple(ordered_pages[:limit])


def _render_audit_required(context: EvaluationContext) -> bool:
    rendering = context.artifacts.get("model_audit_rendering")
    if isinstance(rendering, Mapping):
        if rendering.get("status") != "READY":
            return True
        warnings = rendering.get("warnings")
        if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)):
            if warnings:
                return True
        if rendering.get("object_pixel_parity_anomaly") is True:
            return True
    page_index = context.memo.get("ppt_eval.visual_page_index")
    if isinstance(page_index, VisualPageIndex) and any(
        page.object_pixel_parity_anomaly for page in page_index.pages
    ):
        return True
    observations = context.memo.get("ppt_eval.atomic_observations", ())
    if any(
        isinstance(item, AtomicObservation)
        and item.metric_id == "render_availability_parity"
        and (
            item.metric_status in {MetricStatus.NA, MetricStatus.ERROR}
            or item.severity in {Severity.MAJOR, Severity.CRITICAL}
        )
        for item in observations
    ):
        return True
    scout = context.memo.get("ppt_eval.atlas_scout_result")
    findings = getattr(scout, "findings", ())
    return any(
        getattr(item, "risk_code", None) == "render_artifact_suspected"
        for item in findings
    )


def _result_affected_pages(result: OracleResult) -> set[int]:
    value = result.metadata.get("affected_page_numbers", ())
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return set()
    return {
        int(item)
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item >= 1
    }


def _visual_score_interval(
    score: float | None,
    *,
    audited_count: int,
    planned_count: int,
) -> tuple[float, float] | None:
    """Bound only selected unresolved risk units, never every unobserved deck page."""

    if score is None or planned_count < 1:
        return None
    observed = min(planned_count, max(0, audited_count))
    missing = planned_count - observed
    uncertainty = 0.12
    unseen_lower = max(0.0, score - uncertainty)
    unseen_upper = min(1.0, score + uncertainty)
    lower = 100.0 * (score * observed + unseen_lower * missing) / planned_count
    upper = 100.0 * (score * observed + unseen_upper * missing) / planned_count
    return (lower, upper)


def _adaptive_visual_score(
    calls: Sequence[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]],
    criterion_id: str,
) -> float | None:
    scored = [
        (result, new_pages)
        for result, _active_pages, new_pages in calls
        if result.metric_status == MetricStatus.SCORED
        and result.normalized_score is not None
    ]
    if not scored:
        return None
    if criterion_id in {"cross_slide_consistency", "authorship_specificity"}:
        cohort_scores = [
            float(result.normalized_score)
            for result, _new_pages in scored
            if result.normalized_score is not None
        ]
        return 0.70 * (sum(cohort_scores) / len(cohort_scores)) + 0.30 * min(
            cohort_scores
        )
    page_scores: dict[int, float] = {}
    for result, new_pages in scored:
        raw_scores = result.metadata.get("page_scores")
        if not isinstance(raw_scores, Mapping):
            continue
        for page_number in new_pages:
            raw_score = raw_scores.get(str(page_number), raw_scores.get(page_number))
            if (
                not isinstance(raw_score, bool)
                and isinstance(raw_score, (int, float))
                and 0.0 <= float(raw_score) <= 1.0
            ):
                page_scores[page_number] = float(raw_score)
    if page_scores:
        return sum(page_scores.values()) / len(page_scores)
    total_weight = sum(max(1, len(new_pages)) for _result, new_pages in scored)
    return sum(
        float(result.normalized_score) * max(1, len(new_pages))
        for result, new_pages in scored
        if result.normalized_score is not None
    ) / total_weight


def _profile_pdms_interval(
    context: EvaluationContext,
    *,
    criterion_id: str,
    current_result: OracleResult,
    criterion_interval: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """Project criterion uncertainty through Reducer 8.4 and true PDMS.

    Missing required constructs use explicit mathematical worst/best bounds;
    they are never silently renormalized or filled with a neutral estimate.
    """

    if criterion_interval is None or current_result.normalized_score is None:
        return None
    metric_id = f"structured_vlm_{criterion_id}"
    prior = tuple(
        result
        for result in context.memo.get("ppt_eval.oracle_results", ())
        if isinstance(result, OracleResult) and result.metric_id != metric_id
    )
    raw_initial = context.memo.get(_VISUAL_INITIAL_RESULTS_MEMO_KEY, {})
    initial_models = {
        str(criterion): result
        for criterion, result in raw_initial.items()
        if isinstance(raw_initial, Mapping)
        and isinstance(criterion, str)
        and isinstance(result, OracleResult)
        and result.metric_status == MetricStatus.SCORED
        and result.normalized_score is not None
    } if isinstance(raw_initial, Mapping) else {}
    model_results: dict[str, OracleResult] = {
        result.metric_id.removeprefix("structured_vlm_"): result
        for result in prior
        if result.metric_id.startswith("structured_vlm_")
        and result.metric_status == MetricStatus.SCORED
        and result.normalized_score is not None
    }
    for initial_criterion, initial_result in initial_models.items():
        model_results.setdefault(initial_criterion, initial_result)

    def project(criterion_bound: float, missing_bound: float) -> float | None:
        model = replace(
            current_result,
            oracle_id=f"v8.visual.{criterion_id}",
            metric_id=metric_id,
            metric_status=MetricStatus.SCORED,
            score_role=ScoreRole.BASE_ADDITIVE,
            raw_value=criterion_bound / 100.0,
            normalized_score=criterion_bound / 100.0,
            error_code=None,
            error_message=None,
        )
        projection_memo = dict(context.memo)
        projected_models = dict(model_results)
        projected_models[criterion_id] = model
        projection_memo["ppt_eval.oracle_results"] = [
            *prior,
            *projected_models.values(),
        ]
        projection_context = EvaluationContext(
            case=context.case,
            profile=context.profile,
            artifacts=context.artifacts,
            memo=projection_memo,
        )
        provisional = V8QualityReducerOracle().evaluate(projection_context)
        available: dict[str, OracleResult] = {}
        configured = {
            *context.profile.base_weights,
            *context.profile.scene_weights,
            *context.profile.base_multiplier_metric_ids,
            *context.profile.scene_multiplier_metric_ids,
        }
        for available_result in (*prior, *provisional):
            if available_result.metric_id in configured:
                available[available_result.metric_id] = available_result
        bounded: list[OracleResult] = []
        for configured_metric in sorted(configured):
            configured_result = available.get(configured_metric)
            if configured_result is not None and configured_result.metric_status not in {
                MetricStatus.NA,
                MetricStatus.ERROR,
            }:
                bounded.append(configured_result)
                continue
            is_base_multiplier = (
                configured_metric in context.profile.base_multiplier_metric_ids
            )
            is_scene_multiplier = (
                configured_metric in context.profile.scene_multiplier_metric_ids
            )
            if is_base_multiplier or is_scene_multiplier:
                bounded.append(
                    OracleResult(
                        oracle_id="v8.pdms_interval.bound",
                        metric_id=configured_metric,
                        execution_status=ExecutionStatus.SUCCESS,
                        metric_status=(
                            MetricStatus.PASS
                            if missing_bound == 1.0
                            else MetricStatus.FAIL
                        ),
                        score_role=(
                            ScoreRole.BASE_MULTIPLIER
                            if is_base_multiplier
                            else ScoreRole.SCENE_MULTIPLIER
                        ),
                        raw_value=missing_bound,
                        multiplier=missing_bound,
                        confidence=1.0,
                        severity=Severity.INFO,
                        version=V8_QUALITY_VERSION,
                        metadata={
                            "interval_bound": True,
                            "missingness_policy": "UNOBSERVED_CONSTRUCT_BOUND",
                        },
                    )
                )
            else:
                bounded.append(
                    OracleResult(
                        oracle_id="v8.pdms_interval.bound",
                        metric_id=configured_metric,
                        execution_status=ExecutionStatus.SUCCESS,
                        metric_status=MetricStatus.SCORED,
                        score_role=(
                            ScoreRole.BASE_ADDITIVE
                            if configured_metric in context.profile.base_weights
                            else ScoreRole.SCENE_ADDITIVE
                        ),
                        raw_value=missing_bound,
                        normalized_score=missing_bound,
                        confidence=0.0,
                        severity=Severity.INFO,
                        version=V8_QUALITY_VERSION,
                        metadata={
                            "interval_bound": True,
                            "missingness_policy": "UNOBSERVED_CONSTRUCT_BOUND",
                        },
                    )
                )
        breakdown = PptPdmsAggregator().aggregate(context.profile, bounded)
        return (
            breakdown.full_score
            if breakdown.full_score is not None
            else breakdown.base_score
        )

    lower = project(criterion_interval[0], 0.0)
    upper = project(criterion_interval[1], 1.0)
    if lower is None or upper is None:
        return None
    return (min(lower, upper), max(lower, upper))


def _visual_cost_exhausted(
    context: EvaluationContext,
    calls: Sequence[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]],
) -> bool:
    budget = context.memo.get("ppt_eval.cost_budget")
    spent = context.memo.get("ppt_eval.cost_spent_before_node", 0.0)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or isinstance(spent, bool)
        or not isinstance(spent, (int, float))
    ):
        return False
    return float(spent) + sum(
        result.cost for result, _active, _new in calls
    ) >= float(budget)


def _evaluation_cancelled(context: EvaluationContext) -> bool:
    event = context.memo.get("ppt_eval.cancel_event")
    is_set = getattr(event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _visual_request_budget_exhausted(
    context: EvaluationContext,
    calls: Sequence[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]],
) -> bool:
    return _visual_request_budget_remaining(context, calls) <= 0


def _visual_request_budget_remaining(
    context: EvaluationContext,
    calls: Sequence[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]],
) -> int:
    ledger = context.memo.get("ppt_eval.model_request_budget")
    if isinstance(ledger, ModelRequestBudgetLedger):
        return ledger.snapshot().remaining_attempts
    policy = context.profile.metadata.get("visual_audit")
    maximum = (
        policy.get("maximum_model_requests", 64)
        if isinstance(policy, Mapping)
        else 64
    )
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        return 0
    used = 0
    prior_results = context.memo.get("ppt_eval.oracle_results", ())
    if isinstance(prior_results, Sequence):
        for result in prior_results:
            if not isinstance(result, OracleResult):
                continue
            used += _result_routing_request_count(result)
            if result.metric_id == "visual_atlas_scout_routing":
                audit_metadata = result.metadata.get("audit_metadata")
                if isinstance(audit_metadata, Mapping):
                    count = audit_metadata.get("provider_attempt_count", 0)
                    if isinstance(count, int) and not isinstance(count, bool):
                        used += max(0, count)
    for result, _active, _new in calls:
        used += _result_routing_request_count(result)
    return max(0, maximum - used)


def _result_routing_request_count(result: OracleResult) -> int:
    routing_usage = result.metadata.get(
        "accounted_routing_usage",
        result.metadata.get("routing_usage"),
    )
    if isinstance(routing_usage, Mapping):
        count = routing_usage.get("attempt_count")
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        ):
            return count
    attempts = result.metadata.get("routing_attempts")
    if isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
        return len(attempts)
    return 0


def _store_criterion_pages(
    context: EvaluationContext,
    criterion_id: str,
    pages: Sequence[int],
) -> None:
    value = context.memo.setdefault("ppt_eval.visual_criterion_pages", {})
    if not isinstance(value, MutableMapping):
        raise TypeError("visual criterion-page memo must be a mutable mapping")
    value[criterion_id] = tuple(sorted(set(pages)))


def _all_visual_criterion_pages(context: EvaluationContext) -> set[int]:
    raw = context.memo.get("ppt_eval.visual_criterion_pages", {})
    if not isinstance(raw, Mapping):
        return set()
    return {
        int(page)
        for pages in raw.values()
        if isinstance(pages, Sequence) and not isinstance(pages, (str, bytes))
        for page in pages
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1
    }


def _adaptive_call_record(
    result: OracleResult,
    *,
    call_index: int,
    phase: str,
    active_pages: Sequence[int],
    new_pages: Sequence[int],
    cache_prefix_pages: Sequence[int],
    usage_accounted_by: str,
) -> Mapping[str, Any]:
    return {
        "call_index": call_index,
        "phase": phase,
        "active_page_numbers": list(active_pages),
        "cache_prefix_pages": list(cache_prefix_pages),
        "new_page_numbers": list(new_pages),
        "metric_status": result.metric_status.value,
        "score": result.normalized_score,
        "confidence": result.confidence,
        "defect_severity": result.metadata.get("defect_severity", "NONE"),
        "affected_page_numbers": list(
            sorted(_result_affected_pages(result))
        ),
        "selected_tier": result.metadata.get("selected_tier"),
        "escalation_reason": result.metadata.get("escalation_reason"),
        "advanced_rule_disagreement": result.metadata.get(
            "advanced_rule_disagreement"
        ),
        "request_fingerprint": result.metadata.get("request_fingerprint"),
        "response_fingerprint": result.metadata.get("response_fingerprint"),
        "usage_accounted_by": usage_accounted_by,
    }


def _final_result_from_non_scoring_seed(
    owner: V8TieredVisualCriterionOracle,
    seed: OracleResult,
) -> OracleResult:
    metadata = dict(seed.metadata)
    metadata.update(
        {
            "criterion_id": owner.criterion_id,
            "adaptive_visual": True,
            "adaptive_phase": "FINAL",
            "initial_seed_oracle_id": owner.initial_oracle_id,
            "initial_seed_cost_accounted_separately": True,
            "routing_attempts": [],
            "routing_usage": None,
        }
    )
    return replace(
        seed,
        oracle_id=owner.oracle_id,
        metric_id=owner.metric_id,
        duration_ms=0,
        cost=0.0,
        metadata=metadata,
    )


def _merge_adaptive_visual_results(
    owner: V8TieredVisualCriterionOracle,
    calls: Sequence[tuple[OracleResult, tuple[int, ...], tuple[int, ...]]],
    *,
    audited_pages: tuple[int, ...],
    attempted_pages: tuple[int, ...],
    common_pages: tuple[int, ...],
    ordered_pages: tuple[int, ...],
    stopping_reason: str,
    required_pages: tuple[int, ...],
    policy_coverage_complete: bool,
    policy_unresolved_codes: tuple[str, ...],
    separately_accounted_call_count: int = 0,
    pdms_interval: tuple[float, float] | None = None,
) -> OracleResult:
    scored = [
        (result, new_pages)
        for result, _active_pages, new_pages in calls
        if result.metric_status == MetricStatus.SCORED
        and result.normalized_score is not None
    ]
    base = scored[-1][0] if scored else calls[-1][0]
    total_weight = sum(max(1, len(new_pages)) for _result, new_pages in scored)
    score = (
        sum(
            float(result.normalized_score) * max(1, len(new_pages))
            for result, new_pages in scored
            if result.normalized_score is not None
        )
        / total_weight
        if total_weight
        else None
    )
    confidence = (
        sum(
            result.confidence * max(1, len(new_pages))
            for result, new_pages in scored
        )
        / total_weight
        if total_weight
        else base.confidence
    )
    evidence_items = []
    seen_evidence: set[tuple[str, int | None, str]] = set()
    page_scores: dict[str, float] = {}
    defect_codes: set[str] = set()
    affected_pages: set[int] = set()
    positive_signals: set[str] = set()
    severities: list[str] = []
    routing_attempts: list[Mapping[str, Any]] = []
    accounted_routing_attempts: list[Mapping[str, Any]] = []
    adaptive_calls: list[Mapping[str, Any]] = []
    for call_index, (result, active_pages, new_pages) in enumerate(calls, start=1):
        for item in result.evidence:
            key = (item.evidence_id, item.page_number, item.kind)
            if key not in seen_evidence:
                seen_evidence.add(key)
                evidence_items.append(item)
        raw_page_scores = result.metadata.get("page_scores")
        if isinstance(raw_page_scores, Mapping):
            for page_number in new_pages:
                raw_score = raw_page_scores.get(str(page_number))
                if raw_score is None:
                    raw_score = raw_page_scores.get(page_number)
                if (
                    not isinstance(raw_score, bool)
                    and isinstance(raw_score, (int, float))
                    and 0.0 <= float(raw_score) <= 1.0
                ):
                    page_scores[str(page_number)] = float(raw_score)
        defect_codes.update(
            str(item)
            for item in result.metadata.get("defect_codes", ())
            if str(item)
        )
        affected_pages.update(_result_affected_pages(result))
        positive_signals.update(
            str(item)
            for item in result.metadata.get("positive_quality_signals", ())
            if str(item)
        )
        severities.append(str(result.metadata.get("defect_severity") or "NONE"))
        raw_attempts = result.metadata.get("routing_attempts")
        if isinstance(raw_attempts, Sequence) and not isinstance(
            raw_attempts, (str, bytes)
        ):
            normalized_attempts = [
                {
                    **dict(attempt),
                    "adaptive_call_index": call_index,
                    "active_page_numbers": list(active_pages),
                    "new_page_numbers": list(new_pages),
                    "usage_accounted_by": (
                        owner.initial_oracle_id
                        if call_index <= separately_accounted_call_count
                        else owner.oracle_id
                    ),
                }
                for attempt in raw_attempts
                if isinstance(attempt, Mapping)
            ]
            routing_attempts.extend(normalized_attempts)
            if call_index > separately_accounted_call_count:
                accounted_routing_attempts.extend(normalized_attempts)
        adaptive_calls.append(
            _adaptive_call_record(
                result,
                call_index=call_index,
                phase=("INITIAL" if call_index == 1 else "REFINEMENT"),
                active_pages=active_pages,
                new_pages=new_pages,
                cache_prefix_pages=common_pages,
                usage_accounted_by=(
                    owner.initial_oracle_id
                    if call_index <= separately_accounted_call_count
                    else owner.oracle_id
                ),
            )
        )
    if owner.criterion_id in {
        "cross_slide_consistency",
        "authorship_specificity",
    } and scored:
        cohort_scores = [
            float(result.normalized_score)
            for result, _new_pages in scored
            if result.normalized_score is not None
        ]
        cohort_mean = sum(cohort_scores) / len(cohort_scores)
        score = 0.70 * cohort_mean + 0.30 * min(cohort_scores)
        adaptive_score_reducer = "COHORT_MEAN_70_LOW_TAIL_30_V1"
    elif page_scores:
        score = sum(page_scores.values()) / len(page_scores)
        adaptive_score_reducer = "UNIQUE_PAGE_MEAN_V1"
    else:
        adaptive_score_reducer = "NEW_UNIT_WEIGHTED_FALLBACK_V1"
    if scored:
        confidence = min(result.confidence for result, _new_pages in scored)
    severity_order = {"NONE": 0, "MINOR": 1, "MAJOR": 2, "CRITICAL": 3}
    worst_severity = max(
        severities or ["NONE"],
        key=lambda item: severity_order.get(item, 0),
    )
    routing_usage = _model_routing_usage(routing_attempts)
    accounted_routing_usage = (
        _model_routing_usage(accounted_routing_attempts)
        if accounted_routing_attempts
        else None
    )
    score_interval = pdms_interval
    interval_crosses = bool(
        score_interval
        and any(
            score_interval[0] < threshold <= score_interval[1]
            for threshold in (60.0, 80.0)
        )
    )
    coverage_complete = (
        policy_coverage_complete
        and not policy_unresolved_codes
        and set(required_pages) <= set(audited_pages)
        and bool(scored)
        and base.metric_status == MetricStatus.SCORED
        and not interval_crosses
        and stopping_reason == "ADAPTIVE_STOP_CONDITIONS_MET"
        and all(
            result.metric_status == MetricStatus.SCORED
            for result, _active, _new in calls
        )
    )
    return replace(
        base,
        oracle_id=owner.oracle_id,
        metric_id=owner.metric_id,
        metric_status=(MetricStatus.SCORED if score is not None else MetricStatus.NA),
        raw_value=score,
        normalized_score=score,
        confidence=max(0.0, min(1.0, confidence)),
        evidence=tuple(evidence_items),
        version=owner.version,
        duration_ms=sum(
            result.duration_ms
            for result, _active, _new in calls[separately_accounted_call_count:]
        ),
        cost=sum(
            result.cost
            for result, _active, _new in calls[separately_accounted_call_count:]
        ),
        metadata={
            **dict(base.metadata),
            "routing_mode": "ADAPTIVE_ATOMIC_FLASH_ADVANCED_HUMAN",
            "criterion_id": owner.criterion_id,
            "adaptive_visual": True,
            "adaptive_call_count": len(calls),
            "adaptive_refinement_call_count": (
                len(calls) - separately_accounted_call_count
            ),
            "adaptive_score_reducer": adaptive_score_reducer,
            "adaptive_calls": adaptive_calls,
            "adaptive_audited_pages": list(audited_pages),
            "adaptive_attempted_pages": list(attempted_pages),
            "sampled_pages": list(audited_pages),
            "cache_prefix_pages": list(common_pages),
            "initial_seed_oracle_id": (
                owner.initial_oracle_id
                if separately_accounted_call_count
                else None
            ),
            "initial_seed_cost_accounted_separately": bool(
                separately_accounted_call_count
            ),
            "criterion_page_order": list(ordered_pages),
            "criterion_required_pages": list(required_pages),
            "unresolved_criterion_pages": [
                page_number
                for page_number in required_pages
                if page_number not in audited_pages
            ],
            "coverage_complete_for_criterion": coverage_complete,
            "criterion_progress_unresolved_codes": list(
                policy_unresolved_codes
            ),
            "stopping_reason": stopping_reason,
            "composite_interval_lower_bound": (
                score_interval[0] if score_interval is not None else None
            ),
            "composite_interval_upper_bound": (
                score_interval[1] if score_interval is not None else None
            ),
            "decision_interval_crosses_threshold": interval_crosses,
            "defect_codes": sorted(defect_codes),
            "affected_page_numbers": sorted(affected_pages),
            "positive_quality_signals": sorted(positive_signals),
            "defect_severity": worst_severity,
            "page_scores": page_scores,
            "routing_attempts": routing_attempts,
            "routing_usage": routing_usage,
            "accounted_routing_usage": accounted_routing_usage,
        },
    )


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
            profile_contract_version="8.4",
        )
        self._advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                self.adapter,
                profile_contract_version="8.4",
            )
            if advanced_provider is not None
            else None
        )
        self._v83_flash = GroundedSingleCriterionVlmOracle(
            criterion_id,
            flash_provider,
            self.adapter,
            profile_contract_version="8.3",
        )
        self._v83_advanced = (
            GroundedSingleCriterionVlmOracle(
                criterion_id,
                advanced_provider,
                self.adapter,
                profile_contract_version="8.3",
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

    @staticmethod
    def version_for_profile(profile: Any) -> str:
        return (
            V83_QUALITY_VERSION
            if getattr(profile, "version", None) == "8.3"
            else V8_QUALITY_VERSION
        )

    def evaluate(self, context: EvaluationContext) -> OracleExecutionOutput:
        legacy_replay = context.profile.version == "8.3"
        flash_oracle = self._v83_flash if legacy_replay else self._flash
        advanced_oracle = (
            self._v83_advanced if legacy_replay else self._advanced
        )
        result_version = V83_QUALITY_VERSION if legacy_replay else self.version
        if not self._is_fully_rasterized(context):
            unavailable = flash_oracle.not_applicable(
                "Raster text recovery is unnecessary for a deck with editable semantic content.",
                code="RASTER_TEXT_RECOVERY_NOT_REQUIRED",
            )
            result = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                score_role=ScoreRole.DIAGNOSTIC,
                version=result_version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "raster_only": False,
                    "score_affecting": False,
                },
            )
            return OracleExecutionOutput(results=(result,))

        page_index = context.memo.get("ppt_eval.visual_page_index")
        scout = context.memo.get("ppt_eval.atlas_scout_result")
        plan = context.memo.get("ppt_eval.visual_selection_plan")
        if context.profile.version == "8.4" and (
            not isinstance(page_index, VisualPageIndex)
            or not isinstance(scout, AtlasScoutResult)
            or not isinstance(plan, VisualSelectionPlan)
        ):
            _store_criterion_pages(context, self.criterion_id, ())
            unavailable = flash_oracle.not_applicable(
                "Profile 8.4 visual selection artifacts are unavailable for raster recovery.",
                code="VISUAL_SELECTION_PLAN_UNAVAILABLE",
            )
            result = replace(
                unavailable,
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                score_role=ScoreRole.DIAGNOSTIC,
                version=result_version,
                metadata={
                    **dict(unavailable.metadata),
                    "criterion_id": self.criterion_id,
                    "raster_only": True,
                    "score_affecting": False,
                    "observation_metric_id": self.observation_metric_id,
                    "adaptive_visual": True,
                    "adaptive_audited_pages": [],
                    "coverage_complete_for_criterion": False,
                    "routing_attempts": [],
                    "routing_usage": _model_routing_usage(()),
                },
            )
            return OracleExecutionOutput(results=(result,))

        adaptive_pages: tuple[int, ...] | None = None
        active_store: MutableMapping[str, Any] | None = None
        if context.profile.version == "8.4" and isinstance(
            plan, VisualSelectionPlan
        ):
            adaptive_pages = criterion_page_order(plan, self.criterion_id)
            raw_store = context.memo.setdefault("ppt_eval.visual_active_pages", {})
            if not isinstance(raw_store, MutableMapping):
                raise TypeError("visual active-page memo must be a mutable mapping")
            active_store = raw_store
            if adaptive_pages:
                active_store[self.criterion_id] = adaptive_pages

        model_request_budget_remaining: int | None = None
        flash_reservation = 0
        if adaptive_pages is not None:
            model_request_budget_remaining = _visual_request_budget_remaining(
                context,
                (),
            )
            flash_reservation = _criterion_request_reservation(flash_oracle)
            if model_request_budget_remaining < flash_reservation:
                if active_store is not None:
                    active_store.pop(self.criterion_id, None)
                _store_criterion_pages(context, self.criterion_id, ())
                unavailable = flash_oracle.not_applicable(
                    "The visual model request budget cannot safely reserve raster recovery.",
                    code="VISUAL_MODEL_REQUEST_BUDGET_EXHAUSTED",
                )
                result = replace(
                    unavailable,
                    oracle_id=self.oracle_id,
                    metric_id=self.metric_id,
                    score_role=ScoreRole.DIAGNOSTIC,
                    version=result_version,
                    metadata={
                        **dict(unavailable.metadata),
                        "criterion_id": self.criterion_id,
                        "raster_only": True,
                        "score_affecting": False,
                        "observation_metric_id": self.observation_metric_id,
                        "adaptive_visual": True,
                        "adaptive_audited_pages": [],
                        "coverage_complete_for_criterion": False,
                        "routing_attempts": [],
                        "routing_usage": _model_routing_usage(()),
                        "request_budget": {
                            "remaining_before_call": (
                                model_request_budget_remaining
                            ),
                            "required_reservation": flash_reservation,
                            "reservation_policy": (
                                "PROVIDER_HTTP_MAX_X_CRITERION_REPAIR_MAX"
                            ),
                        },
                    },
                )
                return OracleExecutionOutput(results=(result,))

        flash = flash_oracle.evaluate(context)
        attempted: list[
            tuple[str, GroundedSingleCriterionVlmOracle, OracleResult]
        ] = [("FLASH", flash_oracle, flash)]
        reason: str | None = None
        budget_denied = flash.metadata.get("reason_code") == (
            "MODEL_REQUEST_BUDGET_EXHAUSTED"
        )
        if budget_denied:
            reason = "FLASH_REQUEST_BUDGET_EXHAUSTED"
        elif flash.metadata.get("reason_code") == (
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
        flash_request_count = (
            _oracle_result_model_request_count(flash)
            if flash_oracle.provider is not None
            else 0
        )
        advanced_reservation = (
            _criterion_request_reservation(advanced_oracle)
            if advanced_oracle is not None
            else 0
        )
        advanced_budget_available = (
            not budget_denied
            and (
                model_request_budget_remaining is None
                or model_request_budget_remaining - flash_request_count
                >= advanced_reservation
            )
        )
        if (
            reason is not None
            and advanced_oracle is not None
            and advanced_budget_available
        ):
            advanced = advanced_oracle.evaluate(context)
            attempted.append(("ADVANCED", advanced_oracle, advanced))
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
            tier = (
                "FLASH_UNRESOLVED_ADVANCED_BUDGET_EXHAUSTED"
                if advanced_oracle is not None and not advanced_budget_available
                else "FLASH_UNRESOLVED_ADVANCED_UNCONFIGURED"
            )

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
            version=result_version,
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
                **(
                    {
                        "request_budget": {
                            "remaining_before_call": (
                                model_request_budget_remaining
                            ),
                            "flash_reservation": flash_reservation,
                            "advanced_reservation": advanced_reservation,
                            "advanced_suppressed": (
                                reason is not None
                                and advanced_oracle is not None
                                and not advanced_budget_available
                            ),
                            "reservation_policy": (
                                "PROVIDER_HTTP_MAX_X_CRITERION_REPAIR_MAX"
                            ),
                        }
                    }
                    if adaptive_pages is not None
                    else {}
                ),
                "adaptive_visual": adaptive_pages is not None,
                "adaptive_audited_pages": list(
                    chosen.metadata.get("sampled_pages", ())
                    if adaptive_pages is not None
                    and chosen.metric_status == MetricStatus.SCORED
                    else ()
                ),
            },
        )
        if active_store is not None:
            active_store.pop(self.criterion_id, None)
        if adaptive_pages is not None:
            actual_pages = (
                tuple(int(page) for page in routed.metadata.get("sampled_pages", ()))
                if routed.metric_status == MetricStatus.SCORED
                else ()
            )
            _store_criterion_pages(context, self.criterion_id, actual_pages)
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
        observation_version = (
            V83_QUALITY_VERSION
            if context.profile.version == "8.3"
            else self.version
        )
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
                    version=observation_version,
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
    provider_runtime: dict[str, Any] = {
        key: metadata[key]
        for key in ("image_transport_mode", "context_cache_enabled")
        if key in metadata
    }
    model_request_count = (
        _oracle_result_model_request_count(result)
        if oracle.provider is not None
        else 0
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
        **(
            {"model_request_count": model_request_count}
            if oracle.profile_contract_version == "8.4"
            else {}
        ),
        "request_fingerprint": metadata.get("request_fingerprint"),
        "response_fingerprint": metadata.get("response_fingerprint"),
        "evidence": [_routing_evidence(item) for item in result.evidence],
        **provider_runtime,
    }


def _oracle_result_model_request_count(result: OracleResult) -> int:
    metadata = result.metadata
    explicit_attempt_count = metadata.get("model_request_attempt_count")
    if (
        isinstance(explicit_attempt_count, int)
        and not isinstance(explicit_attempt_count, bool)
        and explicit_attempt_count >= 0
    ):
        return explicit_attempt_count
    current_count = metadata.get("provider_attempts")
    if (
        isinstance(current_count, bool)
        or not isinstance(current_count, int)
        or current_count < 1
    ):
        retry_counts = {
            int(item.payload["adapter_retry_count"])
            for item in result.evidence
            if isinstance(item.payload.get("adapter_retry_count"), int)
            and not isinstance(item.payload.get("adapter_retry_count"), bool)
            and int(item.payload["adapter_retry_count"]) >= 0
        }
        current_count = 1 + (
            next(iter(retry_counts)) if len(retry_counts) == 1 else 0
        )
    first_count = metadata.get("criterion_retry_first_model_request_count", 0)
    if (
        isinstance(first_count, bool)
        or not isinstance(first_count, int)
        or first_count < 0
    ):
        first_count = 0
    return current_count + first_count


def _criterion_request_reservation(
    oracle: GroundedSingleCriterionVlmOracle,
) -> int:
    provider = oracle.provider
    if provider is None:
        return 0
    bound = getattr(provider, "maximum_http_attempts_per_audit", 2)
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:
        bound = 2
    # The Harness permits one bounded criterion-contract repair, and each
    # provider invocation may itself issue up to ``bound`` HTTP attempts.
    return 2 * bound


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
    complete = bool(items) and all(
        item.get("usage_complete") is True for item in items
    )
    result: dict[str, Any] = {
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
        "attempt_count": sum(
            int(item.get("model_request_count", 1)) for item in items
        ),
        "usage_complete": complete,
    }
    for key in (
        "image_tokens",
        "cached_tokens",
        "cache_creation_input_tokens",
        "request_bytes",
    ):
        total = 0
        valid = bool(items) and complete
        for usage in usages:
            if not isinstance(usage, Mapping) or key not in usage:
                valid = False
                break
            value = usage.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                valid = False
                break
            total += value
        if valid:
            result[key] = total
    return result


class V8QualityReducerOracle:
    """Reduce observations by quality attribute; deterministic rules cap model scores."""

    oracle_id = V8_REDUCER_ORACLE_ID
    version = V8_QUALITY_VERSION

    def __init__(self, *, contract_version: str = V8_QUALITY_VERSION) -> None:
        if contract_version not in {V83_QUALITY_VERSION, V8_QUALITY_VERSION}:
            raise ValueError("unsupported v8 quality reducer contract version")
        self.version = contract_version

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

    @staticmethod
    def version_for_profile(profile: Any) -> str:
        return (
            V83_QUALITY_VERSION
            if getattr(profile, "version", None) == "8.3"
            else V8_QUALITY_VERSION
        )

    def evaluate(self, context: EvaluationContext) -> tuple[OracleResult, ...]:
        if (
            context.profile.version == "8.3"
            and self.version != V83_QUALITY_VERSION
        ):
            # Use a separate immutable reducer instance.  Never mutate the
            # registry singleton because concurrent 8.3 replay and 8.4 runs
            # may share it.
            return V8QualityReducerOracle(
                contract_version=V83_QUALITY_VERSION
            ).evaluate(context)
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
        visual_coverage = next(
            (item for item in results if item.metric_id == "visual_audit_coverage"),
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
        elif (
            visual_coverage is not None
            and visual_coverage.metadata.get("coverage_complete") is not True
        ):
            return OracleResult(
                oracle_id=self.oracle_id,
                metric_id="v8_functional_integrity",
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.NA,
                score_role=ScoreRole.BASE_MULTIPLIER,
                raw_value="VISUAL_AUDIT_COVERAGE_INCOMPLETE",
                confidence=0.0,
                severity=Severity.INFO,
                evidence=visual_coverage.evidence,
                version=self.version,
                metadata={
                    "reason_code": "VISUAL_AUDIT_COVERAGE_INCOMPLETE",
                    "gate_verdicts": verdicts,
                    "major_prevalence_by_metric": major_prevalence_by_metric,
                    "visual_coverage": dict(visual_coverage.metadata),
                    "gate_owner_policy": "SCOPED_OWNER_WITH_VLM_CONFIRMATION",
                },
            )
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
    "V8InitialVisualCriterionOracle",
    "V8QualityReducerOracle",
    "V8TieredVisualCriterionOracle",
    "V8_BASE_ADDITIVE_METRICS",
    "V8_OBSERVATION_COMPOSITE_ID",
    "V8_QUALITY_VERSION",
    "V8_VISUAL_CRITERION_IDS",
    "V8_REDUCER_ORACLE_ID",
    "V8_SCENE_METRICS",
]
