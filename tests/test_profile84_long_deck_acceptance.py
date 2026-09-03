from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmarks.profile84_long_deck_acceptance import (
    ATLAS_SCOUT_VERSION,
    BENCHMARK_VERSION,
    CHALLENGE_SET_VERSION,
    COST_MODEL_VERSION,
    LOCAL_AUDIT_FIXTURE_VERSION,
    SELECTION_POLICY_VERSION,
    run_benchmark,
    write_report,
)


def test_profile84_long_deck_offline_acceptance_gate(tmp_path: Path) -> None:
    report = run_benchmark(tmp_path / "work")

    assert report["benchmark_version"] == BENCHMARK_VERSION
    assert report["challenge_set_version"] == CHALLENGE_SET_VERSION
    assert report["profiles"] == {"baseline": "8.3", "candidate": "8.4"}
    assert report["acceptance"]["passed"] is True
    assert all(report["acceptance"]["checks"].values())

    aggregate = report["aggregate"]
    assert aggregate["deck_count"] == 3
    assert aggregate["page_count"] == 170
    assert aggregate["ground_truth_defect_count"] == 18
    assert aggregate["deltas"]["recall_improvement_pp"] >= 10.0
    assert aggregate["deltas"]["precision_decline_pp"] <= 3.0
    assert aggregate["profile_8_4"]["rule_blind_spot_false_pass_count"] == 0
    assert (
        aggregate["deltas"]["uncached_visual_input_token_reduction_percent"]
        >= 25.0
    )
    assert (
        aggregate["deltas"]["estimated_visual_input_cost_reduction_percent"]
        >= 25.0
    )

    long_deck = next(item for item in report["decks"] if item["page_count"] == 100)
    placeholder = next(
        item
        for item in long_deck["defects"]
        if item["defect_type"] == "placeholder_visual"
    )
    assert placeholder["page_number"] == 57
    assert 57 in long_deck["profile_8_4"]["scout_finding_pages"]
    assert 57 in long_deck["profile_8_4"]["criterion_pages"][
        "imagery_data_visualization"
    ]
    assert 57 not in long_deck["profile_8_3"]["page_local_sample_pages"]

    output = write_report(report, tmp_path / "report.json")
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_profile84_benchmark_freezes_non_fitted_cost_and_routing_assumptions(
    tmp_path: Path,
) -> None:
    first = run_benchmark(tmp_path / "first")
    second = run_benchmark(tmp_path / "second")

    assert first == second
    assumptions = first["assumptions"]
    assert assumptions["no_weight_fitting"] is True
    assert assumptions["candidate_8_4"] == {
        "page_index_version": "1.0.0",
        "atlas_scout_version": ATLAS_SCOUT_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "local_audit_fixture_version": LOCAL_AUDIT_FIXTURE_VERSION,
        "page_local_common_cache_prefix": 4,
        "cross_slide_common_cache_prefix": 8,
        "progressive_round_page_count": 2,
        "maximum_atlases_per_scout_request": 12,
        "conditional_render_integrity": "SKIPPED_NO_TRIGGER",
        "cluster_score_propagation": "FORBIDDEN",
        "scout_findings_are_non_scoring": True,
    }
    cost = assumptions["visual_input_cost_model"]
    assert cost["version"] == COST_MODEL_VERSION
    assert cost["billing_status"] == (
        "versioned comparative estimate, not a provider invoice"
    )
    assert cost["atlas_ratio_to_high_resolution"] < 1.0
