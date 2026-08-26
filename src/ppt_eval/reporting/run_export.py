from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _value(report: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in report:
            return report[name]
    return default


def export_run_report(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    """Export one run to audit-friendly Markdown and standalone HTML."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    run_id = str(_value(report, "run_id", "id", default="unknown-run"))
    case_id = str(_value(report, "case_id", default="unknown-case"))
    decision = str(_value(report, "decision", default="UNKNOWN"))
    coverage = str(_value(report, "coverage", "coverage_state", default="UNKNOWN"))
    results = _value(report, "results", "oracle_results", default=[]) or []
    rows = []
    for result in results:
        criterion = result.get("criterion_id") or result.get("oracle_id") or "unknown"
        state = result.get("metric_state") or result.get("status") or "UNKNOWN"
        score = result.get("score", result.get("calibrated_score", "N/A"))
        severity = result.get("severity", "INFO")
        confidence = result.get("confidence", "N/A")
        rows.append(f"| {criterion} | {state} | {score} | {severity} | {confidence} |")
    markdown = "\n".join(
        [
            f"# Run {run_id}",
            "",
            f"- Case: `{case_id}`",
            f"- Decision: `{decision}`",
            f"- Coverage: `{coverage}`",
            f"- Base score: `{_value(report, 'base_score', default='N/A')}`",
            f"- Full score: `{_value(report, 'full_score', default='N/A')}`",
            "",
            "## Atomic Results",
            "",
            "| Criterion | State | Score | Severity | Confidence |",
            "|---|---:|---:|---:|---:|",
            *rows,
            "",
            "## Reproduction Payload",
            "",
            "```json",
            json.dumps(report, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    md_path = output / f"{run_id}.md"
    md_path.write_text(markdown, encoding="utf-8")

    row_html = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row.strip("|").split("|")) + "</tr>"
        for row in rows
    )
    document = f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">
<title>PPT Eval {html.escape(run_id)}</title><style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:0;color:#17201c;background:#eef1ee}}main{{max-width:1100px;margin:auto;padding:28px}}
h1{{font-size:24px}}.meta{{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #ccd5cf;background:white}}
.meta div{{padding:12px;border-right:1px solid #e1e6e2}}.meta span{{display:block;color:#65736a;font-size:12px}}table{{width:100%;border-collapse:collapse;background:white;margin-top:20px}}
th,td{{text-align:left;padding:9px;border-bottom:1px solid #d9dfda}}pre{{overflow:auto;background:#17201c;color:#eef5f0;padding:15px}}
@media(max-width:700px){{.meta{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main><h1>Run {html.escape(run_id)}</h1><section class=\"meta\">
<div><span>Case</span>{html.escape(case_id)}</div><div><span>Decision</span>{html.escape(decision)}</div><div><span>Coverage</span>{html.escape(coverage)}</div><div><span>Score</span>{html.escape(str(_value(report, 'full_score', 'base_score', default='N/A')))}</div>
</section><table><thead><tr><th>Criterion</th><th>State</th><th>Score</th><th>Severity</th><th>Confidence</th></tr></thead><tbody>{row_html}</tbody></table>
<h2>Reproduction Payload</h2><pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre></main></body></html>"""
    html_path = output / f"{run_id}.html"
    html_path.write_text(document, encoding="utf-8")
    return md_path, html_path

