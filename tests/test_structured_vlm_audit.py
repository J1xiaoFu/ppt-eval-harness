from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Mapping

import pytest

from ppt_eval.adapters import (
    ModelAuditModality,
    ModelAuditProvider,
    ModelAuditProviderError,
    ModelAuditRequest,
    PptxAdapter,
)
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.config import default_profile, load_profile
from ppt_eval.domain import (
    EvalCase,
    ExecutionStatus,
    MetricStatus,
    SceneType,
    ScoreRole,
)
from ppt_eval.oracles import (
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_VLM_VISUAL_CRITERIA,
    STRUCTURED_VLM_VISUAL_DIMENSION_METRICS,
    StructuredDimensionsModelAuditOracle,
    StructuredModelAuditOracle,
    StructuredVlmVisualAuditOracle,
    StructuredVlmVisualDimensionsAuditOracle,
    build_default_registry,
)
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

CRITERION_SCORES = {
    "composition_layout": 0.80,
    "typography_legibility": 0.60,
    "color_contrast": 0.40,
    "imagery_data_visualization": 0.90,
    "cross_slide_consistency": 0.70,
    "render_integrity": 0.50,
}
DIMENSION_METRIC_IDS = tuple(
    metric_id for _, metric_id in STRUCTURED_VLM_VISUAL_DIMENSION_METRICS
)


class StructuredFakeProvider:
    def __init__(
        self,
        *,
        global_score: float = 0.02,
        criterion_scores: Mapping[str, float] = CRITERION_SCORES,
        mutate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.global_score = global_score
        self.criterion_scores = dict(criterion_scores)
        self.mutate = mutate
        self.requests: list[ModelAuditRequest] = []

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        payload = deepcopy(
            _response(
                request,
                global_score=self.global_score,
                criterion_scores=self.criterion_scores,
            )
        )
        if self.mutate is not None:
            self.mutate(payload)
        return payload


def _response(
    request: ModelAuditRequest,
    *,
    global_score: float,
    criterion_scores: Mapping[str, float],
) -> dict[str, Any]:
    evidence = [
        {
            "evidence_id": f"summary-{criterion_id}",
            "kind": "criterion_summary",
            "message": f"Grounded summary for {criterion_id}.",
            "page_number": 1,
            "payload": {
                "criterion_id": criterion_id,
                "criterion_score": score,
                "criterion_confidence": 0.82,
                "criterion_observability": "FULL",
            },
        }
        for criterion_id, score in criterion_scores.items()
    ]
    return {
        "score": global_score,
        "confidence": 0.87,
        "model": {
            "provider": "fake",
            "model_id": "structured-vlm",
            "version": "test",
        },
        "prompt": dict(request.prompt.reference()),
        "usage": {"input_tokens": 100, "output_tokens": 50, "cost": 0.003},
        "evidence": evidence,
    }


def _context(tmp_path) -> EvaluationContext:
    deck = build_pptx(tmp_path / "deck.pptx")
    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    return EvaluationContext(
        case=EvalCase(
            case_id="structured-visual",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": (image,)},
        memo={},
    )


def _evaluate(tmp_path, provider: StructuredFakeProvider):
    return StructuredVlmVisualAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_context(tmp_path))


def _evaluate_dimensions(tmp_path, provider: ModelAuditProvider | None):
    return StructuredVlmVisualDimensionsAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_context(tmp_path))


