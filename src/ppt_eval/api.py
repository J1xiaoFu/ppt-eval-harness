from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from ppt_eval.config import case_from_mapping, parse_scene, profile_from_mapping
from ppt_eval.domain import EvalCase, EvalProfile
from ppt_eval.infrastructure.local import validated_record_id
from ppt_eval.infrastructure.uploads import (
    MAX_ATTACHMENT_TOTAL_BYTES,
    MAX_PRESENTATION_UPLOAD_BYTES,
    LocalUploadStore,
    UploadStorageError,
    UploadTooLargeError,
    UploadValidationError,
    UploadWorkspace,
)
from ppt_eval.infrastructure.visual_assets import (
    SignedUrlVisualAssetTransport,
    VisualAssetAccessError,
    VisualAssetCAS,
    VisualAssetCatalog,
    VisualAssetGrantExpired,
    VisualAssetGrantInvalid,
    VisualAssetTransportConfig,
    VisualAssetVariant,
)
from ppt_eval.runtime import LocalEvaluationRuntime, get_runtime
from ppt_eval.version import __version__


class MissingApiDependency(RuntimeError):
    """Raised only when the optional FastAPI stack is not installed."""


class JobQueueFullError(RuntimeError):
    """The bounded local active-job capacity has been exhausted."""


class JobIdempotencyConflictError(ValueError):
    """An idempotency key was reused for a different evaluation input."""


class BatchValidationError(ValueError):
    """A batch submission does not satisfy the bounded public contract."""


