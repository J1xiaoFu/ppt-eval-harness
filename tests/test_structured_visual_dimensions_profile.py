from __future__ import annotations

import json
import math
from pathlib import Path

from ppt_eval.application import RunSupervisor
from ppt_eval.config import default_profile, load_profile, profile_from_mapping
from ppt_eval.domain import (
    CONSTRUCT_WEIGHTED_MEAN,
    CoverageStatus,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreBreakdown,
    ScoreRole,
)
from ppt_eval.scoring import DecisionPolicy, PptPdmsAggregator

PROFILE_PATH = Path(
    "configs/profiles/finished_deck_v6_structured_visual_dimensions_candidate.json"
)
QWEN38_AB_PROFILE_PATH = Path(
    "configs/profiles/finished_deck_v6_structured_visual_dimensions_qwen38_ab.json"
)
V5_PROFILE_PATH = Path(
    "configs/profiles/finished_deck_v5_structured_visual_candidate.json"
)
LEGACY_VLM_METRIC = "vlm_visual_quality_audit"
V5_AGGREGATE_METRIC = "structured_vlm_visual_audit"
STRUCTURED_DIMENSION_METRICS = {
    "structured_vlm_composition_layout",
    "structured_vlm_typography_legibility",
    "structured_vlm_color_contrast",
    "structured_vlm_imagery_data_visualization",
    "structured_vlm_cross_slide_consistency",
    "structured_vlm_render_integrity",
}
OVERLAPPING_DIMENSIONS = {
    "structured_vlm_composition_layout",
    "structured_vlm_typography_legibility",
    "structured_vlm_cross_slide_consistency",
}
INCREMENTAL_DIMENSIONS = STRUCTURED_DIMENSION_METRICS - OVERLAPPING_DIMENSIONS


def test_qwen38_ab_profile_changes_only_identity_and_flash_model() -> None:
    baseline = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    qwen38 = json.loads(QWEN38_AB_PROFILE_PATH.read_text(encoding="utf-8"))

    assert baseline["metadata"]["flash_model"] == "qwen3.7-flash"
    assert qwen38["metadata"]["flash_model"] == "qwen3.8-flash"
    assert qwen38["profile_id"].endswith("qwen38-ab")
    assert qwen38["version"] == "6.0-qwen38-ab"

    for payload in (baseline, qwen38):
        payload.pop("profile_id")
        payload.pop("version")
        payload["metadata"].pop("flash_model")
    assert qwen38 == baseline


def test_v6_profile_loads_without_replacing_v3_or_v5() -> None:
    candidate = load_profile(PROFILE_PATH)
    v5 = load_profile(V5_PROFILE_PATH)
    default = default_profile(SceneType.READY_MADE)

    assert candidate.profile_id == (
        "finished-deck-v6-structured-visual-dimensions-candidate"
    )
    assert candidate.version == "6.0"
    assert candidate.scene == SceneType.READY_MADE
    assert candidate.aggregation_strategy == CONSTRUCT_WEIGHTED_MEAN
    assert candidate.base_construct_weights["content"] == (
        v5.base_construct_weights["content"]
    )
    assert candidate.base_construct_weights["delivery"] == (
        v5.base_construct_weights["delivery"]
    )
    assert candidate.base_construct_weights["handoff"] == (
        v5.base_construct_weights["handoff"]
    )
    assert math.isclose(
        candidate.base_construct_weights["visual_deterministic"]
        + candidate.base_construct_weights["visual_vlm"],
        v5.base_construct_weights["visual"],
        abs_tol=1e-12,
    )
    assert v5.enabled_oracle_ids == (
        "baseline_ppt_quality",
        "structured.model_audits",
    )
    assert V5_AGGREGATE_METRIC in v5.base_weights
    assert default.profile_id == "finished-deck-v8"
    assert default.version == "8.2"


