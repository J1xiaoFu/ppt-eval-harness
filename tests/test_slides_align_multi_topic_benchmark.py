from __future__ import annotations

import pytest

from scripts.benchmarks.evaluate_slides_align_topics import (
    aggregate_suite,
    build_suite_html,
)
from scripts.datasets.fetch_slides_align_market_analysis import PRODUCTS
from scripts.datasets.fetch_slides_align_topics import (
    topic_slug,
    validate_topic_rankings,
)


def test_multi_topic_ranking_keeps_notebook_exclusion_and_original_ranks() -> None:
    labels = ("NotebookLM", *PRODUCTS)
    rows = [
        {
            "product": product,
            "difficulty": "topic_introduction",
            "topic": "example_topic",
            "rank": rank,
        }
        for rank, product in enumerate(labels, start=1)
    ]

    selected = validate_topic_rankings(
        {"results": list(reversed(rows))},
        difficulty="topic_introduction",
        topic="example_topic",
    )

    assert [item["rank"] for item in selected] == list(range(1, 10))
    assert selected[0]["product"] == "NotebookLM"
    assert topic_slug("Chinese_New_Year") == "chinese_new_year"

    duplicate = [dict(item) for item in rows]
    duplicate[-1]["rank"] = 1
    with pytest.raises(ValueError, match="not complete"):
        validate_topic_rankings(
            {"results": duplicate},
            difficulty="topic_introduction",
            topic="example_topic",
        )


def _comparison(topic: str, *, spearman: float, pairwise: float) -> dict:
    cases = []
    for rank, product in enumerate(("A", "B"), start=1):
        cases.append(
            {
                "case_id": f"{topic}-{product}",
                "product": product,
                "human_rank": rank,
                "base_score": 90.0 - rank,
                "full_score": 90.0 - rank,
                "decision": "PASS",
                "coverage": "FULL",
                "renders": {"count": 3},
                "result_status_counts": {"SCORED": 9, "PASS": 4},
                "model_audit_statuses": {
                    "structured_vlm_composition_layout": "SCORED"
                },
                "audit_chain": {"valid": True, "broken_event": None},
                "audit": {
                    "observation_artifact": {"hash_valid": True},
                    "training_eligibility": {
                        "decisions": [
                            {"track": "visual", "status": "TRAIN"},
                            {"track": "full_deck", "status": "REVIEW"},
                        ]
                    },
                },
                "model_usage_summary": {
                    "total_tokens": 100,
                    "cost_known": False,
                    "reported_cost": 0.0,
                },
                "model_audit_routing": [
                    {"stage": "composition", "attempts": [{"tier": "FLASH"}]}
                ],
            }
        )
    return {
        "dataset_id": f"dataset-{topic}",
        "topic": topic,
        "profile_id": "finished-deck-v8",
        "profile_version": "8.3",
        "statistics": {
            "spearman_base_vs_human": spearman,
            "pairwise_base_accuracy": pairwise,
            "comparable_pairs": 1,
        },
        "v8_replay": {"eligible": True},
        "cases": cases,
    }


def test_suite_aggregation_keeps_rank_statistics_within_topics_only() -> None:
    topics = ("alpha", "beta", "gamma")
    suite_manifest = {
        "dataset_id": "suite",
        "source": {"revision": "a" * 40},
        "topics": [{"topic": topic} for topic in topics],
    }
    payload = aggregate_suite(
        suite_manifest=suite_manifest,
        comparisons=(
            _comparison("alpha", spearman=1.0, pairwise=1.0),
            _comparison("beta", spearman=0.5, pairwise=0.5),
            _comparison("gamma", spearman=0.0, pairwise=0.0),
        ),
        topic_report_hrefs={topic: f"../{topic}/index.html" for topic in topics},
    )

    assert payload["case_count"] == 6
    assert payload["rendered_slide_count"] == 18
    assert payload["aggregate"]["macro_spearman_base_vs_human"] == 0.5
    assert payload["aggregate"]["micro_pairwise_within_topics"] == 0.5
    assert payload["aggregate"]["within_topic_comparable_pairs"] == 3
    assert payload["aggregate"]["audit_chain_valid_count"] == 6
    assert payload["aggregate"]["observation_artifact_hash_valid_count"] == 6
    assert payload["aggregate"]["model_tokens"] == 600
    assert payload["aggregate"]["reported_cost"] is None
    assert payload["aggregate"]["exploratory_unqualified"][
        "macro_spearman_base_vs_human"
    ] == pytest.approx(1.0)
    assert payload["aggregate"]["exploratory_unqualified"][
        "micro_pairwise_within_topics"
    ] == 1.0
    assert payload["aggregate"]["exploratory_unqualified"]["full_cases_only"][
        "macro_spearman_base_vs_human"
    ] == pytest.approx(1.0)
    assert payload["methodology"]["global_rank_statistics_prohibited"] is True
    assert "global_spearman" not in payload["aggregate"]

    document = build_suite_html(payload)
    assert "禁止跨主题混排" in document
    assert "未设门诊断 Macro" in document
    assert document.count("打开完整幻灯片与审计") == 3
