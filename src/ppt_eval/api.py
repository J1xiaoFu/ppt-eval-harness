from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ppt_eval.config import case_from_mapping, profile_from_mapping
from ppt_eval.runtime import LocalEvaluationRuntime, get_runtime


class MissingApiDependency(RuntimeError):
    """Raised only when the optional FastAPI stack is not installed."""


class LocalJobManager:
    def __init__(
        self,
        runtime: LocalEvaluationRuntime | None = None,
        workers: int = 2,
    ) -> None:
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ppt-eval")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.runtime = runtime or get_runtime()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = f"job-{uuid.uuid4().hex}"
        with self.lock:
            self.jobs[job_id] = {"job_id": job_id, "status": "PENDING"}

        def work() -> None:
            try:
                case_payload = payload.get("case", payload)
                case = case_from_mapping(case_payload)
                profile_payload = payload.get("profile")
                profile = profile_from_mapping(profile_payload) if profile_payload else None
                with self.lock:
                    self.jobs[job_id] = {"job_id": job_id, "status": "RUNNING"}
                report = self.runtime.evaluate(case, profile)
                with self.lock:
                    self.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "COMPLETED",
                        "run_id": report["run_id"],
                    }
            except Exception as exc:
                with self.lock:
                    self.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        self.executor.submit(work)
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return dict(self.jobs[job_id])


def create_app(runtime: LocalEvaluationRuntime | None = None) -> Any:
    try:
        from fastapi import FastAPI, Header, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, RedirectResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MissingApiDependency(
            "FastAPI is optional; install ppt-eval-harness[api]"
        ) from exc

    app = FastAPI(title="PPT Eval Harness", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost):\d+",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    runtime_instance = runtime or get_runtime()
    jobs = LocalJobManager(runtime_instance)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        valid, broken_event = runtime_instance.audit_log.verify()
        return {"status": "ok" if valid else "degraded", "audit_chain_valid": valid, "broken_event": broken_event}

    @app.post("/v1/evaluations")
    def create_evaluation(
        payload: dict[str, Any], asynchronous: bool = Query(True, alias="async")
    ) -> dict[str, Any]:
        if asynchronous:
            return jobs.submit(payload)
        try:
            case_payload = payload.get("case", payload)
            case = case_from_mapping(case_payload)
            profile_payload = payload.get("profile")
            profile = profile_from_mapping(profile_payload) if profile_payload else None
            return runtime_instance.evaluate(case, profile)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id)
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
            return runtime_instance.list_review_tasks(
                view=view,
                query=query,
                decision=decision,
                coverage=coverage,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/review/tasks/{run_id}")
    def get_review_task(run_id: str) -> dict[str, Any]:
        try:
            detail = runtime_instance.review_task(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        }
        detail["audit_url"] = f"/v1/review/tasks/{run_id}/audit"
        for key in ("results", "gate_results", "model_routes", "manifest"):
            detail.pop(key, None)
        return _public_report(detail)

    @app.get("/v1/review/tasks/{run_id}/audit")
    def get_review_audit(run_id: str) -> dict[str, Any]:
        try:
            detail = runtime_instance.review_task(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_report(
            {
                "run_id": run_id,
                "results": detail["results"],
                "gate_results": detail["gate_results"],
                "model_routes": detail["model_routes"],
                "manifest": detail["manifest"],
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/review/tasks/{run_id}/slides/{page_number}")
    def get_review_slide(run_id: str, page_number: int) -> Any:
        try:
            slides = runtime_instance.review_slide_paths(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or slides not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    @app.get("/v1/review/tasks/{run_id}/reviews")
    def list_reviews(run_id: str) -> list[dict[str, Any]]:
        try:
            return runtime_instance.reviews(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/reviews")
    def create_review(
        payload: dict[str, Any],
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        try:
            if idempotency_key and not payload.get("client_request_id"):
                payload = {**payload, "client_request_id": idempotency_key}
            return runtime_instance.review(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except (TypeError, ValueError) as exc:
            status_code = 409 if "idempotency" in str(exc).lower() else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.post("/v1/feedback")
    def create_feedback(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return runtime_instance.add_feedback(payload)
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/parameter-proposals/{proposal_id}/validate")
    def validate_parameter_proposal(
        proposal_id: str, payload: dict[str, Any]
    ) -> Any:
        try:
            return runtime_instance.proposals.validate(proposal_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/parameter-proposals/{proposal_id}/approve")
    def approve_parameter_proposal(
        proposal_id: str, payload: dict[str, Any]
    ) -> Any:
        try:
            return runtime_instance.proposals.approve(proposal_id, str(payload["approver"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    """Remove host filesystem locations while preserving audit facts."""

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if str(key) != "uri"
                and not (
                    str(key) == "source_uri"
                    and isinstance(item, str)
                    and not item.startswith("https://")
                )
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    sanitized = sanitize(report)
    return sanitized if isinstance(sanitized, dict) else {}


try:  # keep CLI/core imports usable without the optional API extra
    app = create_app()
except MissingApiDependency:  # pragma: no cover - environment dependent
    app = None
