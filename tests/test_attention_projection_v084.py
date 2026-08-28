from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ppt_eval.api import _light_attention_issue
from ppt_eval.application import build_attention_projection, build_review_task_summary
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import build_pptx


def _report(
    *,
    decision: str = "REVIEW",
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": "run-semantic-v084",
        "case_id": "case-semantic-v084",
        "decision": decision,
        "coverage": "DEGRADED",
        "results": results or [],
        "score_breakdown": {"unresolved_metric_ids": []},
    }


def test_atomic_facts_never_become_main_attention_cards() -> None:
    observations = [
        {
            "observation_id": f"obs-{page}",
            "metric_id": "slide_geometry_integrity",
            "unit_key": f"page:{page}",
            "severity": "MAJOR",
            "evidence": [
                {
                    "evidence_id": f"ev-{page}",
                    "kind": "out_of_bounds",
                    "message": "Object extends outside the canvas.",
                    "page_number": page,
                }
            ],
        }
        for page in range(1, 21)
    ]

    attention = build_attention_projection(_report(), observations)
    reordered = build_attention_projection(_report(), list(reversed(observations)))

    assert attention == reordered
    assert attention["items"] == []
    assert attention["attention_summary"]["total_count"] == 0
    assert attention["attention_summary"]["raw_fact_count"] == 20


def test_required_model_gaps_collapse_into_one_evidence_integrity_issue() -> None:
    metric_ids = [
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
    ]
    report = _report(
        results=[
            {
                "metric_id": metric_id,
                "metric_status": "NA",
                "metadata": {
                    "reason_code": "MODEL_AESTHETIC_EVIDENCE_MISSING",
                    "routing_attempts": [
                        {
                            "tier": "PRIMARY",
                            "execution_status": "ERROR",
                            "metric_status": "ERROR",
                            "error_code": "MODEL_PROVIDER_ERROR",
                        }
                    ],
                },
                "evidence": [],
            }
            for metric_id in metric_ids
        ]
    )
    report["score_breakdown"]["unresolved_metric_ids"] = metric_ids

    attention = build_attention_projection(report)

    assert len(attention["items"]) == 1
    issue = attention["items"][0]
    assert issue["kind"] == "EVIDENCE_INTEGRITY"
    assert issue["semantic_family"] == "SYSTEM_INTEGRITY"
    assert issue["consensus"]["status"] == "INSUFFICIENT"
    assert issue["detail_count"] == 12
    assert len(issue["rationales"]) <= 3
    assert attention["attention_details"][0]["raw_candidate_count"] == 12


def test_recovered_provider_attempt_stays_out_of_main_attention() -> None:
    report = _report(
        results=[
            {
                "metric_id": "structured_vlm_render_integrity",
                "metric_status": "SCORED",
                "normalized_score": 0.9,
                "metadata": {
                    "criterion_id": "render_integrity",
                    "routing_attempts": [
                        {
                            "tier": "PRIMARY",
                            "execution_status": "ERROR",
                            "metric_status": "ERROR",
                            "error_code": "MODEL_PROVIDER_ERROR",
                        },
                        {
                            "tier": "FALLBACK",
                            "execution_status": "SUCCESS",
                            "metric_status": "SCORED",
                        },
                    ],
                },
                "evidence": [
                    {
                        "evidence_id": "render-ok",
                        "kind": "criterion_summary",
                        "message": "The sampled page rendered without corruption.",
                        "page_number": 1,
                        "payload": {"severity": "NONE"},
                    }
                ],
            }
        ]
    )

    attention = build_attention_projection(report)

    assert attention["items"] == []
    assert attention["attention_summary"]["state"] == "REVIEW_WITHOUT_LOCALIZED_ISSUE"
    assert any(
        detail["semantic_code"] == "RAW_AUDIT_FACTS"
        and detail["raw_candidates"][0]["kind"] == "PROVIDER_ERROR_RECOVERED"
        for detail in attention["attention_details"]
    )


