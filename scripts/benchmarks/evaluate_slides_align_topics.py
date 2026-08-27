"""Run and aggregate multiple same-topic Slides-Align benchmark slices."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.benchmarks.evaluate_slides_align_sample import (  # noqa: E402
    evaluate,
    pairwise_accuracy,
    spearman,
)


def _counter_add(target: Counter[str], values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        target[str(key)] += int(value)


def _counter_status_values(
    target: Counter[str], values: Mapping[str, Any]
) -> None:
    for value in values.values():
        target[str(value)] += 1


def _exploratory_topic_statistics(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    def calculate(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        ranks = [int(item["human_rank"]) for item in items]
        scores = [float(item["base_score"]) for item in items]
        return {
            "case_count": len(items),
            "spearman_base_vs_human": (
                spearman(scores, [-float(rank) for rank in ranks])
                if len(items) > 1
                else None
            ),
            "pairwise_base_accuracy": (
                pairwise_accuracy(ranks, scores) if len(items) > 1 else None
            ),
            "comparable_pairs": len(items) * (len(items) - 1) // 2,
        }

    full = [item for item in cases if item.get("coverage") == "FULL"]
    return {
        "status": "EXPLORATORY_UNQUALIFIED_NOT_FOR_GATING_OR_WEIGHT_FIT",
        "all_cases": calculate(cases),
        "full_cases_only": calculate(full),
    }


def aggregate_suite(
    *,
    suite_manifest: Mapping[str, Any],
    comparisons: Sequence[Mapping[str, Any]],
    topic_report_hrefs: Mapping[str, str],
) -> dict[str, Any]:
    manifest_topics = {
        str(item["topic"]): item
        for item in suite_manifest.get("topics", ())
        if isinstance(item, Mapping)
    }
    comparison_topics = {str(item["topic"]): item for item in comparisons}
    if set(comparison_topics) != set(manifest_topics):
        raise ValueError("suite comparisons do not match the pinned topic manifest")

    decision_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()
    result_status_counts: Counter[str] = Counter()
    training_counts: Counter[str] = Counter()
    model_status_counts: Counter[str] = Counter()
    topics: list[dict[str, Any]] = []
    total_tokens = 0
    reported_cost = 0.0
    cost_known_all = True
    audit_valid_count = 0
    artifact_hash_valid_count = 0
    fallback_count = 0
    slide_count = 0
    case_count = 0

    for topic_name in sorted(comparison_topics):
        comparison = comparison_topics[topic_name]
        cases = [item for item in comparison.get("cases", ()) if isinstance(item, Mapping)]
        case_summaries: list[dict[str, Any]] = []
        for case in cases:
            case_count += 1
            decision_counts[str(case.get("decision") or "UNKNOWN")] += 1
            coverage_counts[str(case.get("coverage") or "UNKNOWN")] += 1
            _counter_add(result_status_counts, case.get("result_status_counts", {}))
            _counter_status_values(
                model_status_counts,
                case.get("model_audit_statuses", {}),
            )
            renders = case.get("renders")
            if isinstance(renders, Mapping):
                slide_count += int(renders.get("count", 0))
            chain = case.get("audit_chain")
            chain_valid = isinstance(chain, Mapping) and chain.get("valid") is True
            audit_valid_count += int(chain_valid)
            audit = case.get("audit")
            audit = audit if isinstance(audit, Mapping) else {}
            artifact = audit.get("observation_artifact")
            artifact = artifact if isinstance(artifact, Mapping) else {}
            artifact_valid = artifact.get("hash_valid") is True
            artifact_hash_valid_count += int(artifact_valid)
            training = audit.get("training_eligibility")
            training = training if isinstance(training, Mapping) else {}
            tracks: dict[str, str] = {}
            for item in training.get("decisions", ()):
                if not isinstance(item, Mapping):
                    continue
                track = str(item.get("track") or "unknown")
                status = str(item.get("status") or "UNKNOWN")
                tracks[track] = status
                training_counts[f"{track}:{status}"] += 1
            usage = case.get("model_usage_summary")
            usage = usage if isinstance(usage, Mapping) else {}
            total_tokens += int(usage.get("total_tokens", 0) or 0)
            case_cost_known = usage.get("cost_known") is True
            cost_known_all = cost_known_all and case_cost_known
            if case_cost_known:
                reported_cost += float(usage.get("reported_cost", 0.0) or 0.0)
            routing = case.get("model_audit_routing", ())
            fallback_count += sum(
                len(item.get("attempts", ())) > 1
                for item in routing
                if isinstance(item, Mapping)
            )
            case_summaries.append(
                {
                    "case_id": case.get("case_id"),
                    "product": case.get("product"),
                    "human_rank": case.get("human_rank"),
                    "base_score": case.get("base_score"),
                    "full_score": case.get("full_score"),
                    "decision": case.get("decision"),
                    "coverage": case.get("coverage"),
                    "training_tracks": tracks,
                    "audit_chain_valid": chain_valid,
                    "observation_artifact_hash_valid": artifact_valid,
                    "model_tokens": int(usage.get("total_tokens", 0) or 0),
                    "cost_known": case_cost_known,
                }
            )

        statistics = comparison.get("statistics")
        statistics = statistics if isinstance(statistics, Mapping) else {}
        replay = comparison.get("v8_replay")
        replay = replay if isinstance(replay, Mapping) else {}
        topics.append(
            {
                "topic": topic_name,
                "dataset_id": comparison.get("dataset_id"),
                "case_count": len(cases),
                "report_href": topic_report_hrefs[topic_name],
                "rank_statistics_eligible": replay.get("eligible") is True,
                "statistics": dict(statistics),
                "exploratory_statistics": _exploratory_topic_statistics(cases),
                "v8_replay": dict(replay),
                "cases": sorted(
                    case_summaries,
                    key=lambda item: int(item["human_rank"]),
                ),
            }
        )

    eligible_topics = [
        item
        for item in topics
        if item["rank_statistics_eligible"]
        and item["statistics"].get("spearman_base_vs_human") is not None
        and item["statistics"].get("pairwise_base_accuracy") is not None
    ]
    all_topics_eligible = len(eligible_topics) == len(topics)
    macro_spearman = (
        sum(
            float(item["statistics"]["spearman_base_vs_human"])
            for item in eligible_topics
        )
        / len(eligible_topics)
        if all_topics_eligible and eligible_topics
        else None
    )
    macro_pairwise = (
        sum(
            float(item["statistics"]["pairwise_base_accuracy"])
            for item in eligible_topics
        )
        / len(eligible_topics)
        if all_topics_eligible and eligible_topics
        else None
    )
    total_pairs = sum(
        int(item["statistics"].get("comparable_pairs", 0))
        for item in eligible_topics
    )
    micro_pairwise = (
        sum(
            float(item["statistics"]["pairwise_base_accuracy"])
            * int(item["statistics"].get("comparable_pairs", 0))
            for item in eligible_topics
        )
        / total_pairs
        if all_topics_eligible and total_pairs
        else None
    )
    exploratory_macro_spearman = sum(
        float(
            item["exploratory_statistics"]["all_cases"][
                "spearman_base_vs_human"
            ]
        )
        for item in topics
    ) / len(topics)
    exploratory_pair_count = sum(
        int(
            item["exploratory_statistics"]["all_cases"][
                "comparable_pairs"
            ]
        )
        for item in topics
    )
    exploratory_micro_pairwise = sum(
        float(
            item["exploratory_statistics"]["all_cases"][
                "pairwise_base_accuracy"
            ]
        )
        * int(
            item["exploratory_statistics"]["all_cases"][
                "comparable_pairs"
            ]
        )
        for item in topics
    ) / exploratory_pair_count
    exploratory_full_macro_spearman = sum(
        float(
            item["exploratory_statistics"]["full_cases_only"][
                "spearman_base_vs_human"
            ]
        )
        for item in topics
    ) / len(topics)
    exploratory_full_pair_count = sum(
        int(
            item["exploratory_statistics"]["full_cases_only"][
                "comparable_pairs"
            ]
        )
        for item in topics
    )
    exploratory_full_micro_pairwise = sum(
        float(
            item["exploratory_statistics"]["full_cases_only"][
                "pairwise_base_accuracy"
            ]
        )
        * int(
            item["exploratory_statistics"]["full_cases_only"][
                "comparable_pairs"
            ]
        )
        for item in topics
    ) / exploratory_full_pair_count
    return {
        "schema_version": "1.0",
        "dataset_id": suite_manifest.get("dataset_id"),
        "dataset_revision": suite_manifest.get("source", {}).get("revision"),
        "profile_id": comparisons[0].get("profile_id") if comparisons else None,
        "profile_version": comparisons[0].get("profile_version") if comparisons else None,
        "topic_count": len(topics),
        "case_count": case_count,
        "rendered_slide_count": slide_count,
        "topics": topics,
        "aggregate": {
            "all_topics_rank_eligible": all_topics_eligible,
            "eligible_topic_count": len(eligible_topics),
            "macro_spearman_base_vs_human": macro_spearman,
            "macro_pairwise_base_accuracy": macro_pairwise,
            "micro_pairwise_within_topics": micro_pairwise,
            "within_topic_comparable_pairs": total_pairs if all_topics_eligible else 0,
            "decision_counts": dict(sorted(decision_counts.items())),
            "coverage_counts": dict(sorted(coverage_counts.items())),
            "oracle_result_status_counts": dict(sorted(result_status_counts.items())),
            "model_metric_status_counts": dict(sorted(model_status_counts.items())),
            "training_track_status_counts": dict(sorted(training_counts.items())),
            "audit_chain_valid_count": audit_valid_count,
            "observation_artifact_hash_valid_count": artifact_hash_valid_count,
            "fallback_count": fallback_count,
            "model_tokens": total_tokens,
            "cost_known_all": cost_known_all,
            "reported_cost": round(reported_cost, 6) if cost_known_all else None,
            "exploratory_unqualified": {
                "status": "NOT_FOR_GATING_OR_WEIGHT_FIT",
                "macro_spearman_base_vs_human": exploratory_macro_spearman,
                "micro_pairwise_within_topics": exploratory_micro_pairwise,
                "within_topic_comparable_pairs": exploratory_pair_count,
                "full_cases_only": {
                    "macro_spearman_base_vs_human": (
                        exploratory_full_macro_spearman
                    ),
                    "micro_pairwise_within_topics": (
                        exploratory_full_micro_pairwise
                    ),
                    "within_topic_comparable_pairs": exploratory_full_pair_count,
                },
            },
        },
        "methodology": {
            "global_rank_statistics_prohibited": True,
            "rank_scope": "WITHIN_TOPIC_ONLY",
            "macro_policy": "UNWEIGHTED_MEAN_ONLY_WHEN_ALL_TOPICS_ARE_ELIGIBLE",
            "micro_pairwise_policy": "PAIR_WEIGHTED_WITHIN_TOPIC_ONLY",
            "weight_fitting_used": False,
        },
    }


def _stat(value: object, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    return f"{float(value):.0%}" if percent else f"{float(value):.3f}"


def build_suite_html(payload: Mapping[str, Any]) -> str:
    aggregate = payload["aggregate"]
    topic_sections = []
    for topic in payload["topics"]:
        stats = topic["statistics"]
        exploratory = topic["exploratory_statistics"]["all_cases"]
        rows = "".join(
            "<tr>"
            f"<td>{int(case['human_rank'])}</td>"
            f"<td>{html.escape(str(case['product']))}</td>"
            f"<td>{float(case['base_score']):.2f}</td>"
            f"<td>{html.escape(str(case['decision']))}</td>"
            f"<td>{html.escape(str(case['coverage']))}</td>"
            f"<td>{html.escape(', '.join(f'{key}:{value}' for key, value in case['training_tracks'].items()))}</td>"
            "</tr>"
            for case in topic["cases"]
        )
        topic_sections.append(
            f"""<section class="topic"><div class="topic-head"><div><h2>{html.escape(topic['topic'])}</h2>
