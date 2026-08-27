from __future__ import annotations

import hashlib
import json
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
    V8_RASTER_TEXT_MODEL_METRIC_IDS,
    atomic_model_routing_events,
    audit_case_payload,
    average_ranks,
    build_html,
    case_html,
    pairwise_accuracy,
    replay_rank_statistics_eligible,
    selected_metrics,
    spearman,
    structured_visual_case_analysis,
    structured_visual_replay_analysis,
    structured_visual_sensitivity_scores,
    v8_model_case_analysis,
    v8_replay_analysis,
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


def test_case_html_exposes_every_slide_and_evidence_page_jump() -> None:
    item = {
        "case_id": "case-a",
        "product": "Deck A",
        "human_rank": 1,
        "pptx": {"local_path": "decks/example.pptx"},
        "renders": {
            "files": [
                {"local_path": f"renders/slide-{index}.png"}
                for index in range(1, 9)
            ]
        },
        "run_id": "run-test",
        "profile_version": "8.3",
        "decision": "REVIEW",
        "coverage": "FULL",
        "base_score": 76.0,
        "reference_v2_score": None,
        "deterministic_visual_proxy": 80.0,
        "metrics": {},
        "model_audit_routing": [],
        "review_reasons": ["score:review_band"],
        "model_token_usage": {},
        "model_usage_summary": {
            "total_tokens": 100,
            "usage_complete": True,
            "cost_known": False,
            "reported_cost": 0.0,
        },
        "result_status_counts": {"SCORED": 9, "PASS": 1},
        "audit_chain": {"valid": True, "broken_event": None},
        "v8_model_analysis": {
            "metric_statuses": {
                metric_id: "SCORED"
                for metric_id in (*STRUCTURED_VLM_DIMENSION_IDS, "structured_vlm_authorship_specificity")
            }
        },
        "audit": {
            "training_eligibility": {
                "decisions": [
                    {
                        "track": "visual",
                        "status": "REVIEW",
                        "score": 76.0,
                        "reason_codes": ["score:review_band"],
                    }
                ]
            },
            "gate_verdicts": [
                {
                    "metric_id": "v8_functional_integrity",
                    "verdict": "PASS",
                    "reason_code": "PASS",
                    "critical_observation_count": 0,
                    "major_prevalence": 0.0,
                }
            ],
            "reducers": {
                "authorship_specificity_v2": {
                    "score_role": "BASE_ADDITIVE",
                    "score": 0.4,
                    "metric_status": "SCORED",
                    "observability": 1.0,
                    "rule_score": 0.5,
                    "model_score": 0.35,
                    "critical_cap_applied": False,
                    "lineage": {
                        "applicable_observation_count": 2,
                        "observation_count": 2,
                        "input_metric_ids": ["authorship_specificity_signals"],
                    },
                }
            },
            "observation_summary": {
                "count": 2,
                "metric_ids": ["authorship_specificity_signals"],
                "by_scope": {"PAGE": 2},
                "by_status": {"SCORED": 2},
            },
            "observation_artifact": {
                "href": "artifacts/case-a.observations.json",
                "sha256": "a" * 64,
                "hash_valid": True,
            },
            "findings": {
                "total_actionable": 1,
                "displayed": 1,
                "truncated": False,
                "items": [
                    {
                        "page_number": 8,
                        "severity": "MAJOR",
                        "metric_id": "authorship_specificity_signals",
                        "message": "Repeated mechanical cards.",
                        "score": 0.4,
                        "confidence": 0.8,
                        "kind": "formulaicity",
                        "unit_key": "page:8",
                    }
                ],
            },
            "errors": [],
            "degradation_reasons": [],
        },
        "report_href": "case-a.report.json",
        "rank_comparison_eligible": False,
        "selected_human_order": None,
        "baseline_order": None,
        "visual_proxy_order": None,
        "baseline_rank_delta": None,
        "visual_rank_delta": None,
    }

    document = case_html(item, Path("."), Path("."))

    assert document.count('class="slide-thumb"') == 8
    assert 'id="case-a-slide-8"' in document
    assert 'data-open-slide="case-a-slide-8"' in document
    assert "人工审计证据（可跳转幻灯片）" in document
    assert "规则 / VLM" in document
    assert "cost=未知" in document
    assert "下载完整原子观察 JSON" in document


def test_build_html_includes_keyboard_modal_viewer() -> None:
    comparison = {
        "statistics": {
            "spearman_base_vs_human": None,
            "spearman_deterministic_visual_proxy_vs_human": None,
            "pairwise_base_accuracy": None,
            "pairwise_deterministic_visual_proxy_accuracy": None,
            "comparable_pairs": 0,
        },
        "reference_v2_statistics": None,
        "structured_visual_replay": None,
        "v8_replay": {"eligible": False, "valid_case_count": 0, "expected_case_count": 7, "case_failures": {}},
        "cases": [],
        "limitations": [],
        "profile_id": "finished-deck-v8",
    }

    document = build_html(comparison, Path("."), Path("."))

    assert 'id="slide-modal"' in document
    assert 'class="modal-prev"' in document
    assert "ArrowRight" in document
    assert "排名统计已抑制" in document


