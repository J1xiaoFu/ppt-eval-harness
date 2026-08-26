"""Offline construct audit for a real-PPT benchmark comparison.

This script never invokes an Oracle or a remote model.  It reads an existing
comparison, its per-case reports, and the Profile that produced them, then
prints:

* effective metric and construct budgets after Profile normalization;
* descriptive per-metric rank agreement with the human ordering; and
* model-tier/evidence-page diagnostics and pairwise score attribution.

The statistics are diagnostic only.  In particular, correlations from the
three-deck starter slice are not estimates of generalization quality.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPARISON = ROOT / "var" / "datasets" / "slides_align_sample" / "report_qwen_v3" / "comparison.json"
DEFAULT_PROFILE = ROOT / "configs" / "profiles" / "finished_deck_v3.json"

# This registry describes what the current implementations actually observe,
# rather than what their user-facing names might imply.
OBSERVED_CONSTRUCTS = {
    "content_surface_structure": ("content_clarity", "narrative"),
    "content_semantics_model": ("llm_content_quality_audit",),
    "visual_geometry_and_text": (
        "visual_hierarchy",
        "layout",
        "typography",
        "style_consistency",
    ),
    "visual_perception_model": ("vlm_visual_quality_audit",),
    "asset_integrity": ("multimedia_quality",),
    "delivery_engineering": ("editability", "compatibility"),
    "accessibility_metadata": ("accessibility",),
    "completion_hygiene": ("template_residue",),
}

MODEL_TIER_METRICS = (
    "llm_content_quality_audit",
    "vlm_visual_quality_audit",
    "advanced_llm_content_review",
    "advanced_vlm_visual_review",
)


def average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = rank
        index = end
    return result


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_norm = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_norm = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return numerator / (left_norm * right_norm)


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    return pearson(average_ranks(left), average_ranks(right))


def pairwise_accuracy(human_ranks: Sequence[int], scores: Sequence[float]) -> float | None:
    credits: list[float] = []
    for left in range(len(scores)):
        for right in range(left + 1, len(scores)):
            human_direction = human_ranks[right] - human_ranks[left]
            score_direction = scores[left] - scores[right]
            if score_direction == 0:
                credits.append(0.5)
            else:
                credits.append(1.0 if human_direction * score_direction > 0 else 0.0)
    return sum(credits) / len(credits) if credits else None


def load_inputs(comparison_path: Path, profile_path: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    weights = {key: float(value) for key, value in profile["base_weights"].items()}
    report_directory = comparison_path.parent
    cases: list[dict[str, Any]] = []
    for summary in comparison["cases"]:
        report_path = report_directory / f"{summary['case_id']}.report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        results = {
            item["metric_id"]: item for item in report["results"] if item.get("normalized_score") is not None
        }
        cases.append(
            {
                "case_id": summary["case_id"],
                "human_rank": int(summary["human_rank"]),
                "base_score": float(summary["base_score"]),
                "results": results,
            }
        )
    return cases, weights


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def metric_score(case: Mapping[str, Any], metric_id: str) -> float | None:
    result = case["results"].get(metric_id)
    return None if result is None else float(result["normalized_score"])


def print_budget(weights: Mapping[str, float]) -> None:
    total = sum(value for value in weights.values() if value > 0)
    print("# Effective construct budget")
    assigned: set[str] = set()
    for construct, metric_ids in OBSERVED_CONSTRUCTS.items():
        raw = sum(weights.get(metric_id, 0.0) for metric_id in metric_ids)
        assigned.update(metric_ids)
        print(f"{construct:31} {100.0 * raw / total:7.2f}%  {', '.join(metric_ids)}")
    unassigned = sorted(set(weights) - assigned)
    if unassigned:
        raw = sum(weights[metric_id] for metric_id in unassigned)
        print(f"{'unassigned':31} {100.0 * raw / total:7.2f}%  {', '.join(unassigned)}")
    print()


def print_metric_agreement(cases: Sequence[Mapping[str, Any]], weights: Mapping[str, float]) -> None:
    total = sum(value for value in weights.values() if value > 0)
    human_ranks = [int(case["human_rank"]) for case in cases]
    human_utility = [-float(rank) for rank in human_ranks]
    print("# Per-metric descriptive agreement")
    print("metric                         eff_wt  range  spearman  pairwise  scores")
    for metric_id, weight in weights.items():
        values = [metric_score(case, metric_id) for case in cases]
        present = [value for value in values if value is not None]
        if len(present) != len(cases):
            rho = None
            accuracy = None
        else:
            numeric = [float(value) for value in present]
            rho = spearman(numeric, human_utility)
            accuracy = pairwise_accuracy(human_ranks, numeric)
        spread = max(present) - min(present) if present else 0.0
        score_text = ", ".join(f"{case['case_id']}={fmt(value)}" for case, value in zip(cases, values))
        print(
            f"{metric_id:30} {100.0 * weight / total:6.2f}%  {spread:5.3f}  "
            f"{fmt(rho):>8}  {fmt(accuracy):>8}  {score_text}"
        )
    print()


def print_model_tier_agreement(cases: Sequence[Mapping[str, Any]]) -> None:
    human_ranks = [int(case["human_rank"]) for case in cases]
    human_utility = [-float(rank) for rank in human_ranks]
    print("# Model-tier descriptive agreement")
    print("metric                         spearman  pairwise  scores")
    for metric_id in MODEL_TIER_METRICS:
        values = [metric_score(case, metric_id) for case in cases]
        present = [value for value in values if value is not None]
        if len(present) != len(cases):
            rho = None
            accuracy = None
        else:
            numeric = [float(value) for value in present]
            rho = spearman(numeric, human_utility)
            accuracy = pairwise_accuracy(human_ranks, numeric)
        score_text = ", ".join(f"{case['case_id']}={fmt(value)}" for case, value in zip(cases, values))
        print(f"{metric_id:30} {fmt(rho):>8}  {fmt(accuracy):>8}  {score_text}")
    print()


def print_evidence_page_overlap(cases: Sequence[Mapping[str, Any]]) -> None:
    metric_ids = (
        "template_residue",
        "llm_content_quality_audit",
        "vlm_visual_quality_audit",
    )
    print("# Evidence-page overlap (not proof of duplicate findings)")
    for case in cases:
        pages: dict[str, set[int]] = {}
        for metric_id in metric_ids:
            result = case["results"].get(metric_id, {})
            pages[metric_id] = {
                int(item["page_number"])
                for item in result.get("evidence", ())
                if item.get("page_number") is not None
            }
        llm_vlm = sorted(pages["llm_content_quality_audit"] & pages["vlm_visual_quality_audit"])
        template_llm = sorted(pages["template_residue"] & pages["llm_content_quality_audit"])
        template_vlm = sorted(pages["template_residue"] & pages["vlm_visual_quality_audit"])
        print(
            f"{case['case_id']}: llm/vlm={llm_vlm}, template/llm={template_llm}, template/vlm={template_vlm}"
        )
    print()


def print_pair_attribution(cases: Sequence[Mapping[str, Any]], weights: Mapping[str, float]) -> None:
    total = sum(value for value in weights.values() if value > 0)
    print("# Pairwise score attribution (left minus right, points)")
    for left_index in range(len(cases)):
        for right_index in range(left_index + 1, len(cases)):
            left = cases[left_index]
            right = cases[right_index]
            expected = "left" if left["human_rank"] < right["human_rank"] else "right"
            observed = "left" if left["base_score"] > right["base_score"] else "right"
            print(
                f"{left['case_id']} - {right['case_id']}: "
                f"human={expected}, aggregate={observed}, "
                f"delta={left['base_score'] - right['base_score']:+.3f}"
            )
            contributions: list[tuple[str, float]] = []
            for metric_id, weight in weights.items():
                left_score = metric_score(left, metric_id)
                right_score = metric_score(right, metric_id)
                if left_score is None or right_score is None:
                    continue
                contribution = 100.0 * weight * (left_score - right_score) / total
                if abs(contribution) > 1e-9:
                    contributions.append((metric_id, contribution))
            for metric_id, contribution in sorted(contributions, key=lambda item: abs(item[1]), reverse=True):
                print(f"  {metric_id:30} {contribution:+7.3f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    args = parser.parse_args()
    cases, weights = load_inputs(args.comparison.resolve(), args.profile.resolve())
    print_budget(weights)
    print_metric_agreement(cases, weights)
    print_model_tier_agreement(cases)
    print_evidence_page_overlap(cases)
    print_pair_attribution(cases, weights)
    print(
        "NOTE: this is a descriptive n=%d audit; do not tune production weights from these "
        "statistics alone." % len(cases)
    )


if __name__ == "__main__":
    main()
