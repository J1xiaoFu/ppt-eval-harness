from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

import pytest

from ppt_eval.adapters import ModelAuditModality, ModelAuditRequest, PptxAdapter
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.config import default_profile
from ppt_eval.domain import (
    EvalCase,
    ExecutionStatus,
    MetricStatus,
    SceneType,
    ScoreRole,
)
from ppt_eval.oracles import (
    STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_VLM_VISUAL_CRITERIA,
    StructuredModelAuditOracle,
    StructuredVlmVisualAuditOracle,
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