def test_vlm_findings_and_low_composite_form_one_semantic_consensus() -> None:
    results = [
        {
            "metric_id": "structured_vlm_composition_layout",
            "metric_status": "SCORED",
            "severity": "MAJOR",
            "metadata": {"criterion_id": "composition_layout"},
            "evidence": [
                {
                    "evidence_id": "model-cutoff",
                    "kind": "criterion_summary",
                    "message": "The lower text block is visibly cut off.",
                    "page_number": 3,
                    "payload": {
                        "severity": "MAJOR",
                        "defect_codes": ["content_overflow_or_cutoff"],
                    },
                }
            ],
        },
        {
            "metric_id": "composition_craft",
            "metric_status": "SCORED",
            "normalized_score": 0.55,
            "severity": "MAJOR",
            "metadata": {"reducer_id": "v8.composition.reducer"},
            "evidence": [
                {
                    "evidence_id": "reducer-page-3",
                    "kind": "reducer_summary",
                    "message": "Composition reducer retained the low page score.",
                    "page_number": 3,
                }
            ],
        },
    ]
    report = _report(results=results)

    first = build_attention_projection(report)
    reordered = deepcopy(report)
    reordered["results"] = list(reversed(reordered["results"]))
    second = build_attention_projection(reordered)

    assert first == second
    assert len(first["items"]) == 1
    issue = first["items"][0]
    assert issue["semantic_code"] == "TEXT_CUTOFF"
    assert issue["title"] == "文本截断或越界"
    assert "第 3 页" in issue["summary"]
    assert issue["consensus"]["status"] == "SINGLE_SOURCE"
    assert issue["consensus"]["label"] == "模型及其聚合结果"
    assert issue["consensus"]["sources"] == ["MODEL", "REDUCER"]
    assert len(issue["evidence"]) <= 3
    assert "metric_id" not in issue["title"]

    light_issue = _light_attention_issue(issue)
    serialized = str(light_issue)
    assert "metric_id" not in light_issue
    assert "lineage" not in light_issue
    assert "structured_vlm_composition_layout" not in serialized
    assert "composition_craft" not in serialized
    assert "model-cutoff" not in serialized
    assert "reducer-page-3" not in serialized
    assert all(
        not {"evidence_id", "oracle_id", "metric_id"}.intersection(evidence)
        for evidence in light_issue["evidence"]
    )
    assert all(
        set(evidence).issubset({"source", "page_number", "bbox"})
        for evidence in light_issue["evidence"]
    )


def test_reducer_defect_kind_becomes_actionable_language_issue() -> None:
    report = _report(
        results=[
            {
                "metric_id": "language_consistency",
                "metric_status": "SCORED",
                "normalized_score": 0.78,
                "severity": "MINOR",
                "metadata": {"reducer_id": "v8.language.reducer"},
                "evidence": [
                    {
                        "evidence_id": "mixed-language-page-2",
                        "kind": "undeclared_mixed_language",
                        "message": "Visible language differs from the dominant language.",
                        "page_number": 2,
                    }
                ],
            }
        ]
    )

    issue = build_attention_projection(report)["items"][0]

    assert issue["semantic_code"] == "MIXED_LANGUAGE"
    assert issue["title"] == "中英文混用影响语言一致性"
    assert issue["rationales"][0] == "第 2 页：中英文混用影响语言一致性。"


