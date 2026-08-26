"""Evaluate the pinned Slides-Align market-analysis sample and build an HTML report."""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ppt_eval.config import load_profile  # noqa: E402
from ppt_eval.domain import EvalCase, SceneType  # noqa: E402
from ppt_eval.runtime import (  # noqa: E402
    LocalEvaluationRuntime,
    build_runtime_from_environment,
)

VISUAL_PROXY_WEIGHTS = {
    "visual_hierarchy": 0.12,
    "layout": 0.12,
    "typography": 0.10,
    "style_consistency": 0.10,
    "multimedia_quality": 0.08,
}


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks, with larger values receiving larger ranks."""

    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = average
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


def rank_order(cases: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], float]) -> dict[str, int]:
    ordered = sorted(cases, key=key, reverse=True)
    return {str(item["case_id"]): index for index, item in enumerate(ordered, start=1)}


def deterministic_visual_proxy(results: Mapping[str, Mapping[str, Any]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for metric_id, weight in VISUAL_PROXY_WEIGHTS.items():
        result = results.get(metric_id)
        if not result or result.get("normalized_score") is None:
            continue
        numerator += weight * float(result["normalized_score"])
        denominator += weight
    return 100.0 * numerator / denominator if denominator else 0.0


# Backward-compatible import name for existing notebooks/tests.  New reports
# use ``deterministic_visual_proxy`` so it cannot be mistaken for Qwen VLM.
visual_proxy = deterministic_visual_proxy


def selected_metrics(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    ids = (
        "content_clarity",
        "narrative",
        "visual_hierarchy",
        "layout",
        "typography",
        "style_consistency",
        "multimedia_quality",
        "editability",
        "accessibility",
        "template_residue",
        "llm_content_quality_audit",
        "vlm_visual_quality_audit",
        "advanced_llm_content_review",
        "advanced_vlm_visual_review",
    )
    return {
        metric_id: {
            "score": results[metric_id].get("normalized_score"),
            "confidence": results[metric_id].get("confidence"),
            "evidence_count": len(results[metric_id].get("evidence", [])),
            "evidence_kinds": sorted(
                {item.get("kind") for item in results[metric_id].get("evidence", [])}
            ),
            "metadata": results[metric_id].get("metadata", {}),
        }
        for metric_id in ids
        if metric_id in results
    }


def model_routing_events(path: Path, run_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("run_id") != run_id:
            continue
        if payload.get("event_type") != "MODEL_AUDIT_ROUTING":
            continue
        route = payload.get("payload", {})
        events.append(
            {
                "stage": route.get("stage"),
                "route": route.get("route"),
                "advanced_call_status": route.get("advanced_call_status"),
                "final_recommendation": route.get("final_recommendation"),
                "escalation_reasons": list(route.get("escalation_reasons", ())),
                "human_review_reasons": list(
                    route.get("human_review_reasons", ())
                ),
            }
        )
    return events


def evaluate(
    dataset_root: Path,
    output: Path,
    *,
    qwen_v3: bool = False,
    flash_only: bool = False,
    rerun_products: frozenset[str] | None = None,
    reuse_reports_from: Path | None = None,
    reference_report_dir: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads((dataset_root / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    profile_name = "finished_deck_v3.json" if qwen_v3 else "finished_deck_v2.json"
    profile = load_profile(ROOT / "configs" / "profiles" / profile_name)
    reference_dir = reference_report_dir or (dataset_root / "report")
    if flash_only:
        if not qwen_v3:
            raise ValueError("flash_only requires qwen_v3")
        profile = replace(
            profile,
            metadata={
                **dict(profile.metadata),
                "model_audit_routing": "FLASH_ONLY_BENCHMARK",
            },
        )
    available_products = {
        str(artifact["product_label"]) for artifact in manifest["artifacts"]
    }
    if rerun_products is not None:
        unknown = rerun_products - available_products
        if unknown:
            raise ValueError("unknown products: " + ", ".join(sorted(unknown)))
        if not qwen_v3:
            raise ValueError("--rerun-products requires --qwen-v3")
    cases: list[dict[str, Any]] = []
    for artifact in manifest["artifacts"]:
        pptx = dataset_root / artifact["pptx"]["local_path"]
        case_id = Path(artifact["pptx"]["local_path"]).stem
        report_path = output / f"{case_id}.report.json"
        seed_report_path = (
            None
            if reuse_reports_from is None
            else reuse_reports_from / f"{case_id}.report.json"
        )
        reusable_report_path = (
            report_path
            if report_path.is_file()
            else (
                seed_report_path
                if seed_report_path is not None and seed_report_path.is_file()
                else None
            )
        )
        if rerun_products is not None:
            should_evaluate = str(artifact["product_label"]) in rerun_products
        elif reuse_reports_from is not None:
            should_evaluate = reusable_report_path is None
        else:
            should_evaluate = True
        audit_path = output / "runtime" / case_id / "audit" / "events.jsonl"
        if not should_evaluate:
            if reusable_report_path is None:
                raise FileNotFoundError(
                    f"cannot reuse missing report for {artifact['product_label']}"
                )
            report = json.loads(reusable_report_path.read_text(encoding="utf-8"))
            if reusable_report_path != report_path:
                if reuse_reports_from is None:
                    raise RuntimeError("external reusable report has no source directory")
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                audit_path = (
                    reuse_reports_from
                    / "runtime"
                    / case_id
                    / "audit"
                    / "events.jsonl"
                )
        elif qwen_v3:
            runtime = build_runtime_from_environment(
                output / "runtime" / case_id,
                workspace_root=ROOT,
            )
            artifacts = {
                "slide_images": tuple(
                    {
                        "page_number": page_number,
                        "path": str((dataset_root / record["local_path"]).resolve()),
                        "media_type": "image/png",
                        "sha256": record["sha256"],
                    }
                    for page_number, record in enumerate(
                        artifact["rendered_slides"]["files"],
                        start=1,
                    )
                )
            }
            audit_path = runtime.paths.audit
        else:
            runtime = LocalEvaluationRuntime(output / "runtime" / case_id)
            artifacts = None
            audit_path = runtime.paths.audit
        if should_evaluate:
            report = runtime.evaluate(
                EvalCase(
                    case_id=case_id,
                    scene=SceneType.READY_MADE,
                    pptx_path=str(pptx.resolve()),
                    metadata={
                        "dataset_id": manifest["dataset_id"],
                        "human_rank": artifact["human_rank"],
                    },
                ),
                profile,
                artifacts=artifacts,
            )
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        results = {item["metric_id"]: item for item in report["results"]}
        reference_v2_path = reference_dir / f"{case_id}.report.json"
        reference_v2_score = None
        if reference_v2_path.is_file():
            reference_v2 = json.loads(reference_v2_path.read_text(encoding="utf-8"))
            if reference_v2.get("base_score") is not None:
                reference_v2_score = float(reference_v2["base_score"])
        model_statuses = {
            metric_id: results[metric_id]["metric_status"]
            for metric_id in (
                "llm_content_quality_audit",
                "vlm_visual_quality_audit",
                "llm_scenario_compliance_audit",
                "advanced_llm_content_review",
                "advanced_vlm_visual_review",
                "advanced_llm_scenario_review",
            )
            if metric_id in results
        }
        cases.append(
            {
                "case_id": case_id,
                "product": artifact["product_label"],
                "human_rank": int(artifact["human_rank"]),
                "pptx": artifact["pptx"],
                "renders": artifact["rendered_slides"],
                "run_id": report["run_id"],
                "profile_id": report["profile_id"],
                "profile_version": report["profile_version"],
                "decision": report["decision"],
                "coverage": report["coverage"],
                "base_score": float(report["base_score"]),
                "reference_v2_score": reference_v2_score,
                "score_delta_vs_v2": (
                    None
                    if reference_v2_score is None
                    else round(float(report["base_score"]) - reference_v2_score, 6)
                ),
                "full_score": (
                    None
                    if report["full_score"] is None
                    else float(report["full_score"])
                ),
                "deterministic_visual_proxy": deterministic_visual_proxy(results),
                "visual_proxy": deterministic_visual_proxy(results),
                "metrics": selected_metrics(results),
                "model_audit_statuses": model_statuses,
                "model_audit_routing": model_routing_events(
                    audit_path,
                    report["run_id"],
                ),
                "review_reasons": list(report.get("review_reasons", ())),
                "model_versions": dict(report["manifest"].get("model_versions", {})),
                "prompt_versions": dict(report["manifest"].get("prompt_versions", {})),
                "model_token_usage": {
                    metric_id: results[metric_id].get("metadata", {}).get("usage", {})
                    for metric_id in results
                    if results[metric_id].get("metadata", {}).get("audit_type")
                    == "model"
                },
            }
        )

    human_order = rank_order(cases, lambda item: -float(item["human_rank"]))
    baseline_order = rank_order(cases, lambda item: float(item["base_score"]))
    visual_order = rank_order(
        cases,
        lambda item: float(item["deterministic_visual_proxy"]),
    )
    for item in cases:
        case_id = item["case_id"]
        item["selected_human_order"] = human_order[case_id]
        item["baseline_order"] = baseline_order[case_id]
        item["visual_proxy_order"] = visual_order[case_id]
        item["baseline_rank_delta"] = baseline_order[case_id] - human_order[case_id]
        item["visual_rank_delta"] = visual_order[case_id] - human_order[case_id]

    human_utility = [-float(item["human_rank"]) for item in cases]
    base_scores = [float(item["base_score"]) for item in cases]
    visual_scores = [float(item["deterministic_visual_proxy"]) for item in cases]
    comparable_pairs = len(cases) * (len(cases) - 1) // 2
    comparison: dict[str, Any] = {
        "schema_version": "1.1",
        "dataset_id": manifest["dataset_id"],
        "dataset_revision": manifest["source"]["revision"],
        "topic": manifest["selection"]["topic"],
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "model_audit_mode": (
            "QWEN_V3_FLASH_ONLY"
            if flash_only
            else ("QWEN_V3" if qwen_v3 else "V2_SHADOW_OFFLINE")
        ),
        "comparison_scope": (
            f"{len(cases)} same-topic decks with exact per-deck human ranks"
        ),
        "statistics": {
            "spearman_base_vs_human": spearman(base_scores, human_utility),
            "spearman_visual_proxy_vs_human": spearman(visual_scores, human_utility),
            "spearman_deterministic_visual_proxy_vs_human": spearman(
                visual_scores,
                human_utility,
            ),
            "pairwise_base_accuracy": pairwise_accuracy(
                [int(item["human_rank"]) for item in cases], base_scores
            ),
            "pairwise_visual_proxy_accuracy": pairwise_accuracy(
                [int(item["human_rank"]) for item in cases], visual_scores
            ),
            "pairwise_deterministic_visual_proxy_accuracy": pairwise_accuracy(
                [int(item["human_rank"]) for item in cases], visual_scores
            ),
            "comparable_pairs": comparable_pairs,
        },
        "reference_v2_report_dir": str(reference_dir),
        "reference_v2_statistics": _reference_v2_statistics(reference_dir),
        "cases": sorted(cases, key=lambda item: item["human_rank"]),
        "limitations": [
            (
                f"{len(cases)} decks from one topic are diagnostic and cannot estimate "
                "cross-topic generalization."
            ),
            "Human ranks are ordinal within one topic; rank gaps are not interval-scale score gaps.",
            "The deterministic visual proxy does not measure color harmony, pixel-level contrast, "
            "semantic image relevance, or rendered cross-slide aesthetics.",
            "The observed rank agreement may be partly spurious: object-tree penalties can hit intentional "
            "decorative overlap while visible spacing or unresolved placeholders remain undetected.",
            (
                "Qwen scores are single-run judgments; repeatability and calibration error have not "
                "yet been estimated. VLM uploads at most 12 deterministically sampled pages per deck."
                if qwen_v3
                else "Model-assisted audits are v2 Shadow NA because no Provider is configured."
            ),
        ],
    }
    (output / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "index.html").write_text(build_html(comparison, output, dataset_root), encoding="utf-8")
    return comparison


def build_html(comparison: Mapping[str, Any], output: Path, dataset_root: Path) -> str:
    statistics = comparison["statistics"]
    reference_v2 = comparison.get("reference_v2_statistics") or {}
    reference_note = (
        ""
        if (
            reference_v2.get("spearman_base_vs_human") is None
            or reference_v2.get("comparable_pairs")
            != statistics.get("comparable_pairs")
        )
        else (
            '<p class="note"><b>v2 历史参照：</b>同三例总分 Spearman '
            f'{float(reference_v2["spearman_base_vs_human"]):.2f}；当前 v3 为 '
            f'{float(statistics["spearman_base_vs_human"]):.2f}。两者都只是单一 topic 的描述值。</p>'
        )
    )
    case_sections = "".join(case_html(item, output, dataset_root) for item in comparison["cases"])
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in comparison["limitations"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>真实 PPT 人评对照</title><style>
:root{{--ink:#16202a;--muted:#647383;--line:#dce3e9;--paper:#f7f5f0;--accent:#126b5d;--warm:#c85b37}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "Segoe UI",sans-serif}}
main{{max-width:1240px;margin:auto;padding:44px 28px 80px}} h1{{font:700 42px/1.12 Georgia,serif;margin:0 0 12px}}
h2{{font:700 28px/1.2 Georgia,serif}} h3{{margin:0 0 6px}} .lead{{font-size:18px;color:var(--muted);max-width:900px}}
.summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}} .stat,.case{{background:white;border:1px solid var(--line);border-radius:12px}}
.stat{{padding:18px}} .stat b{{display:block;font-size:26px;color:var(--accent)}} .case{{padding:24px;margin:28px 0}}
.case-head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}} .badge{{display:inline-block;padding:4px 9px;border-radius:999px;background:#e6f1ee;color:var(--accent);font-weight:700;margin-right:5px}}
.warn{{background:#fae9e2;color:#8b341e}} .gallery{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0}}
.gallery img{{width:100%;aspect-ratio:16/9;object-fit:contain;background:#111;border-radius:7px;border:1px solid #ccd4da}}
table{{border-collapse:collapse;width:100%;margin:16px 0}} th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left}} th{{color:var(--muted);font-weight:600}}
.two{{display:grid;grid-template-columns:1.1fr .9fr;gap:22px}} code{{font-family:Consolas,monospace;font-size:12px}}
details{{margin-top:12px}} .note{{border-left:4px solid var(--warm);padding:10px 14px;background:#fff3ed}}
@media(max-width:800px){{.summary,.two,.gallery{{grid-template-columns:1fr}}.case-head{{display:block}}}}
</style></head><body><main>
<h1>真实 PPT：人评排名 vs 当前 Harness</h1>
<p class="lead">Slides-Align 固定 revision，同一 market_analysis 主题。{len(comparison['cases'])} 份 PPTX 通过当前
<b>{html.escape(str(comparison['profile_id']))}</b> 评测；人评 rank 越小越好，Harness 分越高越好。</p>
<div class="summary"><div class="stat"><b>{statistics['spearman_base_vs_human']:.2f}</b>总分 Spearman</div>
<div class="stat"><b>{statistics['spearman_deterministic_visual_proxy_vs_human']:.2f}</b>确定性视觉代理 Spearman</div>
<div class="stat"><b>{statistics['pairwise_base_accuracy']:.0%}</b>总分两两一致</div>
<div class="stat"><b>{statistics['pairwise_deterministic_visual_proxy_accuracy']:.0%}</b>确定性视觉代理两两一致</div></div>
{reference_note}
<p class="note"><b>范围限制：</b>这只是 1 个 topic、{len(comparison['cases'])} 份可配对 deck 的诊断切片，不是跨 topic 相关性结论。</p>
{case_sections}
<section><h2>限制与下一步</h2><ul>{limitations}</ul></section>
</main></body></html>"""


