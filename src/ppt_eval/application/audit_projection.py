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

TRIAGE_POLICY_VERSION = "audit-attention@1.0.0"
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


def build_attention_projection(
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
                item
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
    for (metric_id, page_number), group in ordered_groups[:12]:
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
    if coverage != "FULL" or decision in {"FAIL", "ERROR"} or "P0" in issue_priorities:
        priority = "P0"
    elif decision == "REVIEW" or "P1" in issue_priorities:
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
