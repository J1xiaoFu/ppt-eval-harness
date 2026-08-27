"""Oracle ports and composites used by the agentic harness.

The supervisor only calls this interface.  OCR, LLM, VLM, rendering and rule
implementations remain replaceable adapters behind individual Oracles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Iterable,
    Mapping,
    MutableMapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from ppt_eval.domain import (
    AtomicObservation,
    EvalCase,
    EvalProfile,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    OracleResult,
    ScoreRole,
)


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    score_role: ScoreRole = ScoreRole.DIAGNOSTIC
    description: str = ""


@dataclass(frozen=True, slots=True)
class OracleDescriptor:
    oracle_id: str
    name: str
    version: str
    metrics: tuple[MetricDefinition, ...] = ()
    deterministic: bool = True
    description: str = ""


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    case: EvalCase
    profile: EvalProfile
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    memo: MutableMapping[str, Any] = field(default_factory=dict)


# Compatibility name used by worker/adaptor code.  Both names are the same
# contract rather than parallel context types.
ExecutionContext = EvaluationContext


@dataclass(frozen=True, slots=True)
class OracleExecutionOutput:
    """Normalized split between score results and atomic observations."""

    results: tuple[OracleResult, ...] = ()
    observations: tuple[AtomicObservation, ...] = ()


OracleOutput = (
    OracleResult
    | ObservationBatch
    | OracleExecutionOutput
    | Sequence[OracleResult | AtomicObservation | ObservationBatch]
)


@runtime_checkable
class Oracle(Protocol):
    """Stable leaf/composite contract; implementations own all metric logic."""

    def describe(self) -> OracleDescriptor | Mapping[str, Any]:
        ...

    def supports(self, context: EvaluationContext) -> bool:
        ...

    def evaluate(self, context: EvaluationContext) -> OracleOutput:
        ...


def normalize_oracle_output(output: OracleOutput) -> tuple[OracleResult, ...]:
    """Legacy result-only normalization used by historical model routing."""

    normalized = normalize_oracle_execution_output(output)
    if normalized.observations:
        raise TypeError("Oracle output contains atomic observations where results are required")
    return normalized.results


def normalize_oracle_execution_output(output: OracleOutput) -> OracleExecutionOutput:
    """Normalize legacy and v8 Oracle outputs without making observations scoreable."""

    if isinstance(output, OracleExecutionOutput):
        return output
    if isinstance(output, OracleResult):
        return OracleExecutionOutput(results=(output,))
    if isinstance(output, ObservationBatch):
        return OracleExecutionOutput(observations=output.observations)
    if isinstance(output, (str, bytes)):
        raise TypeError("Oracle output must be a result or observation, not text")
    results: list[OracleResult] = []
    observations: list[AtomicObservation] = []
    for item in tuple(output):
        if isinstance(item, OracleResult):
            results.append(item)
        elif isinstance(item, AtomicObservation):
            observations.append(item)
        elif isinstance(item, ObservationBatch):
            observations.extend(item.observations)
        else:
            raise TypeError("Oracle output contains an unsupported item")
    return OracleExecutionOutput(tuple(results), tuple(observations))


def coerce_descriptor(
    value: OracleDescriptor | Mapping[str, Any],
) -> OracleDescriptor:
    """Accept the frozen descriptor and the legacy mapping during migration."""

    if isinstance(value, OracleDescriptor):
        return value
    oracle_id = str(value.get("oracle_id", ""))
    version = str(value.get("version", "1.0"))
    raw_role = value.get("score_role", ScoreRole.DIAGNOSTIC)
    try:
        role = raw_role if isinstance(raw_role, ScoreRole) else ScoreRole(raw_role)
    except ValueError:
        role = ScoreRole.DIAGNOSTIC
    metric_ids = tuple(value.get("metric_ids", ()))
    return OracleDescriptor(
        oracle_id=oracle_id,
        name=str(value.get("name", oracle_id)),
        version=version,
        metrics=tuple(MetricDefinition(str(metric_id), role) for metric_id in metric_ids),
        deterministic=bool(value.get("deterministic", True)),
        description=str(value.get("description", "")),
    )


class OracleRegistry:
    """Explicit registry/factory boundary; registration order has no semantics."""

    def __init__(self, oracles: Iterable[Oracle] = ()) -> None:
        self._oracles: dict[str, Oracle] = {}
        for oracle in oracles:
            self.register(oracle)

    def register(self, oracle: Oracle, *, replace: bool = False) -> None:
        descriptor = coerce_descriptor(oracle.describe())
        oracle_id = descriptor.oracle_id
        if not oracle_id:
            raise ValueError("Oracle descriptor must contain oracle_id")
        if oracle_id in self._oracles and not replace:
            raise ValueError(f"Oracle {oracle_id!r} is already registered")
        self._oracles[oracle_id] = oracle

    def get(self, oracle_id: str) -> Oracle:
        try:
            return self._oracles[oracle_id]
        except KeyError as exc:
            raise KeyError(f"Oracle {oracle_id!r} is not registered") from exc

    def contains(self, oracle_id: str) -> bool:
        return oracle_id in self._oracles

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._oracles))


class CompositeOracle:
    """Composite pattern implementation with deterministic child order."""

    def __init__(
        self,
        oracle_id: str,
        children: Iterable[Oracle],
        *,
        name: str | None = None,
        version: str = "1.0",
        description: str = "",
    ) -> None:
        self._oracle_id = oracle_id
        self._children = tuple(children)
        self._name = name or oracle_id
        self._version = version
        self._description = description

    @property
    def children(self) -> tuple[Oracle, ...]:
        return self._children

    def describe(self) -> OracleDescriptor:
        metrics: list[MetricDefinition] = []
        for child in self._children:
            metrics.extend(coerce_descriptor(child.describe()).metrics)
        return OracleDescriptor(
            oracle_id=self._oracle_id,
            name=self._name,
            version=self._version,
            metrics=tuple(metrics),
            deterministic=all(
                coerce_descriptor(child.describe()).deterministic
                for child in self._children
            ),
            description=self._description,
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(self._children) and any(
            child.supports(context) for child in self._children
        )

    def evaluate(self, context: EvaluationContext) -> OracleOutput:
        results: list[OracleResult] = []
        observations: list[AtomicObservation] = []
        for child in self._children:
            descriptor = coerce_descriptor(child.describe())
            if not child.supports(context):
                results.extend(self._unsupported_results(descriptor))
                continue
            try:
                normalized = normalize_oracle_execution_output(child.evaluate(context))
                results.extend(normalized.results)
                observations.extend(normalized.observations)
            except Exception as exc:  # isolation is a Composite responsibility
                results.extend(self._exception_results(descriptor, exc))
        if observations:
            return OracleExecutionOutput(tuple(results), tuple(observations))
        return tuple(results)

    @staticmethod
    def _unsupported_results(descriptor: OracleDescriptor) -> tuple[OracleResult, ...]:
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
                error_code="UNSUPPORTED",
                error_message="Oracle does not support this evaluation context",
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


class BaselinePptQualityOracle(CompositeOracle):
    """Mandatory baseline composite identified by a stable Harness ID."""

    ORACLE_ID = "baseline_ppt_quality"

    def __init__(self, children: Iterable[Oracle], *, version: str = "1.0") -> None:
        super().__init__(
            self.ORACLE_ID,
            children,
            name="Baseline PPT Quality",
            version=version,
            description="Always-on PPT content, structure, visual and technical quality",
        )
