from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure import LocalArtifactStore
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import build_pptx


def test_local_artifact_store_uses_short_atomic_temp_name(tmp_path) -> None:
    written_names: list[str] = []
    original_write_bytes = Path.write_bytes

    def tracked_write_bytes(path: Path, data: bytes) -> int:
        written_names.append(path.name)
        return original_write_bytes(path, data)

    with patch.object(Path, "write_bytes", tracked_write_bytes):
        artifact = LocalArtifactStore(tmp_path / ("nested-" * 12)).put_bytes(b"observation")

    assert Path(artifact["uri"]).read_bytes() == b"observation"
    assert len(written_names) == 1
    assert written_names[0].startswith(".")
    assert written_names[0].endswith(".tmp")
    assert len(written_names[0]) <= 40


def test_local_runtime_persists_report_manifest_and_valid_audit_chain(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck)),
        default_profile(SceneType.READY_MADE),
    )

    assert report["coverage"] == "DEGRADED"
    assert report["decision"] == "REVIEW"
    assert report["base_score"] is not None
    assert report["manifest"]["input_hash"]
    assert runtime.get(report["run_id"])["case_id"] == "ready"
    assert runtime.audit_log.verify() == (True, None)


def test_release_runtime_rejects_legacy_profile_before_writing(tmp_path) -> None:
    deck = build_pptx(tmp_path / "legacy.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    legacy = replace(default_profile(SceneType.READY_MADE), version="7.0")

    with pytest.raises(ValueError, match="only Profile version 8.3"):
        runtime.evaluate(
            EvalCase(
                case_id="legacy-profile",
                scene=SceneType.READY_MADE,
                pptx_path=str(deck),
            ),
            legacy,
        )

    assert runtime.repository.list() == []


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
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck)),
        default_profile(SceneType.READY_MADE),
    )
    task = runtime.review_task(report["run_id"])
    issue_resolutions = [
        {"issue_id": item["issue_id"], "resolution": "CONFIRMED"}
        for item in task["issues"]
        if item["priority"] in {"P0", "P1"}
    ]
    review = runtime.review(
        {
            "run_id": report["run_id"],
            "verdict": "CONFIRM_SYSTEM_DECISION",
            "reviewer_id": "tester",
            "note": "ok",
            "issue_resolutions": issue_resolutions,
            "track_resolutions": {},
        }
    )
    markdown, html = runtime.export(report["run_id"], tmp_path / "exports")

    assert review["review_id"].startswith("review-")
    assert markdown.is_file() and html.is_file()
    assert report["run_id"] in html.read_text(encoding="utf-8")
    assert runtime.audit_log.verify() == (True, None)
