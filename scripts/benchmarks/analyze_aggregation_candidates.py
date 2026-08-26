"""Audit flat PPT-PDMS compensation against construct-aware candidates.

This is an offline analysis tool.  It never calls a model and never changes
the default runtime Profile.  Candidate weights are predeclared here so a
small gold slice cannot silently fit its own labels.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ppt_eval.config import load_profile  # noqa: E402
from scripts.benchmarks.evaluate_slides_align_sample import (  # noqa: E402
    pairwise_accuracy,
    spearman,
)

CONTENT_METRICS = ("content_clarity", "narrative", "llm_content_quality_audit")
DETERMINISTIC_VISUAL_METRICS = (
    "visual_hierarchy",
    "layout",
    "typography",
    "style_consistency",
    "multimedia_quality",
)
DELIVERY_METRICS = ("editability", "compatibility", "accessibility")
VLM_METRIC = "vlm_visual_quality_audit"
HANDOFF_METRIC = "template_residue"

# Preserve the four construct budgets implied by v3, but cap VLM at ten
# percent of the visual construct instead of letting metric count set the
# construct mass.  These are hypotheses, not fitted production weights.
CONSTRUCT_WEIGHTS = {
    "content": 0.265625,
    "visual": 0.500000,
    "delivery": 0.171875,
    "handoff": 0.062500,
}
VLM_WITHIN_VISUAL = 0.10


def _score(result: Mapping[str, Any]) -> float | None:
    status = result.get("metric_status")
    if status == "PASS":
        return 1.0
    if status == "FAIL":
        return 0.0
    value = result.get("normalized_score")
    return None if value is None else float(value)


def weighted_metric_mean(
    metric_ids: Sequence[str],
    results: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for metric_id in metric_ids:
        result = results.get(metric_id)
        value = None if result is None else _score(result)
        weight = float(weights.get(metric_id, 0.0))
        if value is None or weight <= 0:
            continue
        numerator += weight * value
        denominator += weight
    return numerator / denominator if denominator else None


def construct_scores(
    results: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    *,
    vlm_within_visual: float = VLM_WITHIN_VISUAL,
) -> Mapping[str, float]:
    content = weighted_metric_mean(CONTENT_METRICS, results, weights)
    deterministic_visual = weighted_metric_mean(
        DETERMINISTIC_VISUAL_METRICS,
        results,
        weights,
    )
    delivery = weighted_metric_mean(DELIVERY_METRICS, results, weights)
    vlm_result = results.get(VLM_METRIC)
    vlm = None if vlm_result is None else _score(vlm_result)
    handoff_result = results.get(HANDOFF_METRIC)
    handoff = None if handoff_result is None else _score(handoff_result)
    if (
        content is None
        or deterministic_visual is None
        or delivery is None
        or handoff is None
    ):
        raise ValueError("construct analysis requires complete content/visual/delivery/handoff")
    if not 0.0 <= vlm_within_visual <= 1.0:
        raise ValueError("vlm_within_visual must be in [0,1]")
    if vlm is None:
        visual = float(deterministic_visual)
    else:
        visual = (
            (1.0 - vlm_within_visual) * float(deterministic_visual)
            + vlm_within_visual * vlm
        )
    return {
        "content": float(content),
        "deterministic_visual": float(deterministic_visual),
        "vlm_visual": float("nan") if vlm is None else vlm,
        "visual": visual,
        "delivery": float(delivery),
        "handoff": float(handoff),
    }


def construct_arithmetic(constructs: Mapping[str, float]) -> float:
    return sum(
        CONSTRUCT_WEIGHTS[name] * float(constructs[name])
        for name in CONSTRUCT_WEIGHTS
    )


def preference_without_handoff(constructs: Mapping[str, float]) -> float:
    names = ("content", "visual", "delivery")
    denominator = sum(CONSTRUCT_WEIGHTS[name] for name in names)
    return sum(
        CONSTRUCT_WEIGHTS[name] * float(constructs[name]) for name in names
    ) / denominator


def construct_geometric(constructs: Mapping[str, float]) -> float:
    return math.exp(
        sum(
            CONSTRUCT_WEIGHTS[name] * math.log(max(1e-9, float(constructs[name])))
            for name in CONSTRUCT_WEIGHTS
        )
    )


def analyze(report_dir: Path, profile_path: Path) -> Mapping[str, Any]:
    comparison = json.loads((report_dir / "comparison.json").read_text(encoding="utf-8"))
    profile = load_profile(profile_path)
    rows: list[dict[str, Any]] = []
    for case in comparison["cases"]:
        case_id = str(case["case_id"])
        report = json.loads(
            (report_dir / f"{case_id}.report.json").read_text(encoding="utf-8")
        )
        if report.get("coverage") != "FULL":
            continue
        results = {item["metric_id"]: item for item in report["results"]}
        constructs = construct_scores(results, profile.base_weights)
        multiplier = float(report["score_breakdown"].get("base_multiplier", 1.0))
        rows.append(
            {
                "case_id": case_id,
                "product": case["product"],
                "human_rank": int(case["human_rank"]),
                "constructs": constructs,
                "scores": {
                    "flat_v3": float(report["base_score"]),
                    "construct_cap_arithmetic": 100.0
                    * multiplier
                    * construct_arithmetic(constructs),
                    "preference_without_handoff": 100.0
                    * multiplier
                    * preference_without_handoff(constructs),
                    "construct_geometric": 100.0
                    * multiplier
                    * construct_geometric(constructs),
                },
            }
        )

    schemes = tuple(rows[0]["scores"]) if rows else ()
    human_utility = [-float(row["human_rank"]) for row in rows]
    statistics: dict[str, Any] = {}
    for scheme in schemes:
        scores = [float(row["scores"][scheme]) for row in rows]
        statistics[scheme] = {
            "spearman_vs_human": spearman(scores, human_utility),
            "pairwise_accuracy": pairwise_accuracy(
                [int(row["human_rank"]) for row in rows],
                scores,
            ),
            "order": [
                row["product"]
                for row in sorted(
                    rows,
                    key=lambda item: float(item["scores"][scheme]),
                    reverse=True,
                )
            ],
        }

    return {
        "schema_version": "1.0",
        "source_report": str(report_dir),
        "profile": f"{profile.profile_id}@{profile.version}",
        "full_cases": len(rows),
        "construct_metric_map": {
            "content": list(CONTENT_METRICS),
            "visual_deterministic": list(DETERMINISTIC_VISUAL_METRICS),
            "visual_vlm": [VLM_METRIC],
            "delivery": list(DELIVERY_METRICS),
            "handoff": [HANDOFF_METRIC],
        },
        "construct_weights": dict(CONSTRUCT_WEIGHTS),
        "vlm_within_visual": VLM_WITHIN_VISUAL,
        "statistics": statistics,
        "cases": rows,
        "interpretation_guardrails": [
            "Candidates are predeclared hypotheses, not weights fitted to this slice.",
            "Overall human rank cannot identify which construct is wrong.",
            "Only FULL cases are rank-comparable; degraded base scores are excluded.",
            "Preference_without_handoff still requires handoff floors for acceptability.",
        ],
    }


def markdown(payload: Mapping[str, Any]) -> str:
    schemes = tuple(payload["statistics"])
    header = "| Product | Human | Content | Visual | Delivery | Handoff | " + " | ".join(
        schemes
    ) + " |"
    separator = "|---|---:|---:|---:|---:|---:|" + "---:|" * len(schemes)
    rows = []
    for case in payload["cases"]:
        constructs = case["constructs"]
        scores = case["scores"]
        rows.append(
            "| "
            + str(case["product"])
            + f" | {case['human_rank']} | {100*constructs['content']:.1f}"
            + f" | {100*constructs['visual']:.1f} | {100*constructs['delivery']:.1f}"
            + f" | {100*constructs['handoff']:.1f} | "
            + " | ".join(f"{scores[name]:.2f}" for name in schemes)
            + " |"
        )
    stats = "\n".join(
        f"- `{name}`: Spearman={values['spearman_vs_human']}, "
        f"pairwise={values['pairwise_accuracy']}, order={' > '.join(values['order'])}"
        for name, values in payload["statistics"].items()
    )
    return (
        "# Construct-aware aggregation audit\n\n"
        f"Profile: `{payload['profile']}`; FULL cases: `{payload['full_cases']}`.\n\n"
        + header
        + "\n"
        + separator
        + "\n"
        + "\n".join(rows)
        + "\n\n## Descriptive statistics\n\n"
        + stats
        + "\n\n## Guardrails\n\n"
        + "\n".join(f"- {item}" for item in payload["interpretation_guardrails"])
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample" / "report_qwen_v3",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=ROOT / "configs" / "profiles" / "finished_deck_v3.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report_dir = args.report_dir.resolve()
    payload = analyze(report_dir, args.profile.resolve())
    output = args.output or report_dir / "aggregation_candidates.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output.with_suffix(".md").write_text(markdown(payload), encoding="utf-8")
    print(json.dumps(payload["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
