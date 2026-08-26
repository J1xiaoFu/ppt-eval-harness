"""Deterministic DAG command scheduler with bounded retries and isolation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ppt_eval.domain import (
    DagNode,
    EvalProfile,
    EvaluationDag,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    ScoreRole,
)

from .oracle import (
    EvaluationContext,
    MetricDefinition,
    OracleDescriptor,
    OracleRegistry,
    coerce_descriptor,
    normalize_oracle_output,
)


@dataclass(frozen=True, slots=True)
class SchedulerOutcome:
    results: tuple[OracleResult, ...]
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
        ordered_nodes = self._topological_order(dag)
        results: list[OracleResult] = []
        attempts: dict[str, int] = {}
        versions: dict[str, str] = {}
        total_cost = 0.0

        for node in ordered_nodes:
            if (
                not node.mandatory
                and profile.cost_budget is not None
                and total_cost >= profile.cost_budget
            ):
                node_results = (
                    OracleResult.error(
                        oracle_id=node.oracle_id,
                        metric_id=node.oracle_id,
                        error_code="COST_BUDGET_EXHAUSTED",
                        error_message="Evaluation cost budget was exhausted",
                    ),
                )
                attempts[node.node_id] = 0
                results.extend(node_results)
                continue

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
            versions[node.oracle_id] = descriptor.version
            if not oracle.supports(context):
                node_results = self._unsupported_results(descriptor)
                attempts[node.node_id] = 0
            else:
                node_results, used_attempts = self._execute_with_retries(
                    oracle, descriptor, context, profile.max_retries
                )
                attempts[node.node_id] = used_attempts
            results.extend(node_results)
            total_cost += sum(result.cost for result in node_results)

        return SchedulerOutcome(
            results=tuple(results),
            attempts=attempts,
            oracle_versions=versions,
            total_cost=round(total_cost, 6),
        )

    @staticmethod
    def _execute_with_retries(oracle, descriptor, context, max_retries):
        last_results: tuple[OracleResult, ...] = ()
        for attempt in range(1, max_retries + 2):
            try:
                last_results = normalize_oracle_output(oracle.evaluate(context))
                if not last_results:
                    raise ValueError("Oracle returned no results")
            except Exception as exc:
                last_results = DagScheduler._exception_results(descriptor, exc)
            if not any(
                result.execution_status == ExecutionStatus.ERROR
                for result in last_results
            ):
                return last_results, attempt
        return last_results, max_retries + 1

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
