"""Production-facing audit queue and human-attention projections.

This module consumes only persisted Harness facts.  It deliberately has no
knowledge of benchmark ranks, Spearman statistics, or comparison reports.
Machine reports remain immutable; reviewers append separate ReviewEvent data.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast

TRIAGE_POLICY_VERSION = "audit-attention@0.9.0"
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_SEVERITY_ORDER = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}
_LEGACY_RESOLVED_VERDICTS = {"APPROVE", "REJECT"}
_WRITABLE_REVIEW_VERDICTS = {
    "CONFIRM_SYSTEM_DECISION",
    "OVERRIDE_DECISION",
    "REQUEST_MORE_EVIDENCE",
}
_RESOLVED_VERDICTS = _LEGACY_RESOLVED_VERDICTS | (
    _WRITABLE_REVIEW_VERDICTS - {"REQUEST_MORE_EVIDENCE"}
)
_ISSUE_RESOLUTIONS = {"CONFIRMED", "FALSE_POSITIVE", "INSUFFICIENT_EVIDENCE"}
_DECISIONS = {"PASS", "REVIEW", "FAIL", "ERROR"}


def _build_raw_attention_projection(
    report: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Project persisted evidence into stable, grouped human-attention issues."""

    run_id = str(report.get("run_id") or "unknown-run")
    results = tuple(_mappings(report.get("results")))
    results_by_metric = {
        str(item.get("metric_id")): item
        for item in results
        if item.get("metric_id")
    }
    observations_by_id = {
        str(item.get("observation_id")): item
        for item in observations
        if item.get("observation_id")
    }
    issues: list[dict[str, Any]] = []
    used_observation_ids: set[str] = set()

    def add_issue(
        *,
        category: str,
        priority: str,
        severity: str,
        title: str,
        summary: str,
        metric_id: str | None = None,
        page_numbers: Sequence[int] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
        lineage: Mapping[str, Any] | None = None,
        identity_parts: Sequence[str] = (),
    ) -> None:
        pages = sorted({page for page in page_numbers if page > 0})
        normalized_evidence = [_evidence_projection(item) for item in evidence]
        identity = {
            "run_id": run_id,
            "category": category,
            "metric_id": metric_id,
            "pages": pages,
            "parts": list(identity_parts),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        issues.append(
            {
                "issue_id": f"att-{digest}",
                "priority": priority,
                "kind": category,
                "title": title,
                "summary": summary,
                "severity": severity,
                "status": "OPEN",
                "metric_id": metric_id,
                "page_numbers": pages,
                "evidence": normalized_evidence,
                "lineage": dict(lineage or {}),
            }
        )

    errors = [str(item) for item in report.get("errors", ()) if str(item).strip()]
    if errors:
        add_issue(
            category="HARNESS_ERROR",
            priority="P0",
            severity="CRITICAL",
            title="运行或审计链存在错误",
            summary="；".join(errors[:4]),
            identity_parts=errors,
        )

    functional_gate = results_by_metric.get("v8_functional_integrity")
    nested_gate_metric_ids: set[str] = set()
    if functional_gate is not None:
        metadata = _mapping(functional_gate.get("metadata"))
        for gate in _mappings(metadata.get("gate_verdicts")):
            verdict = str(gate.get("verdict") or "UNRESOLVED")
            if verdict not in {"CONFIRMED", "UNRESOLVED"}:
                continue
            metric_id = str(gate.get("metric_id") or "v8_functional_integrity")
            nested_gate_metric_ids.add(metric_id)
            observation_ids = [str(item) for item in gate.get("observation_ids", ())]
            gate_observations = [
                observations_by_id[item]
                for item in observation_ids
                if item in observations_by_id
            ]
            used_observation_ids.update(observation_ids)
            rule_evidence = [
                evidence
                for observation in gate_observations
                for evidence in _mappings(observation.get("evidence"))
            ]
            pages = _evidence_pages(rule_evidence)
            model_metric_id = str(gate.get("model_metric_id") or "")
            model_result = results_by_metric.get(model_metric_id)
            model_evidence = (
                list(_mappings(model_result.get("evidence")))
                if model_result is not None
                else []
            )
            matching_model_evidence = [
                {**item, "source": "MODEL"}
                for item in model_evidence
                if not pages or _positive_int(item.get("page_number")) in pages
            ]
            all_evidence = [*rule_evidence, *matching_model_evidence]
            severity = "CRITICAL" if verdict == "CONFIRMED" else "MAJOR"
            title = (
                f"{metric_id} 硬门已由同构念证据确认"
                if verdict == "CONFIRMED"
                else f"{metric_id} 硬门证据尚未收敛"
            )
            summary = (
                f"规则严重度 {gate.get('rule_severity') or 'UNKNOWN'}；"
                f"模型严重度 {gate.get('model_severity') or 'UNKNOWN'}；"
                f"候选页 {', '.join(str(page) for page in pages) or '未绑定页面'}。"
            )
            model_metadata = _mapping(model_result.get("metadata")) if model_result else {}
            add_issue(
                category=(
                    "HARD_GATE_CONFIRMED"
                    if verdict == "CONFIRMED"
                    else "HARD_GATE_UNRESOLVED"
                ),
                priority="P0",
                severity=severity,
                title=title,
                summary=summary,
                metric_id=metric_id,
                page_numbers=pages,
                evidence=all_evidence,
                lineage={
                    "verdict": verdict,
                    "rule_severity": gate.get("rule_severity"),
                    "model_metric_id": model_metric_id or None,
                    "model_severity": gate.get("model_severity"),
                    "rule_observation_ids": observation_ids,
                    "model_sampled_pages": list(model_metadata.get("sampled_pages", ())),
                    "forced_rule_pages": list(model_metadata.get("forced_rule_pages", ())),
                },
                identity_parts=[*observation_ids, verdict, model_metric_id],
            )

    breakdown = _mapping(report.get("score_breakdown"))
    unresolved_metrics = {
        str(item) for item in breakdown.get("unresolved_metric_ids", ()) if str(item)
    }
    for metric_id in sorted(unresolved_metrics - nested_gate_metric_ids):
        result = results_by_metric.get(metric_id)
        evidence = list(_mappings(result.get("evidence"))) if result else []
        metadata = _mapping(result.get("metadata")) if result else {}
        add_issue(
            category="UNRESOLVED_METRIC",
            priority="P0",
            severity="MAJOR",
            title=f"{metric_id} 无法形成可计分结论",
            summary=str(metadata.get("reason_code") or "required metric is unresolved"),
            metric_id=metric_id,
            page_numbers=_evidence_pages(evidence),
            evidence=evidence,
            identity_parts=[metric_id, str(metadata.get("reason_code") or "")],
        )

    for result in results:
        metadata = _mapping(result.get("metadata"))
        attempts = tuple(_mappings(metadata.get("routing_attempts")))
        if not attempts:
            continue
        metric_id = str(result.get("metric_id") or metadata.get("criterion_id") or "model")
        failed_attempts = [
            item
            for item in attempts
            if str(item.get("execution_status") or "") == "ERROR"
            or str(item.get("metric_status") or "") == "ERROR"
            or item.get("error_code")
        ]
        if failed_attempts:
            recovered = str(result.get("metric_status") or "") == "SCORED"
            evidence = [
                evidence_item
                for attempt in attempts
                for evidence_item in _mappings(attempt.get("evidence"))
            ]
            codes = sorted(
                {
                    str(item.get("error_code") or "MODEL_PROVIDER_ERROR")
                    for item in failed_attempts
                }
            )
            add_issue(
                category="PROVIDER_ERROR_RECOVERED" if recovered else "PROVIDER_ERROR",
                priority="P1" if recovered else "P0",
                severity="MAJOR" if recovered else "CRITICAL",
                title=(
                    f"{metric_id} Provider 错误已由后续层恢复"
                    if recovered
                    else f"{metric_id} Provider 错误未恢复"
                ),
                summary="、".join(codes),
                metric_id=metric_id,
                page_numbers=_evidence_pages(evidence),
                evidence=evidence,
                lineage={"routing_attempts": [_routing_attempt(item) for item in attempts]},
                identity_parts=[metric_id, *codes],
            )
        reason = str(metadata.get("escalation_reason") or "")
        if "DISAGREEMENT" in reason:
            evidence = [
                evidence_item
                for attempt in attempts
                for evidence_item in _mappings(attempt.get("evidence"))
            ]
            add_issue(
                category="RULE_MODEL_DISAGREEMENT",
                priority="P1",
                severity="MAJOR",
                title=f"{metric_id} 的规则与模型判断不一致",
                summary="已按同构念路由升级；请核对关联页面及最终选用证据。",
                metric_id=metric_id,
                page_numbers=_evidence_pages(evidence),
                evidence=evidence,
                lineage={"routing_attempts": [_routing_attempt(item) for item in attempts]},
                identity_parts=[metric_id, reason],
            )

    observation_groups: dict[tuple[str, int | None], list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        observation_id = str(observation.get("observation_id") or "")
        if observation_id in used_observation_ids:
            continue
        severity = str(observation.get("severity") or "INFO")
        if severity not in {"CRITICAL", "MAJOR"}:
            continue
        page_number = _observation_page(observation)
        key = (str(observation.get("metric_id") or "unknown"), page_number)
        observation_groups[key].append(observation)

    ordered_groups = sorted(
        observation_groups.items(),
        key=lambda pair: (
            min(
                _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4)
                for item in pair[1]
            ),
            pair[0][1] if pair[0][1] is not None else 10**9,
            pair[0][0],
        ),
    )
    for (metric_id, page_number), group in ordered_groups:
        severity = min(
            (str(item.get("severity") or "INFO") for item in group),
            key=lambda item: _SEVERITY_ORDER.get(item, 4),
        )
        evidence = [
            evidence_item
            for observation in group
            for evidence_item in _mappings(observation.get("evidence"))
        ]
        title_suffix = f"第 {page_number} 页" if page_number is not None else "deck 级"
        add_issue(
            category="ATOMIC_DEFECT",
            priority="P1" if severity == "CRITICAL" else "P2",
            severity=severity,
            title=f"{title_suffix}：{metric_id}",
            summary=(
                str(evidence[0].get("message"))
                if evidence and evidence[0].get("message")
                else f"{len(group)} 个原子观察需要人工核对。"
            ),
            metric_id=metric_id,
            page_numbers=(() if page_number is None else (page_number,)),
            evidence=evidence,
            lineage={
                "observation_ids": [str(item.get("observation_id")) for item in group],
                "primary_owner": metric_id,
            },
            identity_parts=[str(item.get("observation_id")) for item in group],
        )

    issues.sort(key=_issue_sort_key)
    return {
        "policy_version": TRIAGE_POLICY_VERSION,
        "items": issues,
        "page_numbers": sorted(
            {page for issue in issues for page in issue["page_numbers"]}
        ),
        "reason_codes": list(dict.fromkeys(issue["kind"] for issue in issues)),
    }


_SEMANTIC_FAMILY_ORDER = {
    "SYSTEM_INTEGRITY": 0,
    "DELIVERY_INTEGRITY": 1,
    "CONTENT_EXPRESSION": 2,
    "TASK_FIDELITY": 3,
    "VISUAL_LAYOUT_READABILITY": 4,
    "VISUAL_COMMUNICATION": 5,
    "SEQUENCE_SYSTEM": 6,
    "AUTHORSHIP": 7,
}
_SEMANTIC_TITLES = {
    "SYSTEM_INTEGRITY": "运行与证据完整性需要复核",
    "DELIVERY_INTEGRITY": "交付完整性需要复核",
    "CONTENT_EXPRESSION": "内容结构与语言表达需要复核",
    "TASK_FIDELITY": "任务、来源与素材对齐需要复核",
    "VISUAL_LAYOUT_READABILITY": "页面布局与文字可读性需要复核",
    "VISUAL_COMMUNICATION": "视觉表达与信息传达需要复核",
    "SEQUENCE_SYSTEM": "页间节奏与视觉系统需要复核",
    "AUTHORSHIP": "内容与视觉的定制化程度需要复核",
}
_METRIC_FAMILY_OWNERS = {
    "v8_functional_integrity": "DELIVERY_INTEGRITY",
    "file_deliverability": "DELIVERY_INTEGRITY",
    "content_structure": "CONTENT_EXPRESSION",
    "language_consistency": "CONTENT_EXPRESSION",
    "compression_richness": "CONTENT_EXPRESSION",
    "critical_content_visibility": "CONTENT_EXPRESSION",
    "instruction": "TASK_FIDELITY",
    "audience": "TASK_FIDELITY",
    "fact_claim": "TASK_FIDELITY",
    "internal_data_consistency": "TASK_FIDELITY",
    "key_point": "TASK_FIDELITY",
    "numeric": "TASK_FIDELITY",
    "source_claim": "TASK_FIDELITY",
    "traceability": "TASK_FIDELITY",
    "asset_coverage": "TASK_FIDELITY",
    "asset_presentation": "TASK_FIDELITY",
    "chart_fidelity": "TASK_FIDELITY",
    "crop_image_integrity": "TASK_FIDELITY",
    "media_integrity": "TASK_FIDELITY",
    "composition_craft": "VISUAL_LAYOUT_READABILITY",
    "typography_craft": "VISUAL_LAYOUT_READABILITY",
    "palette_craft": "VISUAL_COMMUNICATION",
    "visual_communication": "VISUAL_COMMUNICATION",
    "visual_system_sequence": "SEQUENCE_SYSTEM",
    "authorship_specificity_v2": "AUTHORSHIP",
    "authorship_specificity": "AUTHORSHIP",
    "visual_audit_coverage": "SYSTEM_INTEGRITY",
}
_SOURCE_ORDER = {"SYSTEM": 0, "MODEL": 1, "REDUCER": 2, "RULE": 3}
_MAX_MAIN_ISSUES = 8
_MAX_FOCUS_PAGES = 6
_MAX_MAIN_EVIDENCE = 3
_MAX_RATIONALES = 3
_VISUAL_COVERAGE_COLLAPSED_METRICS = frozenset(
    {
        "v8_functional_integrity",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
    }
)
_DEFECT_SEMANTICS = {
    "content_overflow_or_cutoff": ("TEXT_CUTOFF", "文本截断或越界"),
    "occluded_content": ("ELEMENT_OVERLAP", "元素重叠或错位"),
    "content_alignment_issue": ("ELEMENT_MISALIGNMENT", "元素重叠或错位"),
    "cluttered_layout": ("CLUTTERED_LAYOUT", "页面信息拥挤"),
    "poor_visual_hierarchy": ("WEAK_VISUAL_HIERARCHY", "视觉层级不清晰"),
    "illegible_typeface": ("TYPE_LEGIBILITY", "字号与层级可读性"),
    "improper_font_sizing": ("TYPE_LEGIBILITY", "字号与层级可读性"),
    "poor_text_hierarchy": ("TYPE_LEGIBILITY", "字号与层级可读性"),
    "excessive_text_volume": ("READING_LOAD", "页面文字负载过高"),
    "insufficient_color_contrast": ("COLOR_CONTRAST", "文字与背景对比不足"),
    "unclear_data_encoding": ("DATA_ENCODING", "图表或数据编码不清晰"),
    "missing_material_visual_explanation": ("MATERIAL_EXPLANATION", "素材缺少视觉说明"),
    "placeholder_or_stock_visual": ("PLACEHOLDER_VISUAL", "占位或库存素材未完成语义适配"),
    "visible_stock_watermark": ("STOCK_WATERMARK", "图库水印可见"),
    "image_semantics_mismatch": ("IMAGE_SEMANTIC_MISMATCH", "图像与页面主张不匹配"),
    "embedded_text_unreadable": ("EMBEDDED_TEXT_UNREADABLE", "图像或图解内文字难以读取"),
    "disjointed_visual_rhythm": ("VISUAL_RHYTHM", "页间视觉节奏不连贯"),
    "mechanical_cardization": ("TEMPLATE_ROUTINE", "页面呈现机械模板化"),
    "generic_copy_scaffold": ("GENERIC_COPY", "文案缺少具体性"),
    "undeclared_mixed_language": ("MIXED_LANGUAGE", "中英文混用影响语言一致性"),
    "minority_language_page": ("MIXED_LANGUAGE", "中英文混用影响语言一致性"),
    "missing_title_anchor": ("MISSING_TITLE_ANCHOR", "页面缺少清晰标题锚点"),
    "missing_source_reference": ("MISSING_SOURCE_REFERENCE", "来源引用或追溯信息不足"),
    "small_text": ("SMALL_TEXT", "字号过小影响阅读"),
    "out_of_bounds": ("ELEMENT_OUT_OF_BOUNDS", "元素越出页面边界"),
    "overlap": ("ELEMENT_OVERLAP", "元素重叠或错位"),
    "missing_alt_text": ("MISSING_ALT_TEXT", "图片缺少可访问性说明"),
    "rasterized_slide": ("RASTERIZED_SLIDE", "页面栅格化导致编辑性受限"),
    "pixel_contrast_proxy": ("COLOR_CONTRAST", "文字与背景对比不足"),
    "garbled_or_unreadable_text": ("GARBLED_TEXT", "乱码或不可理解文本"),
    "improper_text_styling": ("TEXT_STYLING", "文字样式使用不当"),
    "improper_line_or_character_spacing": ("TEXT_SPACING", "字距或行距影响阅读"),
    "poor_image_quality_or_editing": ("IMAGE_EDITING", "图片质量或编辑不完整"),
    "improper_image_sizing": ("IMAGE_SIZING", "图片尺寸或比例不合适"),
    "irrelevant_visual_content": ("IRRELEVANT_VISUAL", "视觉素材与内容关联较弱"),
    "visible_export_artifact": ("EXPORT_ARTIFACT", "存在可见导出瑕疵"),
    "unbalanced_space_distribution": ("SPACE_DISTRIBUTION", "留白与空间分配失衡"),
    "mismatched_color_combination": ("PALETTE_MISMATCH", "配色组合不协调"),
    "repeated_template_silhouette": ("TEMPLATE_ROUTINE", "页面呈现机械模板化"),
    "ornamental_icon_routine": ("ORNAMENTAL_ICON_ROUTINE", "装饰性小图标重复且缺少功能"),
    "repetitive_decorative_motif": ("REPETITIVE_DECORATION", "装饰元素重复"),
    "inconsistent_component_conventions": ("COMPONENT_INCONSISTENCY", "组件样式约定不一致"),
    "inconsistent_grid_system": ("GRID_INCONSISTENCY", "跨页网格系统不一致"),
    "inconsistent_typography_system": ("TYPE_SYSTEM_INCONSISTENCY", "跨页字体系统不一致"),
    "weak_focal_claim_specificity": ("WEAK_FOCAL_CLAIM", "页面重点主张不够具体"),
}
_DEFECT_PRIORITY = {
    code: rank
    for rank, code in enumerate(
        (
            "content_overflow_or_cutoff",
            "occluded_content",
            "illegible_typeface",
            "improper_font_sizing",
            "insufficient_color_contrast",
            "cluttered_layout",
            "unclear_data_encoding",
            "missing_material_visual_explanation",
            "placeholder_or_stock_visual",
            "visible_stock_watermark",
            "image_semantics_mismatch",
            "embedded_text_unreadable",
            "mechanical_cardization",
            "generic_copy_scaffold",
            "content_alignment_issue",
            "poor_visual_hierarchy",
            "poor_text_hierarchy",
            "excessive_text_volume",
            "disjointed_visual_rhythm",
            "small_text",
            "out_of_bounds",
            "overlap",
            "pixel_contrast_proxy",
            "missing_title_anchor",
            "undeclared_mixed_language",
            "minority_language_page",
            "missing_source_reference",
            "missing_alt_text",
            "rasterized_slide",
            "garbled_or_unreadable_text",
            "improper_text_styling",
            "improper_line_or_character_spacing",
            "poor_image_quality_or_editing",
            "improper_image_sizing",
            "irrelevant_visual_content",
            "visible_export_artifact",
            "unbalanced_space_distribution",
            "mismatched_color_combination",
            "repeated_template_silhouette",
            "ornamental_icon_routine",
            "repetitive_decorative_motif",
            "inconsistent_component_conventions",
            "inconsistent_grid_system",
            "inconsistent_typography_system",
            "weak_focal_claim_specificity",
        )
    )
}
_CONCRETE_SEMANTIC_CODES = frozenset(
    semantic_code for semantic_code, _title in _DEFECT_SEMANTICS.values()
)
_SEMANTIC_CODE_PRIMARY_FAMILY = {
    "COLOR_CONTRAST": "VISUAL_LAYOUT_READABILITY",
    "TEXT_CUTOFF": "VISUAL_LAYOUT_READABILITY",
    "ELEMENT_OVERLAP": "VISUAL_LAYOUT_READABILITY",
    "ELEMENT_MISALIGNMENT": "VISUAL_LAYOUT_READABILITY",
    "SMALL_TEXT": "VISUAL_LAYOUT_READABILITY",
    "TYPE_LEGIBILITY": "VISUAL_LAYOUT_READABILITY",
    "MIXED_LANGUAGE": "CONTENT_EXPRESSION",
    "GARBLED_TEXT": "CONTENT_EXPRESSION",
    "MISSING_TITLE_ANCHOR": "CONTENT_EXPRESSION",
    "MISSING_SOURCE_REFERENCE": "TASK_FIDELITY",
    "TEMPLATE_ROUTINE": "AUTHORSHIP",
    "GENERIC_COPY": "AUTHORSHIP",
}


def build_attention_projection(
    report: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a bounded semantic projection plus complete raw candidate details."""

    raw = _build_raw_attention_projection(report, observations)
    run_id = str(report.get("run_id") or "unknown-run")
    results = tuple(_mappings(report.get("results")))
    results_by_metric = {
        str(item.get("metric_id")): item
        for item in results
        if item.get("metric_id")
    }
    candidates: list[dict[str, Any]] = []

    def candidate(
        family: str,
        *,
        priority: str,
        severity: str,
        rationale: str,
        metrics: Sequence[str] = (),
        pages: Sequence[int] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
        raw_issue_ids: Sequence[str] = (),
        sources: Sequence[str] = (),
        consensus: str | None = None,
        evidence_gap: bool = False,
    ) -> None:
        candidates.append(
            {
                "family": family,
                "priority": priority,
                "severity": severity,
                "rationale": rationale,
                "metrics": tuple(str(item) for item in metrics if str(item)),
                "pages": tuple(page for page in pages if page > 0),
                "evidence": tuple(evidence),
                "raw_issue_ids": tuple(str(item) for item in raw_issue_ids),
                "sources": tuple(str(item) for item in sources if str(item)),
                "consensus": consensus,
                "evidence_gap": evidence_gap,
            }
        )

    visual_coverage_result = results_by_metric.get("visual_audit_coverage")
    visual_coverage_metadata = (
        _mapping(visual_coverage_result.get("metadata"))
        if visual_coverage_result is not None
        else {}
    )
    visual_coverage_incomplete = bool(
        visual_coverage_result is not None
        and visual_coverage_metadata.get("coverage_complete") is not True
    )
    if visual_coverage_incomplete and visual_coverage_result is not None:
        coverage_evidence = tuple(
            _mappings(visual_coverage_result.get("evidence"))
        )
        unresolved_pages = tuple(
            int(page)
            for page in visual_coverage_metadata.get(
                "forced_pages_not_audited", ()
            )
            if isinstance(page, int) and not isinstance(page, bool) and page > 0
        )
        candidate(
            "SYSTEM_INTEGRITY",
            priority="P1",
            severity="MAJOR",
            rationale=(
                "视觉审计的 Atlas、强制页、cluster 或模型证据未完整；"
                "请先核对 Coverage 合同中的未解风险。"
            ),
            metrics=("visual_audit_coverage",),
            pages=unresolved_pages,
            evidence=coverage_evidence,
            sources=("SYSTEM",),
            consensus="INSUFFICIENT",
            evidence_gap=True,
        )

    for item in raw["items"]:
        kind = str(item.get("kind") or "")
        metric_id = str(item.get("metric_id") or "")
        raw_id = str(item.get("issue_id") or "")
        pages = tuple(int(page) for page in item.get("page_numbers", ()) if int(page) > 0)
        raw_evidence = tuple(_mappings(item.get("evidence")))
        if (
            visual_coverage_incomplete
            and kind == "UNRESOLVED_METRIC"
            and metric_id in _VISUAL_COVERAGE_COLLAPSED_METRICS
        ):
            continue
        if kind == "ATOMIC_DEFECT":
            continue
        if kind == "HARNESS_ERROR":
            candidate(
                "SYSTEM_INTEGRITY",
                priority="P0",
                severity="CRITICAL",
                rationale="运行错误或审计链异常阻止了可靠结论。",
                evidence=raw_evidence,
                raw_issue_ids=(raw_id,),
                sources=("SYSTEM",),
                evidence_gap=True,
            )
            continue
        if kind.startswith("HARD_GATE_"):
            lineage = _mapping(item.get("lineage"))
            model_metric = str(lineage.get("model_metric_id") or "")
            model_result = results_by_metric.get(model_metric)
            model_evidence = tuple(
                _model_evidence(model_result) if model_result is not None else ()
            )
            model_pages = set(_evidence_pages(model_evidence))
            same_page_model = bool(model_pages.intersection(pages))
            family = _semantic_family(model_metric or metric_id)
            if same_page_model:
                candidate(
                    family,
                    priority="P0" if kind == "HARD_GATE_CONFIRMED" else "P1",
                    severity=str(item.get("severity") or "MAJOR"),
                    rationale=(
                        "规则候选已由同页视觉语义证据确认。"
                        if kind == "HARD_GATE_CONFIRMED"
                        else "规则与同页视觉语义证据尚未形成一致结论。"
                    ),
                    metrics=(metric_id, model_metric),
                    pages=(*pages, *model_pages),
                    evidence=(*model_evidence, *raw_evidence),
                    raw_issue_ids=(raw_id,),
                    sources=("RULE", "MODEL"),
                    consensus=(
                        "AGREED" if kind == "HARD_GATE_CONFIRMED" else "CONFLICT"
                    ),
                )
            else:
                candidate(
                    family,
                    priority="P0" if kind == "HARD_GATE_CONFIRMED" else "P1",
                    severity=str(item.get("severity") or "MAJOR"),
                    rationale=(
                        "该质量构念的严重问题已确认，但主视图只有规则侧可定位证据。"
                        if kind == "HARD_GATE_CONFIRMED"
                        else "该质量构念缺少足够的同页复核证据。"
                    ),
                    metrics=(metric_id,),
                    pages=pages,
                    evidence=raw_evidence,
                    raw_issue_ids=(raw_id,),
                    sources=("RULE",),
                    consensus=(
                        "SINGLE_SOURCE"
                        if kind == "HARD_GATE_CONFIRMED"
                        else "INSUFFICIENT"
                    ),
                    evidence_gap=kind != "HARD_GATE_CONFIRMED",
                )
            continue
        if kind == "PROVIDER_ERROR_RECOVERED":
            # The final result and evidence are usable. Keep the failed attempt in
            # full audit lineage, but do not spend reviewer attention on an
            # operational retry that recovered successfully.
            continue
        if kind == "PROVIDER_ERROR":
            candidate(
                "SYSTEM_INTEGRITY",
                priority="P1",
                severity="MAJOR",
                rationale="模型审计调用缺失、失败或未提供完整证据。",
                metrics=(metric_id,),
                pages=pages,
                evidence=raw_evidence,
                raw_issue_ids=(raw_id,),
                sources=("SYSTEM",),
                consensus="INSUFFICIENT",
                evidence_gap=True,
            )
            continue
        if kind == "RULE_MODEL_DISAGREEMENT":
            candidate(
                _semantic_family(metric_id),
                priority="P1",
                severity="MAJOR",
                rationale="规则与视觉语义审计对同一质量构念判断不一致。",
                metrics=(metric_id,),
                pages=pages,
                evidence=raw_evidence,
                raw_issue_ids=(raw_id,),
                sources=("RULE", "MODEL"),
                consensus="CONFLICT",
            )
            continue
        if kind == "UNRESOLVED_METRIC":
            reason = str(item.get("summary") or "")
            if _evidence_gap_reason(reason):
                candidate(
                    "SYSTEM_INTEGRITY",
                    priority="P1",
                    severity="MAJOR",
                    rationale="必需审计证据不足或模型能力尚未配置。",
                    metrics=(metric_id,),
                    pages=pages,
                    evidence=raw_evidence,
                    raw_issue_ids=(raw_id,),
                    sources=("SYSTEM",),
                    consensus="INSUFFICIENT",
                    evidence_gap=True,
                )
            else:
                candidate(
                    _semantic_family(metric_id),
                    priority="P1",
                    severity="MAJOR",
                    rationale="必需的综合质量结论尚未收敛。",
                    metrics=(metric_id,),
                    pages=pages,
                    evidence=raw_evidence,
                    raw_issue_ids=(raw_id,),
                    sources=("REDUCER",),
                    consensus="INSUFFICIENT",
                    evidence_gap=True,
                )

    for result in sorted(results, key=lambda item: str(item.get("metric_id") or "")):
        metric_id = str(result.get("metric_id") or "")
        metadata = _mapping(result.get("metadata"))
        score = result.get("normalized_score")
        is_quality_aggregate = bool(
            metadata.get("reducer_id") or metadata.get("fusion_mode")
        )
        if is_quality_aggregate and _number(score) and float(cast(int | float, score)) < 0.8:
            numeric_score = float(cast(int | float, score))
            candidate(
                _semantic_family(metric_id),
                priority="P1" if numeric_score < 0.6 else "P2",
                severity=str(result.get("severity") or "MINOR"),
                rationale=(
                    "综合质量明显低于审计关注线。"
                    if numeric_score < 0.6
                    else "综合质量低于稳定交付区间。"
                ),
                metrics=(metric_id,),
                pages=_evidence_pages(tuple(_mappings(result.get("evidence")))),
                evidence=tuple(_reducer_evidence(result)),
                sources=("REDUCER",),
                consensus="SINGLE_SOURCE",
            )
        for evidence_item in _model_evidence(result):
            severity = str(evidence_item.get("severity") or "INFO").upper()
            if severity not in {"MAJOR", "CRITICAL"}:
                continue
            candidate(
                _semantic_family(metric_id),
                priority="P1",
                severity=severity,
                rationale="视觉语义审计发现需要人工确认的显著质量问题。",
                metrics=(metric_id,),
                pages=_evidence_pages((evidence_item,)),
                evidence=(evidence_item,),
                sources=("MODEL",),
                consensus="SINGLE_SOURCE",
            )

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in candidates:
        grouped[str(item["family"])].append(item)
    semantic_issues = _deduplicate_semantic_issues(run_id, grouped)
    semantic_issues.sort(key=_semantic_issue_sort_key)
    required = [
        item for item in semantic_issues if item.get("priority") in {"P0", "P1"}
    ]
    optional = [
        item for item in semantic_issues if item.get("priority") not in {"P0", "P1"}
    ]
    presented = [*required, *optional[: max(0, _MAX_MAIN_ISSUES - len(required))]]
    raw_details = [_raw_attention_detail(item) for item in raw["items"]]
    raw_by_id = {
        str(item.get("raw_issue_id")): item
        for item in raw_details
        if item.get("raw_issue_id")
    }
    details = [
        _semantic_attention_detail(item, raw_by_id) for item in semantic_issues
    ]
    assigned_raw_ids = {
        str(raw_id)
        for item in semantic_issues
        for raw_id in _mapping(item.get("lineage")).get("raw_issue_ids", ())
    }
    unassigned_by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw_detail in raw_details:
        if str(raw_detail.get("raw_issue_id")) in assigned_raw_ids:
            continue
        unassigned_by_family[
            _semantic_family(str(raw_detail.get("metric_id") or ""))
        ].append(raw_detail)
    details.extend(
        {
            "semantic_issue_id": None,
            "semantic_family": family,
            "semantic_code": "RAW_AUDIT_FACTS",
            "metric_ids": sorted(
                {
                    str(item.get("metric_id"))
                    for item in values
                    if item.get("metric_id")
                }
            ),
            "all_page_numbers": sorted(
                {
                    int(page)
                    for item in values
                    for page in item.get("page_numbers", ())
                }
            ),
            "raw_candidates": [dict(item) for item in values],
            "raw_candidate_count": len(values),
            "detail_count": len(values),
        }
        for family, values in sorted(
            unassigned_by_family.items(),
            key=lambda pair: _SEMANTIC_FAMILY_ORDER[pair[0]],
        )
    )
    decision = str(report.get("decision") or "ERROR")
    if presented:
        state = "ACTIONABLE"
        summary_title = "评测结论需要人工复核"
    elif decision == "REVIEW":
        state = "REVIEW_WITHOUT_LOCALIZED_ISSUE"
        summary_title = "评测需要复核，但尚无局部化主疑点"
    elif decision == "PASS":
        state = "NO_ISSUE"
        summary_title = "未发现需要优先处理的语义疑点"
    else:
        state = "UNLOCATED_FAILURE"
        summary_title = "评测失败且缺少可定位的主疑点"
    required_count = sum(
        item.get("priority") in {"P0", "P1"} for item in presented
    )
    non_atomic_fact_count = sum(
        item.get("kind") != "ATOMIC_DEFECT" for item in raw_details
    )
    raw_fact_count = len(observations) + non_atomic_fact_count
    return {
        "policy_version": TRIAGE_POLICY_VERSION,
        "items": presented,
        "page_numbers": sorted(
            {page for issue in presented for page in issue["page_numbers"]}
        ),
        "reason_codes": [str(issue["kind"]) for issue in presented],
        "attention_summary": {
            "state": state,
            "title": summary_title,
            "description": (
                f"主视图呈现 {len(presented)} 个语义问题；"
                f"完整审计保留 {len(observations)} 条原子观察"
                f"和 {non_atomic_fact_count} 条其他审计事实。"
            ),
            "total_count": len(presented),
            "required_count": required_count,
            "raw_fact_count": raw_fact_count,
        },
        "attention_details": details,
    }


def _deduplicate_semantic_issues(
    run_id: str,
    grouped: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    records: list[tuple[str, Sequence[Mapping[str, Any]], dict[str, Any]]] = []
    for family in sorted(grouped, key=lambda value: _SEMANTIC_FAMILY_ORDER[value]):
        candidates = grouped[family]
        records.append((family, candidates, _semantic_issue(run_id, family, candidates)))

    concrete_buckets: dict[
        tuple[str, tuple[int, ...]],
        list[tuple[str, Sequence[Mapping[str, Any]], dict[str, Any]]],
    ] = defaultdict(list)
    issues: list[dict[str, Any]] = []
    for family, candidates, issue in records:
        semantic_code = str(issue.get("semantic_code") or "")
        lineage = _mapping(issue.get("lineage"))
        all_pages = tuple(
            sorted(int(page) for page in lineage.get("all_page_numbers", ()))
        )
        if semantic_code not in _CONCRETE_SEMANTIC_CODES:
            issues.append(issue)
            continue
        concrete_buckets[(semantic_code, all_pages)].append(
            (family, candidates, issue)
        )

    for (semantic_code, _all_pages), bucket in sorted(
        concrete_buckets.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        if len(bucket) == 1:
            family, _candidates, issue = bucket[0]
            lineage = dict(_mapping(issue.get("lineage")))
            lineage["primary_owner"] = family
            lineage["contributing_families"] = [family]
            issue["lineage"] = lineage
            issues.append(issue)
            continue
        contributing_families = sorted(
            {family for family, _candidates, _issue in bucket},
            key=lambda family: _SEMANTIC_FAMILY_ORDER[family],
        )
        mapped_owner = _SEMANTIC_CODE_PRIMARY_FAMILY.get(semantic_code)
        primary_owner = (
            mapped_owner
            if mapped_owner in contributing_families
            else contributing_families[0]
        )
        combined_candidates = sorted(
            (
                candidate
                for _family, candidates, _issue in bucket
                for candidate in candidates
            ),
            key=_semantic_candidate_sort_key,
        )
        merged = _semantic_issue(run_id, primary_owner, combined_candidates)
        lineage = dict(_mapping(merged.get("lineage")))
        lineage["primary_owner"] = primary_owner
        lineage["contributing_families"] = contributing_families
        merged["lineage"] = lineage
        issues.append(merged)
    return issues


def _semantic_issue(
    run_id: str,
    family: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    priority = min(
        (str(item.get("priority") or "P3") for item in candidates),
        key=lambda value: _PRIORITY_ORDER.get(value, 4),
    )
    severity = min(
        (str(item.get("severity") or "INFO") for item in candidates),
        key=lambda value: _SEVERITY_ORDER.get(value, 4),
    )
    all_pages = sorted(
        {
            int(page)
            for item in candidates
            for page in item.get("pages", ())
            if int(page) > 0
        }
    )
    focus_pages = all_pages[:_MAX_FOCUS_PAGES]
    metrics = sorted(
        {
            str(metric)
            for item in candidates
            for metric in item.get("metrics", ())
            if str(metric)
        }
    )
    generic_rationales = list(
        dict.fromkeys(
            str(item.get("rationale") or "")
            for item in candidates
            if str(item.get("rationale") or "")
        )
    )
    ranked_evidence = _rank_semantic_evidence(
        [
            evidence_item
            for item in candidates
            for evidence_item in _mappings(item.get("evidence"))
        ]
    )
    model_messages = [
        str(item.get("message") or "")
        for item in ranked_evidence
        if item.get("source") == "MODEL" and str(item.get("message") or "")
    ]
    semantic_rationales = [
        rationale
        for item in ranked_evidence
        for rationale in (_semantic_evidence_rationale(item),)
        if rationale
    ]
    rationales = list(
        dict.fromkeys([*semantic_rationales, *model_messages, *generic_rationales])
    )[:_MAX_RATIONALES]
    defect_codes = sorted(
        {
            str(code)
            for item in ranked_evidence
            for code in item.get("_defect_codes", ())
            if str(code)
        },
        key=lambda code: (_DEFECT_PRIORITY.get(code, 10**9), code),
    )
    semantic_defects: list[tuple[str, str]] = []
    seen_defect_titles: set[str] = set()
    for code in defect_codes:
        semantic = _DEFECT_SEMANTICS.get(code)
        if semantic is None or semantic[1] in seen_defect_titles:
            continue
        semantic_defects.append(semantic)
        seen_defect_titles.add(semantic[1])
    defect_pages = sorted(
        {
            page
            for item in ranked_evidence
            if any(
                str(code) in _DEFECT_SEMANTICS
                for code in item.get("_defect_codes", ())
            )
            for page in _evidence_pages((item,))
        }
    )
    evidence = [
        {str(key): value for key, value in item.items() if not str(key).startswith("_")}
        for item in ranked_evidence[:_MAX_MAIN_EVIDENCE]
    ]
    consensuses = {
        str(item.get("consensus"))
        for item in candidates
        if item.get("consensus")
    }
    if "CONFLICT" in consensuses:
        consensus_status = "CONFLICT"
    elif "AGREED" in consensuses:
        consensus_status = "AGREED"
    elif any(bool(item.get("evidence_gap")) for item in candidates):
        consensus_status = "INSUFFICIENT"
    else:
        consensus_status = "SINGLE_SOURCE"
    sources = sorted(
        {
            str(source)
            for item in candidates
            for source in item.get("sources", ())
            if str(source)
        },
        key=lambda source: _SOURCE_ORDER.get(source, 9),
    )
    conflicting_count = sum(
        item.get("consensus") == "CONFLICT" for item in candidates
    )
    consensus_label = {
        "AGREED": "多源证据一致",
        "CONFLICT": "多源证据冲突",
        "SINGLE_SOURCE": "单一证据来源",
        "INSUFFICIENT": "证据不足",
    }[consensus_status]
    if consensus_status == "SINGLE_SOURCE" and {"MODEL", "REDUCER"}.issubset(
        sources
    ):
        consensus_label = "模型及其聚合结果"
    consensus = {
        "status": consensus_status,
        "sources": sources,
        "label": consensus_label,
        "supporting_count": len(candidates) - conflicting_count,
        "conflicting_count": conflicting_count,
    }
    evidence_integrity = family == "SYSTEM_INTEGRITY" and any(
        bool(item.get("evidence_gap")) for item in candidates
    )
    kind = "EVIDENCE_INTEGRITY" if evidence_integrity else family
    identity = {
        "run_id": run_id,
        "policy_version": TRIAGE_POLICY_VERSION,
        "semantic_family": family,
        "semantic_codes": [item[0] for item in semantic_defects],
        "all_pages": all_pages,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    title = _SEMANTIC_TITLES[family]
    semantic_code = kind
    if len(semantic_defects) == 1:
        semantic_code, title = semantic_defects[0]
    elif semantic_defects:
        semantic_code = f"MULTI_DEFECT_{family}"
        title = "、".join(item[1] for item in semantic_defects[:2])
        if len(semantic_defects) > 2:
            title += "等问题"
    if semantic_defects:
        summary_pages = defect_pages or focus_pages
        shown_pages = summary_pages[:_MAX_FOCUS_PAGES]
        page_text = "、".join(f"第 {page} 页" for page in shown_pages)
        if len(summary_pages) > len(shown_pages):
            page_text += f"等共 {len(summary_pages)} 页"
        page_text = page_text or "抽样页面"
        defect_text = "、".join(item[1] for item in semantic_defects[:3])
        if len(semantic_defects) > 3:
            defect_text += "等"
        audit_label = (
            "视觉语义审计"
            if any(item.get("source") == "MODEL" for item in ranked_evidence)
            else "综合审计"
        )
        summary = (
            f"{audit_label}在{page_text}定位到{defect_text}；"
            "请结合关联页确认影响范围。"
        )
    else:
        summary = "；".join(generic_rationales[:_MAX_RATIONALES]) or "该综合质量构念需要人工复核。"
    return {
        "issue_id": f"att-{digest}",
        "priority": priority,
        "kind": kind,
        "semantic_code": semantic_code,
        "semantic_family": family,
        "title": title,
        "summary": summary,
        "rationales": rationales,
        "consensus": consensus,
        "detail_count": len(candidates),
        "severity": severity,
        "status": "OPEN",
        "metric_id": None,
        "page_numbers": focus_pages,
        "evidence": evidence,
        "lineage": {
            "metric_ids": metrics,
            "all_page_numbers": all_pages,
            "raw_issue_ids": sorted(
                {
                    str(raw_id)
                    for item in candidates
                    for raw_id in item.get("raw_issue_ids", ())
                    if str(raw_id)
                }
            ),
            "candidate_count": len(candidates),
            "semantic_candidates": [
                _semantic_candidate_detail(item)
                for item in sorted(candidates, key=_semantic_candidate_sort_key)
            ],
        },
    }


def _semantic_evidence_rationale(item: Mapping[str, Any]) -> str | None:
    codes = sorted(
        {
            str(code)
            for code in item.get("_defect_codes", ())
            if str(code) in _DEFECT_SEMANTICS
        },
        key=lambda code: (_DEFECT_PRIORITY.get(code, 10**9), code),
    )
    titles = list(
        dict.fromkeys(_DEFECT_SEMANTICS[code][1] for code in codes)
    )
    if not titles:
        return None
    pages = _evidence_pages((item,))
    shown_pages = pages[:_MAX_FOCUS_PAGES]
    if shown_pages:
        page_text = "、".join(f"第 {page} 页" for page in shown_pages)
        if len(pages) > len(shown_pages):
            page_text += f"等共 {len(pages)} 页"
    else:
        page_text = "跨页审计"
    return f"{page_text}：{'、'.join(titles[:3])}。"


def _semantic_candidate_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _PRIORITY_ORDER.get(str(item.get("priority") or "P3"), 4),
        _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4),
        str(item.get("family") or ""),
        tuple(sorted(str(metric) for metric in item.get("metrics", ()))),
        tuple(sorted(int(page) for page in item.get("pages", ()) if int(page) > 0)),
        str(item.get("rationale") or ""),
    )


def _semantic_candidate_detail(item: Mapping[str, Any]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    for raw_evidence in _rank_semantic_evidence(_mappings(item.get("evidence"))):
        projected = {
            str(key): value
            for key, value in raw_evidence.items()
            if not str(key).startswith("_")
        }
        defect_codes = [
            str(code) for code in raw_evidence.get("_defect_codes", ()) if str(code)
        ]
        if defect_codes:
            projected["defect_codes"] = defect_codes
        affected_pages = [
            int(page)
            for page in raw_evidence.get("_affected_page_numbers", ())
            if int(page) > 0
        ]
        if affected_pages:
            projected["affected_page_numbers"] = sorted(set(affected_pages))
        evidence.append(projected)
    return {
        "semantic_family": item.get("family"),
        "priority": item.get("priority"),
        "severity": item.get("severity"),
        "rationale": item.get("rationale"),
        "metric_ids": sorted(str(metric) for metric in item.get("metrics", ())),
        "page_numbers": sorted(
            int(page) for page in item.get("pages", ()) if int(page) > 0
        ),
        "sources": sorted(
            (str(source) for source in item.get("sources", ())),
            key=lambda source: _SOURCE_ORDER.get(source, 9),
        ),
        "consensus": item.get("consensus"),
        "evidence_gap": bool(item.get("evidence_gap")),
        "evidence": evidence,
    }


def _semantic_family(metric_id: str) -> str:
    normalized = metric_id.casefold()
    explicit_owner = _METRIC_FAMILY_OWNERS.get(normalized)
    if explicit_owner is not None:
        return explicit_owner
    if any(
        token in normalized
        for token in ("harness", "provider", "evidence_integrity")
    ):
        return "SYSTEM_INTEGRITY"
    if any(token in normalized for token in ("author", "specificity")):
        return "AUTHORSHIP"
    if any(
        token in normalized
        for token in ("sequence", "cross_slide", "transition", "duplicate_slide")
    ):
        return "SEQUENCE_SYSTEM"
    if any(
        token in normalized
        for token in ("typograph", "readability", "legibility", "contrast", "reading")
    ):
        return "VISUAL_LAYOUT_READABILITY"
    if any(
        token in normalized
        for token in ("imagery", "palette", "visual_communication", "data_visual")
    ):
        return "VISUAL_COMMUNICATION"
    if any(
        token in normalized
        for token in ("layout", "composition", "geometry", "crop", "render_integrity")
    ):
        return "VISUAL_LAYOUT_READABILITY"
    if any(
        token in normalized
        for token in (
            "requirement",
            "instruction",
            "audience",
            "source",
            "claim",
            "asset",
            "chart",
            "numeric",
            "key_point",
            "traceability",
            "media",
            "task_fidelity",
        )
    ):
        return "TASK_FIDELITY"
    if any(
        token in normalized
        for token in (
            "content",
            "language",
            "title",
            "structure",
            "expression",
            "compression",
        )
    ):
        return "CONTENT_EXPRESSION"
    if any(token in normalized for token in ("deliver", "functional", "security")):
        return "DELIVERY_INTEGRITY"
    return "CONTENT_EXPRESSION"


def _evidence_gap_reason(reason: str) -> bool:
    normalized = reason.upper()
    return any(
        token in normalized
        for token in (
            "MISSING",
            "UNCONFIGURED",
            "UNAVAILABLE",
            "PROVIDER",
            "INSUFFICIENT",
            "ERROR",
        )
    ) or reason == "required metric is unresolved"


def _model_evidence(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = _mapping(result.get("metadata"))
    if not metadata.get("criterion_id") and not str(result.get("metric_id") or "").startswith(
        "structured_vlm_"
    ):
        return []
    projected: list[dict[str, Any]] = []
    for item in _mappings(result.get("evidence")):
        evidence = _evidence_projection(item)
        payload = _mapping(item.get("payload"))
        evidence["source"] = "MODEL"
        evidence["severity"] = (
            item.get("severity") or payload.get("severity") or result.get("severity")
        )
        evidence["_defect_codes"] = list(payload.get("defect_codes", ()))
        evidence["_affected_page_numbers"] = [
            page
            for page in (
                _positive_int(value)
                for value in payload.get("affected_page_numbers", ())
            )
            if page is not None
        ]
        projected.append(evidence)
    return projected


def _reducer_evidence(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for item in _mappings(result.get("evidence")):
        evidence = _evidence_projection(item)
        evidence["source"] = "REDUCER"
        kind = str(evidence.get("kind") or "")
        if kind in _DEFECT_SEMANTICS:
            evidence["_defect_codes"] = [kind]
        projected.append(evidence)
    return projected


def _rank_semantic_evidence(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in evidence:
        projected = dict(item)
        source = str(projected.get("source") or "RULE")
        if source not in _SOURCE_ORDER:
            source = "RULE"
        projected["source"] = source
        kind = str(projected.get("kind") or "")
        if not projected.get("_defect_codes") and kind in _DEFECT_SEMANTICS:
            projected["_defect_codes"] = [kind]
        key = (
            source,
            kind,
            _positive_int(projected.get("page_number")),
            str(projected.get("object_id") or ""),
            str(projected.get("message") or ""),
        )
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = projected
            continue
        chosen = min((existing, projected), key=_semantic_evidence_choice_key)
        merged = dict(chosen)
        merged["_defect_codes"] = sorted(
            {
                str(code)
                for value in (existing, projected)
                for code in value.get("_defect_codes", ())
                if str(code)
            }
        )
        merged["_affected_page_numbers"] = sorted(
            {
                int(page)
                for value in (existing, projected)
                for page in value.get("_affected_page_numbers", ())
                if int(page) > 0
            }
        )
        deduplicated[key] = merged
    return sorted(
        deduplicated.values(),
        key=lambda item: (
            _SOURCE_ORDER.get(str(item.get("source") or "RULE"), 4),
            _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4),
            0 if _positive_int(item.get("page_number")) is not None else 1,
            _positive_int(item.get("page_number")) or 10**9,
            str(item.get("evidence_id") or ""),
            str(item.get("kind") or ""),
            str(item.get("object_id") or ""),
            str(item.get("message") or ""),
            json.dumps(item.get("bbox"), sort_keys=True, separators=(",", ":")),
            tuple(sorted(str(code) for code in item.get("_defect_codes", ()))),
            tuple(sorted(int(page) for page in item.get("_affected_page_numbers", ()))),
        ),
    )


def _semantic_evidence_choice_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    confidence = item.get("confidence")
    confidence_rank = (
        -float(cast(int | float, confidence)) if _number(confidence) else 1.0
    )
    return (
        _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4),
        confidence_rank,
        str(item.get("evidence_id") or ""),
        json.dumps(item.get("bbox"), sort_keys=True, separators=(",", ":")),
        tuple(sorted(str(code) for code in item.get("_defect_codes", ()))),
    )


def _raw_attention_detail(item: Mapping[str, Any]) -> dict[str, Any]:
    lineage = _mapping(item.get("lineage"))
    evidence = [dict(value) for value in _mappings(item.get("evidence"))]
    oracle_ids = sorted(
        {
            str(value.get("oracle_id"))
            for value in evidence
            if value.get("oracle_id")
        }
    )
    observation_ids = sorted(
        {
            str(value)
            for key in ("observation_ids", "rule_observation_ids")
            for value in lineage.get(key, ())
            if str(value)
        }
    )
    return {
        "raw_issue_id": item.get("issue_id"),
        "kind": item.get("kind"),
        "metric_id": item.get("metric_id"),
        "oracle_ids": oracle_ids,
        "observation_ids": observation_ids,
        "page_numbers": list(item.get("page_numbers", ())),
        "evidence": evidence,
        "lineage": dict(lineage),
    }


def _semantic_attention_detail(
    issue: Mapping[str, Any],
    raw_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    lineage = _mapping(issue.get("lineage"))
    raw_candidates = [
        dict(raw_by_id[raw_id])
        for raw_id in lineage.get("raw_issue_ids", ())
        if raw_id in raw_by_id
    ]
    return {
        "semantic_issue_id": issue.get("issue_id"),
        "semantic_family": issue.get("semantic_family"),
        "semantic_code": issue.get("semantic_code"),
        "metric_ids": list(lineage.get("metric_ids", ())),
        "all_page_numbers": list(lineage.get("all_page_numbers", ())),
        "semantic_candidates": [
            dict(candidate)
            for candidate in _mappings(lineage.get("semantic_candidates"))
        ],
        "raw_candidates": raw_candidates,
        "raw_candidate_count": len(raw_candidates),
        "detail_count": issue.get("detail_count"),
    }


def _semantic_issue_sort_key(item: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    pages = item.get("page_numbers", ())
    return (
        _PRIORITY_ORDER.get(str(item.get("priority") or "P3"), 4),
        _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4),
        _SEMANTIC_FAMILY_ORDER.get(str(item.get("semantic_family") or ""), 99),
        min(pages, default=10**9),
        str(item.get("issue_id") or ""),
    )


def build_review_task_summary(
    report: Mapping[str, Any],
    *,
    observations: Sequence[Mapping[str, Any]] = (),
    reviews: Sequence[Mapping[str, Any]] = (),
    page_count: int = 0,
) -> dict[str, Any]:
    attention = build_attention_projection(report, observations)
    latest_review = _latest_review(reviews)
    review_state = _review_state(latest_review)
    decision = str(report.get("decision") or "ERROR")
    coverage = str(report.get("coverage") or "UNASSESSABLE")
    issue_priorities = [str(item.get("priority") or "P3") for item in attention["items"]]
    if decision in {"FAIL", "ERROR"} or "P0" in issue_priorities:
        priority = "P0"
    elif (
        coverage in {"DEGRADED", "BASE_ONLY", "UNASSESSABLE"}
        or decision == "REVIEW"
        or "P1" in issue_priorities
    ):
        priority = "P1"
    elif attention["items"] or _non_train_tracks(report):
        priority = "P2"
    else:
        priority = "P3"
    headline = (
        str(attention["items"][0]["title"])
        if attention["items"]
        else "系统未记录优先疑点，可进行常规抽查"
    )
    score = report.get("overall_score")
    if not _number(score):
        score = report.get("full_score")
    if not _number(score):
        score = report.get("base_score")
    return {
        "run_id": str(report.get("run_id") or ""),
        "case_id": str(report.get("case_id") or ""),
        "scenario": str(report.get("scenario") or report.get("scene") or "unknown"),
        "decision": decision,
        "coverage": coverage,
        "score": float(cast(int | float, score)) if _number(score) else None,
        "priority": priority,
        "priority_reason": headline,
        "issue_count": len(attention["items"]),
        "page_count": page_count,
        "review_state": review_state,
        "created_at": report.get("created_at"),
        "profile_id": report.get("profile_id"),
        "profile_version": report.get("profile_version"),
        "training_tracks": _training_tracks(report),
        "latest_review": dict(latest_review) if latest_review is not None else None,
        "triage_policy_version": TRIAGE_POLICY_VERSION,
        "attention_summary": dict(attention["attention_summary"]),
    }


def audit_task_sort_key(task: Mapping[str, Any]) -> tuple[int, int, str, str]:
    """Stable production ordering; intentionally excludes benchmark labels."""

    state_rank = {"OPEN": 0, "NEEDS_EVIDENCE": 1, "RESOLVED": 2}.get(
        str(task.get("review_state") or "OPEN"), 3
    )
    priority_rank = _PRIORITY_ORDER.get(str(task.get("priority") or "P3"), 4)
    created_at = str(task.get("created_at") or "")
    return state_rank, priority_rank, created_at, str(task.get("run_id") or "")


def normalize_review_payload(
    report: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    valid_issue_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate and enrich an append-only review event."""

    run_id = str(payload.get("run_id") or "").strip()
    if not run_id or run_id != str(report.get("run_id") or ""):
        raise ValueError("review run_id does not match the persisted report")
    reviewer_id = str(payload.get("reviewer_id") or "").strip()
    if not reviewer_id:
        raise ValueError("reviewer_id must be non-blank")
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in _WRITABLE_REVIEW_VERDICTS:
        raise ValueError("unsupported review verdict")
    note = str(payload.get("note") or "").strip()
    if verdict in {"OVERRIDE_DECISION", "REQUEST_MORE_EVIDENCE"} and not note:
        raise ValueError("override and evidence-request reviews require a note")
    target_decision = payload.get("target_decision")
    if verdict == "OVERRIDE_DECISION":
        target = str(target_decision or "").upper()
        if target not in _DECISIONS:
            raise ValueError("OVERRIDE_DECISION requires a valid target_decision")
        target_decision = target
    else:
        target_decision = None

    known_issue_ids = set(valid_issue_ids)
    resolutions: list[dict[str, Any]] = []
    seen_issue_ids: set[str] = set()
    for item in _mappings(payload.get("issue_resolutions")):
        issue_id = str(item.get("issue_id") or "")
        resolution = str(item.get("resolution") or "").upper()
        resolution_note = str(item.get("note") or "").strip()
        if issue_id not in known_issue_ids:
            raise ValueError(f"unknown attention issue {issue_id!r}")
        if issue_id in seen_issue_ids:
            raise ValueError(f"duplicate attention issue {issue_id!r}")
        if resolution not in _ISSUE_RESOLUTIONS:
            raise ValueError("unsupported issue resolution")
        if resolution != "CONFIRMED" and not resolution_note and not note:
            raise ValueError("false-positive and insufficient-evidence judgments require a note")
        seen_issue_ids.add(issue_id)
        resolutions.append(
            {
                "issue_id": issue_id,
                "resolution": resolution,
                "note": resolution_note,
            }
        )

    client_request_id = str(payload.get("client_request_id") or "").strip()
    if client_request_id and len(client_request_id) > 128:
        raise ValueError("client_request_id is too long")
    manifest = _mapping(report.get("manifest"))
    observation_artifact = _mapping(report.get("observation_artifact"))
    return {
        "run_id": run_id,
        "reviewer_id": reviewer_id,
        "verdict": verdict,
        "target_decision": target_decision,
        "note": note,
        "client_request_id": client_request_id or None,
        "issue_resolutions": resolutions,
        "track_resolutions": _track_resolutions(payload.get("track_resolutions")),
        "machine_decision": report.get("decision"),
        "machine_coverage": report.get("coverage"),
        "report_hash": manifest.get("result_hash"),
        "observation_hash": observation_artifact.get("sha256"),
        "triage_policy_version": TRIAGE_POLICY_VERSION,
    }


def _training_tracks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    training = _mapping(report.get("training_eligibility"))
    return [
        {
            "track": item.get("track"),
            "status": item.get("status"),
            "score": item.get("score"),
            "reason_codes": list(item.get("reason_codes", ())),
        }
        for item in _mappings(training.get("decisions"))
    ]


def _non_train_tracks(report: Mapping[str, Any]) -> bool:
    return any(str(item.get("status") or "") != "TRAIN" for item in _training_tracks(report))


def _track_resolutions(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("track_resolutions must be a mapping")
    allowed_tracks = {"visual", "layout", "content", "full_deck"}
    allowed_statuses = {"TRAIN", "REVIEW", "REJECT"}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        track = str(key)
        status = str(item).upper()
        if track not in allowed_tracks or status not in allowed_statuses:
            raise ValueError("invalid training track resolution")
        normalized[track] = status
    return normalized


def _latest_review(reviews: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return max(reviews, key=lambda item: str(item.get("created_at") or ""), default=None)


def _review_state(review: Mapping[str, Any] | None) -> str:
    if review is None:
        return "OPEN"
    verdict = str(review.get("verdict") or "").upper()
    if verdict == "REQUEST_MORE_EVIDENCE":
        return "NEEDS_EVIDENCE"
    return "RESOLVED" if verdict in _RESOLVED_VERDICTS else "OPEN"


def _routing_attempt(item: Mapping[str, Any]) -> dict[str, Any]:
    model = _mapping(item.get("model"))
    return {
        "tier": item.get("tier"),
        "selected": item.get("selected"),
        "execution_status": item.get("execution_status"),
        "metric_status": item.get("metric_status"),
        "confidence": item.get("confidence"),
        "error_code": item.get("error_code"),
        "model_id": model.get("model_id") or item.get("configured_model"),
    }


def _evidence_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(item.get("payload"))
    source = str(item.get("source") or "")
    if source not in {"RULE", "MODEL", "REDUCER", "SYSTEM"}:
        source = "MODEL" if item.get("criterion_id") or payload.get("criterion_id") else "RULE"
    result: dict[str, Any] = {
        "evidence_id": item.get("evidence_id"),
        "source": source,
        "oracle_id": item.get("oracle_id"),
        "metric_id": item.get("metric_id"),
        "page_number": _positive_int(item.get("page_number")),
        "object_id": item.get("object_id"),
        "kind": item.get("kind"),
        "message": str(item.get("message") or "未提供文字证据"),
        "confidence": item.get("confidence"),
        "severity": item.get("severity") or payload.get("severity"),
    }
    bbox = item.get("bbox")
    if isinstance(bbox, Sequence) and not isinstance(bbox, (str, bytes)) and len(bbox) == 4:
        if all(_number(value) for value in bbox):
            result["bbox"] = [float(value) for value in bbox]
    return result


def _observation_page(observation: Mapping[str, Any]) -> int | None:
    unit_key = str(observation.get("unit_key") or "")
    if unit_key.startswith("page:"):
        try:
            value = int(unit_key.split(":", 1)[1])
        except ValueError:
            value = 0
        if value > 0:
            return value
    return next(
        (
            page
            for page in (
                _positive_int(item.get("page_number"))
                for item in _mappings(observation.get("evidence"))
            )
            if page is not None
        ),
        None,
    )


def _evidence_pages(evidence: Sequence[Mapping[str, Any]]) -> list[int]:
    pages: set[int] = set()
    for item in evidence:
        page = _positive_int(item.get("page_number"))
        if page is not None:
            pages.add(page)
        payload = _mapping(item.get("payload"))
        for affected in payload.get("affected_page_numbers", ()):
            page = _positive_int(affected)
            if page is not None:
                pages.add(page)
        for affected in item.get("_affected_page_numbers", ()):
            page = _positive_int(affected)
            if page is not None:
                pages.add(page)
    return sorted(pages)


def _issue_sort_key(item: Mapping[str, Any]) -> tuple[int, int, int, str]:
    pages = item.get("page_numbers", ())
    first_page = min(pages, default=10**9)
    return (
        _PRIORITY_ORDER.get(str(item.get("priority") or "P3"), 4),
        _SEVERITY_ORDER.get(str(item.get("severity") or "INFO"), 4),
        first_page,
        str(item.get("issue_id") or ""),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    integer = int(value)
    return integer if integer > 0 and integer == float(value) else None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "TRIAGE_POLICY_VERSION",
    "audit_task_sort_key",
    "build_attention_projection",
    "build_review_task_summary",
    "normalize_review_payload",
    "utc_now",
]
