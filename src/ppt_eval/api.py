from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ppt_eval.config import case_from_mapping, profile_from_mapping
from ppt_eval.runtime import get_runtime


class LocalJobManager:
    def __init__(self, workers: int = 2) -> None:
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ppt-eval")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

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
                report = get_runtime().evaluate(case, profile)
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


def create_app():
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("FastAPI is optional; install ppt-eval-harness[api]") from exc

    app = FastAPI(title="PPT Eval Harness", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    jobs = LocalJobManager()

    @app.get("/healthz")
    def healthz():
        valid, broken_event = get_runtime().audit_log.verify()
        return {"status": "ok" if valid else "degraded", "audit_chain_valid": valid, "broken_event": broken_event}

    @app.post("/v1/evaluations")
    def create_evaluation(payload: dict[str, Any], asynchronous: bool = Query(True, alias="async")):
        if asynchronous:
            return jobs.submit(payload)
        try:
            case_payload = payload.get("case", payload)
            case = case_from_mapping(case_payload)
            profile_payload = payload.get("profile")
            profile = profile_from_mapping(profile_payload) if profile_payload else None
            return get_runtime().evaluate(case, profile)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/v1/evaluations")
    def list_evaluations():
        return get_runtime().list()

    @app.get("/v1/evaluations/{run_id}")
    def get_evaluation(run_id: str):
        try:
            return get_runtime().get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/v1/reviews")
    def create_review(payload: dict[str, Any]):
        try:
            return get_runtime().review(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/v1/feedback")
    def create_feedback(payload: dict[str, Any]):
        try:
            return get_runtime().add_feedback(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/v1/parameter-proposals")
    def create_parameter_proposal(payload: dict[str, Any]):
        try:
            proposal = get_runtime().proposals.create(
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
    def validate_parameter_proposal(proposal_id: str, payload: dict[str, Any]):
        try:
            return get_runtime().proposals.validate(proposal_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/parameter-proposals/{proposal_id}/approve")
    def approve_parameter_proposal(proposal_id: str, payload: dict[str, Any]):
        try:
            return get_runtime().proposals.approve(proposal_id, str(payload["approver"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


try:  # keep CLI/core imports usable without the optional API extra
    app = create_app()
except RuntimeError:  # pragma: no cover - environment dependent
    app = None
