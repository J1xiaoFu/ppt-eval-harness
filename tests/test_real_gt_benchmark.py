from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from ppt_eval.config import load_profile
from scripts.benchmarks.evaluate_slides_align_sample import (
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_DIMENSIONS_VLM_ORACLE_ID,
    STRUCTURED_VLM_DIMENSION_IDS,
    STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
    STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT,
    atomic_model_routing_events,
    average_ranks,
    case_html,
    pairwise_accuracy,
    selected_metrics,
    spearman,
    structured_visual_case_analysis,
    structured_visual_replay_analysis,
    structured_visual_sensitivity_scores,
    visual_proxy,
)

DETERMINISTIC_VISUAL_IDS = (
    "visual_hierarchy",
    "layout",
    "typography",
    "style_consistency",
    "multimedia_quality",
)


def _v6_profile():
    return load_profile("configs/profiles/finished_deck_v6_structured_visual_dimensions_candidate.json")


def _scored_result(metric_id: str, score: float) -> dict[str, object]:
    return {
        "oracle_id": "baseline_ppt_quality",
        "metric_id": metric_id,
        "version": "2.1.0",
        "metric_status": "SCORED",
        "score_role": "BASE_ADDITIVE",
        "normalized_score": score,
        "confidence": 1.0,
        "cost": 0.0,
        "evidence": [],
        "metadata": {},
    }


def _dimension_results(score: float = 0.5) -> list[dict[str, object]]:
    prompt = dict(STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT.reference())
    owner = STRUCTURED_VLM_DIMENSION_IDS[0]
    results: list[dict[str, object]] = []
    for index, metric_id in enumerate(STRUCTURED_VLM_DIMENSION_IDS):
        criterion_id = metric_id.removeprefix("structured_vlm_")
        metadata: dict[str, object] = {
            "audit_type": "model",
            "prompt": prompt,
            "structured_contract_version": (STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION),
            "output_mode": "SINGLE_CALL_DIMENSION_PROJECTION",
            "batch_request_metric_id": "structured_vlm_visual_dimensions_batch",
            "dimension_batch_validated": True,
            "criterion_id": criterion_id,
            "criterion_score": score,
            "criterion_confidence": 0.8,
            "criterion_confidence_floor": 0.6,
            "criterion_observability": "FULL",
            "criterion_score_used_for_metric": True,
            "model_global_score_used_for_metric": False,
            "request_fingerprint": "a" * 64,
            "response_fingerprint": "b" * 64,
            "model": {"model_id": "qwen3.7-flash"},
            "shared_call_usage_owner_metric_id": owner,
            "shared_call_usage_owner": metric_id == owner,
            "cost_allocation_method": "EQUAL_BY_OUTPUT_METRIC",
            "cost_allocation_fraction": 1 / 6,
            "allocated_cost": 1 / 6,
        }
        if metric_id == owner:
            metadata["usage"] = {"total_tokens": 100, "cost": 1.0}
        results.append(
            {
                "oracle_id": STRUCTURED_DIMENSIONS_VLM_ORACLE_ID,
                "metric_id": metric_id,
                "version": STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
                "metric_status": "SCORED",
                "score_role": "BASE_ADDITIVE",
                "normalized_score": score,
                "confidence": 0.8,
                "cost": 1 / 6,
                "evidence": [
                    {
                        "kind": "criterion_summary",
                        "payload": {"criterion_id": criterion_id},
                    }
                ],
                "metadata": metadata,
            }
        )
    return results


def _v6_report(
    *,
    coverage: str = "FULL",
    case_id: str = "case-0",
) -> dict[str, object]:
    profile = _v6_profile()
    results = [
        *(_scored_result(metric_id, 0.8) for metric_id in DETERMINISTIC_VISUAL_IDS),
        *_dimension_results(),
    ]
    prompt = STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT.reference()
    prompt_version = f"{prompt['prompt_id']}@{prompt['version']}#{prompt['sha256']}"
    return {
        "case_id": case_id,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "coverage": coverage,
        "results": results,
        "score_breakdown": {
            "base_construct_scores": {
                "visual_deterministic": 0.8,
                "visual_vlm": 0.5,
            }
        },
        "manifest": {
            "case_id": case_id,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "oracle_versions": {
                STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID: (STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION)
            },
            "prompt_versions": {metric_id: prompt_version for metric_id in STRUCTURED_VLM_DIMENSION_IDS},
            "model_versions": {
                metric_id: "test/qwen3.7-flash@qwen3.7-flash" for metric_id in STRUCTURED_VLM_DIMENSION_IDS
            },
        },
    }


