from __future__ import annotations

from scripts.benchmarks.evaluate_slides_align_sample import (
    average_ranks,
    pairwise_accuracy,
    spearman,
    visual_proxy,
)


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