def case_html(item: Mapping[str, Any], output: Path, dataset_root: Path) -> str:
    render_files = item["renders"]["files"]
    images = "".join(
        f'<img loading="lazy" src="{html.escape(Path("..", record["local_path"]).as_posix())}" '
        f'alt="{html.escape(item["product"])} slide {index}">'
        for index, record in enumerate(render_files[:6], start=1)
    )
    all_images = "".join(
        f'<a href="{html.escape(Path("..", record["local_path"]).as_posix())}">slide {index}</a> '
        for index, record in enumerate(render_files, start=1)
    )
    metric_rows = "".join(
        f"<tr><td><code>{html.escape(metric_id)}</code></td>"
        f"<td>{100.0 * float(metric['score']):.1f}</td>"
        f"<td>{int(metric['evidence_count'])}</td>"
        f"<td>{html.escape(', '.join(str(value) for value in metric['evidence_kinds']))}</td></tr>"
        for metric_id, metric in item["metrics"].items()
        if metric["score"] is not None
    )
    agreement = item["baseline_rank_delta"] == 0 and item["visual_rank_delta"] == 0
    badge = "排序一致" if agreement else "存在排名偏差"
    badge_class = "badge" if agreement else "badge warn"
    explanation = comparison_explanation(item)
    route_summary = " -> ".join(
        f"{event['stage']}:{event['route']}"
        for event in item.get("model_audit_routing", ())
    ) or "not enabled"
    review_summary = ", ".join(item.get("review_reasons", ())) or "none"
    total_model_tokens = sum(
        int(usage.get("total_tokens", 0) or 0)
        for usage in item.get("model_token_usage", {}).values()
    )
    pptx_link = Path("..", item["pptx"]["local_path"]).as_posix()
    current_major = str(item.get("profile_version", "")).split(".", 1)[0]
    current_label = f"v{current_major}" if current_major else "Harness"
    reference_badge = (
        ""
        if item.get("reference_v2_score") is None or current_major == "2"
        else f'<span class="badge">v2 {float(item["reference_v2_score"]):.2f}</span>'
    )
    return f"""<section class="case"><div class="case-head"><div><h2>{html.escape(item['product'])}</h2>
<span class="{badge_class}">{badge}</span><span class="badge">人评 #{item['human_rank']}</span>
{reference_badge}<span class="badge">{current_label} {item['base_score']:.2f}</span><span class="badge">确定性视觉代理 {item['deterministic_visual_proxy']:.2f}</span></div>
<div><b>{item['decision']} / {item['coverage']}</b><br><code>{html.escape(item['run_id'])}</code></div></div>
<div class="gallery">{images}</div><details><summary>打开全部 {len(render_files)} 页</summary>{all_images}</details>
<div class="two"><div><h3>关键 Metric</h3><table><thead><tr><th>Metric</th><th>分数</th><th>证据数</th><th>证据类型</th></tr></thead>
<tbody>{metric_rows}</tbody></table></div><div><h3>对照解释</h3><p>{html.escape(explanation)}</p>
<p>当前样本集的人评顺序 #{item['selected_human_order']}；Baseline 顺序 #{item['baseline_order']}；
确定性视觉代理顺序 #{item['visual_proxy_order']}。</p>
<p><b>模型路由：</b>{html.escape(route_summary)}<br>
<b>处置原因：</b>{html.escape(review_summary)}<br>
<b>模型 token：</b>{total_model_tokens}</p>
<p><a href="{html.escape(pptx_link)}">打开原始 PPTX</a></p></div></div></section>"""