DEFAULT_MAX_REQUEST_BODY_BYTES = (
    MAX_PRESENTATION_UPLOAD_BYTES + MAX_ATTACHMENT_TOTAL_BYTES + 2 * 1024 * 1024
)
MAX_BATCH_PRESENTATIONS = 16
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _batch_openapi_schema() -> dict[str, Any]:
    item_properties: dict[str, Any] = {
        "index": {"type": "integer", "minimum": 1, "maximum": 16},
        "case_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "original_name": {"type": "string"},
        "input_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "size_bytes": {"type": "integer", "minimum": 1},
        "job_id": {"type": "string", "pattern": "^job-[0-9a-f]{32}$"},
        "status": {
            "type": "string",
            "enum": ["PENDING", "RUNNING", "COMPLETED", "FAILED"],
        },
        "stage": {
            "type": "string",
            "enum": ["QUEUED", "EVALUATING", "READY_FOR_REVIEW", "FAILED"],
        },
        "created_at": {"type": "string", "format": "date-time"},
        "started_at": {
            "anyOf": [
                {"type": "string", "format": "date-time"},
                {"type": "null"},
            ]
        },
        "completed_at": {
            "anyOf": [
                {"type": "string", "format": "date-time"},
                {"type": "null"},
            ]
        },
        "run_id": {"type": "string"},
        "evaluation_url": {"type": "string"},
        "review_task_url": {"type": "string"},
        "review_url": {"type": "string"},
        "error_code": {
            "type": "string",
            "enum": ["EVALUATION_FAILED", "JOB_SCHEDULING_FAILED"],
        },
    }
    return {
        "type": "object",
        "required": [
            "batch_id",
            "status",
            "stage",
            "created_at",
            "started_at",
            "completed_at",
            "summary",
            "items",
        ],
        "properties": {
            "batch_id": {
                "type": "string",
                "pattern": "^batch-[0-9a-f]{32}$",
            },
            "status": {
                "type": "string",
                "enum": [
                    "PENDING",
                    "RUNNING",
                    "COMPLETED",
                    "PARTIALLY_FAILED",
                    "FAILED",
                ],
            },
            "stage": {
                "type": "string",
                "enum": [
                    "QUEUED",
                    "EVALUATING",
                    "READY_FOR_REVIEW",
                    "PARTIALLY_READY_FOR_REVIEW",
                    "FAILED",
                ],
            },
            "created_at": {"type": "string", "format": "date-time"},
            "started_at": {
                "anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]
            },
            "completed_at": {
                "anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]
            },
            "summary": {
                "type": "object",
                "required": ["total", "pending", "running", "completed", "failed"],
                "properties": {
                    "total": {"type": "integer", "minimum": 1, "maximum": 16},
                    **{
                        name: {"type": "integer", "minimum": 0, "maximum": 16}
                        for name in ("pending", "running", "completed", "failed")
                    },
                },
                "additionalProperties": False,
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "required": [
                        "index",
                        "case_id",
                        "original_name",
                        "input_sha256",
                        "size_bytes",
                        "job_id",
                        "status",
                        "stage",
                        "created_at",
                        "started_at",
                        "completed_at",
                    ],
                    "properties": item_properties,
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class BatchCaseSubmission:
    case: EvalCase
    fingerprint: str
    metadata: Mapping[str, Any]
    profile: EvalProfile | None = None
    cleanup: Callable[[], None] | None = None


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before multipart parsing or spooling."""

    def __init__(self, app: Any, *, maximum_bytes: int) -> None:
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes <= 0
        ):
            raise ValueError("maximum request body bytes must be a positive integer")
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        content_length = _scope_header(scope, b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                await _asgi_json_error(send, 400, "INVALID_CONTENT_LENGTH")
                return
            if declared < 0:
                await _asgi_json_error(send, 400, "INVALID_CONTENT_LENGTH")
                return
            if declared > self.maximum_bytes:
                await _asgi_json_error(send, 413, "REQUEST_BODY_TOO_LARGE")
                return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message: dict[str, Any] = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body) if isinstance(body, bytes) else 0
                if received > self.maximum_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await _asgi_json_error(send, 413, "REQUEST_BODY_TOO_LARGE")


class LocalWriteOriginMiddleware:
    """Block browser-originated writes except from this local service UI."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("method") in _WRITE_METHODS:
            origin = _scope_header(scope, b"origin")
            if origin is not None and not _allowed_local_origin(origin):
                await _asgi_json_error(send, 403, "ORIGIN_FORBIDDEN")
                return
        await self.app(scope, receive, send)


class LocalJobManager:
    def __init__(
        self,
        runtime: LocalEvaluationRuntime | None = None,
        workers: int = 2,
        max_active_jobs: int = 32,
        max_terminal_jobs: int = 256,
        max_terminal_batches: int = 64,
    ) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        if (
            isinstance(max_active_jobs, bool)
            or not isinstance(max_active_jobs, int)
            or max_active_jobs < workers
        ):
            raise ValueError("max_active_jobs must be an integer not smaller than workers")
        if (
            isinstance(max_terminal_jobs, bool)
            or not isinstance(max_terminal_jobs, int)
            or max_terminal_jobs < 1
        ):
            raise ValueError("max_terminal_jobs must be a positive integer")
        if (
            isinstance(max_terminal_batches, bool)
            or not isinstance(max_terminal_batches, int)
            or max_terminal_batches < 1
        ):
            raise ValueError("max_terminal_batches must be a positive integer")
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ppt-eval")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.runtime = runtime or get_runtime()
        self._active_slots = threading.BoundedSemaphore(max_active_jobs)
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._terminal_job_ids: deque[str] = deque()
        self._max_terminal_jobs = max_terminal_jobs
        self.batches: dict[str, dict[str, Any]] = {}
        self._batch_idempotency: dict[str, tuple[str, str]] = {}
        self._terminal_batch_ids: deque[str] = deque()
        self._max_terminal_batches = max_terminal_batches
        self._job_batches: dict[str, tuple[str, int]] = {}
        self._shutdown = False

    def submit(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = _payload_fingerprint(payload)

        def evaluate() -> dict[str, Any]:
            case_payload = payload.get("case", payload)
            case = case_from_mapping(case_payload)
            profile_payload = payload.get("profile")
            profile = profile_from_mapping(profile_payload) if profile_payload else None
            return self.runtime.evaluate(case, profile)

        return self._enqueue(
            evaluate,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
        )

    def submit_case(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        *,
        fingerprint: str,
        cleanup: Callable[[], None] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def evaluate() -> dict[str, Any]:
            return self.runtime.evaluate(case, profile)

        return self._enqueue(
            evaluate,
            fingerprint=fingerprint,
            cleanup=cleanup,
            idempotency_key=idempotency_key,
        )

    def _enqueue(
        self,
        evaluate: Callable[[], dict[str, Any]],
        *,
        fingerprint: str,
        cleanup: Callable[[], None] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = (
            validated_record_id(idempotency_key, label="idempotency key")
            if idempotency_key
            else None
        )
        with self.lock:
            if self._shutdown:
                if cleanup is not None:
                    _run_cleanup(cleanup)
                raise RuntimeError("local job manager is shut down")
            if key is not None:
                existing = self._idempotency.get(key)
                if existing is not None:
                    existing_fingerprint, existing_job_id = existing
                    if cleanup is not None:
                        _run_cleanup(cleanup)
                    if existing_fingerprint != fingerprint:
                        raise JobIdempotencyConflictError(
                            "idempotency key was reused with different input"
                        )
                    return dict(self.jobs[existing_job_id])
            if not self._reserve_slots_unlocked(1):
                if cleanup is not None:
                    _run_cleanup(cleanup)
                raise JobQueueFullError("local evaluation queue is full")
            job_id = f"job-{uuid.uuid4().hex}"
            self.jobs[job_id] = self._new_job(job_id)
            if key is not None:
                self._idempotency[key] = (fingerprint, job_id)

        try:
            self._schedule_reserved_job(job_id, evaluate, cleanup=cleanup)
        except Exception:
            with self.lock:
                self.jobs.pop(job_id, None)
                if key is not None:
                    self._idempotency.pop(key, None)
            self._active_slots.release()
            if cleanup is not None:
                _run_cleanup(cleanup)
            raise
        return self.get(job_id)

    def submit_batch(
        self,
        submissions: Sequence[BatchCaseSubmission],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        items = tuple(submissions)
        if not 1 <= len(items) <= MAX_BATCH_PRESENTATIONS:
            self._cleanup_batch(items)
            raise BatchValidationError(
                f"batch must contain between 1 and {MAX_BATCH_PRESENTATIONS} presentations"
            )
        fingerprint = _payload_fingerprint(
            {
                "items": [
                    {
                        "fingerprint": item.fingerprint,
                        "metadata": {
                            str(name): value
                            for name, value in item.metadata.items()
                        },
                    }
                    for item in items
                ]
            }
        )
        key = (
            validated_record_id(idempotency_key, label="idempotency key")
            if idempotency_key
            else None
        )
        with self.lock:
            if self._shutdown:
                self._cleanup_batch(items)
                raise RuntimeError("local job manager is shut down")
            if key is not None:
                existing = self._batch_idempotency.get(key)
                if existing is not None:
                    existing_fingerprint, existing_batch_id = existing
                    self._cleanup_batch(items)
                    if existing_fingerprint != fingerprint:
                        raise JobIdempotencyConflictError(
                            "idempotency key was reused with different batch input"
                        )
                    return self._get_batch_unlocked(existing_batch_id)
            if not self._reserve_slots_unlocked(len(items)):
                self._cleanup_batch(items)
                raise JobQueueFullError("local evaluation queue is full")

            batch_id = f"batch-{uuid.uuid4().hex}"
            job_ids: list[str] = []
            batch_items: list[dict[str, Any]] = []
            for index, submission in enumerate(items, start=1):
                job_id = f"job-{uuid.uuid4().hex}"
                job = self._new_job(job_id)
                self.jobs[job_id] = job
                self._job_batches[job_id] = (batch_id, index - 1)
                job_ids.append(job_id)
                batch_items.append(
                    {
                        "index": index,
                        **{
                            str(name): value
                            for name, value in submission.metadata.items()
                        },
                        **job,
                    }
                )
            self.batches[batch_id] = {
                "batch_id": batch_id,
                "status": "PENDING",
                "stage": "QUEUED",
                "created_at": _utc_now(),
                "started_at": None,
                "completed_at": None,
                "items": batch_items,
                "job_ids": job_ids,
                "terminal_recorded": False,
                "acceptance_pending": True,
            }
            if key is not None:
                self._batch_idempotency[key] = (fingerprint, batch_id)

        for job_id, submission in zip(job_ids, items):
            def evaluate(
                submission: BatchCaseSubmission = submission,
            ) -> dict[str, Any]:
                return self.runtime.evaluate(submission.case, submission.profile)

            try:
                self._schedule_reserved_job(
                    job_id,
                    evaluate,
                    cleanup=submission.cleanup,
                )
            except Exception:
                if submission.cleanup is not None:
                    _run_cleanup(submission.cleanup)
                with self.lock:
                    self.jobs[job_id] = {
                        **self.jobs[job_id],
                        "status": "FAILED",
                        "stage": "FAILED",
                        "error_code": "JOB_SCHEDULING_FAILED",
                        "completed_at": _utc_now(),
                    }
                    self._sync_batch_item_unlocked(job_id)
                    self._remember_terminal_unlocked(job_id)
                self._active_slots.release()
        with self.lock:
            batch = self.batches[batch_id]
            batch["acceptance_pending"] = False
            accepted = self._get_batch_unlocked(batch_id)
            self._prune_terminal_batches_unlocked(protected_batch_id=batch_id)
            return accepted

    def _schedule_reserved_job(
        self,
        job_id: str,
        evaluate: Callable[[], dict[str, Any]],
        *,
        cleanup: Callable[[], None] | None,
    ) -> None:
        def work() -> None:
            try:
                with self.lock:
                    self.jobs[job_id] = {
                        **self.jobs[job_id],
                        "status": "RUNNING",
                        "stage": "EVALUATING",
                        "started_at": _utc_now(),
                    }
                    self._sync_batch_item_unlocked(job_id)
                try:
                    report = evaluate()
                    run_id = validated_record_id(report.get("run_id"), label="run_id")
                    terminal = {
                        "status": "COMPLETED",
                        "stage": "READY_FOR_REVIEW",
                        "run_id": run_id,
                        "evaluation_url": f"/v1/evaluations/{run_id}",
                        "review_task_url": f"/v1/review/tasks/{run_id}",
                        "review_url": f"/review/?view=all&run={run_id}",
                    }
                except Exception:
                    terminal = {
                        "status": "FAILED",
                        "stage": "FAILED",
                        "error_code": "EVALUATION_FAILED",
                    }
                if cleanup is not None:
                    _run_cleanup(cleanup)
                with self.lock:
                    self.jobs[job_id] = {
                        **self.jobs[job_id],
                        **terminal,
                        "completed_at": _utc_now(),
                    }
                    self._sync_batch_item_unlocked(job_id)
                    self._remember_terminal_unlocked(job_id)
            finally:
                self._active_slots.release()

        self.executor.submit(work)

    def _new_job(self, job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "status": "PENDING",
            "stage": "QUEUED",
            "created_at": _utc_now(),
            "started_at": None,
            "completed_at": None,
        }

    def _reserve_slots_unlocked(self, count: int) -> bool:
        acquired = 0
        for _ in range(count):
            if not self._active_slots.acquire(blocking=False):
                for _ in range(acquired):
                    self._active_slots.release()
                return False
            acquired += 1
        return True

    @staticmethod
    def _cleanup_batch(items: Sequence[BatchCaseSubmission]) -> None:
        for item in items:
            if item.cleanup is not None:
                _run_cleanup(item.cleanup)

    def _sync_batch_item_unlocked(self, job_id: str) -> None:
        batch_reference = self._job_batches.get(job_id)
        if batch_reference is None:
            return
        batch_id, item_offset = batch_reference
        batch = self.batches.get(batch_id)
        if batch is None:
            return
        batch["items"][item_offset] = {
            **batch["items"][item_offset],
            **self.jobs[job_id],
        }
        if self.jobs[job_id]["status"] in {"COMPLETED", "FAILED"}:
            self._job_batches.pop(job_id, None)
        self._refresh_batch_unlocked(batch_id)

    def _refresh_batch_unlocked(self, batch_id: str) -> None:
        batch = self.batches[batch_id]
        statuses = [str(item["status"]) for item in batch["items"]]
        total = len(statuses)
        counts = {
            "total": total,
            "pending": statuses.count("PENDING"),
            "running": statuses.count("RUNNING"),
            "completed": statuses.count("COMPLETED"),
            "failed": statuses.count("FAILED"),
        }
        if counts["completed"] + counts["failed"] == total:
            if counts["completed"] == total:
                status, stage = "COMPLETED", "READY_FOR_REVIEW"
            elif counts["failed"] == total:
                status, stage = "FAILED", "FAILED"
            else:
                status, stage = "PARTIALLY_FAILED", "PARTIALLY_READY_FOR_REVIEW"
            batch["completed_at"] = batch.get("completed_at") or _utc_now()
            if not batch["terminal_recorded"]:
                batch["terminal_recorded"] = True
                self._remember_terminal_batch_unlocked(batch_id)
        elif counts["running"] or counts["completed"] or counts["failed"]:
            status, stage = "RUNNING", "EVALUATING"
        else:
            status, stage = "PENDING", "QUEUED"
        batch["status"] = status
        batch["stage"] = stage
        started_at = [
            str(item["started_at"])
            for item in batch["items"]
            if item.get("started_at")
        ]
        batch["started_at"] = min(started_at) if started_at else None
        batch["summary"] = counts

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.lock:
            return self._get_batch_unlocked(batch_id)

    def _get_batch_unlocked(self, batch_id: str) -> dict[str, Any]:
        if batch_id not in self.batches:
            raise KeyError(batch_id)
        self._refresh_batch_unlocked(batch_id)
        batch = self.batches[batch_id]
        return {
            str(key): (
                [dict(item) for item in value]
                if key == "items"
                else dict(value)
                if key == "summary"
                else value
            )
            for key, value in batch.items()
            if key not in {"job_ids", "terminal_recorded", "acceptance_pending"}
        }

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return dict(self.jobs[job_id])

    def _remember_terminal_batch_unlocked(self, batch_id: str) -> None:
        self._terminal_batch_ids.append(batch_id)
        protected = (
            batch_id
            if self.batches.get(batch_id, {}).get("acceptance_pending") is True
            else None
        )
        self._prune_terminal_batches_unlocked(protected_batch_id=protected)

    def _prune_terminal_batches_unlocked(
        self,
        *,
        protected_batch_id: str | None = None,
    ) -> None:
        while len(self._terminal_batch_ids) > self._max_terminal_batches:
            expired: str | None = None
            for _ in range(len(self._terminal_batch_ids)):
                candidate = self._terminal_batch_ids.popleft()
                record = self.batches.get(candidate)
                if candidate == protected_batch_id or (
                    record is not None
                    and record.get("acceptance_pending") is True
                ):
                    self._terminal_batch_ids.append(candidate)
                    continue
                expired = candidate
                break
            if expired is None:
                return
            self.batches.pop(expired, None)
            stale_keys = [
                key
                for key, (_fingerprint, stored_batch_id) in self._batch_idempotency.items()
                if stored_batch_id == expired
            ]
            for key in stale_keys:
                self._batch_idempotency.pop(key, None)

    def shutdown(self, *, wait: bool = False) -> None:
        with self.lock:
            if self._shutdown:
                return
            self._shutdown = True
        self.executor.shutdown(wait=wait, cancel_futures=False)

    def _remember_terminal_unlocked(self, job_id: str) -> None:
        self._terminal_job_ids.append(job_id)
        while len(self._terminal_job_ids) > self._max_terminal_jobs:
            expired = self._terminal_job_ids.popleft()
            self.jobs.pop(expired, None)
            stale_keys = [
                key
                for key, (_fingerprint, stored_job_id) in self._idempotency.items()
                if stored_job_id == expired
            ]
            for key in stale_keys:
                self._idempotency.pop(key, None)


def create_app(
    runtime: LocalEvaluationRuntime | None = None,
    *,
    upload_store: LocalUploadStore | None = None,
    job_manager: LocalJobManager | None = None,
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> Any:
    try:
        import python_multipart  # noqa: F401
        from fastapi import (
            Depends,
            FastAPI,
            File,
            Form,
            Header,
            HTTPException,
            Query,
            Request,
            UploadFile,
        )
        from fastapi import Path as ApiPath
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MissingApiDependency(
            "FastAPI is optional; install ppt-eval-harness[api]"
        ) from exc

    # Endpoint functions are nested to keep FastAPI optional.  With postponed
    # annotations FastAPI resolves UploadFile from module globals, so publish
    # only this type after the guarded import succeeds.
    globals()["UploadFile"] = UploadFile
    globals()["Request"] = Request

    app = FastAPI(title="PPT Eval Harness", version=__version__)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_bytes=max_request_body_bytes,
    )
    app.add_middleware(LocalWriteOriginMiddleware)
    # Starlette applies the last-added middleware outermost.  CORS must wrap
    # early Origin/body-limit responses so the local Vite UI can read them.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost):\d+",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    runtime_instance = runtime or get_runtime()
    visual_asset_config = VisualAssetTransportConfig.from_environment()
    jobs = job_manager or LocalJobManager(runtime_instance)
    uploads = upload_store or LocalUploadStore(
        runtime_instance.paths.root / "uploads",
    )
    app.state.job_manager = jobs
    app.state.upload_store = uploads
    app.router.add_event_handler("shutdown", jobs.shutdown)

    visual_asset_transport = runtime_instance.visual_asset_transport
    if visual_asset_config.signed_url_enabled and visual_asset_transport is None:
        visual_asset_transport = SignedUrlVisualAssetTransport(
            config=visual_asset_config,
            catalog=VisualAssetCatalog(
                {
                    VisualAssetVariant.SLIDE: (runtime_instance.paths.render_cache,),
                    VisualAssetVariant.ATLAS: (
                        runtime_instance.paths.artifacts / "visual-atlases",
                    ),
                    VisualAssetVariant.CROP: (
                        runtime_instance.paths.artifacts / "visual-crops",
                    ),
                }
            ),
            content_store=VisualAssetCAS(
                runtime_instance.paths.render_cache / "visual-cas",
            ),
        )
    if visual_asset_transport is not None:
        app.state.visual_asset_transport = visual_asset_transport
        app.state.visual_asset_transport_config = {
            "mode": "signed-url",
            "signed_url_enabled": True,
            "ttl_seconds": visual_asset_transport.signer.ttl_seconds,
        }
    else:
        app.state.visual_asset_transport_config = {
            "mode": visual_asset_config.mode,
            "signed_url_enabled": False,
            "ttl_seconds": visual_asset_config.ttl_seconds,
        }

    async def validate_batch_multipart_fields(request: Request) -> None:
        form = await request.form()
        allowed = {"presentations", "case_ids", "scene", "request", "audience"}
        unknown = sorted(set(form.keys()) - allowed)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "BATCH_INVALID",
                    "message": (
                        "unsupported batch multipart fields: " + ", ".join(unknown)
                    ),
                },
            )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        valid, broken_event = runtime_instance.audit_log.verify()
        return {
            "status": "ok" if valid else "degraded",
            "service_version": __version__,
            "audit_chain_valid": valid,
            "broken_event": broken_event,
        }

    if visual_asset_transport is not None:

        @app.get("/v1/model-assets/{variant}/{asset_sha256}")
        def get_model_visual_asset(
            variant: str,
            asset_sha256: str,
            expires: int = Query(...),
            signature: str = Query(...),
        ) -> Any:
            try:
                asset_variant = VisualAssetVariant(variant)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="visual asset not found") from exc
            try:
                asset, asset_bytes = visual_asset_transport.catalog.verified_snapshot(
                    variant=asset_variant,
                    asset_sha256=asset_sha256,
                )
                visual_asset_transport.signer.verify(
                    asset,
                    expires=expires,
                    signature=signature,
                )
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="visual asset not found") from exc
            except VisualAssetGrantExpired as exc:
                raise HTTPException(status_code=410, detail="visual asset URL expired") from exc
            except VisualAssetGrantInvalid as exc:
                raise HTTPException(status_code=403, detail="invalid visual asset URL") from exc
            except (VisualAssetAccessError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="visual asset not found") from exc
            remaining = max(0, expires - int(datetime.now(timezone.utc).timestamp()))
            return Response(
                content=asset_bytes,
                media_type=asset.media_type,
                headers={
                    "Cache-Control": f"public, max-age={remaining}, immutable",
                    "ETag": f'"{asset.sha256}"',
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    @app.post("/v1/evaluations")
    def create_evaluation(
        payload: dict[str, Any],
        asynchronous: bool = Query(True, alias="async"),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        try:
            if asynchronous:
                return jobs.submit(payload, idempotency_key=idempotency_key)
            case_payload = payload.get("case", payload)
            case = case_from_mapping(case_payload)
            profile_payload = payload.get("profile")
            profile = profile_from_mapping(profile_payload) if profile_payload else None
            return _public_report(runtime_instance.evaluate(case, profile))
        except JobQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail="evaluation queue is full",
                headers={"Retry-After": "1"},
            ) from exc
        except JobIdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.post(
        "/v1/evaluations/upload",
        status_code=202,
        responses={
            200: {"description": "Synchronous evaluation report."},
            202: {"description": "Evaluation accepted into the bounded local queue."},
            400: {"description": "The Content-Length header is invalid."},
            403: {"description": "The browser Origin is not a permitted local UI."},
            409: {"description": "Idempotency key conflicts with another input."},
            413: {"description": "A streamed upload exceeded its byte limit."},
            422: {"description": "Filename, suffix, scene, or OOXML preflight failed."},
            429: {"description": "The bounded local evaluation queue is full."},
            503: {"description": "The controlled upload store is unavailable."},
        },
    )
    def create_uploaded_evaluation(
        presentation: UploadFile = File(...),
        case_id: str = Form(...),
        scene: str = Form(...),
        request_text: str | None = Form(None, alias="request"),
        audience: str | None = Form(None),
        source_materials: list[UploadFile] | None = File(None),
        assets: list[UploadFile] | None = File(None),
        asynchronous: bool = Query(True, alias="async"),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> Any:
        workspace: UploadWorkspace | None = None
        try:
            safe_case_id = _validated_case_id(case_id)
            safe_scene = parse_scene(scene).value
            safe_idempotency_key = (
                validated_record_id(idempotency_key, label="idempotency key")
                if idempotency_key
                else None
            )
            safe_request = _validated_optional_text(
                request_text,
                label="request",
                maximum_characters=20_000,
            )
            safe_audience = _validated_optional_text(
                audience,
                label="audience",
                maximum_characters=2_000,
            )
            submission = {
                "case_id": safe_case_id,
                "scene": safe_scene,
                "request": safe_request,
                "audience": safe_audience,
            }
            workspace = uploads.prepare(
                presentation,
                source_materials=tuple(source_materials or ()),
                assets=tuple(assets or ()),
                submission=submission,
            )
            case = _case_from_upload_workspace(workspace, submission)
            if asynchronous:
                input_sha256 = workspace.presentation_sha256
                total_size_bytes = workspace.total_size_bytes
                job = jobs.submit_case(
                    case,
                    fingerprint=workspace.fingerprint,
                    cleanup=workspace.cleanup,
                    idempotency_key=safe_idempotency_key,
                )
                workspace = None  # ownership transferred to LocalJobManager
                payload = {
                    **job,
                    "input_sha256": input_sha256,
                    "size_bytes": total_size_bytes,
                }
                return JSONResponse(
                    status_code=202,
                    content=payload,
                    headers={"Location": f"/v1/jobs/{job['job_id']}"},
                )
            report = runtime_instance.evaluate(case)
            _run_cleanup(workspace.cleanup)
            workspace = None
            return JSONResponse(status_code=200, content=_public_report(report))
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=exc.error_code) from exc
        except UploadValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.error_code, "message": _public_error_detail(exc)},
            ) from exc
        except UploadStorageError as exc:
            raise HTTPException(status_code=503, detail=exc.error_code) from exc
        except JobQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail="evaluation queue is full",
                headers={"Retry-After": "1"},
            ) from exc
        except JobIdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc
        finally:
            if workspace is not None:
                _run_cleanup(workspace.cleanup)

    @app.post(
        "/v1/evaluation-batches/upload",
        status_code=202,
        response_model=None,
        responses={
            202: {
                "description": "Batch accepted into the bounded local queue.",
                "headers": {
                    "Location": {
                        "description": "Batch status URL.",
                        "schema": {"type": "string"},
                    }
                },
                "content": {
                    "application/json": {"schema": _batch_openapi_schema()}
                },
            },
            400: {"description": "The Content-Length header is invalid."},
            403: {"description": "The browser Origin is not a permitted local UI."},
            409: {"description": "Idempotency key conflicts with another batch."},
            413: {"description": "The request or an uploaded PPTX exceeded its limit."},
            422: {"description": "Batch fields or a presentation failed validation."},
            429: {"description": "The bounded local evaluation queue is full."},
            503: {"description": "The controlled upload store is unavailable."},
        },
    )
    def create_uploaded_evaluation_batch(
        presentations: list[UploadFile] = File(
            ...,
            min_length=1,
            max_length=MAX_BATCH_PRESENTATIONS,
        ),
        case_ids: list[str] = Form(
            ...,
            min_length=1,
            max_length=MAX_BATCH_PRESENTATIONS,
        ),
        scene: Literal["ready_made"] = Form("ready_made"),
        request_text: str | None = Form(
            None,
            alias="request",
            max_length=20_000,
        ),
        audience: str | None = Form(None, max_length=2_000),
        idempotency_key: str | None = Header(
            None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        _validated_fields: None = Depends(validate_batch_multipart_fields),
    ) -> Any:
        workspaces: list[UploadWorkspace] = []
        try:
            presentation_files = tuple(presentations)
            if not 1 <= len(presentation_files) <= MAX_BATCH_PRESENTATIONS:
                raise BatchValidationError(
                    f"batch must contain between 1 and {MAX_BATCH_PRESENTATIONS} presentations"
                )
            if len(case_ids) != len(presentation_files):
                raise BatchValidationError(
                    "case_ids must contain exactly one value per presentation"
                )
            safe_case_ids = tuple(_validated_case_id(value) for value in case_ids)
            if len(set(safe_case_ids)) != len(safe_case_ids):
                raise BatchValidationError("case_ids must be unique within a batch")
            safe_scene = parse_scene(scene).value
            if safe_scene != "ready_made":
                raise BatchValidationError(
                    "batch upload currently supports only the ready_made scene"
                )
            safe_request = _validated_optional_text(
                request_text,
                label="request",
                maximum_characters=20_000,
            )
            safe_audience = _validated_optional_text(
                audience,
                label="audience",
                maximum_characters=2_000,
            )
            safe_idempotency_key = (
                validated_record_id(idempotency_key, label="idempotency key")
                if idempotency_key
                else None
            )
            submissions: list[BatchCaseSubmission] = []
            for case_id, presentation in zip(safe_case_ids, presentation_files):
                submission = {
                    "case_id": case_id,
                    "scene": safe_scene,
                    "request": safe_request,
                    "audience": safe_audience,
                }
                workspace = uploads.prepare(
                    presentation,
                    submission=submission,
                )
                workspaces.append(workspace)
                submissions.append(
                    BatchCaseSubmission(
                        case=_case_from_upload_workspace(workspace, submission),
                        fingerprint=workspace.fingerprint,
                        cleanup=workspace.cleanup,
                        metadata={
                            "case_id": case_id,
                            "original_name": workspace.presentation_original_name,
                            "input_sha256": workspace.presentation_sha256,
                            "size_bytes": workspace.total_size_bytes,
                        },
                    )
                )
            batch = jobs.submit_batch(
                submissions,
                idempotency_key=safe_idempotency_key,
            )
            workspaces = []  # ownership transferred to LocalJobManager
            return JSONResponse(
                status_code=202,
                content=_public_report(batch),
                headers={
                    "Location": (
                        f"/v1/evaluation-batches/{batch['batch_id']}"
                    )
                },
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=exc.error_code) from exc
        except UploadValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.error_code, "message": _public_error_detail(exc)},
            ) from exc
        except BatchValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "BATCH_INVALID", "message": _public_error_detail(exc)},
            ) from exc
        except UploadStorageError as exc:
            raise HTTPException(status_code=503, detail=exc.error_code) from exc
        except JobQueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail="evaluation queue is full",
                headers={"Retry-After": "1"},
            ) from exc
        except JobIdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="EVALUATION_SERVICE_UNAVAILABLE",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc
        finally:
            for workspace in workspaces:
                _run_cleanup(workspace.cleanup)

    @app.get(
        "/v1/evaluation-batches/{batch_id}",
        response_model=None,
        responses={
            200: {
                "description": "Current aggregate and ordered per-item status.",
                "content": {
                    "application/json": {"schema": _batch_openapi_schema()}
                },
            },
            404: {"description": "Batch not found."},
            422: {"description": "Batch identifier has an invalid format."},
        },
    )
    def get_evaluation_batch(
        batch_id: str = ApiPath(..., pattern=r"^batch-[0-9a-f]{32}$"),
    ) -> dict[str, Any]:
        try:
            safe_batch_id = validated_record_id(batch_id, label="batch_id")
            return _public_report(jobs.get_batch(safe_batch_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="batch not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return _public_report(jobs.get(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/v1/evaluations")
    def list_evaluations() -> list[dict[str, Any]]:
        return [_public_report(item) for item in runtime_instance.list()]

    @app.get("/v1/evaluations/{run_id}")
    def get_evaluation(run_id: str) -> dict[str, Any]:
        try:
            return _public_report(runtime_instance.get(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.get("/v1/review/tasks")
    def list_review_tasks(
        view: str = Query("queue"),
        query: str = Query(""),
        decision: str | None = Query(None),
        coverage: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            return _public_report(runtime_instance.list_review_tasks(
                view=view,
                query=query,
                decision=decision,
                coverage=coverage,
                limit=limit,
                offset=offset,
            ))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.get("/v1/review/tasks/{run_id}")
    def get_review_task(run_id: str) -> dict[str, Any]:
        try:
            detail = runtime_instance.review_task(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail="review task is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc
        detail["slides"] = [
            {
                **slide,
                "image_url": (
                    f"/v1/review/tasks/{run_id}/slides/{slide['page_number']}"
                ),
                "thumbnail_url": (
                    f"/v1/review/tasks/{run_id}/slides/{slide['page_number']}"
                ),
            }
            for slide in detail["slides"]
        ]
        detail["artifacts"] = {
            **detail["artifacts"],
            "report_url": f"/v1/review/tasks/{run_id}/report",
            "observations_url": (
                f"/v1/review/tasks/{run_id}/artifacts/atomic_observations"
                if detail["artifacts"]["atomic_observations"]["available"]
                else None
            ),
            "source_pptx_url": (
                f"/v1/review/tasks/{run_id}/artifacts/source_pptx"
                if detail["artifacts"]["source_pptx"]["available"]
                else None
            ),
            "render_manifest_url": (
                f"/v1/review/tasks/{run_id}/artifacts/slide_render_manifest"
                if detail["artifacts"]["slide_render_manifest"]["available"]
                else None
            ),
            "visual_contract_urls": {
                role: (
                    f"/v1/review/tasks/{run_id}/artifacts/{role}"
                    if isinstance(reference, Mapping)
                    and reference.get("available") is True
                    else None
                )
                for role, reference in detail["artifacts"].items()
                if role.startswith("visual_") or role == "atlas_scout"
            },
        }
        detail["audit_url"] = f"/v1/review/tasks/{run_id}/audit"
        detail["issues"] = [
            _light_attention_issue(item)
            for item in detail.get("issues", ())
            if isinstance(item, Mapping)
        ]
        raw_inputs = detail.get("inputs")
        raw_inputs = raw_inputs if isinstance(raw_inputs, Mapping) else {}
        detail["inputs"] = [
            {
                **dict(item),
                "download_url": (
                    f"/v1/review/tasks/{run_id}/inputs/"
                    f"{item['role']}/{item['index']}"
                    if item.get("available") is True
                    else None
                ),
            }
            for field in ("source_materials", "assets")
            for item in raw_inputs.get(field, ())
            if isinstance(item, Mapping)
        ]
        for key in ("results", "gate_results", "model_routes", "manifest"):
            detail.pop(key, None)
        detail.pop("attention_details", None)
        detail.pop("observation_count", None)
        detail.pop("visual_audit_summary", None)
        return _public_report(detail)

    @app.get("/v1/review/tasks/{run_id}/audit")
    def get_review_audit(run_id: str) -> dict[str, Any]:
        try:
            detail = runtime_instance.review_task(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc
        artifact_links = detail.get("artifacts")
        artifact_links = artifact_links if isinstance(artifact_links, Mapping) else {}
        observation = artifact_links.get("atomic_observations")
        observation = observation if isinstance(observation, Mapping) else {}
        observation_available = observation.get("available") is True
        visual_contract_artifacts = {
            role: {
                **dict(reference),
                "url": (
                    f"/v1/review/tasks/{run_id}/artifacts/{role}"
                    if reference.get("available") is True
                    else None
                ),
            }
            for role, reference in artifact_links.items()
            if isinstance(reference, Mapping)
            and (role.startswith("visual_") or role == "atlas_scout")
        }
        return _public_report(
            {
                "run_id": run_id,
                "results": detail["results"],
                "gate_results": detail["gate_results"],
                "model_routes": detail["model_routes"],
                "manifest": detail["manifest"],
                "service_version": detail.get("service_version", "0.8.3"),
                "attention_summary": detail["attention_summary"],
                "attention_details": detail["attention_details"],
                "visual_audit_summary": detail.get("visual_audit_summary", {}),
                "visual_contract_artifacts": visual_contract_artifacts,
                "observation_artifact": {
                    "available": observation_available,
                    "url": (
                        f"/v1/review/tasks/{run_id}/artifacts/atomic_observations"
                        if observation_available
                        else None
                    ),
                    "count": detail["observation_count"],
                    "sha256": detail["observation_hash"],
                    "valid": detail["audit_integrity"].get(
                        "observation_artifact_valid"
                    ),
                },
                "reviews": detail["reviews"],
                "audit_integrity": detail["audit_integrity"],
            }
        )

    @app.get("/v1/review/tasks/{run_id}/report")
    def get_review_report(run_id: str) -> dict[str, Any]:
        try:
            return _public_report(runtime_instance.get(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.get("/v1/review/tasks/{run_id}/slides/{page_number}")
    def get_review_slide(run_id: str, page_number: int) -> Any:
        try:
            slides = runtime_instance.review_slide_paths(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or slides not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc
        if page_number < 1 or page_number > len(slides):
            raise HTTPException(status_code=404, detail="slide not found")
        return FileResponse(
            slides[page_number - 1],
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/review/tasks/{run_id}/artifacts/{role}")
    def get_review_artifact(run_id: str, role: str) -> Any:
        try:
            path, metadata = runtime_instance.review_artifact(run_id, role)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc
        disposition = "attachment" if role == "source_pptx" else "inline"
        return FileResponse(
            path,
            media_type=str(metadata["media_type"]),
            filename=str(metadata["original_name"]),
            content_disposition_type=disposition,
            headers={
                "X-Content-Type-Options": "nosniff",
                "ETag": f'"{metadata["sha256"]}"',
            },
        )

    @app.get("/v1/review/tasks/{run_id}/inputs/{role}/{index}")
    def get_review_input(run_id: str, role: str, index: int) -> Any:
        try:
            path, metadata = runtime_instance.review_input_artifact(
                run_id,
                role,
                index,
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="input artifact not found") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=_public_error_detail(exc),
            ) from exc
        return FileResponse(
            path,
            media_type=str(metadata["media_type"]),
            filename=str(metadata["original_name"]),
            content_disposition_type="attachment",
            headers={
                "X-Content-Type-Options": "nosniff",
                "ETag": f'"{metadata["sha256"]}"',
            },
        )

    @app.get("/v1/review/tasks/{run_id}/reviews")
    def list_reviews(run_id: str) -> list[dict[str, Any]]:
        try:
            value = _sanitize_public_value(runtime_instance.reviews(run_id))
            return value if isinstance(value, list) else []
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.post("/v1/reviews")
    def create_review(
        payload: dict[str, Any],
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        try:
            if idempotency_key and not payload.get("client_request_id"):
                payload = {**payload, "client_request_id": idempotency_key}
            return _public_report(runtime_instance.review(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except (TypeError, ValueError) as exc:
            status_code = 409 if "idempotency" in str(exc).lower() else 422
            raise HTTPException(
                status_code=status_code,
                detail=_public_error_detail(exc),
            ) from exc

    @app.post("/v1/feedback")
    def create_feedback(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return _public_report(runtime_instance.add_feedback(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/v1/parameter-proposals")
    def create_parameter_proposal(payload: dict[str, Any]) -> Any:
        try:
            proposal = runtime_instance.proposals.create(
                profile_id=str(payload["profile_id"]),
                base_version=str(payload["base_version"]),
                proposed_changes=dict(payload["proposed_changes"]),
                rationale=str(payload["rationale"]),
                evidence_run_ids=tuple(payload["evidence_run_ids"]),
            )
            return proposal
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_public_error_detail(exc)) from exc

    @app.post("/v1/parameter-proposals/{proposal_id}/validate")
    def validate_parameter_proposal(
        proposal_id: str, payload: dict[str, Any]
    ) -> Any:
        try:
            return runtime_instance.proposals.validate(proposal_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc

    @app.post("/v1/parameter-proposals/{proposal_id}/approve")
    def approve_parameter_proposal(
        proposal_id: str, payload: dict[str, Any]
    ) -> Any:
        try:
            return runtime_instance.proposals.approve(proposal_id, str(payload["approver"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_public_error_detail(exc)) from exc

    configured_ui_directory = str(os.getenv("PPT_EVAL_UI_DIR") or "").strip()
    ui_directory = (
        Path(configured_ui_directory)
        if configured_ui_directory
        else Path.cwd() / "ui" / "dist"
    )
    if ui_directory.is_dir():
        @app.get("/", include_in_schema=False)
        def review_ui_redirect() -> Any:
            return RedirectResponse(url="/review/")

        app.mount(
            "/review",
            StaticFiles(directory=ui_directory, html=True),
            name="review-ui",
        )

    return app


_OMIT = object()
_PATH_KEYS = frozenset(
    {
        "cache_dir",
        "document_path",
        "path",
        "pptx_path",
        "uri",
        "workspace",
        "workspace_dir",
    }
)
_EMBEDDED_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s:'\"(])(?:[a-z]:[\\/]|\\\\)")
_EMBEDDED_POSIX_PATH = re.compile(
    r"(?:^|[\s:'\"(=])/(?!/|v1(?:/|$)|review(?:/|$))[^\s'\"<>]+"
)


def _public_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursive public projection with no host filesystem locations."""

    sanitized = _sanitize_public_value(report)
    return sanitized if isinstance(sanitized, dict) else {}


def _light_attention_issue(item: Mapping[str, Any]) -> dict[str, Any]:
    hidden = {"kind", "semantic_family", "metric_id", "lineage"}
    value = {str(key): entry for key, entry in item.items() if str(key) not in hidden}
    light_evidence_keys = {"source", "page_number", "bbox"}
    value["evidence"] = [
        {
            str(key): entry
            for key, entry in evidence.items()
            if str(key) in light_evidence_keys
        }
        for evidence in item.get("evidence", ())
        if isinstance(evidence, Mapping)
    ]
    return value


def _sanitize_public_value(value: Any, *, key: str | None = None) -> Any:
    normalized_key = (key or "").casefold()
    if normalized_key in _PATH_KEYS or normalized_key.endswith(("_path", "_paths")):
        return _OMIT
    if normalized_key == "source_uri" and isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            return _OMIT
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            replacement = _sanitize_public_value(item, key=name)
            if replacement is not _OMIT:
                sanitized[name] = replacement
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            replacement
            for item in value
            if (replacement := _sanitize_public_value(item)) is not _OMIT
        ]
    if isinstance(value, str) and _contains_local_path(value):
        return "[redacted-local-path]"
    return value


def _contains_local_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    parsed = urlsplit(stripped)
    if (
        parsed.scheme == ""
        and parsed.netloc == ""
        and parsed.path.startswith(("/v1/", "/review/"))
        and "\\" not in stripped
        and ".." not in parsed.path.split("/")
        and not any(ord(character) < 32 for character in stripped)
    ):
        return False
    if parsed.scheme in {"http", "https", "urn"}:
        return False
    if parsed.scheme.casefold() == "file":
        return True
    try:
        if Path(stripped).is_absolute() or PureWindowsPath(stripped).is_absolute():
            return True
    except (OSError, ValueError):
        return True
    return bool(
        _EMBEDDED_WINDOWS_PATH.search(value) or _EMBEDDED_POSIX_PATH.search(value)
    )


def _public_error_detail(exc: Exception) -> str:
    message = str(exc).strip() or "request validation failed"
    sanitized = _sanitize_public_value(message)
    if sanitized == "[redacted-local-path]":
        return "request validation failed"
    return str(sanitized)[:500]


def _case_from_upload_workspace(
    workspace: UploadWorkspace,
    submission: Mapping[str, Any],
) -> EvalCase:
    return case_from_mapping(
        {
            **submission,
            "pptx_path": str(workspace.presentation_path),
            "source_materials": [
                str(path) for path in workspace.source_material_paths
            ],
            "assets": [str(path) for path in workspace.asset_paths],
            "metadata": {
                "source_pptx_original_name": workspace.presentation_original_name,
                "artifact_hashes": {
                    "source_pptx": workspace.presentation_sha256,
                    **{
                        f"source_material/{index}": digest
                        for index, digest in enumerate(
                            workspace.source_material_hashes,
                            start=1,
                        )
                    },
                    **{
                        f"asset/{index}": digest
                        for index, digest in enumerate(
                            workspace.asset_hashes,
                            start=1,
                        )
                    },
                },
            },
        }
    )


def _validated_case_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ValueError("case_id must be non-blank, at most 128 characters, and contain no controls")
    return text


def _validated_optional_text(
    value: object,
    *,
    label: str,
    maximum_characters: int,
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > maximum_characters or any(
        (ord(character) < 32 and character not in "\n\r\t")
        or ord(character) == 127
        for character in text
    ):
        raise ValueError(
            f"{label} must be at most {maximum_characters} characters and contain no controls"
        )
    return text


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_cleanup(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except (OSError, RuntimeError):
        pass


def _scope_header(scope: Mapping[str, Any], name: bytes) -> str | None:
    headers = scope.get("headers", ())
    if not isinstance(headers, (list, tuple)):
        return None
    for raw_name, raw_value in headers:
        if (
            isinstance(raw_name, bytes)
            and isinstance(raw_value, bytes)
            and raw_name.lower() == name
        ):
            return raw_value.decode("latin-1").strip()
    return None


def _allowed_local_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    del port
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


async def _asgi_json_error(send: Any, status_code: int, code: str) -> None:
    body = json.dumps({"detail": code}, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


try:  # keep CLI/core imports usable without the optional API extra
    app = create_app()
except MissingApiDependency:  # pragma: no cover - environment dependent
    app = None
