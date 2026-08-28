from __future__ import annotations

import asyncio
import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ppt_eval.adapters import RenderResult
from ppt_eval.api import (
    JobQueueFullError,
    LocalJobManager,
    RequestBodyLimitMiddleware,
    _public_report,
    create_app,
)
from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from ppt_eval.infrastructure.uploads import LocalUploadStore
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.api_client import make_test_client
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


class UploadReviewRenderer:
    renderer_id = "upload-review-renderer"
    version = "1.0"

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        del pptx_path
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        image = output / "Slide1.PNG"
        image.write_bytes(PNG_1X1)
        return RenderResult(self.renderer_id, self.version, (image,))


class RecordingRuntime(LocalEvaluationRuntime):
    seen_case: dict[str, Any] | None = None

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.seen_case = {
            "case_id": case.case_id,
            "presentation_path": case.pptx_path,
            "source_paths": tuple(case.source_materials),
            "asset_paths": tuple(case.assets),
            "source_bytes": tuple(Path(item).read_bytes() for item in case.source_materials),
            "asset_bytes": tuple(Path(item).read_bytes() for item in case.assets),
            "source_names": tuple(Path(item).name for item in case.source_materials),
            "asset_names": tuple(Path(item).name for item in case.assets),
        }
        return super().evaluate(case, profile, **kwargs)


class FailingRuntime(LocalEvaluationRuntime):
    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del profile, kwargs
        raise RuntimeError(f"synthetic failure at {case.pptx_path}")


class BlockingRuntime(LocalEvaluationRuntime):
    def __init__(self, root: Path, release: threading.Event) -> None:
        super().__init__(root)
        self.release = release

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del case, profile, kwargs
        self.release.wait(timeout=2)
        return {"run_id": "run-blocking-test"}


class ImmediateRuntime(LocalEvaluationRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.count = 0

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del case, profile, kwargs
        self.count += 1
        return {"run_id": f"run-immediate-{self.count}"}


def _poll_job(client: Any, job_id: str) -> dict[str, Any]:
    for _ in range(250):
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"COMPLETED", "FAILED"}:
            return dict(payload)
        time.sleep(0.02)
    raise AssertionError("evaluation job did not finish")


def _client(
    runtime: LocalEvaluationRuntime,
    *,
    upload_store: LocalUploadStore | None = None,
    job_manager: LocalJobManager | None = None,
    max_request_body_bytes: int | None = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "upload_store": upload_store,
        "job_manager": job_manager,
    }
    if max_request_body_bytes is not None:
        kwargs["max_request_body_bytes"] = max_request_body_bytes
    return make_test_client(lambda: create_app(runtime, **kwargs))