<span class="badge">{topic['case_count']} decks</span>
<span class="badge">Spearman {_stat(stats.get('spearman_base_vs_human'))}</span>
<span class="badge">Pairwise {_stat(stats.get('pairwise_base_accuracy'), percent=True)}</span></div>
<span class="badge">诊断 Spearman {_stat(exploratory.get('spearman_base_vs_human'))}</span>
<a class="open" href="{html.escape(topic['report_href'])}">打开完整幻灯片与审计 →</a></div>
<table><thead><tr><th>人评</th><th>产品</th><th>分数</th><th>Decision</th><th>Coverage</th><th>训练轨</th></tr></thead>
<tbody>{rows}</tbody></table></section>"""
        )
    sections = "".join(topic_sections)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Slides-Align 三主题评测审计</title><style>
:root{{--ink:#17212b;--muted:#657482;--line:#dbe3e8;--paper:#f5f3ee;--accent:#126b5d;--warn:#b4532a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "Segoe UI",sans-serif}}
main{{max-width:1280px;margin:auto;padding:42px 28px 80px}}h1{{font:700 40px/1.15 Georgia,serif;margin:0}}h2{{font:700 27px Georgia,serif;margin:0 0 8px}}
.lead{{color:var(--muted);font-size:18px;max-width:920px}}.summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}}
.stat,.topic{{background:#fff;border:1px solid var(--line);border-radius:12px}}.stat{{padding:17px}}.stat b{{display:block;color:var(--accent);font-size:25px}}
.topic{{padding:24px;margin:24px 0}}.topic-head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}.badge{{display:inline-block;background:#e6f1ee;color:var(--accent);border-radius:999px;padding:4px 9px;margin-right:5px;font-weight:700}}
.open{{color:var(--accent);font-weight:700;text-decoration:none}}table{{border-collapse:collapse;width:100%;margin-top:16px}}th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left}}th{{color:var(--muted)}}
.note{{border-left:4px solid var(--warn);background:#fff1ea;padding:10px 14px}}@media(max-width:800px){{.summary{{grid-template-columns:1fr 1fr}}.topic-head{{display:block}}}}
</style></head><body><main><h1>Slides-Align 三主题真实 PPT 基线审计</h1>
<p class="lead">固定 revision <code>{html.escape(str(payload['dataset_revision']))}</code>；{payload['topic_count']} 个 topic-introduction 主题，
共 {payload['case_count']} 份 PPTX / {payload['rendered_slide_count']} 页。排名统计只在主题内计算。</p>
<div class="summary"><div class="stat"><b>{_stat(aggregate['macro_spearman_base_vs_human'])}</b>Macro Spearman</div>
<div class="stat"><b>{_stat(aggregate['exploratory_unqualified']['macro_spearman_base_vs_human'])}</b>未设门诊断 Macro</div>
<div class="stat"><b>{aggregate['audit_chain_valid_count']}/{payload['case_count']}</b>审计链有效</div>
<div class="stat"><b>{aggregate['observation_artifact_hash_valid_count']}/{payload['case_count']}</b>Observation hash</div></div>
<p class="note"><b>禁止跨主题混排：</b>没有 global Spearman、global pairwise 或跨主题总顺序。正式 Macro 只在三个主题都通过完整性合同时展示；“诊断”统计包含 DEGRADED case，只用于发现偏差，不得门禁或拟合。</p>
{sections}</main></body></html>"""