def _expected_cases() -> tuple[tuple[str, str, int], ...]:
    return tuple((f"case-{index}", f"product-{index}", index + 1) for index in range(7))


def test_rank_statistics_match_human_order_and_handle_ties() -> None:
    human_utility = [-2.0, -3.0, -6.0]
    aligned_scores = [88.9, 86.1, 79.5]
    reversed_scores = list(reversed(aligned_scores))

    assert average_ranks([1.0, 1.0, 3.0]) == [1.5, 1.5, 3.0]
    assert round(spearman(aligned_scores, human_utility) or 0.0, 12) == 1.0
    assert round(spearman(reversed_scores, human_utility) or 0.0, 12) == -1.0
    assert pairwise_accuracy([2, 3, 6], aligned_scores) == 1.0
    assert pairwise_accuracy([2, 3, 6], reversed_scores) == 0.0
    assert pairwise_accuracy([2, 3], [50.0, 50.0]) == 0.5


def test_visual_proxy_renormalizes_only_available_registered_metrics() -> None:
    results = {
        "visual_hierarchy": {"normalized_score": 0.5},
        "layout": {"normalized_score": 1.0},
        "typography": {"normalized_score": None},
    }

    assert visual_proxy(results) == 75.0


def test_v6_visual_sensitivity_uses_preregistered_shares_only() -> None:
    profile = _v6_profile()
    results = {item["metric_id"]: item for item in _v6_report()["results"]}

    analysis = structured_visual_sensitivity_scores(results, profile)

    assert analysis["deterministic_visual_score"] == pytest.approx(0.8)
    assert analysis["structured_vlm_score"] == pytest.approx(0.5)
    assert analysis["scores_by_vlm_share"] == pytest.approx(
        {"0.10": 0.77, "0.15": 0.755, "0.20": 0.74, "0.25": 0.725}
    )


def test_v6_visual_sensitivity_excludes_delivery_and_handoff() -> None:
    profile = _v6_profile()
    base_results = {item["metric_id"]: item for item in _v6_report()["results"]}
    with_nonvisual_extremes = {
        **base_results,
        "editability": _scored_result("editability", 0.0),
        "compatibility": _scored_result("compatibility", 1.0),
        "accessibility": _scored_result("accessibility", 0.0),
        "template_residue": _scored_result("template_residue", 0.0),
    }

    assert (
        structured_visual_sensitivity_scores(
            base_results,
            profile,
        )["scores_by_vlm_share"]
        == structured_visual_sensitivity_scores(
            with_nonvisual_extremes,
            profile,
        )["scores_by_vlm_share"]
    )


def test_v6_visual_sensitivity_matches_optional_metric_renormalization() -> None:
    profile = _v6_profile()
    results = {item["metric_id"]: item for item in _v6_report()["results"]}
    results["multimedia_quality"] = {
        **results["multimedia_quality"],
        "metric_status": "NA",
        "normalized_score": None,
    }

    analysis = structured_visual_sensitivity_scores(results, profile)

    assert analysis["deterministic_visual_score"] == pytest.approx(0.8)
    assert analysis["missing_deterministic_metric_ids"] == ["multimedia_quality"]
    assert analysis["scores_by_vlm_share"]["0.10"] == pytest.approx(0.77)
    assert analysis["effective_vlm_share_by_nominal_share"]["0.10"] == 0.10