def test_structured_vlm_accepts_one_grounded_summary_per_fixed_criterion(
    tmp_path,
) -> None:
    provider = StructuredFakeProvider()

    result = _evaluate(tmp_path, provider)

    assert result.execution_status == ExecutionStatus.SUCCESS
    assert result.metric_status == MetricStatus.SCORED
    assert result.metric_id == "structured_vlm_visual_audit"
    assert result.score_role == ScoreRole.BASE_ADDITIVE
    assert len(result.evidence) == len(STRUCTURED_VLM_VISUAL_CRITERIA) == 6
    assert result.confidence == 0.87
    assert result.cost == 0.003
    assert result.metadata["criterion_scores"] == CRITERION_SCORES
    assert result.metadata["scoring_mode"] == "HARNESS_WEIGHTED_CRITERIA"
    request = provider.requests[0]
    assert request.modality == ModelAuditModality.VLM
    assert request.metric_id == "structured_vlm_visual_audit"
    assert request.prompt.prompt_id == "ppt-vlm-structured-visual-quality-audit"
    assert request.prompt.version == "1.0.0"
    assert "\n    Do not make a final" not in request.prompt.instructions
    assert [image.page_number for image in request.images] == [1]


def test_structured_vlm_recomputes_score_and_ignores_model_global_score(
    tmp_path,
) -> None:
    provider = StructuredFakeProvider(global_score=0.02)

    result = _evaluate(tmp_path, provider)

    expected = sum(
        CRITERION_SCORES[criterion_id] * weight
        for criterion_id, weight in STRUCTURED_VLM_VISUAL_CRITERIA
    )
    assert result.normalized_score == pytest.approx(expected)
    assert result.normalized_score == pytest.approx(0.68)
    assert result.normalized_score != 0.02
    assert result.metadata["model_global_score"] == 0.02
    assert result.metadata["model_global_score_used"] is False
    assert result.metadata["harness_recomputed_score"] == pytest.approx(expected)


def test_structured_vlm_rejects_a_missing_criterion(tmp_path) -> None:
    scores = dict(CRITERION_SCORES)
    scores.pop("render_integrity")

    result = _evaluate(
        tmp_path,
        StructuredFakeProvider(criterion_scores=scores),
    )

    assert result.execution_status == ExecutionStatus.ERROR
    assert result.metric_status == MetricStatus.ERROR
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert "missing required criterion IDs: render_integrity" in (
        result.error_message or ""
    )


def test_structured_vlm_rejects_a_duplicate_criterion(tmp_path) -> None:
    def duplicate(payload: dict[str, Any]) -> None:
        repeated = deepcopy(payload["evidence"][0])
        repeated["evidence_id"] = "duplicate-composition-layout"
        payload["evidence"].append(repeated)

    result = _evaluate(
        tmp_path,
        StructuredFakeProvider(mutate=duplicate),
    )

    assert result.execution_status == ExecutionStatus.ERROR
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert "duplicates criterion_id 'composition_layout'" in (
        result.error_message or ""
    )


def test_structured_vlm_rejects_an_out_of_range_criterion_score(tmp_path) -> None:
    scores = {**CRITERION_SCORES, "color_contrast": 1.01}

    result = _evaluate(
        tmp_path,
        StructuredFakeProvider(criterion_scores=scores),
    )

    assert result.execution_status == ExecutionStatus.ERROR
    assert result.error_code == "MODEL_RESPONSE_INVALID"
    assert "criterion_score for 'color_contrast' must be a finite number in [0,1]" in (
        result.error_message or ""
    )


def test_structured_model_composite_is_registered_as_a_legacy_replacement() -> None:
    adapter = PptxAdapter(backend="ooxml")
    registry = build_default_registry(adapter)

    assert STRUCTURED_MODEL_AUDIT_COMPOSITE_ID == "structured.model_audits"
    assert registry.contains(STRUCTURED_MODEL_AUDIT_COMPOSITE_ID)
    assert registry.contains("high_cost.model_audits")
    descriptor = StructuredModelAuditOracle(adapter).describe()
    assert descriptor.oracle_id == STRUCTURED_MODEL_AUDIT_COMPOSITE_ID
    assert [metric.metric_id for metric in descriptor.metrics] == [
        "llm_content_quality_audit",
        "structured_vlm_visual_audit",
        "llm_scenario_compliance_audit",
    ]


