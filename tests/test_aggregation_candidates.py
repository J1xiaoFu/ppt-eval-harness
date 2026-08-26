from __future__ import annotations

from scripts.benchmarks.analyze_aggregation_candidates import (
    CONSTRUCT_WEIGHTS,
    construct_arithmetic,
    construct_scores,
    preference_without_handoff,
)


def _scored(value: float) -> dict[str, object]:
    return {"metric_status": "SCORED", "normalized_score": value}


def test_construct_budget_caps_vlm_inside_visual_group() -> None:
    metric_values = {
        "content_clarity": 0.8,
        "narrative": 0.8,
        "llm_content_quality_audit": 0.8,
        "visual_hierarchy": 0.6,
        "layout": 0.6,
        "typography": 0.6,
        "style_consistency": 0.6,
        "multimedia_quality": 0.6,
        "vlm_visual_quality_audit": 1.0,
        "editability": 0.9,
        "compatibility": 0.9,
        "accessibility": 0.9,
        "template_residue": 1.0,
    }
    results = {key: _scored(value) for key, value in metric_values.items()}
    weights = {key: 1.0 for key in metric_values}

    constructs = construct_scores(results, weights, vlm_within_visual=0.10)

    assert round(constructs["deterministic_visual"], 12) == 0.6
    assert round(constructs["visual"], 12) == 0.64
    assert round(sum(CONSTRUCT_WEIGHTS.values()), 12) == 1.0


def test_preference_channel_does_not_hide_handoff_constraint_in_score() -> None:
    strong_handoff = {
        "content": 0.8,
        "visual": 0.8,
        "delivery": 0.8,
        "handoff": 1.0,
    }
    weak_handoff = {**strong_handoff, "handoff": 0.1}

    assert preference_without_handoff(strong_handoff) == preference_without_handoff(
        weak_handoff
    )
    assert construct_arithmetic(weak_handoff) < construct_arithmetic(strong_handoff)