def test_v6_visual_hard_cap_survives_optional_deterministic_na() -> None:
    profile = _v6_profile()
    results = {
        item["metric_id"]: item
        for item in [
            *(_scored_result(metric_id, 1.0) for metric_id in DETERMINISTIC_VISUAL_IDS),
            *_dimension_results(score=0.0),
        ]
    }
    results["multimedia_quality"] = {
        **results["multimedia_quality"],
        "metric_status": "NA",
        "normalized_score": None,
    }

    analysis = structured_visual_sensitivity_scores(results, profile)

    assert analysis["deterministic_visual_score"] == 1.0
    assert analysis["structured_vlm_score"] == 0.0
    assert analysis["scores_by_vlm_share"]["0.20"] == pytest.approx(0.80)
    assert analysis["effective_vlm_share_by_nominal_share"]["0.20"] == 0.20


def test_v6_profile_budget_mismatch_is_rejected() -> None:
    profile = _v6_profile()
    weights = dict(profile.base_weights)
    weights[STRUCTURED_VLM_DIMENSION_IDS[0]] *= 2
    mismatched = replace(profile, base_weights=weights)
    results = {item["metric_id"]: item for item in _v6_report()["results"]}

    with pytest.raises(ValueError, match="disagree"):
        structured_visual_sensitivity_scores(results, mismatched)


def test_v6_replay_requires_all_seven_full_contract_aligned_cases() -> None:
    profile = _v6_profile()
    cases = []
    for index in range(7):
        report = _v6_report(case_id=f"case-{index}")
        cases.append(
            {
                "case_id": f"case-{index}",
                "product": f"product-{index}",
                "human_rank": index + 1,
                "coverage": "FULL",
                "structured_visual_analysis": structured_visual_case_analysis(
                    report,
                    profile,
                    expected_case_id=f"case-{index}",
                ),
            }
        )

    replay = structured_visual_replay_analysis(
        cases,
        profile,
        expected_cases=_expected_cases(),
    )

    assert replay is not None
    assert replay["eligible"] is True
    assert replay["full_case_count"] == 7
    assert replay["complete_six_dimension_case_count"] == 7
    assert replay["comparable_pairs"] == 21
    assert replay["statistics"]["0.10"]["analysis_role"] == "SENSITIVITY_ONLY"
    assert replay["statistics"]["0.15"]["analysis_role"] == "SENSITIVITY_ONLY"
    assert replay["statistics"]["0.20"]["analysis_role"] == (
        "PREREGISTERED_PRIMARY"
    )
    assert replay["statistics"]["0.25"]["analysis_role"] == "SENSITIVITY_ONLY"
    assert replay["delivery_handoff_used_for_rank_calibration"] is False
    assert replay["rank_fit_used"] is False


def test_v6_replay_suppresses_statistics_for_ineligible_cases() -> None:
    profile = _v6_profile()
    for failure in ("missing_dimension", "non_full", "stale_profile", "stale_case"):
        reports = [_v6_report(case_id=f"case-{index}") for index in range(7)]
        if failure == "missing_dimension":
            reports[0]["results"] = [
                item
                for item in reports[0]["results"]
                if item["metric_id"] != STRUCTURED_VLM_DIMENSION_IDS[-1]
            ]
        elif failure == "non_full":
            reports[0]["coverage"] = "DEGRADED"
        elif failure == "stale_profile":
            reports[0]["profile_version"] = "6.0-stale"
        else:
            reports[0]["case_id"] = "different-case"
        cases = [
            {
                "case_id": f"case-{index}",
                "product": f"product-{index}",
                "human_rank": index + 1,
                "coverage": report["coverage"],
                "structured_visual_analysis": structured_visual_case_analysis(
                    report,
                    profile,
                    expected_case_id=f"case-{index}",
                ),
            }
            for index, report in enumerate(reports)
        ]

        replay = structured_visual_replay_analysis(
            cases,
            profile,
            expected_cases=_expected_cases(),
        )

        assert replay is not None, failure
        assert replay["eligible"] is False, failure
        assert replay["comparable_pairs"] == 0, failure
        assert replay["statistics"]["0.10"][
            "spearman_visual_construct_vs_human"
        ] is None, failure
        assert replay["statistics"]["0.20"][
            "pairwise_visual_construct_accuracy"
        ] is None, failure


