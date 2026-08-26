from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ppt_eval.application import DagScheduler, EvaluationService, RunSupervisor
from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, EvalProfile
from ppt_eval.flywheel import (
    ActiveSampler,
    JsonlRecordStore,
    ParameterProposalService,
    feedback_from_mapping,
)
from ppt_eval.infrastructure import (
    JsonlAuditLog,
    JsonRunRepository,
    LocalArtifactStore,
    font_fingerprint,
    git_sha,
    to_primitive,
)
from ppt_eval.oracles import build_default_registry
from ppt_eval.reporting import export_run_report


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    reports: Path
    audit: Path
    artifacts: Path

    @classmethod
    def under(cls, root: str | Path) -> "RuntimePaths":
        root_path = Path(root)
        return cls(
            root=root_path,
            reports=root_path / "runs",
            audit=root_path / "audit" / "events.jsonl",
            artifacts=root_path / "artifacts",
        )


def normalized_report_payload(outcome: Any) -> dict[str, Any]:
    payload = to_primitive(outcome.report)
    payload["scenario"] = payload.get("scene")
    payload["degradation_reasons"] = list(payload.get("review_reasons", ()))
    payload["manifest"] = to_primitive(outcome.manifest)
    payload["score_breakdown"] = to_primitive(outcome.score) if outcome.score else None
    return payload


class LocalEvaluationRuntime:
    """Runnable composition root for CLI/API and local shadow evaluation."""

    def __init__(self, root: str | Path = "var") -> None:
        self.paths = RuntimePaths.under(root)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.repository = JsonRunRepository(self.paths.reports)
        self.audit_log = JsonlAuditLog(self.paths.audit)
        self.artifacts = LocalArtifactStore(self.paths.artifacts)
        self._git_sha = git_sha(Path.cwd())
        self._font_fingerprint = font_fingerprint()
        self.registry = build_default_registry()
        self.feedback_store = JsonlRecordStore(self.paths.root / "feedback" / "records.jsonl")
        self.proposal_store = JsonlRecordStore(self.paths.root / "proposals" / "events.jsonl")
        self.proposals = ParameterProposalService(self.proposal_store, self._audit_proposal)
        self.active_sampler = ActiveSampler()
        supervisor = RunSupervisor(
            DagScheduler(self.registry),
            audit_log=self.audit_log,
        )
        self.service = EvaluationService(supervisor)
        self._lock = threading.RLock()

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        profile = profile or default_profile(case.scene)
        profile = replace(
            profile,
            metadata={
                **dict(profile.metadata),
                "git_sha": self._git_sha,
                "font_fingerprint": self._font_fingerprint,
                "renderer_versions": {
                    **dict(profile.metadata.get("renderer_versions", {})),
                    "pptx_object_tree": "1.0",
                },
            },
        )
        outcome = self.service.evaluate(case, profile, run_id=run_id)
        payload = normalized_report_payload(outcome)
        with self._lock:
            self.repository.save(payload)
        return payload

    def get(self, run_id: str) -> dict[str, Any]:
        return self.repository.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        return self.repository.list()

    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload["run_id"])
        self.repository.get(run_id)
        record = self.repository.add_review(payload)
        self.audit_log.append(
            run_id=run_id,
            event_type="HUMAN_REVIEW_RECORDED",
            actor=str(payload.get("reviewer_id") or "reviewer"),
            payload=record,
        )
        return record

    def export(self, run_id: str, output_dir: str | Path) -> tuple[Path, Path]:
        return export_run_report(self.get(run_id), output_dir)

    def add_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.repository.get(str(payload["run_id"]))
        record = feedback_from_mapping(payload)
        self.feedback_store.append(record)
        self.audit_log.append(
            run_id=record.run_id,
            event_type="FEEDBACK_RECORDED",
            actor=str(payload.get("actor") or "feedback-pipeline"),
            payload=to_primitive(record),
        )
        return to_primitive(record)

    def _audit_proposal(self, proposal: Any) -> None:
        self.audit_log.append(
            run_id=proposal.proposal_id,
            event_type=f"PARAMETER_PROPOSAL_{proposal.status.value}",
            actor="parameter-governance",
            payload=to_primitive(proposal),
        )


_runtime: LocalEvaluationRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> LocalEvaluationRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            root = os.getenv("PPT_EVAL_DATA_DIR", "var")
            _runtime = LocalEvaluationRuntime(root)
        return _runtime
