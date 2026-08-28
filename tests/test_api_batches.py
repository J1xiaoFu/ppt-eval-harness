from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ppt_eval.api import BatchCaseSubmission, LocalJobManager, create_app
from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import build_pptx


class SelectiveRuntime(LocalEvaluationRuntime):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[str] = []

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(case.case_id)
        if "fail" in case.case_id:
            raise RuntimeError(f"synthetic failure at {case.pptx_path}")
        return super().evaluate(case, profile, **kwargs)


class ImmediateBatchRuntime(LocalEvaluationRuntime):
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
        return {"run_id": f"run-batch-immediate-{self.count}"}


class BlockingBatchRuntime(LocalEvaluationRuntime):
    def __init__(
        self,
        root: Path,
        release: threading.Event,
        started: threading.Event,
    ) -> None:
        super().__init__(root)
        self.release = release
        self.started = started

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del case, profile, kwargs
        self.started.set()
        self.release.wait(timeout=3)
        return {"run_id": "run-batch-blocking"}


def _client(
    runtime: LocalEvaluationRuntime,
    *,
    job_manager: LocalJobManager | None = None,
    max_request_body_bytes: int | None = None,
) -> Any | None:
    try:
        import python_multipart
        from fastapi.testclient import TestClient
    except ImportError:
        return None
    del python_multipart
    kwargs: dict[str, Any] = {"job_manager": job_manager}
    if max_request_body_bytes is not None:
        kwargs["max_request_body_bytes"] = max_request_body_bytes
    return TestClient(create_app(runtime, **kwargs))


