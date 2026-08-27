from __future__ import annotations

import json
from pathlib import Path

from ppt_eval.config import default_profile, load_profile, profile_from_mapping
from ppt_eval.domain import CONSTRUCT_WEIGHTED_MEAN, SceneType

PROFILE_PATH = Path(
    "configs/profiles/finished_deck_v5_structured_visual_candidate.json"
)
LEGACY_VLM_METRIC = "vlm_visual_quality_audit"
STRUCTURED_VLM_METRIC = "structured_vlm_visual_audit"


def test_v5_structured_visual_profile_loads_and_preserves_construct_budgets() -> None:
    candidate = load_profile(PROFILE_PATH)
    predecessor = load_profile(
        "configs/profiles/finished_deck_v4_construct_candidate.json"
    )

    assert candidate.profile_id == "finished-deck-v5-structured-visual-candidate"
    assert candidate.version == "5.0"
    assert candidate.scene == SceneType.READY_MADE
    assert candidate.aggregation_strategy == CONSTRUCT_WEIGHTED_MEAN
    assert candidate.base_construct_weights == predecessor.base_construct_weights
    assert candidate.base_construct_weights == {
        "content": 0.265625,
        "visual": 0.5,
        "delivery": 0.171875,
        "handoff": 0.0625,
    }


def test_v5_replaces_legacy_vlm_without_double_execution_or_scoring() -> None:
    candidate = load_profile(PROFILE_PATH)

    assert candidate.enabled_oracle_ids == (
        "baseline_ppt_quality",
        "structured.model_audits",
    )
    assert "high_cost.model_audits" not in candidate.enabled_oracle_ids
    assert "structured_visual.model_audits" not in candidate.enabled_oracle_ids

    configured_metric_ids = {
        *candidate.base_weights,
        *candidate.scene_weights,
        *candidate.base_multiplier_metric_ids,
        *candidate.scene_multiplier_metric_ids,
    }
    assert STRUCTURED_VLM_METRIC in configured_metric_ids
    assert STRUCTURED_VLM_METRIC in (candidate.required_metric_ids or ())
    assert candidate.metric_review_thresholds[STRUCTURED_VLM_METRIC] == 0.70
    assert LEGACY_VLM_METRIC not in configured_metric_ids
    assert LEGACY_VLM_METRIC not in (candidate.required_metric_ids or ())
    assert LEGACY_VLM_METRIC not in candidate.metric_review_thresholds
    assert LEGACY_VLM_METRIC not in candidate.base_metric_constructs


def test_v5_caps_structured_vlm_at_ten_percent_of_visual_construct() -> None:
    candidate = load_profile(PROFILE_PATH)
    visual_metrics = {
        metric_id
        for metric_id, construct in candidate.base_metric_constructs.items()
        if construct == "visual"
    }
    visual_weight = sum(candidate.base_weights[item] for item in visual_metrics)
    structured_share = candidate.base_weights[STRUCTURED_VLM_METRIC] / visual_weight

    assert round(structured_share, 8) == 0.10
    assert round(
        structured_share * candidate.base_construct_weights["visual"], 8
    ) == 0.05


def test_v5_remains_unvalidated_and_does_not_replace_default_v3() -> None:
    candidate = load_profile(PROFILE_PATH)
    default = default_profile(SceneType.READY_MADE)

    assert candidate.metadata["lifecycle"] == "EXPERIMENTAL"
    assert candidate.metadata["calibration_status"] == "UNVALIDATED"
    assert candidate.metadata["production_approved"] is False
    assert candidate.metadata["model_audit_routing"] == "STRUCTURED_FLASH_ONLY"
    assert candidate.metadata["advanced_routing_status"] == (
        "PENDING_CRITERION_ISOMORPHIC_PLUS"
    )
    assert candidate.metadata["diagnostic_metrics"] == ["body_completeness"]
    assert default.profile_id == "finished-deck-v8"
    assert default.version == "8.0"
    assert default.aggregation_strategy != CONSTRUCT_WEIGHTED_MEAN
    assert LEGACY_VLM_METRIC not in default.base_weights
    assert "composition_craft" in default.base_weights
    assert STRUCTURED_VLM_METRIC not in default.base_weights


def test_v5_construct_validation_rejects_unassigned_structured_metric() -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    del payload["base_metric_constructs"][STRUCTURED_VLM_METRIC]

    try:
        profile_from_mapping(payload)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing structured metric construct must be rejected")

    assert "base_metric_constructs must assign each positive metric once" in message
    assert STRUCTURED_VLM_METRIC in message