def test_dimension_oracle_projects_one_call_into_six_scoreable_results(
    tmp_path,
) -> None:
    provider = StructuredFakeProvider(global_score=0.02)

    results = _evaluate_dimensions(tmp_path, provider)

    assert len(provider.requests) == 1
    assert provider.requests[0].audit_id == "structured_dimensions_vlm_audit_oracle"
    assert provider.requests[0].metric_id == "structured_vlm_visual_dimensions_batch"
    assert provider.requests[0].prompt.prompt_id == (
        "ppt-vlm-structured-visual-dimensions-audit"
    )
    assert provider.requests[0].prompt.version == "1.2.0"
    instructions = provider.requests[0].prompt.instructions
    normalized_instructions = " ".join(instructions.split())
    assert "mutually exclusive visual criteria" in normalized_instructions
    assert "same defect in another criterion_summary" in normalized_instructions
    assert "source belongs to the content audit" in normalized_instructions
    assert "0.25 means widespread major defects" in normalized_instructions
    assert "data-heavy deck with no usable visualization" in normalized_instructions
    assert "criterion_score must be numeric" in normalized_instructions
    assert "must be JSON null for INSUFFICIENT" in normalized_instructions
    assert "0.5 means repeated noticeable defects" in normalized_instructions
    assert "Aggregation weights belong only to the versioned Profile" in (
        normalized_instructions
    )
    assert "Harness weights" not in instructions
    assert tuple(result.metric_id for result in results) == DIMENSION_METRIC_IDS
    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert all(result.score_role == ScoreRole.BASE_ADDITIVE for result in results)
    assert all(result.confidence == pytest.approx(0.82) for result in results)
    assert {
        result.metadata["criterion_id"]: result.normalized_score
        for result in results
    } == CRITERION_SCORES
    assert sum(result.cost for result in results) == pytest.approx(0.003)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 150
    assert sum(
        bool(result.metadata["shared_call_usage_owner"]) for result in results
    ) == 1
    for result in results:
        assert result.metadata["model_global_score"] == 0.02
        assert result.metadata["model_global_score_used_for_metric"] is False
        assert result.metadata["dimension_batch_validated"] is True
        assert result.metadata["criterion_confidence"] == pytest.approx(0.82)
        assert result.metadata["criterion_observability"] == "FULL"
        assert result.metadata["criterion_score_used_for_metric"] is True
        assert result.metadata["cost_allocation_method"] == (
            "EQUAL_BY_OUTPUT_METRIC"
        )
        assert result.metadata["cost_allocation_fraction"] == pytest.approx(1 / 6)
        assert "criterion_weight" not in result.metadata
        assert "criterion_weights" not in result.metadata
        assert "criterion_contributions" not in result.metadata
        assert "harness_recomputed_score" not in result.metadata
        assert "harness_aggregate_score" not in result.metadata
        assert len(result.evidence) == 1
        assert result.evidence[0].payload["criterion_id"] == result.metadata[
            "criterion_id"
        ]


def test_dimension_oracle_fans_batch_contract_failure_out_to_all_metrics(
    tmp_path,
) -> None:
    scores = dict(CRITERION_SCORES)
    scores.pop("render_integrity")
    provider = StructuredFakeProvider(criterion_scores=scores)

    results = _evaluate_dimensions(tmp_path, provider)

    assert len(provider.requests) == 2
    assert tuple(result.metric_id for result in results) == DIMENSION_METRIC_IDS
    assert all(result.execution_status == ExecutionStatus.ERROR for result in results)
    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.error_code == "MODEL_RESPONSE_INVALID" for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.006)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 300
    assert sum(
        bool(result.metadata["shared_call_usage_owner"]) for result in results
    ) == 1
    assert all(
        result.metadata["criterion_contract_validated"] is False
        for result in results
    )
    assert all("response_fingerprint" in result.metadata for result in results)
    assert all(
        "missing required criterion IDs: render_integrity"
        in (result.error_message or "")
        for result in results
    )