def test_v6_replay_rejects_stale_prompt_and_duplicate_usage() -> None:
    profile = _v6_profile()
    report = deepcopy(_v6_report())
    dimensions = [item for item in report["results"] if item["metric_id"] in STRUCTURED_VLM_DIMENSION_IDS]
    dimensions[0]["metadata"]["prompt"]["version"] = "1.0.0"
    dimensions[1]["metadata"]["usage"] = {"total_tokens": 100}

    analysis = structured_visual_case_analysis(
        report,
        profile,
        expected_case_id="case-0",
    )

    assert analysis["eligible"] is False
    assert analysis["oracle_projection_contract_ok"] is False
    assert any(
        failure.endswith(":prompt_reference") for failure in analysis["oracle_projection_contract_failures"]
    )
    assert any(
        failure.endswith(":duplicate_usage") for failure in analysis["oracle_projection_contract_failures"]
    )


def test_v6_replay_treats_insufficient_dimension_as_valid_na_projection() -> None:
    profile = _v6_profile()
    report = deepcopy(_v6_report())
    dimension = next(
        result
        for result in report["results"]
        if result["metric_id"] == STRUCTURED_VLM_DIMENSION_IDS[-1]
    )
    dimension["metric_status"] = "NA"
    dimension["normalized_score"] = None
    dimension["metadata"]["criterion_score"] = None
    dimension["metadata"]["criterion_observability"] = "INSUFFICIENT"
    dimension["metadata"]["criterion_score_used_for_metric"] = False
    dimension["metadata"]["reason_code"] = (
        "CRITERION_OBSERVABILITY_INSUFFICIENT"
    )
    report["coverage"] = "DEGRADED"

    analysis = structured_visual_case_analysis(
        report,
        profile,
        expected_case_id="case-0",
    )

    assert analysis["eligible"] is False
    assert analysis["all_dimensions_scored"] is False
    assert analysis["oracle_projection_contract_ok"] is True
    assert analysis["criterion_observability"][STRUCTURED_VLM_DIMENSION_IDS[-1]] == (
        "INSUFFICIENT"
    )


def test_v6_replay_treats_low_confidence_dimension_as_valid_na_projection() -> None:
    profile = _v6_profile()
    report = deepcopy(_v6_report())
    dimension = next(
        result
        for result in report["results"]
        if result["metric_id"] == STRUCTURED_VLM_DIMENSION_IDS[2]
    )
    dimension["metric_status"] = "NA"
    dimension["normalized_score"] = None
    dimension["confidence"] = 0.59
    dimension["metadata"]["criterion_confidence"] = 0.59
    dimension["metadata"]["criterion_score_used_for_metric"] = False
    dimension["metadata"]["reason_code"] = (
        "CRITERION_CONFIDENCE_BELOW_PROFILE_FLOOR"
    )
    report["coverage"] = "DEGRADED"

    analysis = structured_visual_case_analysis(
        report,
        profile,
        expected_case_id="case-0",
    )

    assert analysis["eligible"] is False
    assert analysis["all_dimensions_scored"] is False
    assert analysis["oracle_projection_contract_ok"] is True
    assert analysis["criterion_confidences"][STRUCTURED_VLM_DIMENSION_IDS[2]] == (
        pytest.approx(0.59)
    )


def test_v6_replay_rejects_stale_manifest_prompt_version() -> None:
    profile = _v6_profile()
    report = deepcopy(_v6_report())
    report["manifest"]["prompt_versions"][STRUCTURED_VLM_DIMENSION_IDS[0]] = (
        "ppt-vlm-structured-visual-dimensions-audit@1.0.0#" + "0" * 64
    )

    analysis = structured_visual_case_analysis(
        report,
        profile,
        expected_case_id="case-0",
    )

    assert analysis["eligible"] is False
    assert analysis["manifest_contract_ok"] is False
    assert analysis["manifest_contract_failures"] == ["manifest:prompt_versions"]


def test_v6_replay_accepts_vendor_model_version_suffix() -> None:
    profile = _v6_profile()
    report = deepcopy(_v6_report())
    actual_model = "qwen3.7-flash-2026-08-01"
    for result in report["results"]:
        if result["metric_id"] in STRUCTURED_VLM_DIMENSION_IDS:
            result["metadata"]["model"]["model_id"] = actual_model
            report["manifest"]["model_versions"][result["metric_id"]] = (
                f"test/{actual_model}@{actual_model}"
            )
    next(
        result
        for result in report["results"]
        if result["metric_id"] == STRUCTURED_VLM_DIMENSION_IDS[1]
    )["metadata"]["criterion_observability"] = "PARTIAL"

    analysis = structured_visual_case_analysis(
        report,
        profile,
        expected_case_id="case-0",
    )

    assert analysis["eligible"] is True
    assert analysis["model_ids"] == [actual_model]