def test_v6_uses_only_versioned_six_dimension_model_composite() -> None:
    candidate = load_profile(PROFILE_PATH)

    assert candidate.enabled_oracle_ids == (
        "baseline_ppt_quality",
        "structured_dimensions.model_audits",
    )
    assert "high_cost.model_audits" not in candidate.enabled_oracle_ids
    assert "structured.model_audits" not in candidate.enabled_oracle_ids
    assert STRUCTURED_DIMENSION_METRICS <= set(candidate.base_weights)
    assert STRUCTURED_DIMENSION_METRICS <= set(
        candidate.required_metric_ids or ()
    )
    assert LEGACY_VLM_METRIC not in candidate.base_weights
    assert V5_AGGREGATE_METRIC not in candidate.base_weights
    assert LEGACY_VLM_METRIC not in (candidate.required_metric_ids or ())
    assert V5_AGGREGATE_METRIC not in (candidate.required_metric_ids or ())


def test_v6_six_dimensions_share_twenty_percent_visual_budget() -> None:
    candidate = load_profile(PROFILE_PATH)
    deterministic_weight = candidate.base_construct_weights[
        "visual_deterministic"
    ]
    model_weight = candidate.base_construct_weights["visual_vlm"]
    model_share = model_weight / (deterministic_weight + model_weight)

    assert math.isclose(model_share, 0.20, abs_tol=1e-10)
    assert math.isclose(model_weight, 0.10, abs_tol=1e-10)
    assert {
        candidate.base_metric_constructs[metric_id]
        for metric_id in STRUCTURED_DIMENSION_METRICS
    } == {"visual_vlm"}
    assert {
        candidate.base_metric_constructs[metric_id]
        for metric_id in (
            "visual_hierarchy",
            "layout",
            "typography",
            "style_consistency",
            "multimedia_quality",
        )
    } == {"visual_deterministic"}


def test_v6_overlap_audit_allocates_more_to_incremental_dimensions() -> None:
    candidate = load_profile(PROFILE_PATH)
    model_weight = math.fsum(
        candidate.base_weights[item] for item in STRUCTURED_DIMENSION_METRICS
    )
    actual_budget = {
        metric_id: candidate.base_weights[metric_id] / model_weight
        for metric_id in STRUCTURED_DIMENSION_METRICS
    }
    expected_budget = {
        "structured_vlm_composition_layout": 0.10,
        "structured_vlm_typography_legibility": 0.10,
        "structured_vlm_color_contrast": 0.20,
        "structured_vlm_imagery_data_visualization": 0.25,
        "structured_vlm_cross_slide_consistency": 0.10,
        "structured_vlm_render_integrity": 0.25,
    }

    for metric_id, expected in expected_budget.items():
        assert math.isclose(actual_budget[metric_id], expected, abs_tol=1e-10)
    assert math.isclose(
        math.fsum(actual_budget[item] for item in OVERLAPPING_DIMENSIONS),
        0.30,
        abs_tol=1e-10,
    )
    assert math.isclose(
        math.fsum(actual_budget[item] for item in INCREMENTAL_DIMENSIONS),
        0.70,
        abs_tol=1e-10,
    )


def test_v6_only_legibility_and_render_integrity_have_model_floors() -> None:
    candidate = load_profile(PROFILE_PATH)
    model_floors = {
        metric_id: threshold
        for metric_id, threshold in candidate.metric_review_thresholds.items()
        if metric_id in STRUCTURED_DIMENSION_METRICS
    }

    assert model_floors == {
        "structured_vlm_typography_legibility": 0.70,
        "structured_vlm_render_integrity": 0.70,
    }


