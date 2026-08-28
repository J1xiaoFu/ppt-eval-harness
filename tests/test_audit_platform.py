from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import pytest

from ppt_eval.adapters import RenderResult
from ppt_eval.api import create_app
from ppt_eval.application import (
    TRIAGE_POLICY_VERSION,
    audit_task_sort_key,
    build_attention_projection,
)
from ppt_eval.cli import build_parser
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure import JsonRunRepository, LocalArtifactStore
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


class ReviewRenderer:
    renderer_id = "review-test-renderer"
    version = "1.0"

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        del pptx_path
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        image = output / "Slide1.PNG"
        image.write_bytes(PNG_1X1)
        return RenderResult(self.renderer_id, self.version, (image,))


def _gate_report() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    observation = {
        "observation_id": "obs-geometry-page-3",
        "oracle_id": "v8.atomic",
        "metric_id": "slide_geometry_integrity",
        "scope": "PAGE",
        "unit_key": "page:3",
        "metric_status": "SCORED",
        "local_score": 0.2,
        "confidence": 1.0,
        "severity": "CRITICAL",
        "evidence": [
            {
                "evidence_id": "ev-page-3",
                "kind": "out_of_bounds",
                "message": "Object extends outside the canvas.",
                "page_number": 3,
                "bbox": [0.8, 0.1, 0.3, 0.2],
            }
        ],
    }
    report = {
        "run_id": "run-audit-projection",
        "case_id": "case-audit-projection",
        "decision": "FAIL",
        "coverage": "FULL",
        "created_at": "2026-08-28T01:00:00Z",
        "results": [
            {
                "metric_id": "v8_functional_integrity",
                "metric_status": "FAIL",
                "score_role": "BASE_MULTIPLIER",
                "metadata": {
                    "gate_verdicts": [
                        {
                            "metric_id": "slide_geometry_integrity",
                            "rule_severity": "CRITICAL",
                            "verdict": "CONFIRMED",
                            "model_metric_id": "structured_vlm_composition_layout",
                            "model_severity": "MAJOR",
                            "observation_ids": ["obs-geometry-page-3"],
                        }
                    ]
                },
            },
            {
                "metric_id": "structured_vlm_composition_layout",
                "metric_status": "SCORED",
                "metadata": {"sampled_pages": [1, 3], "forced_rule_pages": [3]},
                "evidence": [
                    {
                        "evidence_id": "ev-model-page-3",
                        "kind": "criterion_summary",
                        "message": "Visible content is cut off.",
                        "page_number": 3,
                        "payload": {
                            "affected_page_numbers": [3],
                            "severity": "MAJOR",
                        },
                    }
                ],
            },
        ],
        "score_breakdown": {"unresolved_metric_ids": []},
    }
    return report, [observation]


def test_attention_projection_groups_gate_and_never_uses_benchmark_rank() -> None:
    report, observations = _gate_report()
    report["human_rank"] = 1

    first = build_attention_projection(report, observations)
    second = build_attention_projection(report, observations)

    assert first == second
    assert first["policy_version"] == TRIAGE_POLICY_VERSION
    assert len(first["items"]) == 1
    issue = first["items"][0]
    assert issue["kind"] == "HARD_GATE_CONFIRMED"
    assert issue["page_numbers"] == [3]
    assert issue["lineage"]["forced_rule_pages"] == [3]
    assert issue["issue_id"].startswith("att-")
    assert "human_rank" not in json.dumps(first)

    tasks = [
        {"run_id": "newer", "priority": "P1", "review_state": "OPEN", "created_at": "2"},
        {"run_id": "older", "priority": "P1", "review_state": "OPEN", "created_at": "1"},
    ]
    assert [item["run_id"] for item in sorted(tasks, key=audit_task_sort_key)] == [
        "older",
        "newer",
    ]


def test_run_and_artifact_identifiers_reject_path_traversal(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path / "runs")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="invalid format"):
        repository.get("../secret")
    with pytest.raises(ValueError, match="64 lowercase"):
        artifacts.resolve("../secret")

    with pytest.raises(ValueError, match="unsupported review verdict"):
        repository.add_review(
            {
                "run_id": "run-legacy",
                "reviewer_id": "reviewer-a",
                "verdict": "APPROVE",
            }
        )


