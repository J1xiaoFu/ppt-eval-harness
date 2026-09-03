"""Deterministic DAG command scheduler with bounded retries and isolation."""

from __future__ import annotations

import copy
import queue
import threading
from dataclasses import dataclass, replace
from typing import Mapping, cast

from ppt_eval.domain import (
    AtomicObservation,
    DagNode,
    EvalProfile,
    EvaluationDag,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    ScoreRole,
)

from .model_request_budget import ModelRequestBudgetLedger
from .oracle import (
    EvaluationContext,
    MetricDefinition,
    Oracle,
    OracleDescriptor,
    OracleExecutionOutput,
    OracleOutput,
    OracleRegistry,
    coerce_descriptor,
    normalize_oracle_execution_output,
)


@dataclass(frozen=True, slots=True)
class SchedulerOutcome:
    results: tuple[OracleResult, ...]
    observations: tuple[AtomicObservation, ...]
    attempts: Mapping[str, int]
    oracle_versions: Mapping[str, str]
    total_cost: float


class DagScheduler:
    """Executes the precompiled plan without model-driven routing."""

    def __init__(self, registry: OracleRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        dag: EvaluationDag,
        context: EvaluationContext,
        profile: EvalProfile,
    ) -> SchedulerOutcome:
        if profile.version == "8.4":
            visual_policy = profile.metadata.get("visual_audit")
            maximum_requests = (
                visual_policy.get("maximum_model_requests", 64)
                if isinstance(visual_policy, Mapping)
                else 64
            )
            if (
                isinstance(maximum_requests, bool)
                or not isinstance(maximum_requests, int)
                or maximum_requests < 1
            ):
                raise ValueError(
                    "Profile 8.4 maximum_model_requests must be a positive integer"
                )
            existing_ledger = context.memo.get("ppt_eval.model_request_budget")
            if existing_ledger is None:
                context.memo["ppt_eval.model_request_budget"] = (
                    ModelRequestBudgetLedger(maximum_requests)
                )
            elif not isinstance(existing_ledger, ModelRequestBudgetLedger):
                raise TypeError("model request budget memo has an invalid value")
            elif existing_ledger.maximum_requests != maximum_requests:
                raise ValueError("model request budget does not match the Profile cap")
        ordered_nodes = self._topological_order(dag)
        results: list[OracleResult] = []
        observations: list[AtomicObservation] = []
        attempts: dict[str, int] = {}
        versions: dict[str, str] = {}
        total_cost = 0.0

        for node in ordered_nodes:
            context.memo["ppt_eval.cost_spent_before_node"] = total_cost
            context.memo["ppt_eval.cost_budget"] = profile.cost_budget
            node_observations: tuple[AtomicObservation, ...] = ()
            if not self.registry.contains(node.oracle_id):
                attempts[node.node_id] = 0
                results.append(
                    OracleResult.error(
                        oracle_id=node.oracle_id,
                        metric_id=node.oracle_id,
                        error_code="ORACLE_NOT_REGISTERED",
                        error_message=f"Oracle {node.oracle_id!r} is not registered",
                    )
                )
                continue

            oracle = self.registry.get(node.oracle_id)
            descriptor = coerce_descriptor(oracle.describe())
            contract_version = self._profile_oracle_version(
                oracle,
                descriptor.version,
                profile,
            )
            descriptor = replace(descriptor, version=contract_version)
            versions[node.oracle_id] = contract_version
            if (
                not node.mandatory
                and not descriptor.deterministic
                and profile.cost_budget is not None
                and total_cost >= profile.cost_budget
            ):
                # Budget exhaustion stops nodes that may spend more, but must
                # not suppress deterministic SELECT/FUSE/REDUCE work.  Those
                # nodes turn the evidence already acquired into an auditable
                # coverage certificate and final score/review decision.
                node_results = self._cost_budget_exhausted_results(descriptor)
                attempts[node.node_id] = 0
            elif not oracle.supports(context):
                node_results = self._unsupported_results(descriptor)
                attempts[node.node_id] = 0
            else:
                # A retry re-invokes the whole top-level Oracle.  For a
                # non-deterministic composite that can repeat successful API
                # calls merely because a different child returned ERROR.
                # Keep model/network Oracles at-most-once at this layer;
                # deterministic Oracles retain the Profile retry policy.
                max_retries = profile.max_retries if descriptor.deterministic else 0
                node_output, used_attempts, completed_memo = self._execute_with_retries(
                    oracle,
                    descriptor,
                    context,
                    max_retries,
                    profile.oracle_timeout_seconds,
                )
                context.memo.clear()
                context.memo.update(completed_memo)
                node_results = node_output.results
                node_observations = node_output.observations
                attempts[node.node_id] = used_attempts
                observations.extend(node_observations)
                store = context.memo.setdefault("ppt_eval.atomic_observations", [])
                if not isinstance(store, list):
                    raise TypeError("atomic observation memo store must be a list")
                store.extend(node_observations)
            results.extend(node_results)
            result_store = context.memo.setdefault("ppt_eval.oracle_results", [])
            if not isinstance(result_store, list):
                raise TypeError("oracle result memo store must be a list")
            result_store.extend(node_results)
            total_cost += sum(result.cost for result in node_results) + sum(
                observation.cost for observation in node_observations
            )

        return SchedulerOutcome(
            results=tuple(results),
            observations=tuple(observations),
            attempts=attempts,
            oracle_versions=versions,
            total_cost=round(total_cost, 6),
        )

    @staticmethod
    def _profile_oracle_version(
        oracle: Oracle,
        default: str,
        profile: EvalProfile,
    ) -> str:
        """Resolve a replay contract version without mutating a shared Oracle."""

        resolver = getattr(oracle, "version_for_profile", None)
        if resolver is None or not callable(resolver):
            return default
        value = resolver(profile)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Oracle profile contract version must not be blank")
        return value.strip()

    @staticmethod
    def _execute_with_retries(
        oracle: Oracle,
        descriptor: OracleDescriptor,
        context: EvaluationContext,
        max_retries: int,
        timeout_seconds: float,
    ) -> tuple[OracleExecutionOutput, int, Mapping[str, object]]:
        last_output = OracleExecutionOutput()
        base_memo = copy.deepcopy(dict(context.memo))
        for attempt in range(1, max_retries + 2):
            attempt_memo = copy.deepcopy(base_memo)
            cancel_event = threading.Event()
            attempt_memo["ppt_eval.cancel_event"] = cancel_event
            attempt_context = EvaluationContext(
                case=context.case,
                profile=context.profile,
                artifacts=context.artifacts,
                memo=attempt_memo,
            )
            try:
                raw_output = DagScheduler._evaluate_with_timeout(
                    oracle,
                    attempt_context,
                    timeout_seconds,
                    cancel_event=cancel_event,
                )
                last_output = normalize_oracle_execution_output(raw_output)
                if not last_output.results and not last_output.observations:
                    raise ValueError("Oracle returned no results or observations")
            except Exception as exc:
                last_output = OracleExecutionOutput(
                    results=DagScheduler._exception_results(descriptor, exc)
                )
            has_error = any(
                result.execution_status == ExecutionStatus.ERROR
                for result in last_output.results
            ) or any(
                observation.execution_status == ExecutionStatus.ERROR
                for observation in last_output.observations
            )
            if not has_error:
                attempt_memo.pop("ppt_eval.cancel_event", None)
                return last_output, attempt, attempt_memo
        return last_output, max_retries + 1, base_memo

    @staticmethod
    def _evaluate_with_timeout(
        oracle: Oracle,
        context: EvaluationContext,
        timeout_seconds: float,
        *,
        cancel_event: threading.Event,
    ) -> OracleOutput:
        completed: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                completed.put((True, oracle.evaluate(context)))
            except Exception as exc:  # propagated on the scheduler thread
                completed.put((False, exc))

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            cancel_event.set()
            raise TimeoutError(
                f"Oracle exceeded configured timeout of {timeout_seconds:g} seconds"
            )
        succeeded, value = completed.get_nowait()
        if not succeeded:
            if not isinstance(value, Exception):
                raise RuntimeError("Oracle worker returned an invalid exception payload")
            raise value
        return cast(OracleOutput, value)

    @staticmethod
    def _unsupported_results(
        descriptor: OracleDescriptor,
    ) -> tuple[OracleResult, ...]:
        metrics = descriptor.metrics or (
            MetricDefinition(descriptor.oracle_id, ScoreRole.DIAGNOSTIC),
        )
        return tuple(
            OracleResult(
                oracle_id=descriptor.oracle_id,
                metric_id=metric.metric_id,
                execution_status=ExecutionStatus.SKIPPED,
                metric_status=MetricStatus.NA,
                score_role=metric.score_role,
                confidence=1.0,
                version=descriptor.version,
            )
            for metric in metrics
        )

    @staticmethod
    def _cost_budget_exhausted_results(
        descriptor: OracleDescriptor,
    ) -> tuple[OracleResult, ...]:
        metrics = descriptor.metrics or (
            MetricDefinition(descriptor.oracle_id, ScoreRole.DIAGNOSTIC),
        )
        return tuple(
            OracleResult.error(
                oracle_id=descriptor.oracle_id,
                metric_id=metric.metric_id,
                score_role=metric.score_role,
                error_code="COST_BUDGET_EXHAUSTED",
                error_message="Evaluation cost budget was exhausted",
                version=descriptor.version,
            )
            for metric in metrics
        )

    @staticmethod
    def _exception_results(
        descriptor: OracleDescriptor, exc: Exception
    ) -> tuple[OracleResult, ...]:
        metrics = descriptor.metrics or (
            MetricDefinition(descriptor.oracle_id, ScoreRole.DIAGNOSTIC),
        )
        return tuple(
            OracleResult.error(
                oracle_id=descriptor.oracle_id,
                metric_id=metric.metric_id,
                score_role=metric.score_role,
                error_code="ORACLE_EXCEPTION",
                error_message=f"{type(exc).__name__}: {exc}",
                version=descriptor.version,
            )
            for metric in metrics
        )

    @staticmethod
    def _topological_order(dag: EvaluationDag) -> tuple[DagNode, ...]:
        remaining = list(dag.nodes)
        complete: set[str] = set()
        ordered: list[DagNode] = []
        while remaining:
            ready = [
                node for node in remaining if set(node.dependencies) <= complete
            ]
            if not ready:
                raise ValueError("evaluation graph contains a dependency cycle")
            # The immutable profile order is the deterministic tie breaker.
            for node in ready:
                ordered.append(node)
                complete.add(node.node_id)
                remaining.remove(node)
        return tuple(ordered)