def test_v6_is_unvalidated_and_weights_are_not_rank_fitted() -> None:
    candidate = load_profile(PROFILE_PATH)

    assert candidate.metadata["lifecycle"] == "EXPERIMENTAL"
    assert candidate.metadata["calibration_status"] == "UNVALIDATED"
    assert candidate.metadata["production_approved"] is False
    assert candidate.metadata["weight_selection_basis"] == (
        "PRE_REGISTERED_STRONG_PIXEL_EVIDENCE_PRIOR"
    )
    assert candidate.metadata["rank_fit_used"] is False
    assert candidate.metadata["cost_weight_decoupled"] is True
    assert candidate.metadata["vlm_visual_construct_share"] == 0.20
    assert candidate.metadata["vlm_overall_score_share"] == 0.10
    assert candidate.metadata["vlm_share_semantics"] == (
        "HARD_CAP_VIA_SEPARATE_CONSTRUCT"
    )
    assert candidate.metadata["vlm_dimension_min_confidence"] == 0.60


def test_v6_hard_cap_survives_optional_multimedia_na() -> None:
    candidate = load_profile(PROFILE_PATH)
    results: list[OracleResult] = []
    for metric_id in candidate.base_weights:
        if metric_id == "multimedia_quality":
            results.append(
                OracleResult(
                    oracle_id="test",
                    metric_id=metric_id,
                    execution_status=ExecutionStatus.SUCCESS,
                    metric_status=MetricStatus.NA,
                    score_role=ScoreRole.BASE_ADDITIVE,
                )
            )
            continue
        score = 0.0 if metric_id in STRUCTURED_DIMENSION_METRICS else 1.0
        results.append(
            OracleResult(
                oracle_id="test",
                metric_id=metric_id,
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.SCORED,
                score_role=ScoreRole.BASE_ADDITIVE,
                normalized_score=score,
            )
        )
    for metric_id in candidate.base_multiplier_metric_ids:
        results.append(
            OracleResult(
                oracle_id="test",
                metric_id=metric_id,
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.PASS,
                score_role=ScoreRole.BASE_MULTIPLIER,
                multiplier=1.0,
            )
        )

    breakdown = PptPdmsAggregator().aggregate(candidate, results)

    assert breakdown.coverage == CoverageStatus.FULL
    assert breakdown.base_construct_scores["visual_deterministic"] == 1.0
    assert breakdown.base_construct_scores["visual_vlm"] == 0.0
    assert math.isclose(breakdown.base_score or 0.0, 90.0, abs_tol=1e-9)


def test_v6_routes_low_dimension_floors_directly_to_human_review() -> None:
    candidate = load_profile(PROFILE_PATH)

    assert candidate.metadata["model_audit_routing"] == (
        "STRUCTURED_DIMENSIONS_FLASH_ONLY"
    )
    assert candidate.metadata["advanced_routing_status"] == (
        "PENDING_DIMENSION_ISOMORPHIC_ADVANCED_VLM"
    )
    assert candidate.metadata["flash_model"] == "qwen3.7-flash"
    assert candidate.metadata["advanced_model"] == "qwen3.8-flash"
    assert RunSupervisor._tiered_model_audit_enabled(candidate) is False

    low_legibility = OracleResult(
        oracle_id="structured_dimensions_vlm_audit_oracle",
        metric_id="structured_vlm_typography_legibility",
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        score_role=ScoreRole.BASE_ADDITIVE,
        normalized_score=0.69,
        confidence=0.90,
    )
    breakdown = ScoreBreakdown(
        base_additive=0.90,
        scene_additive=None,
        base_multiplier=1.0,
        scene_multiplier=1.0,
        base_score=90.0,
        full_score=90.0,
        coverage=CoverageStatus.FULL,
        base_complete=True,
        scene_complete=True,
    )
    decision, reasons = DecisionPolicy().decide(
        candidate,
        breakdown,
        (low_legibility,),
    )

    assert decision == EvaluationDecision.REVIEW
    assert reasons == (
        "metric_floor_review:structured_vlm_typography_legibility",
    )


def test_v6_validation_rejects_an_unassigned_dimension() -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    missing_metric = "structured_vlm_color_contrast"
    del payload["base_metric_constructs"][missing_metric]

    try:
        profile_from_mapping(payload)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("an unassigned structured dimension must be rejected")

    assert "base_metric_constructs must assign each positive metric once" in message
    assert missing_metric in message
