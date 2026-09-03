"""Deterministic top-level Harness state machine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
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
    RunManifest,
    SceneType,
    ScoreBreakdown,
    SupervisorState,
)
from ppt_eval.scoring import DecisionPolicy, PptPdmsAggregator
from ppt_eval.training_eligibility import TrainingTrack, assess_training_eligibility

from .audit import AuditLog
from .oracle import (
    EvaluationContext,
)
from .profile import ProfileCompiler
from .scheduler import DagScheduler, SchedulerOutcome


@dataclass(frozen=True, slots=True)
class SupervisionOutcome:
    report: EvalReport
    manifest: RunManifest
    score: ScoreBreakdown | None
    observations: tuple[AtomicObservation, ...] = ()
    visual_contracts: Mapping[str, Any] = field(default_factory=dict)


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
        visual_contracts: Mapping[str, Any] = {}

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
            raw_visual_contracts = context.memo.get("ppt_eval.visual_contracts", {})
            if isinstance(raw_visual_contracts, Mapping):
                visual_contracts = dict(raw_visual_contracts)

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
                visual_audit_summary=self._visual_audit_summary(visual_contracts),
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
                visual_contracts,
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
                visual_contracts=visual_contracts,
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
        visual_contracts: Mapping[str, Any],
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
            visual_audit_summary=self._visual_audit_summary(visual_contracts),
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
            visual_contracts,
        )

    @staticmethod
    def _visual_audit_summary(
        contracts: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        certificate = contracts.get("visual_coverage_certificate")
        scout = contracts.get("atlas_scout")
        plan = contracts.get("visual_selection_plan")
        rounds = contracts.get("visual_audit_rounds", ())
        if certificate is None and scout is None and plan is None:
            return {}

        def value(source: Any, name: str, default: Any) -> Any:
            return getattr(source, name, default)

        criterion_pages = value(certificate, "criterion_pages", {})
        usage = value(certificate, "metadata", {})
        usage = usage.get("usage", {}) if isinstance(usage, Mapping) else {}
        return {
            "total_pages": value(certificate, "total_pages", 0),
            "atlas_covered_pages": len(
                value(certificate, "atlas_covered_page_numbers", ())
            ),
            "atlas_coverage_complete": bool(
                value(certificate, "atlas_coverage_complete", False)
            ),
            "criterion_high_resolution_pages": {
                str(key): list(pages)
                for key, pages in criterion_pages.items()
            }
            if isinstance(criterion_pages, Mapping)
            else {},
            "round_count": len(rounds) if isinstance(rounds, Sequence) else 0,
            "stopping_reason": value(certificate, "stopping_reason", None),
            "coverage_complete": bool(
                value(certificate, "coverage_complete", False)
            ),
            "unresolved_risks": list(
                value(certificate, "unresolved_risk_codes", ())
            ),
            "asset_transport": (
                usage.get("asset_transport")
                if isinstance(usage, Mapping)
                else None
            ),
            "image_tokens": (
                usage.get("image_tokens") if isinstance(usage, Mapping) else None
            ),
            "cached_tokens": (
                usage.get("cached_tokens") if isinstance(usage, Mapping) else None
            ),
            "request_bytes": (
                usage.get("request_bytes") if isinstance(usage, Mapping) else None
            ),
            "request_count": (
                usage.get("request_count") if isinstance(usage, Mapping) else 0
            ),
            "usage_complete": (
                usage.get("usage_complete")
                if isinstance(usage, Mapping)
                else False
            ),
            "cost_known": (
                usage.get("cost_known")
                if isinstance(usage, Mapping)
                else False
            ),
            "reported_cost": (
                usage.get("reported_cost")
                if isinstance(usage, Mapping)
                else None
            ),
            "selection_plan_id": value(plan, "plan_id", None),
            "scout_id": value(scout, "scout_id", None),
        }

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
                "authorship_specificity_v2",
            )
        )
        layout_score = average(
            ("content_structure", "composition_craft", "typography_craft")
        )
        content_metric_ids = tuple(
            metric_id
            for metric_id in (
                "content_structure",
                "language_consistency",
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
        gate_result = next(
            (
                item
                for item in outcome.results
                if item.metric_id == "v8_functional_integrity"
            ),
            None,
        )
        gate_verdicts = (
            gate_result.metadata.get("gate_verdicts", ())
            if gate_result is not None
            else ()
        )
        critical_codes = tuple(
            str(verdict.get("metric_id"))
            for verdict in gate_verdicts
            if isinstance(verdict, Mapping)
            and verdict.get("verdict") == "CONFIRMED"
            and str(
                verdict.get("model_severity")
                or verdict.get("rule_severity")
                or ""
            )
            == "CRITICAL"
        )
        if (
            gate_result is not None
            and gate_result.multiplier == 0.0
            and gate_result.metadata.get("reason_code")
            == "FILE_DELIVERABILITY_FAILED"
        ):
            critical_codes = tuple(
                dict.fromkeys((*critical_codes, "file_deliverability"))
            )
        gate_unresolved = (
            gate_result is not None
            and gate_result.metric_status == MetricStatus.NA
            and gate_result.metadata.get("reason_code") == "GATE_AUDIT_UNRESOLVED"
        )
        if gate_unresolved:
            visual_score = None
            layout_score = None
            content_score = None
            full_score = None
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
    """Thin use-case facade for API and CLI delivery adapters."""

    def __init__(self, supervisor: RunSupervisor) -> None:
        self.supervisor = supervisor

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile,
        **kwargs: Any,
    ) -> SupervisionOutcome:
        return self.supervisor.run(case, profile, **kwargs)
