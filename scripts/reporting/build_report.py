#!/usr/bin/env python3
"""Build a read-only HTML audit site and the nine-slide deck input."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from verify_audit import load_events, load_json, validate_events, validate_files, validate_snapshot

GENERATOR_VERSION = "1.0.0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inline(text: str) -> str:
    value = html.escape(text)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    return value


def markdown_to_html(markdown: str) -> str:
    """Render a deliberately small, escaped Markdown subset without scripts."""
    lines = markdown.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    list_open = False
    code_open = False
    table_rows: list[list[str]] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            out.append(f"<p>{inline(' '.join(part.strip() for part in paragraph))}</p>")
            paragraph = []

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        rows = table_rows
        table_rows = []
        if len(rows) > 1 and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in rows[1]):
            header, body = rows[0], rows[2:]
        else:
            header, body = rows[0], rows[1:]
        out.append("<div class=\"table-wrap\"><table><thead><tr>" + "".join(f"<th>{inline(cell)}</th>" for cell in header) + "</tr></thead><tbody>")
        for row in body:
            out.append("<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>")
        out.append("</tbody></table></div>")

    for raw in lines + [""]:
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            flush_table()
            if list_open:
                out.append("</ul>")
                list_open = False
            out.append("</code></pre>" if code_open else "<pre><code>")
            code_open = not code_open
            continue
        if code_open:
            out.append(html.escape(line) + "\n")
            continue
        if line.startswith("|") and line.endswith("|"):
            flush_paragraph()
            table_rows.append([cell.strip() for cell in line.strip("|").split("|")])
            continue
        flush_table()
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            if list_open:
                out.append("</ul>")
                list_open = False
            level = min(len(heading.group(1)) + 1, 5)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif line.startswith("- "):
            flush_paragraph()
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline(line[2:].strip())}</li>")
        elif re.match(r"^\d+\.\s", line):
            flush_paragraph()
            if not list_open:
                out.append("<ul>")
                list_open = True
            numbered_item = re.sub(r"^\d+\.\s+", "", line)
            out.append(f"<li>{inline(numbered_item)}</li>")
        elif line.startswith("> "):
            flush_paragraph()
            out.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        elif not line.strip():
            flush_paragraph()
            if list_open:
                out.append("</ul>")
                list_open = False
        else:
            paragraph.append(line)
    return "\n".join(out)


def build_html(data: dict[str, Any], events: list[dict[str, Any]], root: Path, input_hashes: dict[str, str]) -> str:
    project = data["project"]
    phases_html: list[str] = []
    for ordinal, phase in enumerate(data["phases"], 1):
        sources: list[str] = []
        for relative in phase["evidence"]:
            source_path = (root / relative).resolve()
            content = source_path.read_text(encoding="utf-8")
            sources.append(
                f"<details><summary>{html.escape(relative)} <span class=\"hash\">sha256:{sha256(source_path)[:12]}</span></summary>"
                f"<article class=\"markdown\">{markdown_to_html(content)}</article></details>"
            )
        gate = phase["gate"]
        phases_html.append(f"""
        <section id="phase-{ordinal}" class="phase">
          <div class="phase-kicker">0{ordinal} / {html.escape(phase['name'])}</div>
          <h2>{html.escape(phase['objective'])}</h2>
          <div class="phase-grid">
            <div><h3>执行过程</h3><ol>{''.join(f'<li>{inline(x)}</li>' for x in phase['process'])}</ol></div>
            <div><h3>决策与取舍</h3><ul>{''.join(f'<li>{inline(x)}</li>' for x in phase['decisions'])}</ul></div>
          </div>
          <div class="gate"><strong>阶段门禁：{html.escape(gate['status'])}</strong>
            <span>{gate['criteria_met']} / {gate['criteria_total']} 条已满足</span><p>{inline(gate['note'])}</p></div>
          <h3>遗留风险</h3><ul>{''.join(f'<li>{inline(x)}</li>' for x in phase['risks'])}</ul>
          <h3>证据文件</h3>{''.join(sources)}
        </section>""")

    trace_rows = "".join(
        "<tr>" + "".join(f"<td><code>{html.escape(str(row[key]))}</code></td>" for key in ("requirement", "decision", "oracle", "test", "experiment", "release")) + "</tr>"
        for row in data["traceability"]
    )
    event_rows = "".join(
        f"<tr><td><code>{html.escape(event['event_id'])}</code></td><td>{html.escape(event['occurred_at'])}</td>"
        f"<td>{html.escape(event['event_type'])}</td><td><code>{html.escape(event['subject_id'])}</code></td>"
        f"<td>{inline(json.dumps(event['payload'], ensure_ascii=False, separators=(',', ':')))}</td></tr>"
        for event in events
    )
    targets = "".join(
        f"<tr><td>{html.escape(target['metric'])}</td><td><code>{html.escape(target['operator'])} {target['target']}</code></td>"
        f"<td class=\"status\">{html.escape(target['status'])}</td></tr>" for target in data["acceptance_targets"]
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>{html.escape(project['name'])} · 审计汇报</title>
<style>
:root{{--ink:#121212;--muted:#60656f;--rule:#c9cdd3;--paper:#fff;--panel:#f2f3f5;--accent:#1477d4;--warn:#b44d00}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif}}
header,main,footer{{max-width:1160px;margin:auto;padding:0 40px}} header{{padding-top:72px;padding-bottom:52px;border-bottom:1px solid var(--rule)}}
.eyebrow,.phase-kicker{{font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}}
h1{{font-size:48px;line-height:1.12;max-width:900px;margin:18px 0}} h2{{font-size:34px;line-height:1.25;margin:10px 0 30px}} h3{{font-size:20px;margin:28px 0 10px}}
.lead{{font-size:21px;max-width:850px;color:#323740}} .meta{{display:flex;gap:28px;flex-wrap:wrap;color:var(--muted)}}
nav{{position:sticky;top:0;background:#fffffff5;border-bottom:1px solid var(--rule);z-index:2}} nav div{{max-width:1160px;margin:auto;padding:12px 40px;display:flex;gap:24px}}
a{{color:var(--accent);text-decoration:none}} .phase{{padding:70px 0;border-bottom:1px solid var(--rule)}} .phase-grid{{display:grid;grid-template-columns:1fr 1fr;gap:52px}}
.gate{{margin:28px 0;padding:20px 24px;border-left:5px solid var(--warn);background:var(--panel)}} .gate span{{margin-left:20px;color:var(--muted)}}
details{{border-top:1px solid var(--rule);padding:14px 0}} summary{{cursor:pointer;font-weight:700}} .hash{{font:12px ui-monospace,monospace;color:var(--muted)}}
.markdown{{padding:12px 24px;background:#fafafa;border-left:2px solid var(--rule)}} .markdown h2{{font-size:25px;margin:20px 0 10px}} .markdown h3{{font-size:19px}}
.table-wrap{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:10px 12px;border-bottom:1px solid #dde0e4;text-align:left;vertical-align:top}} th{{background:var(--panel)}}
code{{font-family:ui-monospace,Consolas,monospace;font-size:.9em}} pre{{padding:18px;background:#15171a;color:#f5f5f5;overflow:auto}} blockquote{{border-left:4px solid var(--accent);margin:18px 0;padding:4px 18px;color:var(--muted)}}
.status{{font-weight:800;color:var(--warn)}} footer{{padding-top:36px;padding-bottom:70px;color:var(--muted)}}
@media(max-width:720px){{header,main,footer{{padding-left:20px;padding-right:20px}} h1{{font-size:36px}} .phase-grid{{grid-template-columns:1fr}} nav div{{padding-left:20px;overflow:auto}}}}
@media print{{nav{{display:none}} details>article{{display:block}} .phase{{break-before:page}}}}
</style></head><body>
<header><div class="eyebrow">只读审计快照 · {html.escape(project['status'])}</div><h1>{html.escape(project['name'])}</h1>
<p class="lead">{html.escape(project['purpose'])}</p><div class="meta"><span>项目 {html.escape(project['id'])}</span><span>截至 {html.escape(project['as_of'])}</span><span>Schema {html.escape(data['schema_version'])}</span></div></header>
<nav><div><a href="#phase-1">调研</a><a href="#phase-2">开发</a><a href="#phase-3">评测</a><a href="#trace">追踪</a><a href="#events">事件</a></div></nav>
<main>{''.join(phases_html)}
<section id="trace" class="phase"><div class="phase-kicker">Traceability</div><h2>结论沿不可变 ID 回放</h2><div class="table-wrap"><table><thead><tr><th>REQ</th><th>ADR</th><th>Oracle</th><th>Test</th><th>Experiment</th><th>Release</th></tr></thead><tbody>{trace_rows}</tbody></table></div></section>
<section class="phase"><div class="phase-kicker">Pre-registered targets</div><h2>门槛是计划，不是已达成绩</h2><table><thead><tr><th>指标</th><th>目标</th><th>状态</th></tr></thead><tbody>{targets}</tbody></table></section>
<section id="events" class="phase"><div class="phase-kicker">Append-only log</div><h2>{len(events)} 条示例审计事件</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>时间</th><th>类型</th><th>主体</th><th>Payload</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>
</main><footer>输入哈希：audit <code>{input_hashes['audit']}</code> · events <code>{input_hashes['events']}</code><br>由 reporting generator {GENERATOR_VERSION} 生成；页面无 JavaScript、表单或外部请求。</footer>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    audit_path = args.audit.resolve()
    events_path = args.events.resolve()
    data = load_json(audit_path)
    events = load_events(events_path)
    validate_snapshot(data)
    validate_events(events)
    validate_files(audit_path, data)
    root = audit_path.parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    input_hashes = {"audit": sha256(audit_path), "events": sha256(events_path)}
    source_hashes = {relative: sha256(root / relative) for phase in data["phases"] for relative in phase["evidence"]}
    deck_data = {
        "schema_version": "1.0",
        "generated_from": {"audit": str(audit_path), "events": str(events_path), "hashes": input_hashes},
        "project": data["project"], "formula": data["formula"], "scenarios": data["scenarios"],
        "acceptance_targets": data["acceptance_targets"], "traceability": data["traceability"],
        "slides": data["deck"]["slides"], "deck": {key: data["deck"][key] for key in ("title", "audience", "takeaway")},
        "source_hashes": source_hashes,
    }
    deck_path = output / "deck-data.json"
    deck_path.write_text(json.dumps(deck_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path = output / "project-audit.html"
    html_path.write_text(build_html(data, events, root, input_hashes), encoding="utf-8")
    manifest = {
        "generator": f"build_report.py/{GENERATOR_VERSION}", "generated_at": data["project"]["as_of"],
        "inputs": input_hashes, "sources": source_hashes,
        "outputs": {"project-audit.html": sha256(html_path), "deck-data.json": sha256(deck_path)},
        "presentation": {
            "status": "pending_artifact_tool_execution",
            "expected_output": "ppt-eval-interview.pptx",
            "slide_count": 9,
            "chapters": ["调研", "开发", "评测"],
        },
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {html_path}")
    print(f"wrote {deck_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
