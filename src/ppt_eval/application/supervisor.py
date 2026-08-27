"""Deterministic top-level Harness state machine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from ppt_eval.domain import (
    AtomicObservation,
    CoverageStatus,
    EvalCase,
    EvalProfile,
    EvalReport,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    RunManifest,
    SceneType,
    ScoreBreakdown,
    ScoreRole,
    Severity,
    SupervisorState,
)
from ppt_eval.scoring import DecisionPolicy, PptPdmsAggregator
from ppt_eval.training_eligibility import TrainingTrack, assess_training_eligibility

from .audit import AuditLog
from .model_escalation import ModelAuditEscalationPolicy, ModelEscalationOutcome
from .oracle import (
    EvaluationContext,
    MetricDefinition,
    Oracle,
    OracleDescriptor,
    coerce_descriptor,
    normalize_oracle_output,
)
from .profile import ProfileCompiler
from .scheduler import DagScheduler, SchedulerOutcome


@dataclass(frozen=True, slots=True)
class SupervisionOutcome:
    report: EvalReport
    manifest: RunManifest
    score: ScoreBreakdown | None
    observations: tuple[AtomicObservation, ...] = ()


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
    _TIERED_MODEL_AUDIT_ROUTING = "FLASH_ADVANCED_HUMAN"
    _LEGACY_TIERED_MODEL_AUDIT_ROUTING = "FLASH_PLUS_HUMAN"

    def __init__(
        self,
        scheduler: DagScheduler,
        *,
        compiler: ProfileCompiler | None = None,
        aggregator: PptPdmsAggregator | None = None,
        decision_policy: DecisionPolicy | None = None,
        advanced_model_review: Oracle | None = None,
        model_escalation_policy: ModelAuditEscalationPolicy | None = None,
        audit_log: AuditLog | None = None,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.compiler = compiler or ProfileCompiler()
        self.aggregator = aggregator or PptPdmsAggregator()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.advanced_model_review = advanced_model_review
        self.model_escalation_policy = (
            model_escalation_policy or ModelAuditEscalationPolicy()
        )
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
            if self._tiered_model_audit_enabled(profile):
                provisional_decision = decision
                provisional_reasons = review_reasons
                flash_results = self._flash_model_results(scheduler_outcome.results)
                routing = self.model_escalation_policy.decide(
                    provisional_decision=provisional_decision,
                    coverage=breakdown.coverage,
                    provisional_reasons=provisional_reasons,
                    flash_results=flash_results,
                )
                self._audit_model_routing(
                    run_id,
                    routing_policy=str(profile.metadata.get("model_audit_routing")),
                    stage="FLASH",
                    provisional_decision=provisional_decision,
                    provisional_reasons=provisional_reasons,
                    outcome=routing,
                )
                if routing.should_call_advanced:
                    scheduler_outcome, advanced_results, call_status = (
                        self._execute_advanced_model_review(
                            context,
                            profile,
                            scheduler_outcome,
                        )
                    )
                    routing = self.model_escalation_policy.decide(
                        provisional_decision=provisional_decision,
                        coverage=breakdown.coverage,
                        provisional_reasons=provisional_reasons,
                        flash_results=flash_results,
                        advanced_results=advanced_results,
                    )
                    self._audit_model_routing(
                        run_id,
                        routing_policy=str(
                            profile.metadata.get("model_audit_routing")
                        ),
                        stage=(
                            "PLUS"
                            if profile.metadata.get("model_audit_routing")
                            == self._LEGACY_TIERED_MODEL_AUDIT_ROUTING
                            else "ADVANCED"
                        ),
                        provisional_decision=provisional_decision,
                        provisional_reasons=provisional_reasons,
                        outcome=routing,
                        advanced_call_status=call_status,
                    )
                decision = routing.final_recommendation
                review_reasons = tuple(
                    dict.fromkeys(
                        provisional_reasons
                        + routing.escalation_reasons
                        + routing.human_review_reasons
                    )
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
            training_eligibility = self._training_eligibility(
                case,
                profile,
                breakdown,
                scheduler_outcome,
            )
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
                training_eligibility=training_eligibility,
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
                model_versions=self._merge_declared_and_actual_versions(
                    profile.metadata.get("model_versions"),
                    self._result_model_versions(scheduler_outcome),
                ),
                prompt_versions=self._merge_declared_and_actual_versions(
                    profile.metadata.get("prompt_versions"),
                    self._result_prompt_versions(scheduler_outcome),
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
            return SupervisionOutcome(
                report,
                manifest,
                breakdown,
                scheduler_outcome.observations,
            )
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
            model_versions=self._merge_declared_and_actual_versions(
                profile.metadata.get("model_versions"),
                self._result_model_versions(scheduler_outcome),
            ),
            prompt_versions=self._merge_declared_and_actual_versions(
                profile.metadata.get("prompt_versions"),
                self._result_prompt_versions(scheduler_outcome),
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
        return SupervisionOutcome(
            report,
            manifest,
            None,
            scheduler_outcome.observations if scheduler_outcome else (),
        )

    @classmethod
    def _tiered_model_audit_enabled(cls, profile: EvalProfile) -> bool:
        """Require an explicit Profile opt-in before model-driven escalation."""

        return profile.metadata.get("model_audit_routing") in {
            cls._TIERED_MODEL_AUDIT_ROUTING,
            cls._LEGACY_TIERED_MODEL_AUDIT_ROUTING,
        }

    @staticmethod
    def _flash_model_results(
        results: tuple[OracleResult, ...],
    ) -> tuple[OracleResult, ...]:
        """Select only score-bearing FLASH audit results for policy voting.

        Deterministic metrics must never be mistaken for model votes.  PLUS
        results are diagnostic by contract and are therefore excluded as
        well, even if a caller reuses a previously enriched result tuple.
        """

        return tuple(
            result
            for result in results
            if result.score_role != ScoreRole.DIAGNOSTIC
            and result.metadata.get("audit_type") == "model"
        )

    def _execute_advanced_model_review(
        self,
        context: EvaluationContext,
        profile: EvalProfile,
        scheduler_outcome: SchedulerOutcome,
    ) -> tuple[SchedulerOutcome, tuple[OracleResult, ...], str]:
        """Run the PLUS composite once and append its diagnostic telemetry.

        The baseline ``ScoreBreakdown`` has already been frozen by the caller.
        These results are appended for evidence, routing, cost and provenance;
        they are deliberately not sent through the score aggregator again.
        """

        oracle = self.advanced_model_review
        if oracle is None:
            return scheduler_outcome, (), "UNCONFIGURED"

        descriptor = coerce_descriptor(oracle.describe())
        attempt_key = f"escalation:{descriptor.oracle_id}"
        attempts = 0
        if (
            profile.cost_budget is not None
            and scheduler_outcome.total_cost >= profile.cost_budget
        ):
            results = self._advanced_error_results(
                descriptor,
                error_code="COST_BUDGET_EXHAUSTED",
                error_message="Evaluation cost budget was exhausted before PLUS review",
            )
            call_status = "COST_BUDGET_EXHAUSTED"
        else:
            attempts = 1
            try:
                if not oracle.supports(context):
                    results = self._advanced_unavailable_results(descriptor)
                    call_status = "UNSUPPORTED"
                else:
                    results = normalize_oracle_output(oracle.evaluate(context))
                    if not results:
                        raise ValueError("Advanced model review returned no results")
                    call_status = (
                        "COMPLETED_WITH_ERRORS"
                        if any(
                            result.execution_status == ExecutionStatus.ERROR
                            for result in results
                        )
                        else "COMPLETED"
                    )
            except Exception as exc:
                results = self._advanced_error_results(
                    descriptor,
                    error_code="ADVANCED_REVIEW_EXCEPTION",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                call_status = "ERROR"

        enriched = SchedulerOutcome(
            results=scheduler_outcome.results + results,
            observations=scheduler_outcome.observations,
            attempts={
                **dict(scheduler_outcome.attempts),
                attempt_key: attempts,
            },
            oracle_versions={
                **dict(scheduler_outcome.oracle_versions),
                descriptor.oracle_id: descriptor.version,
            },
            total_cost=round(
                scheduler_outcome.total_cost
                + sum(result.cost for result in results),
                6,
            ),
        )
        return enriched, results, call_status

    @staticmethod
    def _descriptor_metrics(
        descriptor: OracleDescriptor,
    ) -> tuple[MetricDefinition, ...]:
        return descriptor.metrics or (
            MetricDefinition(descriptor.oracle_id, ScoreRole.DIAGNOSTIC),
        )

    @classmethod
    def _advanced_error_results(
        cls,
        descriptor: OracleDescriptor,
        *,
        error_code: str,
        error_message: str,
    ) -> tuple[OracleResult, ...]:
        return tuple(
            OracleResult.error(
                oracle_id=descriptor.oracle_id,
                metric_id=metric.metric_id,
                score_role=metric.score_role,
                error_code=error_code,
                error_message=error_message,
                version=descriptor.version,
            )
            for metric in cls._descriptor_metrics(descriptor)
        )

    @classmethod
    def _advanced_unavailable_results(
        cls,
        descriptor: OracleDescriptor,
    ) -> tuple[OracleResult, ...]:
        return tuple(
            OracleResult(
                oracle_id=descriptor.oracle_id,
                metric_id=metric.metric_id,
                execution_status=ExecutionStatus.SKIPPED,
                metric_status=MetricStatus.NA,
                score_role=metric.score_role,
                confidence=1.0,
                version=descriptor.version,
                error_code="ADVANCED_REVIEW_UNSUPPORTED",
                error_message="Advanced model review does not support this evaluation context",
                metadata={"reason_code": "ADVANCED_REVIEW_UNSUPPORTED"},
            )
            for metric in cls._descriptor_metrics(descriptor)
        )

    def _audit_model_routing(
        self,
        run_id: str,
        *,
        routing_policy: str,
        stage: str,
        provisional_decision: EvaluationDecision,
        provisional_reasons: tuple[str, ...],
        outcome: ModelEscalationOutcome,
        advanced_call_status: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "routing_policy": routing_policy,
            "stage": stage,
            "route": outcome.route.value,
            "should_call_advanced": outcome.should_call_advanced,
            "provisional_decision": provisional_decision.value,
            "provisional_reasons": list(provisional_reasons),
            "final_recommendation": outcome.final_recommendation.value,
            "escalation_reasons": list(outcome.escalation_reasons),
            "human_review_reasons": list(outcome.human_review_reasons),
            "flash_recommendation": (
                None
                if outcome.flash_recommendation is None
                else outcome.flash_recommendation.value
            ),
            "plus_recommendation": (
                None
                if outcome.plus_recommendation is None
                else outcome.plus_recommendation.value
            ),
        }
        if advanced_call_status is not None:
            payload["advanced_call_status"] = advanced_call_status
        self._audit(run_id, "MODEL_AUDIT_ROUTING", payload, self._now())

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

    @classmethod
    def _merge_declared_and_actual_versions(
        cls,
        declared: Any,
        actual: Mapping[str, str],
    ) -> Mapping[str, str]:
        """Merge version metadata with observed provider output taking priority.

        Profiles may declare the intended deployment, but only a validated
        provider response proves what actually ran.  A metric-id collision is
        therefore resolved deterministically in favor of the observed value.
        """

        return {**cls._string_mapping(declared), **dict(actual)}

    @classmethod
    def _result_model_versions(
        cls,
        outcome: SchedulerOutcome | None,
    ) -> Mapping[str, str]:
        versions: dict[str, str] = {}
        for result in outcome.results if outcome else ():
            if result.metadata.get("response_schema_version") is not None:
                cls._record_model_version(
                    versions,
                    result.metric_id,
                    result.metadata.get("model"),
                )
            attempts = result.metadata.get("routing_attempts")
            if isinstance(attempts, Sequence) and not isinstance(
                attempts, (str, bytes)
            ):
                for index, attempt in enumerate(attempts, start=1):
                    if not isinstance(attempt, Mapping):
                        continue
                    tier = str(attempt.get("tier") or index).strip().lower()
                    cls._record_model_version(
                        versions,
                        f"{result.metric_id}#{tier}",
                        attempt.get("model"),
                    )
        return versions

    @staticmethod
    def _record_model_version(
        versions: dict[str, str],
        key: str,
        model: object,
    ) -> None:
        if not isinstance(model, Mapping):
            return
        provider = str(model.get("provider") or "").strip()
        model_id = str(model.get("model_id") or "").strip()
        version = str(model.get("version") or "").strip()
        if provider and model_id and version:
            versions[key] = f"{provider}/{model_id}@{version}"

    @staticmethod
    def _result_prompt_versions(
        outcome: SchedulerOutcome | None,
    ) -> Mapping[str, str]:
        versions: dict[str, str] = {}
        for result in outcome.results if outcome else ():
            if result.metadata.get("response_schema_version") is None:
                continue
            prompt = result.metadata.get("prompt")
            if not isinstance(prompt, Mapping):
                continue
            prompt_id = str(prompt.get("prompt_id") or "").strip()
            version = str(prompt.get("version") or "").strip()
            fingerprint = str(prompt.get("sha256") or "").strip()
            if prompt_id and version and fingerprint:
                versions[result.metric_id] = (
                    f"{prompt_id}@{version}#{fingerprint}"
                )
        return versions

    @staticmethod
    def _result_errors(outcome: SchedulerOutcome) -> tuple[str, ...]:
        return tuple(
            f"{item.oracle_id}:{item.error_code or 'ERROR'}:{item.error_message or ''}"
            for item in outcome.results
            if item.execution_status == ExecutionStatus.ERROR
        )

    @staticmethod
    def _training_eligibility(
        case: EvalCase,
        profile: EvalProfile,
        breakdown: ScoreBreakdown,
        outcome: SchedulerOutcome,
    ) -> Mapping[str, Any]:
        if not str(profile.version).startswith("8"):
            return {}
        scores = {
            item.metric_id: item.normalized_score
            for item in outcome.results
            if item.metric_status == MetricStatus.SCORED
            and item.normalized_score is not None
        }

        def average(metric_ids: tuple[str, ...]) -> float | None:
            values = [scores[metric_id] for metric_id in metric_ids if metric_id in scores]
            return 100.0 * sum(values) / len(values) if values else None

        visual_score = average(
            (
                "composition_craft",
                "palette_craft",
                "visual_communication",
                "visual_system_sequence",
                "authorship_specificity",
            )
        )
        layout_score = average(
            ("content_structure", "composition_craft", "typography_craft")
        )
        content_metric_ids = tuple(
            metric_id
            for metric_id in (
                "content_structure",
                "instruction",
                "audience",
                "fact_claim",
                "source_claim",
                "key_point",
                "numeric",
                "compression_richness",
                "traceability",
                "asset_coverage",
                "chart_fidelity",
            )
            if metric_id in scores
        )
        content_score = average(content_metric_ids)
        full_score = breakdown.full_score
        critical_codes = tuple(
            observation.metric_id
            for observation in outcome.observations
            if observation.severity == Severity.CRITICAL
            and (observation.critical or observation.key_unit)
        )
        raster_only = any(
            observation.metric_id == "slide_editability"
            and observation.metadata.get("raster_only") is True
            for observation in outcome.observations
        )
        content_evidence = (
            bool(case.request)
            if case.scene == SceneType.TEXT_TO_PPT
            else bool(case.source_materials)
            if case.scene == SceneType.PROJECT_SUMMARY
            else bool(case.request or case.assets or case.metadata.get("chart_expectations"))
            if case.scene == SceneType.MULTIMODAL
            else False
        )
        eligibility = assess_training_eligibility(
            {
                TrainingTrack.VISUAL: visual_score,
                TrainingTrack.LAYOUT: layout_score,
                TrainingTrack.CONTENT: content_score,
                TrainingTrack.FULL_DECK: full_score,
            },
            critical_issue_codes=critical_codes,
            content_evidence_available=content_evidence,
            raster_only=raster_only,
            train_threshold=profile.pass_threshold,
            review_threshold=profile.review_threshold,
        )
        return eligibility.to_mapping()

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

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile,
        **kwargs: Any,
    ) -> SupervisionOutcome:
        return self.supervisor.run(case, profile, **kwargs)