def test_cli_writes_only_current_review_contract() -> None:
    parsed = build_parser().parse_args(
        [
            "review",
            "run-current-contract",
            "REQUEST_MORE_EVIDENCE",
            "--reviewer",
            "reviewer-a",
            "--note",
            "need another render",
        ]
    )
    assert parsed.verdict == "REQUEST_MORE_EVIDENCE"
    with redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        build_parser().parse_args(
            ["review", "run-legacy", "APPROVE", "--reviewer", "reviewer-a"]
        )


def test_review_api_serves_queue_slides_artifacts_and_idempotent_history(
    tmp_path: Path,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient

    deck = build_pptx(tmp_path / "audit-source.pptx")
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        slide_renderer=ReviewRenderer(),
        review_rendering=True,
    )
    report = runtime.evaluate(
        EvalCase(
            case_id="audit-platform-case",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        )
    )
    client = TestClient(create_app(runtime))

    queue = client.get("/v1/review/tasks?view=all").json()
    assert queue["total"] == 1
    assert queue["items"][0]["run_id"] == report["run_id"]
    assert "results" not in queue["items"][0]

    detail_response = client.get(f"/v1/review/tasks/{report['run_id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["slides"][0]["image_url"].endswith("/slides/1")
    assert detail["artifacts"]["source_pptx_url"].endswith("/artifacts/source_pptx")
    assert "human_rank" not in json.dumps(detail)
    assert '"uri"' not in json.dumps(detail)
    assert "results" not in detail
    full_audit = client.get(detail["audit_url"])
    assert full_audit.status_code == 200
    assert full_audit.json()["results"]

    slide = client.get(detail["slides"][0]["image_url"])
    assert slide.status_code == 200
    assert slide.content == PNG_1X1
    source = client.get(detail["artifacts"]["source_pptx_url"])
    assert source.status_code == 200
    assert source.content == deck.read_bytes()
    observations = client.get(detail["artifacts"]["observations_url"])
    assert observations.status_code == 200
    render_manifest = client.get(detail["artifacts"]["render_manifest_url"])
    assert render_manifest.status_code == 200
    render_payload = render_manifest.json()
    assert render_payload["schema_version"] == "1.1"
    assert render_payload["slide_images"][0]["sha256"]

    incomplete_final = client.post(
        "/v1/reviews",
        json={
            "run_id": report["run_id"],
            "reviewer_id": "reviewer-a",
            "verdict": "CONFIRM_SYSTEM_DECISION",
            "note": "",
            "issue_resolutions": [],
            "track_resolutions": {},
        },
    )
    assert incomplete_final.status_code == 422
    assert "P0/P1" in incomplete_final.json()["detail"]
    legacy_write = client.post(
        "/v1/reviews",
        json={
            "run_id": report["run_id"],
            "reviewer_id": "reviewer-a",
            "verdict": "APPROVE",
            "note": "legacy write must be rejected",
        },
    )
    assert legacy_write.status_code == 422

    review_payload = {
        "run_id": report["run_id"],
        "reviewer_id": "reviewer-a",
        "verdict": "REQUEST_MORE_EVIDENCE",
        "note": "Need a second render for the disputed pages.",
        "issue_resolutions": [],
        "track_resolutions": {},
    }
    headers = {"Idempotency-Key": "review-request-1"}
    first_review = client.post("/v1/reviews", json=review_payload, headers=headers)
    second_review = client.post("/v1/reviews", json=review_payload, headers=headers)
    assert first_review.status_code == 200
    assert second_review.status_code == 200
    assert first_review.json()["review_id"] == second_review.json()["review_id"]
    conflict = client.post(
        "/v1/reviews",
        json={**review_payload, "note": "Different retry payload."},
        headers=headers,
    )
    assert conflict.status_code == 409
    history = client.get(f"/v1/review/tasks/{report['run_id']}/reviews").json()
    assert len(history) == 1
    refreshed = client.get(f"/v1/review/tasks/{report['run_id']}").json()
    assert refreshed["review_state"] == "NEEDS_EVIDENCE"