def comparison_explanation(item: Mapping[str, Any]) -> str:
    metrics = item["metrics"]
    order_note = (
        f"当前样本集中人评顺序为 {item['selected_human_order']}，"
        f"Harness 顺序为 {item['baseline_order']}"
    )
    llm = _metric_percent(metrics, "llm_content_quality_audit")
    vlm = _metric_percent(metrics, "vlm_visual_quality_audit")
    if item["product"] == "Kimi-Banana":
        editability = _metric_percent(metrics, "editability")
        return (
            f"{order_note}。该 deck 为整页栅格图，editability {editability}；"
            f"渲染语义内容/视觉为 {llm}/{vlm}。人评可见偏好与可编辑交付质量"
            "不是同一构念，不能通过调单一权重消除这个差异。"
        )
    if item["product"] == "Skywork-Banana":
        return (
            f"{order_note}。确定性 layout/typography 较高，但 Flash 内容与视觉分别为"
            f" {llm}/{vlm}，表明对象树的“结构整齐”不等于真实交付观感和内容质量。"
        )
    if item["product"] == "Quake":
        residue = _metric_percent(metrics, "template_residue")
        return (
            f"{order_note}。模板残留仅 {residue}，对应结束页日期/报告人占位符；"
            f"Flash 内容/视觉为 {llm}/{vlm}。该例能检验新模板 Oracle 是否修正旧基线的"
            "高分补偿问题。"
        )
    layout = _metric_percent(metrics, "layout")
    return (
        f"{order_note}。Layout {layout}，Flash 内容/视觉为 {llm}/{vlm}。"
        "若 VLM 视觉分明显高于人评相对名次，说明当前 Prompt/权重可能偏好"
        "表面规整度，而没有充分捕捉内容价值或人类偏好。"
    )