def test_dimension_oracle_retries_criterion_contract_once_and_sums_usage(
    tmp_path,
) -> None:
    class MissingScoreThenValidProvider:
        def __init__(self) -> None:
            self.requests: list[ModelAuditRequest] = []

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.requests.append(request)
            payload = deepcopy(
                _response(
                    request,
                    global_score=0.5,
                    criterion_scores=CRITERION_SCORES,
                )
            )
            if len(self.requests) == 1:
                payload["evidence"][0]["payload"].pop("criterion_score")
            return payload

    provider = MissingScoreThenValidProvider()
    results = _evaluate_dimensions(tmp_path, provider)

    assert len(provider.requests) == 2
    assert provider.requests[0].fingerprint == provider.requests[1].fingerprint
    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.006)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 300
    assert all(result.metadata["criterion_retry_count"] == 1 for result in results)
    assert all(
        result.metadata["criterion_retry_usage_complete"] is True
        for result in results
    )


def test_dimension_oracle_rejects_two_invalid_criterion_responses_with_usage(
    tmp_path,
) -> None:
    class AlwaysMissingScoreProvider:
        def __init__(self) -> None:
            self.requests: list[ModelAuditRequest] = []

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.requests.append(request)
            payload = deepcopy(
                _response(
                    request,
                    global_score=0.5,
                    criterion_scores=CRITERION_SCORES,
                )
            )
            payload["evidence"][0]["payload"].pop("criterion_score")
            return payload

    provider = AlwaysMissingScoreProvider()
    results = _evaluate_dimensions(tmp_path, provider)

    assert len(provider.requests) == 2
    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.error_code == "MODEL_RESPONSE_INVALID" for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.006)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 300
    assert all(len(result.metadata["criterion_retry_errors"]) == 2 for result in results)


def test_dimension_oracle_preserves_usage_when_grounding_contract_fails(
    tmp_path,
) -> None:
    def move_evidence_outside_deck(payload: dict[str, Any]) -> None:
        payload["evidence"][0]["page_number"] = 2

    results = _evaluate_dimensions(
        tmp_path,
        StructuredFakeProvider(mutate=move_evidence_outside_deck),
    )

    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.error_code == "MODEL_RESPONSE_INVALID" for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.003)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 150
    assert all(
        result.metadata["telemetry_recovered_from_invalid_response"] is True
        for result in results
    )
    assert all(
        result.metadata["response_contract_validated"] is False
        for result in results
    )


def test_dimension_prompt_rejects_extra_findings_without_changing_v5_replay(
    tmp_path,
) -> None:
    def add_extra_finding(payload: dict[str, Any]) -> None:
        payload["evidence"].append(
            {
                "evidence_id": "extra-finding",
                "kind": "model_audit_finding",
                "message": "An optional detail outside the fixed summaries.",
                "page_number": 1,
                "payload": {},
            }
        )

    legacy = _evaluate(
        tmp_path,
        StructuredFakeProvider(mutate=add_extra_finding),
    )
    dimensions = _evaluate_dimensions(
        tmp_path,
        StructuredFakeProvider(mutate=add_extra_finding),
    )

    assert legacy.metric_status == MetricStatus.SCORED
    assert len(legacy.evidence) == 7
    assert all(result.metric_status == MetricStatus.ERROR for result in dimensions)
    assert all(result.error_code == "MODEL_RESPONSE_INVALID" for result in dimensions)
    assert all(
        "exactly six criterion_summary items" in (result.error_message or "")
        for result in dimensions
    )


def test_dimension_oracle_fans_unconfigured_provider_out_to_six_na(
    tmp_path,
) -> None:
    results = _evaluate_dimensions(tmp_path, None)

    assert tuple(result.metric_id for result in results) == DIMENSION_METRIC_IDS
    assert all(result.execution_status == ExecutionStatus.SUCCESS for result in results)
    assert all(result.metric_status == MetricStatus.NA for result in results)
    assert all(
        result.metadata["reason_code"] == "MODEL_PROVIDER_UNCONFIGURED"
        for result in results
    )
    assert sum(result.cost for result in results) == 0.0
    assert all("usage" not in result.metadata for result in results)