def _presentation_file(deck: Path, name: str = "market-research.pptx") -> tuple[str, bytes, str]:
    return (
        name,
        deck.read_bytes(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def test_multipart_async_upload_reaches_review_with_sources_assets_and_no_paths(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = RecordingRuntime(
        tmp_path / "var",
        slide_renderer=UploadReviewRenderer(),
        review_rendering=True,
    )
    client = _client(runtime)

    response = client.post(
        "/v1/evaluations/upload?async=true",
        data={
            "case_id": "市场调研-七切片",
            "scene": "ready_made",
            "request": "Summarize the market evidence.",
            "audience": "Strategy leadership",
        },
        files=[
            ("presentation", _presentation_file(deck)),
            ("source_materials", ("brief.txt", b"market size 2026", "text/plain")),
            ("source_materials", ("notes.md", b"competitor evidence", "text/markdown")),
            ("assets", ("chart.png", PNG_1X1, "image/png")),
            ("assets", ("table.csv", b"year,value\n2026,42\n", "text/csv")),
        ],
    )

    assert response.status_code == 202
    submitted = response.json()
    assert response.headers["location"] == f"/v1/jobs/{submitted['job_id']}"
    assert submitted["status"] in {"PENDING", "RUNNING", "COMPLETED"}
    assert len(submitted["input_sha256"]) == 64
    assert str(tmp_path) not in response.text

    job = _poll_job(client, submitted["job_id"])
    assert job["status"] == "COMPLETED"
    assert job["stage"] == "READY_FOR_REVIEW"
    assert job["started_at"] and job["completed_at"]
    assert job["review_task_url"].endswith(job["run_id"])
    assert job["review_url"] == f"/review/?view=all&run={job['run_id']}"
    assert str(tmp_path) not in json.dumps(job)

    assert runtime.seen_case is not None
    assert runtime.seen_case["case_id"] == "市场调研-七切片"
    assert runtime.seen_case["source_bytes"] == (
        b"market size 2026",
        b"competitor evidence",
    )
    assert runtime.seen_case["asset_bytes"] == (
        PNG_1X1,
        b"year,value\n2026,42\n",
    )
    assert runtime.seen_case["source_names"] == ("brief.txt", "notes.md")
    assert runtime.seen_case["asset_names"] == ("chart.png", "table.csv")
    assert not Path(runtime.seen_case["presentation_path"]).exists()
    assert all(not Path(item).exists() for item in runtime.seen_case["source_paths"])
    assert all(not Path(item).exists() for item in runtime.seen_case["asset_paths"])

    report_response = client.get(job["evaluation_url"])
    assert report_response.status_code == 200
    report = report_response.json()
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert '"uri"' not in serialized
    assert '"pptx_path"' not in serialized
    artifact_hashes = report["manifest"]["artifact_hashes"]
    assert artifact_hashes["source_pptx"] == submitted["input_sha256"]
    assert len(artifact_hashes["source_material/1"]) == 64
    assert len(artifact_hashes["source_material/2"]) == 64
    assert len(artifact_hashes["asset/1"]) == 64
    assert len(artifact_hashes["asset/2"]) == 64

    review = client.get(job["review_task_url"])
    assert review.status_code == 200
    review_payload = review.json()
    assert review_payload["slides"][0]["image_url"].endswith("/slides/1")
    inputs = {item["original_name"]: item for item in review_payload["inputs"]}
    assert set(inputs) == {"brief.txt", "notes.md", "chart.png", "table.csv"}
    assert all(item["available"] is True for item in inputs.values())
    assert client.get(inputs["brief.txt"]["download_url"]).content == b"market size 2026"
    assert client.get(inputs["chart.png"]["download_url"]).content == PNG_1X1
    source = client.get(review_payload["artifacts"]["source_pptx_url"])
    assert source.status_code == 200
    assert source.content == deck.read_bytes()
    observations = client.get(review_payload["artifacts"]["observations_url"])
    assert observations.status_code == 200
    assert observations.json()
    render_manifest = client.get(
        review_payload["artifacts"]["render_manifest_url"]
    )
    assert render_manifest.status_code == 200
    render_payload = render_manifest.json()
    assert render_payload["slide_images"][0]["sha256"]

    brief_path = runtime.artifacts.resolve(inputs["brief.txt"]["sha256"])
    brief_path.write_bytes(b"tampered")
    corrupted = client.get(job["review_task_url"])
    assert corrupted.status_code == 200
    corrupt_brief = next(
        item for item in corrupted.json()["inputs"] if item["original_name"] == "brief.txt"
    )
    assert corrupt_brief["available"] is False
    assert corrupt_brief["download_url"] is None
    assert client.get(inputs["brief.txt"]["download_url"]).status_code == 409


def test_multipart_sync_and_legacy_json_api_use_public_projection(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    client = _client(runtime)

    upload_schema = client.app.openapi()["components"]["schemas"][
        "Body_create_uploaded_evaluation_v1_evaluations_upload_post"
    ]["properties"]
    upload_operation = client.app.openapi()["paths"]["/v1/evaluations/upload"]["post"]
    assert {"200", "202", "400", "403", "409", "413", "422", "429", "503"} <= set(
        upload_operation["responses"]
    )
    assert upload_schema["presentation"]["type"] == "string"
    assert upload_schema["presentation"]["contentMediaType"] == (
        "application/octet-stream"
    )
    assert upload_schema["source_materials"]["anyOf"][0]["items"][
        "contentMediaType"
    ] == "application/octet-stream"
    assert upload_schema["assets"]["anyOf"][0]["items"]["contentMediaType"] == (
        "application/octet-stream"
    )

    uploaded = client.post(
        "/v1/evaluations/upload?async=false",
        data={"case_id": "sync-upload", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["run_id"]
    assert str(tmp_path) not in uploaded.text
    assert '"uri"' not in uploaded.text

    legacy = client.post(
        "/v1/evaluations?async=false",
        json={
            "case": {
                "case_id": "legacy-local-path",
                "scene": "ready_made",
                "pptx_path": str(deck),
            }
        },
    )
    assert legacy.status_code == 200
    assert legacy.json()["run_id"]
    assert str(tmp_path) not in legacy.text
    assert '"uri"' not in legacy.text


def test_upload_rejects_wrong_suffix_dangerous_filename_and_invalid_zip(
    tmp_path: Path,
) -> None:
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    client = _client(runtime)
    for filename, content in (
        ("deck.pdf", b"not a presentation"),
        ("../deck.pptx", b"not a presentation"),
        (r"C:\deck.pptx", b"not a presentation"),
        (f"{'a' * 121}.pptx", b"not a presentation"),
        ("deck.pptx", b"not a zip package"),
    ):
        response = client.post(
            "/v1/evaluations/upload",
            data={"case_id": "bad-upload", "scene": "ready_made"},
            files={"presentation": (filename, content, "application/octet-stream")},
        )
        assert response.status_code == 422, filename
        assert str(tmp_path) not in response.text
        assert not tuple((runtime.paths.root / "uploads" / ".incoming").glob("*"))

    deck = build_pptx(tmp_path / "valid.pptx")
    unsafe_attachment = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "bad-attachment", "scene": "ready_made"},
        files=[
            ("presentation", _presentation_file(deck)),
            ("assets", ("payload.exe", b"blocked", "application/octet-stream")),
        ],
    )
    assert unsafe_attachment.status_code == 422
    assert "unsupported file extension" in unsafe_attachment.text

    invalid_windows_name = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "bad-windows-name", "scene": "ready_made"},
        files=[
            ("presentation", _presentation_file(deck)),
            ("assets", ("bad?.png", PNG_1X1, "image/png")),
        ],
    )
    assert invalid_windows_name.status_code == 422
    assert "filename is unsafe" in invalid_windows_name.text

    unreadable_source_type = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "binary-source", "scene": "project_summary"},
        files=[
            ("presentation", _presentation_file(deck)),
            ("source_materials", ("brief.pdf", b"binary", "application/pdf")),
        ],
    )
    assert unreadable_source_type.status_code == 422
    assert "unsupported file extension" in unreadable_source_type.text

    wrong_media_type = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "bad-media", "scene": "ready_made"},
        files={"presentation": ("valid.pptx", deck.read_bytes(), "image/png")},
    )
    assert wrong_media_type.status_code == 422
    assert "unsupported media type" in wrong_media_type.text