def test_v6_replay_rejects_manifest_identity_substitution() -> None:
    profile = _v6_profile()
    cases = [
        {
            "case_id": f"case-{index}",
            "product": f"product-{index}",
            "human_rank": index + 1,
            "coverage": "FULL",
            "structured_visual_analysis": structured_visual_case_analysis(
                _v6_report(case_id=f"case-{index}"),
                profile,
                expected_case_id=f"case-{index}",
            ),
        }
        for index in range(7)
    ]
    substituted = list(_expected_cases())
    substituted[-1] = ("different-case", "product-6", 7)

    replay = structured_visual_replay_analysis(
        cases,
        profile,
        expected_cases=substituted,
    )

    assert replay is not None
    assert replay["eligible"] is False
    assert replay["manifest_identity_match"] is False
    assert replay["comparable_pairs"] == 0
    assert replay["missing_manifest_identities"] == [("different-case", "product-6", 7)]


def test_ineligible_v6_case_html_hides_order_and_rank_delta() -> None:
    item = {
        "case_id": "case-0",
        "product": "product-0",
        "human_rank": 1,
        "pptx": {"local_path": "decks/example.pptx"},
        "renders": {"files": []},
        "run_id": "run-test",
        "profile_version": "6.0",
        "decision": "REVIEW",
        "coverage": "DEGRADED",
        "base_score": 75.0,
        "reference_v2_score": None,
        "deterministic_visual_proxy": 80.0,
        "metrics": {},
        "model_audit_routing": [],
        "review_reasons": ["missing_metric"],
        "model_token_usage": {},
        "rank_comparison_eligible": False,
        "selected_human_order": None,
        "baseline_order": None,
        "visual_proxy_order": None,
        "baseline_rank_delta": None,
        "visual_rank_delta": None,
    }

    document = case_html(item, Path("."), Path("."))

    assert "排名统计已抑制" in document
    assert "系统顺序与排名偏差不予展示" in document
    assert "#None" not in document


def test_selected_metrics_exports_all_six_v6_dimensions() -> None:
    results = {item["metric_id"]: item for item in _dimension_results()}

    selected = selected_metrics(results)

    assert tuple(selected) == STRUCTURED_VLM_DIMENSION_IDS
    assert all(item["metric_status"] == "SCORED" for item in selected.values())
    assert sum(float(item["cost"]) for item in selected.values()) == pytest.approx(1.0)


def test_atomic_model_routing_events_exposes_cross_provider_attempts() -> None:
    metric_id = STRUCTURED_VLM_DIMENSION_IDS[0]
    results = {
        metric_id: {
            "metadata": {
                "criterion_id": "composition_layout",
                "escalation_reason": "FLASH_LOW_CONFIDENCE",
                "routing_attempts": [
                    {
                        "tier": "FLASH",
                        "configured_model": "qwen3.8-flash",
                        "metric_status": "SCORED",
                        "model": {
                            "provider": "qwen-dashscope-openai-compatible",
                            "model_id": "qwen3.8-flash",
                            "version": "qwen3.8-flash",
                        },
                    },
                    {
                        "tier": "ADVANCED",
                        "configured_model": "glm-5.3-flash",
                        "metric_status": "SCORED",
                        "model": {
                            "provider": "zhipu-bigmodel-openai-compatible",
                            "model_id": "glm-5.3-flash",
                            "version": "glm-5.3-flash",
                        },
                    },
                ],
            }
        }
    }

    events = atomic_model_routing_events(results)

    assert len(events) == 1
    assert events[0]["stage"] == "composition_layout"
    assert events[0]["route"] == (
        "qwen3.8-flash:SCORED -> glm-5.3-flash:SCORED "
        "(FLASH_LOW_CONFIDENCE)"
    )