def _metric_percent(metrics: Mapping[str, Any], metric_id: str) -> str:
    value = metrics.get(metric_id, {}).get("score")
    return "N/A" if value is None else f"{100.0 * float(value):.1f}"


def _reference_v2_statistics(reference_dir: Path) -> Mapping[str, Any] | None:
    path = reference_dir / "comparison.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    statistics = payload.get("statistics")
    return dict(statistics) if isinstance(statistics, Mapping) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample" / "report",
    )
    parser.add_argument(
        "--qwen-v3",
        action="store_true",
        help="run the v3 Flash -> Plus -> Human profile with pinned upstream renders",
    )
    parser.add_argument(
        "--flash-only",
        action="store_true",
        help="score with Flash but disable conditional Plus calls for calibration runs",
    )
    parser.add_argument(
        "--rerun-products",
        nargs="+",
        metavar="PRODUCT",
        help="rerun only these products and reuse the other reports in --output",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="make comparison/HTML only from existing per-product reports",
    )
    parser.add_argument(
        "--reuse-reports-from",
        type=Path,
        help="reuse matching reports from another output and evaluate only missing products",
    )
    parser.add_argument(
        "--reference-report-dir",
        type=Path,
        help="same-code baseline report directory used for score/statistic deltas",
    )
    args = parser.parse_args()
    if args.reuse_existing and args.rerun_products is not None:
        parser.error("--reuse-existing and --rerun-products are mutually exclusive")
    if args.flash_only and not args.qwen_v3:
        parser.error("--flash-only requires --qwen-v3")
    comparison = evaluate(
        args.dataset_root.resolve(),
        args.output.resolve(),
        qwen_v3=args.qwen_v3,
        flash_only=args.flash_only,
        rerun_products=(
            frozenset()
            if args.reuse_existing
            else (
                None
                if args.rerun_products is None
                else frozenset(args.rerun_products)
            )
        ),
        reuse_reports_from=(
            None
            if args.reuse_reports_from is None
            else args.reuse_reports_from.resolve()
        ),
        reference_report_dir=(
            None
            if args.reference_report_dir is None
            else args.reference_report_dir.resolve()
        ),
    )
    print(json.dumps(comparison["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