def test_vlm_affected_pages_are_bounded_in_main_and_complete_in_lineage() -> None:
    affected_pages = [1, 4, 8, 15, 22, 23, 24, 26]
    report = _report(
        results=[
            {
                "metric_id": "structured_vlm_authorship_specificity",
                "metric_status": "SCORED",
                "metadata": {"criterion_id": "authorship_specificity"},
                "evidence": [
                    {
                        "evidence_id": "authorship-deck",
                        "kind": "criterion_summary",
                        "message": "The deck repeatedly reuses a mechanical card template.",
                        "page_number": 8,
                        "payload": {
                            "severity": "MAJOR",
                            "defect_codes": ["mechanical_cardization"],
                            "affected_page_numbers": affected_pages,
                        },
                    }
                ],
            }
        ]
    )

    attention = build_attention_projection(report)
    issue = attention["items"][0]
    candidate = attention["attention_details"][0]["semantic_candidates"][0]

    assert issue["page_numbers"] == affected_pages[:6]
    assert "等共 8 页" in issue["summary"]
    assert candidate["page_numbers"] == affected_pages
    assert candidate["evidence"][0]["affected_page_numbers"] == affected_pages


def test_duplicate_semantic_evidence_selection_is_order_independent() -> None:
    evidence = [
        {
            "evidence_id": "z-major",
            "kind": "criterion_summary",
            "message": "The title is cut off.",
            "page_number": 2,
            "payload": {
                "severity": "MAJOR",
                "defect_codes": ["content_overflow_or_cutoff"],
            },
        },
        {
            "evidence_id": "a-critical",
            "kind": "criterion_summary",
            "message": "The title is cut off.",
            "page_number": 2,
            "payload": {
                "severity": "CRITICAL",
                "defect_codes": ["content_overflow_or_cutoff"],
            },
        },
    ]
    report = _report(
        results=[
            {
                "metric_id": "structured_vlm_composition_layout",
                "metric_status": "SCORED",
                "metadata": {"criterion_id": "composition_layout"},
                "evidence": evidence,
            }
        ]
    )
    reordered = deepcopy(report)
    reordered["results"][0]["evidence"] = list(reversed(evidence))

    first = build_attention_projection(report)
    second = build_attention_projection(reordered)

    assert first == second
    assert first["items"][0]["evidence"][0]["evidence_id"] == "a-critical"


def test_semantic_budget_has_exactly_eight_families_and_degraded_is_p1() -> None:
    metric_ids = [
        "harness_system_integrity",
        "functional_quality",
        "content_structure",
        "source_claim_alignment",
        "composition_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
    ]
    results = [
        {
            "metric_id": metric_id,
            "metric_status": "SCORED",
            "normalized_score": 0.5,
            "severity": "MAJOR",
            "metadata": {"reducer_id": f"reducer-{index}"},
            "evidence": [],
        }
        for index, metric_id in enumerate(metric_ids, start=1)
    ]
    report = _report(results=results)

    attention = build_attention_projection(report)
    summary = build_review_task_summary(report)

    assert len(attention["items"]) == 8
    assert len({item["semantic_family"] for item in attention["items"]}) == 8
    assert all(item["detail_count"] >= 1 for item in attention["items"])
    assert summary["priority"] == "P1"
    assert attention["attention_summary"]["required_count"] == 8
    assert {item["priority"] for item in attention["items"]} == {"P1"}


def test_zero_localized_issue_summary_distinguishes_pass_review_and_fail() -> None:
    expected = {
        "PASS": "NO_ISSUE",
        "REVIEW": "REVIEW_WITHOUT_LOCALIZED_ISSUE",
        "FAIL": "UNLOCATED_FAILURE",
    }

    for decision, state in expected.items():
        attention = build_attention_projection(_report(decision=decision))

        assert attention["items"] == []
        assert attention["attention_summary"]["state"] == state
        assert attention["attention_summary"]["total_count"] == 0


def test_new_and_legacy_reports_project_service_versions(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "versioned.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(
            case_id="versioned",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        )
    )
    assert report["service_version"] == "0.8.4"

    legacy = runtime.repository.get(report["run_id"])
    legacy.pop("service_version", None)
    runtime.repository.save(legacy)

    assert runtime.get(report["run_id"])["service_version"] == "0.8.3"
    assert runtime.review_task(report["run_id"])["service_version"] == "0.8.3"