def run_suite(
    suite_root: Path,
    output: Path,
    *,
    profile_path: Path,
    topic_report_name: str,
    qwen_v3: bool,
    reuse_existing: bool,
) -> dict[str, Any]:
    suite_manifest = json.loads(
        (suite_root / "manifest.json").read_text(encoding="utf-8")
    )
    comparisons: list[Mapping[str, Any]] = []
    hrefs: dict[str, str] = {}
    for item in suite_manifest["topics"]:
        topic = str(item["topic"])
        topic_root = suite_root / str(item["local_directory"])
        topic_output = topic_root / topic_report_name
        comparison = evaluate(
            topic_root,
            topic_output,
            qwen_v3=qwen_v3,
            rerun_products=frozenset() if reuse_existing else None,
            profile_path=profile_path,
        )
        comparisons.append(comparison)
        hrefs[topic] = Path(
            os.path.relpath(topic_output / "index.html", output)
        ).as_posix()
    payload = aggregate_suite(
        suite_manifest=suite_manifest,
        comparisons=comparisons,
        topic_report_hrefs=hrefs,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "index.html").write_text(
        build_suite_html(payload),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_three_topics",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "var"
            / "datasets"
            / "slides_align_three_topics"
            / "report_v83_multi"
        ),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=ROOT / "configs" / "profiles" / "finished_deck_v8.json",
    )
    parser.add_argument("--topic-report-name", default="report_v83")
    parser.add_argument("--qwen-v3", action="store_true")
    parser.add_argument("--reuse-existing", action="store_true")
    args = parser.parse_args()
    payload = run_suite(
        args.suite_root.resolve(),
        args.output.resolve(),
        profile_path=args.profile.resolve(),
        topic_report_name=args.topic_report_name,
        qwen_v3=args.qwen_v3,
        reuse_existing=args.reuse_existing,
    )
    print(json.dumps(payload["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
