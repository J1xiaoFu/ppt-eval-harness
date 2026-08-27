from __future__ import annotations

from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import build_pptx


def test_local_runtime_persists_report_manifest_and_valid_audit_chain(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck)),
        EvalProfile.default(SceneType.READY_MADE, version="1.0"),
    )

    assert report["coverage"] == "FULL"
    assert report["base_score"] is not None
    assert report["manifest"]["input_hash"]
    assert runtime.get(report["run_id"])["case_id"] == "ready"
    assert runtime.audit_log.verify() == (True, None)


def test_runtime_scene_degradation_keeps_intrinsic_score_and_routes_review(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(
            case_id="text",
            scene=SceneType.TEXT_TO_PPT,
            pptx_path=str(deck),
            request="制作一份中文项目汇报",
        )
    )

    assert report["coverage"] == "DEGRADED"
    assert report["decision"] == "REVIEW"
    assert report["base_score"] is not None
    assert report["full_score"] is None
    assert "unresolved_metric:fact_claim" in report["degradation_reasons"]


def test_review_and_run_export_are_audited(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck))
    )
    review = runtime.review(
        {"run_id": report["run_id"], "verdict": "APPROVE", "reviewer_id": "tester", "note": "ok"}
    )
    markdown, html = runtime.export(report["run_id"], tmp_path / "exports")

    assert review["review_id"].startswith("review-")
    assert markdown.is_file() and html.is_file()
    assert report["run_id"] in html.read_text(encoding="utf-8")
    assert runtime.audit_log.verify() == (True, None)