def _presentation(deck: Path, name: str) -> tuple[str, bytes, str]:
    return (
        name,
        deck.read_bytes(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def _submit_batch(
    client: Any,
    decks: list[tuple[Path, str]],
    case_ids: list[str],
    *,
    headers: dict[str, str] | None = None,
    scene: str = "ready_made",
    extra_files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> Any:
    files = [
        ("presentations", _presentation(deck, name))
        for deck, name in decks
    ]
    files.extend(extra_files or ())
    return client.post(
        "/v1/evaluation-batches/upload",
        data={"case_ids": case_ids, "scene": scene},
        files=files,
        headers=headers,
    )


def _poll_batch(client: Any, batch_id: str) -> dict[str, Any]:
    for _ in range(300):
        response = client.get(f"/v1/evaluation-batches/{batch_id}")
        assert response.status_code == 200
        payload = dict(response.json())
        summary = payload["summary"]
        assert (
            summary["pending"]
            + summary["running"]
            + summary["completed"]
            + summary["failed"]
            == summary["total"]
            == len(payload["items"])
        )
        if payload["status"] in {"COMPLETED", "PARTIALLY_FAILED", "FAILED"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("evaluation batch did not finish")


def test_batch_upload_completes_in_order_and_survives_as_runs(
    tmp_path: Path,
) -> None:
    first_deck = build_pptx(tmp_path / "first.pptx")
    second_deck = build_pptx(tmp_path / "second.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return

    response = _submit_batch(
        client,
        [(first_deck, "first.pptx"), (second_deck, "second.pptx")],
        ["batch-first", "batch-second"],
    )

    assert response.status_code == 202
    submitted = response.json()
    assert response.headers["location"] == (
        f"/v1/evaluation-batches/{submitted['batch_id']}"
    )
    assert [item["index"] for item in submitted["items"]] == [1, 2]
    assert [item["case_id"] for item in submitted["items"]] == [
        "batch-first",
        "batch-second",
    ]
    assert [item["original_name"] for item in submitted["items"]] == [
        "first.pptx",
        "second.pptx",
    ]
    assert str(tmp_path) not in response.text
    assert all(
        hidden not in response.text
        for hidden in ("pptx_path", '"uri"', "workspace")
    )

    terminal = _poll_batch(client, submitted["batch_id"])
    assert terminal["status"] == "COMPLETED"
    assert terminal["stage"] == "READY_FOR_REVIEW"
    assert terminal["summary"] == {
        "total": 2,
        "pending": 0,
        "running": 0,
        "completed": 2,
        "failed": 0,
    }
    assert terminal["started_at"] and terminal["completed_at"]
    run_ids = [item["run_id"] for item in terminal["items"]]
    assert len(set(run_ids)) == 2
    for item in terminal["items"]:
        assert item["review_url"] == f"/review/?view=all&run={item['run_id']}"
        assert client.get(item["evaluation_url"]).status_code == 200
        assert client.get(item["review_task_url"]).status_code == 200
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))

    restarted = _client(LocalEvaluationRuntime(runtime.paths.root))
    assert restarted is not None
    assert restarted.get(
        f"/v1/evaluation-batches/{submitted['batch_id']}"
    ).status_code == 404
    for run_id in run_ids:
        assert restarted.get(f"/v1/evaluations/{run_id}").status_code == 200
        assert restarted.get(f"/v1/review/tasks/{run_id}").status_code == 200


def test_batch_runtime_failure_is_isolated_and_aggregated(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = SelectiveRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return

    response = _submit_batch(
        client,
        [(deck, "good.pptx"), (deck, "fail.pptx")],
        ["good-case", "fail-case"],
    )
    assert response.status_code == 202
    terminal = _poll_batch(client, response.json()["batch_id"])

    assert terminal["status"] == "PARTIALLY_FAILED"
    assert terminal["stage"] == "PARTIALLY_READY_FOR_REVIEW"
    assert terminal["summary"]["completed"] == 1
    assert terminal["summary"]["failed"] == 1
    good, failed = terminal["items"]
    assert good["status"] == "COMPLETED"
    assert good["run_id"]
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "EVALUATION_FAILED"
    assert "run_id" not in failed and "review_url" not in failed
    serialized = json.dumps(terminal)
    assert str(tmp_path) not in serialized
    assert "synthetic failure" not in serialized


def test_batch_all_runtime_failures_reach_failed_terminal_state(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = SelectiveRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return

    response = _submit_batch(
        client,
        [(deck, "fail-one.pptx"), (deck, "fail-two.pptx")],
        ["fail-one", "fail-two"],
    )
    terminal = _poll_batch(client, response.json()["batch_id"])

    assert terminal["status"] == "FAILED"
    assert terminal["stage"] == "FAILED"
    assert terminal["summary"]["failed"] == 2
    assert all(item["error_code"] == "EVALUATION_FAILED" for item in terminal["items"])


def test_batch_preflight_and_capacity_rejection_are_atomic(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = ImmediateBatchRuntime(tmp_path / "preflight-var")
    manager = LocalJobManager(runtime, workers=1, max_active_jobs=2)
    client = _client(runtime, job_manager=manager)
    if client is None:
        return

    malformed = client.post(
        "/v1/evaluation-batches/upload",
        data={"case_ids": ["valid", "invalid"], "scene": "ready_made"},
        files=[
            ("presentations", _presentation(deck, "valid.pptx")),
            (
                "presentations",
                ("invalid.pptx", b"not-a-zip", "application/octet-stream"),
            ),
        ],
    )
    assert malformed.status_code == 422
    assert runtime.count == 0
    assert manager.jobs == {}
    assert manager.batches == {}
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))

    release = threading.Event()
    started = threading.Event()
    blocking = BlockingBatchRuntime(tmp_path / "capacity-var", release, started)
    capacity_manager = LocalJobManager(blocking, workers=1, max_active_jobs=2)
    capacity_manager.submit_case(
        EvalCase(
            case_id="blocking",
            scene=SceneType.READY_MADE,
            pptx_path="unused.pptx",
        ),
        fingerprint="a" * 64,
    )
    assert started.wait(timeout=1)
    capacity_client = _client(blocking, job_manager=capacity_manager)
    assert capacity_client is not None
    rejected = _submit_batch(
        capacity_client,
        [(deck, "one.pptx"), (deck, "two.pptx")],
        ["one", "two"],
    )
    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "1"
    assert len(capacity_manager.jobs) == 1
    assert capacity_manager.batches == {}
    assert not tuple((blocking.paths.root / "uploads" / "work").glob("upload-*"))
    release.set()
    capacity_manager.shutdown(wait=True)
    manager.shutdown(wait=True)
    unavailable = _submit_batch(
        client,
        [(deck, "after-shutdown.pptx")],
        ["after-shutdown"],
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "EVALUATION_SERVICE_UNAVAILABLE"
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))


def test_batch_idempotency_binds_ordered_content_and_cleans_retries(
    tmp_path: Path,
) -> None:
    first_deck = build_pptx(tmp_path / "first.pptx")
    second_deck = build_pptx(tmp_path / "second.pptx")
    runtime = ImmediateBatchRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return
    headers = {"Idempotency-Key": "batch-request-1"}
    decks = [(first_deck, "first.pptx"), (second_deck, "second.pptx")]
    case_ids = ["first", "second"]

    first = _submit_batch(client, decks, case_ids, headers=headers)
    assert first.status_code == 202
    terminal = _poll_batch(client, first.json()["batch_id"])
    second = _submit_batch(client, decks, case_ids, headers=headers)
    assert second.status_code == 202
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert [item["job_id"] for item in second.json()["items"]] == [
        item["job_id"] for item in terminal["items"]
    ]
    assert runtime.count == 2

    conflict = _submit_batch(
        client,
        list(reversed(decks)),
        case_ids,
        headers=headers,
    )
    assert conflict.status_code == 409
    assert runtime.count == 2
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))


def test_batch_contract_rejects_ambiguous_or_unbounded_inputs(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = ImmediateBatchRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return

    empty = client.post(
        "/v1/evaluation-batches/upload",
        data={"case_ids": ["missing-file"], "scene": "ready_made"},
    )
    mismatch = _submit_batch(
        client,
        [(deck, "one.pptx"), (deck, "two.pptx")],
        ["one"],
    )
    duplicate = _submit_batch(
        client,
        [(deck, "one.pptx"), (deck, "two.pptx")],
        ["same", "same"],
    )
    wrong_scene = _submit_batch(
        client,
        [(deck, "one.pptx")],
        ["one"],
        scene="text_to_ppt",
    )
    attachment = _submit_batch(
        client,
        [(deck, "one.pptx")],
        ["one"],
        extra_files=[
            ("source_materials", ("brief.txt", b"source", "text/plain"))
        ],
    )
    unknown_field = _submit_batch(
        client,
        [(deck, "one.pptx")],
        ["one"],
        extra_files=[
            ("unexpected", ("secret.txt", b"ignored?", "text/plain"))
        ],
    )
    too_many = _submit_batch(
        client,
        [(deck, f"deck-{index}.pptx") for index in range(17)],
        [f"case-{index}" for index in range(17)],
    )

    assert empty.status_code == 422
    assert mismatch.status_code == 422
    assert duplicate.status_code == 422
    assert wrong_scene.status_code == 422
    assert attachment.status_code == 422
    assert unknown_field.status_code == 422
    assert unknown_field.json()["detail"]["code"] == "BATCH_INVALID"
    assert too_many.status_code == 422
    assert runtime.count == 0
    assert not tuple((runtime.paths.root / "uploads" / "work").glob("upload-*"))

    guarded = _client(runtime, max_request_body_bytes=64)
    assert guarded is not None
    body_rejected = _submit_batch(
        guarded,
        [(deck, "guarded.pptx")],
        ["guarded"],
    )
    assert body_rejected.status_code == 413
    assert body_rejected.json()["detail"] == "REQUEST_BODY_TOO_LARGE"


def test_batch_terminal_snapshot_outlives_child_job_eviction(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = ImmediateBatchRuntime(tmp_path / "var")
    manager = LocalJobManager(
        runtime,
        workers=1,
        max_active_jobs=2,
        max_terminal_jobs=1,
        max_terminal_batches=2,
    )
    client = _client(runtime, job_manager=manager)
    if client is None:
        return

    response = _submit_batch(
        client,
        [(deck, "one.pptx"), (deck, "two.pptx")],
        ["one", "two"],
    )
    terminal = _poll_batch(client, response.json()["batch_id"])

    assert terminal["status"] == "COMPLETED"
    assert len(terminal["items"]) == 2
    assert terminal["summary"]["completed"] == 2
    assert len(manager.jobs) == 1
    manager.shutdown(wait=True)


def test_concurrent_fast_batches_return_before_terminal_retention_eviction(
    tmp_path: Path,
) -> None:
    runtime = ImmediateBatchRuntime(tmp_path / "var")
    manager = LocalJobManager(
        runtime,
        workers=2,
        max_active_jobs=2,
        max_terminal_jobs=2,
        max_terminal_batches=1,
    )

    def submit(index: int) -> dict[str, Any]:
        case_id = f"retention-race-{index}"
        return manager.submit_batch(
            [
                BatchCaseSubmission(
                    case=EvalCase(
                        case_id=case_id,
                        scene=SceneType.READY_MADE,
                        pptx_path="unused.pptx",
                    ),
                    fingerprint=str(index) * 64,
                    metadata={
                        "case_id": case_id,
                        "original_name": f"{case_id}.pptx",
                        "input_sha256": str(index) * 64,
                        "size_bytes": 1,
                    },
                )
            ]
        )

    with ThreadPoolExecutor(max_workers=2) as callers:
        results = list(callers.map(submit, (1, 2)))

    assert len({item["batch_id"] for item in results}) == 2
    assert all(item["items"][0]["job_id"] for item in results)
    manager.shutdown(wait=True)


def test_dynamic_openapi_exposes_bounded_async_batch_contract(tmp_path: Path) -> None:
    runtime = ImmediateBatchRuntime(tmp_path / "var")
    client = _client(runtime)
    if client is None:
        return
    document = client.app.openapi()
    operation = document["paths"]["/v1/evaluation-batches/upload"]["post"]
    schema_ref = operation["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]["$ref"]
    schema = document["components"]["schemas"][schema_ref.rsplit("/", 1)[1]]

    assert set(operation["responses"]) >= {
        "202",
        "400",
        "403",
        "409",
        "413",
        "422",
        "429",
        "503",
    }
    assert schema["properties"]["presentations"]["minItems"] == 1
    assert schema["properties"]["presentations"]["maxItems"] == 16
    assert schema["properties"]["case_ids"]["minItems"] == 1
    assert schema["properties"]["case_ids"]["maxItems"] == 16
    accepted = operation["responses"]["202"]
    assert "Location" in accepted["headers"]
    assert accepted["content"]["application/json"]["schema"]["properties"][
        "batch_id"
    ]["pattern"] == "^batch-[0-9a-f]{32}$"
    batch_path = document["paths"]["/v1/evaluation-batches/{batch_id}"]["get"]
    assert {"200", "404", "422"} <= set(batch_path["responses"])
    batch_parameter = next(
        item for item in batch_path["parameters"] if item["name"] == "batch_id"
    )
    assert batch_parameter["schema"]["pattern"] == "^batch-[0-9a-f]{32}$"
    assert client.get("/v1/evaluation-batches/not-a-batch").status_code == 422