def test_dimension_oracle_fans_missing_render_input_out_to_six_na(
    tmp_path,
) -> None:
    provider = StructuredFakeProvider()
    context = _context(tmp_path)
    without_renders = EvaluationContext(
        case=context.case,
        profile=context.profile,
        artifacts={},
        memo={},
    )

    results = StructuredVlmVisualDimensionsAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(without_renders)

    assert provider.requests == []
    assert tuple(result.metric_id for result in results) == DIMENSION_METRIC_IDS
    assert all(result.execution_status == ExecutionStatus.SUCCESS for result in results)
    assert all(result.metric_status == MetricStatus.NA for result in results)
    assert all(
        result.metadata["reason_code"] == "RENDERED_SLIDES_UNAVAILABLE"
        for result in results
    )


def test_dimension_oracle_fans_provider_exception_out_to_six_errors(
    tmp_path,
) -> None:
    class FailingProvider:
        def __init__(self) -> None:
            self.requests: list[ModelAuditRequest] = []

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.requests.append(request)
            raise RuntimeError("synthetic provider failure")

    provider = FailingProvider()
    results = _evaluate_dimensions(tmp_path, provider)

    assert len(provider.requests) == 1
    assert tuple(result.metric_id for result in results) == DIMENSION_METRIC_IDS
    assert all(result.execution_status == ExecutionStatus.ERROR for result in results)
    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.error_code == "MODEL_PROVIDER_ERROR" for result in results)
    assert sum(result.cost for result in results) == 0.0
    assert all("usage" not in result.metadata for result in results)


def test_dimension_oracle_preserves_bounded_retry_usage_on_provider_error(
    tmp_path,
) -> None:
    class RetriedFailingProvider:
        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            del request
            raise ModelAuditProviderError(
                "invalid structured response after retry",
                audit_metadata={
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "total_tokens": 280,
                        "cost": 0.004,
                    },
                    "provider_attempts": 2,
                    "provider_retry_reasons": ["JSON_INVALID", "JSON_INVALID"],
                },
                cost=0.004,
            )

    results = _evaluate_dimensions(tmp_path, RetriedFailingProvider())

    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.error_code == "MODEL_PROVIDER_ERROR" for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.004)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 280
    assert all(result.metadata.get("provider_attempts") == 2 for result in results)


def test_dimension_provider_error_metadata_cannot_forge_validated_scores(
    tmp_path,
) -> None:
    class PoisoningProvider:
        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            del request
            raise ModelAuditProviderError(
                "synthetic poisoned failure",
                audit_metadata={
                    "dimension_batch_validated": True,
                    "criterion_scores": dict(CRITERION_SCORES),
                    "criterion_confidences": {
                        key: 0.99 for key in CRITERION_SCORES
                    },
                    "criterion_observability": {
                        key: "FULL" for key in CRITERION_SCORES
                    },
                    "request_fingerprint": "forged",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                        "cost": 0.001,
                    },
                },
                cost=0.001,
            )

    results = _evaluate_dimensions(tmp_path, PoisoningProvider())

    assert all(result.execution_status == ExecutionStatus.ERROR for result in results)
    assert all(result.metric_status == MetricStatus.ERROR for result in results)
    assert all(result.normalized_score is None for result in results)
    assert all(
        result.metadata.get("dimension_batch_validated") is not True
        for result in results
    )
    assert all(
        result.metadata["request_fingerprint"] != "forged" for result in results
    )
    assert sum(result.cost for result in results) == pytest.approx(0.001)