def test_upload_store_startup_never_deletes_unleased_workspaces(tmp_path: Path) -> None:
    upload_root = tmp_path / "uploads"
    incoming = upload_root / ".incoming"
    work = upload_root / "work"
    incoming.mkdir(parents=True)
    work.mkdir()
    orphan_incoming = incoming / f".{('a' * 32)}.tmp"
    unrelated_incoming = incoming / "keep.txt"
    orphan_incoming.write_bytes(b"partial")
    unrelated_incoming.write_bytes(b"keep")

    orphan_work = work / f"upload-{('b' * 32)}"
    unrelated_work = work / f"upload-{('c' * 32)}"
    orphan_work.mkdir()
    unrelated_work.mkdir()
    (orphan_work / ".ppt-eval-upload-workspace").write_text("1\n", encoding="ascii")
    (orphan_work / "partial.pptx").write_bytes(b"partial")
    (unrelated_work / "sentinel.txt").write_bytes(b"keep")

    LocalUploadStore(upload_root)

    assert orphan_incoming.read_bytes() == b"partial"
    assert unrelated_incoming.read_bytes() == b"keep"
    assert (orphan_work / "partial.pptx").read_bytes() == b"partial"
    assert (unrelated_work / "sentinel.txt").read_bytes() == b"keep"


def test_origin_and_request_body_guards_run_before_multipart(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "guarded.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    client = _client(runtime, max_request_body_bytes=64)
    forbidden = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "origin", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
        headers={"Origin": "https://evil.example"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "ORIGIN_FORBIDDEN"

    oversized = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "body-limit", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
        headers={"Origin": "http://localhost:5173"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "REQUEST_BODY_TOO_LARGE"
    assert oversized.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))

    sent: list[dict[str, Any]] = []
    messages = iter(
        (
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        )
    )

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def drain_app(scope: Any, receive_call: Any, send_call: Any) -> None:
        del scope
        while True:
            message = await receive_call()
            if not message.get("more_body"):
                break
        await send_call({"type": "http.response.start", "status": 204, "headers": []})
        await send_call({"type": "http.response.body", "body": b""})

    middleware = RequestBodyLimitMiddleware(drain_app, maximum_bytes=5)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "headers": []},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 413


def test_public_projection_redacts_arbitrary_embedded_absolute_paths() -> None:
    projected = _public_report(
        {
            "errors": [
                "failed at /data/private/deck.pptx",
                "failed at /workspace/tenant/source.txt",
                "failed at /etc/passwd",
            ],
            "review_url": "/review/?view=all&run=run-1",
        }
    )
    assert projected["errors"] == ["[redacted-local-path]"] * 3
    assert projected["review_url"] == "/review/?view=all&run=run-1"