def test_audit_case_payload_verifies_and_exports_full_observation_artifact(
    tmp_path: Path,
) -> None:
    observations = [
        {
            "observation_id": "obs-1",
            "oracle_id": "v8.test",
            "metric_id": "slide_geometry_integrity",
            "scope": "PAGE",
            "unit_key": "page:2",
            "metric_status": "SCORED",
            "local_score": 0.4,
            "confidence": 1.0,
            "severity": "MAJOR",
            "evidence": [
                {
                    "kind": "clipping",
                    "message": "Text is clipped.",
                    "page_number": 2,
                }
            ],
        }
    ]
    source = tmp_path / "source.observations.json"
    source.write_text(json.dumps(observations), encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report = {
        "results": [
            {
                "oracle_id": "v8.quality_reducers",
                "metric_id": "v8_functional_integrity",
                "metric_status": "FAIL",
                "score_role": "BASE_MULTIPLIER",
                "normalized_score": None,
                "confidence": 1.0,
                "severity": "CRITICAL",
                "metadata": {
                    "reason_code": "CRITICAL_OBSERVATION",
                    "critical_observation_ids": ["obs-1"],
                },
            }
        ],
        "manifest": {"artifact_hashes": {"atomic_observations": digest}},
        "observation_artifact": {
            "uri": str(source),
            "sha256": digest,
            "size_bytes": source.stat().st_size,
            "media_type": "application/vnd.ppt-eval.observations+json",
        },
        "training_eligibility": {"decisions": []},
        "observation_summary": {"count": 1},
    }

    payload = audit_case_payload(report, tmp_path / "report", "case-a")

    artifact = payload["observation_artifact"]
    assert artifact["hash_valid"] is True
    assert artifact["target_sha256"] == digest
    assert artifact["target_size_bytes"] == source.stat().st_size
    assert artifact["available"] is True
    assert (tmp_path / "report" / artifact["href"]).read_bytes() == source.read_bytes()
    assert payload["gate_verdicts"][0]["verdict"] == "FAIL"
    assert payload["findings"]["items"][0]["page_number"] == 2


def test_v8_replay_fails_closed_on_any_model_na_or_error() -> None:
    required = (*STRUCTURED_VLM_DIMENSION_IDS, "structured_vlm_authorship_specificity")

    def report(case_id: str) -> dict[str, object]:
        return {
            "case_id": case_id,
            "coverage": "FULL",
            "manifest": {"case_id": case_id},
            "results": [
                {
                    "metric_id": metric_id,
                    "metric_status": "SCORED",
                    "normalized_score": 0.8,
                    "metadata": {
                        "routing_attempts": [
                            {
                                "tier": "FLASH",
                                "selected": True,
                                "metric_status": "SCORED",
                            }
                        ],
                        "routing_usage": {"usage_complete": True},
                    },
                }
                for metric_id in required
            ],
        }

    cases = []
    expected = []
    for index in range(7):
        case_id = f"case-{index}"
        analysis = v8_model_case_analysis(report(case_id), expected_case_id=case_id)
        cases.append(
            {
                "case_id": case_id,
                "product": f"product-{index}",
                "human_rank": index + 1,
                "v8_model_analysis": analysis,
            }
        )
        expected.append((case_id, f"product-{index}", index + 1))
    valid = v8_replay_analysis(cases, expected_cases=expected)
    assert valid["eligible"] is True
    assert replay_rank_statistics_eligible(None, valid) is True

    broken_report = report("case-0")
    broken_report["results"][0]["metric_status"] = "ERROR"
    broken_report["results"][0]["normalized_score"] = None
    cases[0]["v8_model_analysis"] = v8_model_case_analysis(
        broken_report, expected_case_id="case-0"
    )
    broken = v8_replay_analysis(cases, expected_cases=expected)

    assert broken["eligible"] is False
    assert "metric_not_scored" in " ".join(broken["case_failures"]["case-0"])
    assert replay_rank_statistics_eligible(None, broken) is False


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


def test_atomic_model_routing_events_includes_raster_text_observation_calls() -> None:
    metric_id = V8_RASTER_TEXT_MODEL_METRIC_IDS[0]
    results = {
        metric_id: {
            "metadata": {
                "criterion_id": "raster_content_structure",
                "routing_attempts": [
                    {
                        "tier": "FLASH",
                        "configured_model": "qwen3.8-flash",
                        "metric_status": "SCORED",
                    }
                ],
            }
        }
    }

    events = atomic_model_routing_events(results)

    assert len(events) == 1
    assert events[0]["stage"] == "raster_content_structure"
    assert events[0]["route"] == "qwen3.8-flash:SCORED"