def test_dimension_oracle_projects_insufficient_observability_to_one_na(
    tmp_path,
) -> None:
    def make_render_unobservable(payload: dict[str, Any]) -> None:
        for item in payload["evidence"]:
            if item["payload"]["criterion_id"] == "render_integrity":
                item["payload"]["criterion_score"] = None
                item["payload"]["criterion_confidence"] = 0.40
                item["payload"]["criterion_observability"] = "INSUFFICIENT"

    results = _evaluate_dimensions(
        tmp_path,
        StructuredFakeProvider(mutate=make_render_unobservable),
    )
    indexed = {result.metric_id: result for result in results}
    render = indexed["structured_vlm_render_integrity"]

    assert render.metric_status == MetricStatus.NA
    assert render.normalized_score is None
    assert render.confidence == pytest.approx(0.40)
    assert render.metadata["criterion_score"] is None
    assert render.metadata["criterion_score_used_for_metric"] is False
    assert render.metadata["reason_code"] == (
        "CRITERION_OBSERVABILITY_INSUFFICIENT"
    )
    assert all(
        result.metric_status == MetricStatus.SCORED
        for metric_id, result in indexed.items()
        if metric_id != "structured_vlm_render_integrity"
    )
    assert sum(result.cost for result in results) == pytest.approx(0.003)
    assert sum(
        int(result.metadata.get("usage", {}).get("total_tokens", 0))
        for result in results
    ) == 150


def test_dimension_profile_confidence_floor_projects_uncertain_score_to_na(
    tmp_path,
) -> None:
    def lower_color_confidence(payload: dict[str, Any]) -> None:
        for item in payload["evidence"]:
            if item["payload"]["criterion_id"] == "color_contrast":
                item["payload"]["criterion_confidence"] = 0.59

    provider = StructuredFakeProvider(mutate=lower_color_confidence)
    base_context = _context(tmp_path)
    context = EvaluationContext(
        case=base_context.case,
        profile=load_profile(
            "configs/profiles/finished_deck_v6_structured_visual_dimensions_candidate.json"
        ),
        artifacts=base_context.artifacts,
        memo={},
    )

    results = StructuredVlmVisualDimensionsAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)
    color = next(
        result
        for result in results
        if result.metric_id == "structured_vlm_color_contrast"
    )

    assert color.metric_status == MetricStatus.NA
    assert color.normalized_score is None
    assert color.metadata["criterion_observability"] == "FULL"
    assert color.metadata["criterion_confidence"] == pytest.approx(0.59)
    assert color.metadata["criterion_confidence_floor"] == pytest.approx(0.60)
    assert color.metadata["criterion_score_used_for_metric"] is False
    assert color.metadata["reason_code"] == (
        "CRITERION_CONFIDENCE_BELOW_PROFILE_FLOOR"
    )


def test_dimension_profile_rejects_invalid_confidence_floor_before_provider_call(
    tmp_path,
) -> None:
    provider = StructuredFakeProvider()
    base_context = _context(tmp_path)
    invalid_profile = replace(
        base_context.profile,
        metadata={"vlm_dimension_min_confidence": 1.1},
    )
    context = EvaluationContext(
        case=base_context.case,
        profile=invalid_profile,
        artifacts=base_context.artifacts,
        memo={},
    )

    with pytest.raises(RuntimeError, match="must be in"):
        StructuredVlmVisualDimensionsAuditOracle(
            provider,
            PptxAdapter(backend="ooxml"),
        ).evaluate(context)

    assert provider.requests == []


def test_dimension_composite_is_versioned_without_changing_v5_aggregate() -> None:
    adapter = PptxAdapter(backend="ooxml")
    registry = build_default_registry(adapter)

    assert STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID == (
        "structured_dimensions.model_audits"
    )
    assert registry.contains(STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID)
    legacy = StructuredModelAuditOracle(adapter).describe()
    dimensions = StructuredDimensionsModelAuditOracle(adapter).describe()
    assert dimensions.version == "1.2.0"
    assert [metric.metric_id for metric in legacy.metrics] == [
        "llm_content_quality_audit",
        "structured_vlm_visual_audit",
        "llm_scenario_compliance_audit",
    ]
    assert [metric.metric_id for metric in dimensions.metrics] == [
        "llm_content_quality_audit",
        *DIMENSION_METRIC_IDS,
        "llm_scenario_compliance_audit",
    ]
    assert "structured_vlm_visual_audit" not in {
        metric.metric_id for metric in dimensions.metrics
    }