def test_upload_rejects_oversize_and_unsafe_ooxml(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    constrained = LocalUploadStore(
        runtime.paths.root / "uploads",
        max_presentation_bytes=64,
    )
    client = _client(runtime, upload_store=constrained)
    oversized = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "oversized", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
    )
    assert oversized.status_code == 413

    unsafe = build_pptx(tmp_path / "unsafe.pptx")
    with zipfile.ZipFile(unsafe, "a") as archive:
        archive.writestr("../escape.bin", b"blocked")
    normal_store = LocalUploadStore(
        runtime.paths.root / "uploads-normal",
    )
    unsafe_client = _client(runtime, upload_store=normal_store)
    rejected = unsafe_client.post(
        "/v1/evaluations/upload",
        data={"case_id": "unsafe", "scene": "ready_made"},
        files={"presentation": _presentation_file(unsafe, "unsafe.pptx")},
    )
    assert rejected.status_code == 422
    assert "unsafe_entry_path" in rejected.text


def test_upload_idempotency_reuses_same_job_and_conflicts_on_changed_input(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    client = _client(runtime)
    headers = {"Idempotency-Key": "upload-request-1"}

    def submit(case_id: str) -> Any:
        return client.post(
            "/v1/evaluations/upload",
            data={"case_id": case_id, "scene": "ready_made"},
            files={"presentation": _presentation_file(deck)},
            headers=headers,
        )

    first = submit("same-input")
    second = submit("same-input")
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    conflict = submit("changed-input")
    assert conflict.status_code == 409

    _poll_job(client, first.json()["job_id"])
    work_root = runtime.paths.root / "uploads" / "work"
    for _ in range(100):
        if not tuple(work_root.glob("upload-*")):
            break
        time.sleep(0.01)
    assert not tuple(work_root.glob("upload-*"))


def test_job_failure_and_bounded_queue_do_not_expose_workspace_paths(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    failing = FailingRuntime(tmp_path / "failing-var")
    client = _client(failing)
    submitted = client.post(
        "/v1/evaluations/upload",
        data={"case_id": "failure", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
    )
    assert submitted.status_code == 202
    failed = _poll_job(client, submitted.json()["job_id"])
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "EVALUATION_FAILED"
    assert str(tmp_path) not in json.dumps(failed)

    release = threading.Event()
    blocking = BlockingRuntime(tmp_path / "blocking-var", release)
    jobs = LocalJobManager(blocking, workers=1, max_active_jobs=1)
    case = EvalCase(
        case_id="blocking",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )
    jobs.submit_case(case, fingerprint="a" * 64)
    with pytest.raises(JobQueueFullError):
        jobs.submit_case(case, fingerprint="b" * 64)
    release.set()
    jobs.shutdown(wait=True)

    second_release = threading.Event()
    second_runtime = BlockingRuntime(tmp_path / "queue-var", second_release)
    queue_jobs = LocalJobManager(second_runtime, workers=1, max_active_jobs=1)
    queue_jobs.submit_case(case, fingerprint="c" * 64)
    queue_client = _client(second_runtime, job_manager=queue_jobs)
    queue_full = queue_client.post(
        "/v1/evaluations/upload",
        data={"case_id": "queue-full", "scene": "ready_made"},
        files={"presentation": _presentation_file(deck)},
    )
    assert queue_full.status_code == 429
    assert queue_full.headers["retry-after"] == "1"
    second_release.set()
    queue_jobs.shutdown(wait=True)


def test_terminal_job_retention_and_shutdown_are_bounded(tmp_path: Path) -> None:
    runtime = ImmediateRuntime(tmp_path / "immediate-var")
    jobs = LocalJobManager(
        runtime,
        workers=1,
        max_active_jobs=2,
        max_terminal_jobs=1,
    )
    case = EvalCase(
        case_id="retention",
        scene=SceneType.READY_MADE,
        pptx_path="unused.pptx",
    )

    def wait(job_id: str) -> dict[str, Any]:
        for _ in range(100):
            value = jobs.get(job_id)
            if value["status"] in {"COMPLETED", "FAILED"}:
                return value
            time.sleep(0.01)
        raise AssertionError("job did not finish")

    first = jobs.submit_case(case, fingerprint="1" * 64)
    wait(first["job_id"])
    second = jobs.submit_case(case, fingerprint="2" * 64)
    wait(second["job_id"])
    with pytest.raises(KeyError):
        jobs.get(first["job_id"])
    jobs.shutdown(wait=True)
    with pytest.raises(RuntimeError, match="shut down"):
        jobs.submit_case(case, fingerprint="3" * 64)
