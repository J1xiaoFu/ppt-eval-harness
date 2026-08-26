"""Deterministic top-level Harness state machine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from ppt_eval.domain import (
    CoverageStatus,
    EvalCase,
    EvalProfile,
    EvalReport,
    EvaluationDecision,
    ExecutionStatus,
    RunManifest,
    ScoreBreakdown,
    SupervisorState,
)
from ppt_eval.scoring import DecisionPolicy, PptPdmsAggregator

from .audit import AuditLog
from .oracle import EvaluationContext
from .profile import ProfileCompiler
from .scheduler import DagScheduler, SchedulerOutcome


@dataclass(frozen=True, slots=True)
class SupervisionOutcome:
    report: EvalReport
    manifest: RunManifest
    score: ScoreBreakdown | None


class RunSupervisor:
    """Runs OBSERVE -> PLAN -> ACT -> VERIFY -> FINALIZE/REVIEW.

    All routing is determined by the profile and this state table.  Oracle
    prose, model output, and metric values cannot add commands to the DAG.
    """

    _ALLOWED_TRANSITIONS: Mapping[SupervisorState, frozenset[SupervisorState]] = {
        SupervisorState.OBSERVE: frozenset(
            (SupervisorState.PLAN, SupervisorState.REVIEW)
        ),
        SupervisorState.PLAN: frozenset((SupervisorState.ACT, SupervisorState.REVIEW)),
        SupervisorState.ACT: frozenset(
            (SupervisorState.VERIFY, SupervisorState.REVIEW)
        ),
        SupervisorState.VERIFY: frozenset(
            (SupervisorState.FINALIZE, SupervisorState.REVIEW)
        ),
        SupervisorState.FINALIZE: frozenset(),
        SupervisorState.REVIEW: frozenset(),
    }

    def __init__(
        self,
        scheduler: DagScheduler,
        *,
        compiler: ProfileCompiler | None = None,
        aggregator: PptPdmsAggregator | None = None,
        decision_policy: DecisionPolicy | None = None,
        audit_log: AuditLog | None = None,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.compiler = compiler or ProfileCompiler()
        self.aggregator = aggregator or PptPdmsAggregator()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.audit_log = audit_log
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid4()}")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        case: EvalCase,
        profile: EvalProfile,
        *,
        artifacts: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> SupervisionOutcome:
        run_id = run_id or self._id_factory("run")
        report_id = self._id_factory("report")
        started_at = self._now()
        input_hash = self._input_hash(case)
        profile_fingerprint = self._hash(profile)
        history = [SupervisorState.OBSERVE]
        self._audit(run_id, "STATE_TRANSITION", {"state": "OBSERVE"}, started_at)
        scheduler_outcome: SchedulerOutcome | None = None

        try:
            if case.scene != profile.scene:
                raise ValueError(
                    f"case scene {case.scene.value!r} does not match profile scene "
                    f"{profile.scene.value!r}"
                )
            self._transition(history, SupervisorState.PLAN, run_id)
            dag = self.compiler.compile(profile)
            self._audit(
                run_id,
                "PLAN_COMPILED",
                {"nodes": [node.node_id for node in dag.nodes]},
                self._now(),
            )

            self._transition(history, SupervisorState.ACT, run_id)
            context = EvaluationContext(
                case=case,
                profile=profile,
                artifacts=artifacts or {},
                memo={},
            )
            scheduler_outcome = self.scheduler.execute(dag, context, profile)

            self._transition(history, SupervisorState.VERIFY, run_id)
            breakdown = self.aggregator.aggregate(
                profile, scheduler_outcome.results
            )
            decision, review_reasons = self.decision_policy.decide(
                profile, breakdown, scheduler_outcome.results
            )
            final_state = (
                SupervisorState.REVIEW
                if decision in (EvaluationDecision.REVIEW, EvaluationDecision.ERROR)
                else SupervisorState.FINALIZE
            )
            self._transition(history, final_state, run_id)
            completed_at = self._now()

            scene_score = (
                None
                if breakdown.scene_additive is None
                else round(
                    100.0
                    * breakdown.scene_multiplier
                    * breakdown.scene_additive,
                    6,
                )
            )
            errors = self._result_errors(scheduler_outcome)
            report = EvalReport(
                report_id=report_id,
                run_id=run_id,
                case_id=case.case_id,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                scene=case.scene,
                coverage=breakdown.coverage,
                decision=decision,
                base_score=breakdown.base_score,
                scene_score=scene_score,
                full_score=breakdown.full_score,
                overall_score=(
                    breakdown.full_score
                    if breakdown.full_score is not None
                    else breakdown.base_score
                ),
                results=scheduler_outcome.results,
                review_reasons=review_reasons,
                errors=errors,
                created_at=completed_at,
            )
            manifest = RunManifest(
                run_id=run_id,
                case_id=case.case_id,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                state=history[-1],
                state_history=tuple(history),
                coverage=breakdown.coverage,
                input_hash=input_hash,
                profile_fingerprint=profile_fingerprint,
                result_hash=self._hash(report),
                git_sha=self._optional_text(profile.metadata.get("git_sha")),
                container_image=self._optional_text(
                    profile.metadata.get("container_image")
                ),
                font_fingerprint=self._optional_text(
                    profile.metadata.get("font_fingerprint")
                ),
                oracle_versions=scheduler_outcome.oracle_versions,
                model_versions=self._string_mapping(
                    profile.metadata.get("model_versions")
                ),
                prompt_versions=self._string_mapping(
                    profile.metadata.get("prompt_versions")
                ),
                renderer_versions=self._string_mapping(
                    profile.metadata.get("renderer_versions")
                ),
                artifact_hashes=self._string_mapping(
                    case.metadata.get("artifact_hashes")
                ),
                random_seed=int(profile.metadata.get("random_seed", 0)),
                cost=scheduler_outcome.total_cost,
                duration_ms=self._duration_ms(started_at, completed_at),
                started_at=started_at,
                completed_at=completed_at,
            )
            self._audit(
                run_id,
                "RUN_COMPLETED",
                {
                    "decision": decision.value,
                    "coverage": breakdown.coverage.value,
                    "result_hash": manifest.result_hash,
                },
                completed_at,
            )
            return SupervisionOutcome(report, manifest, breakdown)
        except Exception as exc:
            return self._failed_outcome(
                case=case,
                profile=profile,
                run_id=run_id,
                report_id=report_id,
                started_at=started_at,
                input_hash=input_hash,
                profile_fingerprint=profile_fingerprint,
                history=history,
                scheduler_outcome=scheduler_outcome,
                exc=exc,
            )

    def _failed_outcome(
        self,
        *,
        case: EvalCase,
        profile: EvalProfile,
        run_id: str,
        report_id: str,
        started_at: str,
        input_hash: str,
        profile_fingerprint: str,
        history: list[SupervisorState],
        scheduler_outcome: SchedulerOutcome | None,
        exc: Exception,
    ) -> SupervisionOutcome:
        if history[-1] not in (SupervisorState.REVIEW, SupervisorState.FINALIZE):
            self._transition(history, SupervisorState.REVIEW, run_id)
        completed_at = self._now()
        message = f"{type(exc).__name__}: {exc}"
        results = scheduler_outcome.results if scheduler_outcome else ()
        report = EvalReport(
            report_id=report_id,
            run_id=run_id,
            case_id=case.case_id,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            scene=case.scene,
            coverage=CoverageStatus.UNASSESSABLE,
            decision=EvaluationDecision.ERROR,
            base_score=None,
            scene_score=None,
            full_score=None,
            overall_score=None,
            results=results,
            review_reasons=("harness_error",),
            errors=(message,),
            created_at=completed_at,
        )
        manifest = RunManifest(
            run_id=run_id,
            case_id=case.case_id,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            state=history[-1],
            state_history=tuple(history),
            coverage=CoverageStatus.UNASSESSABLE,
            input_hash=input_hash,
            profile_fingerprint=profile_fingerprint,
            result_hash=self._hash(report),
            oracle_versions=(
                scheduler_outcome.oracle_versions if scheduler_outcome else {}
            ),
            cost=scheduler_outcome.total_cost if scheduler_outcome else 0.0,
            duration_ms=self._duration_ms(started_at, completed_at),
            started_at=started_at,
            completed_at=completed_at,
            error=message,
        )
        self._audit(
            run_id,
            "RUN_FAILED",
            {"error": message, "result_hash": manifest.result_hash},
            completed_at,
        )
        return SupervisionOutcome(report, manifest, None)

    def _transition(
        self,
        history: list[SupervisorState],
        target: SupervisorState,
        run_id: str,
    ) -> None:
        current = history[-1]
        if target not in self._ALLOWED_TRANSITIONS[current]:
            raise RuntimeError(
                f"invalid supervisor transition {current.value} -> {target.value}"
            )
        history.append(target)
        self._audit(
            run_id, "STATE_TRANSITION", {"state": target.value}, self._now()
        )

    def _audit(
        self, run_id: str, event_type: str, payload: Mapping[str, Any], at: str
    ) -> None:
        if self.audit_log is not None:
            self.audit_log.append(
                run_id=run_id,
                event_type=event_type,
                actor="RunSupervisor",
                payload=payload,
                occurred_at=at,
            )

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    @staticmethod
    def _duration_ms(started_at: str, completed_at: str) -> int:
        elapsed = datetime.fromisoformat(completed_at) - datetime.fromisoformat(
            started_at
        )
        return max(0, round(elapsed.total_seconds() * 1000))

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _string_mapping(value: Any) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            return {}
        return {str(key): str(item) for key, item in value.items()}

    @staticmethod
    def _result_errors(outcome: SchedulerOutcome) -> tuple[str, ...]:
        return tuple(
            f"{item.oracle_id}:{item.error_code or 'ERROR'}:{item.error_message or ''}"
            for item in outcome.results
            if item.execution_status == ExecutionStatus.ERROR
        )

    @classmethod
    def _hash(cls, value: Any) -> str:
        payload = cls._jsonable(value)
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _input_hash(cls, case: EvalCase) -> str:
        digest = hashlib.sha256(
            json.dumps(
                cls._jsonable(case), ensure_ascii=True, sort_keys=True
            ).encode("utf-8")
        )
        path = Path(case.pptx_path)
        if path.is_file():
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                digest.update(
                    f"UNREADABLE:{type(exc).__name__}:{exc.errno}".encode("ascii")
                )
        return digest.hexdigest()

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return cls._jsonable(asdict(value))
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._jsonable(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)


class EvaluationService:
    """Thin use-case facade for API, CLI and worker delivery adapters."""

    def __init__(self, supervisor: RunSupervisor) -> None:
        self.supervisor = supervisor

    def evaluate(self, case: EvalCase, profile: EvalProfile, **kwargs) -> SupervisionOutcome:
        return self.supervisor.run(case, profile, **kwargs)
