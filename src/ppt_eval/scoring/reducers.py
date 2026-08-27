"""Deterministic reducers from atomic observations to legacy Oracle results."""

from __future__ import annotations

from math import ceil
from typing import Iterable, Sequence

from ppt_eval.domain import (
    AtomicObservation,
    EvaluationScope,
    Evidence,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    OracleResult,
    ReducerSpec,
    Severity,
)

PAGE_QUALITY = "PAGE_QUALITY"
PAIR_QUALITY = "PAIR_QUALITY"
IMPORTANCE_COVERAGE = "IMPORTANCE_COVERAGE"
SUPPORTED_REDUCER_KINDS = frozenset(
    (PAGE_QUALITY, PAIR_QUALITY, IMPORTANCE_COVERAGE)
)

_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
}


class ReductionError(ValueError):
    """An observation batch violates its reducer contract."""


class ReducerEngine:
    """Reduce a batch while retaining atomic lineage in result metadata."""

    def reduce(self, batch: ObservationBatch, spec: ReducerSpec) -> OracleResult:
        self._validate_contract(batch, spec)
        relevant = tuple(
            item
            for item in batch.observations
            if item.metric_id in spec.input_metric_ids
        )
        applicable = tuple(item for item in relevant if _local_score(item) is not None)
        unavailable = tuple(item for item in relevant if _local_score(item) is None)
        observability = _observability(relevant, applicable)
        metadata = self._metadata(
            batch,
            spec,
            relevant,
            applicable,
            unavailable,
            observability,
        )
        evidence = _collect_evidence(relevant)

        if not applicable or (spec.required and observability < spec.minimum_observability):
            return OracleResult(
                oracle_id=spec.output_oracle_id,
                metric_id=spec.output_metric_id,
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.NA,
                score_role=spec.output_score_role,
                confidence=_confidence(applicable),
                severity=Severity.INFO,
                evidence=evidence,
                version=spec.version,
                duration_ms=batch.duration_ms,
                cost=batch.cost,
                metadata=metadata,
            )

        score, components = self._reduce_score(applicable, spec.reducer_kind)
        critical = tuple(
            item
            for item in applicable
            if item.severity == Severity.CRITICAL and (item.critical or item.key_unit)
        )
        uncapped_score = score
        if critical:
            score = min(score, spec.critical_cap)
        metadata = {
            **metadata,
            "components": components,
            "uncapped_score": round(uncapped_score, 6),
            "critical_cap_applied": bool(critical and score < uncapped_score),
            "critical_observation_ids": tuple(
                item.observation_id for item in critical
            ),
        }
        return OracleResult(
            oracle_id=spec.output_oracle_id,
            metric_id=spec.output_metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.SCORED,
            score_role=spec.output_score_role,
            raw_value=round(uncapped_score, 6),
            normalized_score=round(score, 6),
            confidence=_confidence(applicable),
            severity=_maximum_severity(applicable),
            evidence=evidence,
            version=spec.version,
            duration_ms=batch.duration_ms,
            cost=batch.cost,
            metadata=metadata,
        )

    @staticmethod
    def _validate_contract(batch: ObservationBatch, spec: ReducerSpec) -> None:
        if spec.reducer_kind not in SUPPORTED_REDUCER_KINDS:
            raise ReductionError(f"unsupported reducer_kind {spec.reducer_kind!r}")
        expected_for_kind = {
            PAGE_QUALITY: EvaluationScope.PAGE,
            PAIR_QUALITY: EvaluationScope.SLIDE_PAIR,
        }.get(spec.reducer_kind)
        if expected_for_kind is not None and spec.expected_scope != expected_for_kind:
            raise ReductionError(
                f"{spec.reducer_kind} requires {expected_for_kind.value} observations"
            )
        wrong_scope = tuple(
            item.observation_id
            for item in batch.observations
            if item.metric_id in spec.input_metric_ids
            and item.scope != spec.expected_scope
        )
        if wrong_scope:
            raise ReductionError(
                "configured observations have the wrong scope: "
                + ", ".join(wrong_scope)
            )

    @staticmethod
    def _reduce_score(
        applicable: Sequence[AtomicObservation], reducer_kind: str
    ) -> tuple[float, dict[str, float]]:
        values = tuple(_required_local_score(item) for item in applicable)
        if reducer_kind == PAGE_QUALITY:
            mean = sum(values) / len(values)
            p20 = _nearest_rank(values, 0.20)
            key_values = tuple(
                _required_local_score(item) for item in applicable if item.key_unit
            )
            if key_values:
                key_min = min(key_values)
                score = 0.60 * mean + 0.25 * p20 + 0.15 * key_min
                return score, {
                    "mean": round(mean, 6),
                    "p20_nearest_rank": round(p20, 6),
                    "key_min": round(key_min, 6),
                }
            # The key-page component is N/A, so its weight is removed rather
            # than silently assigning a zero to otherwise valid observations.
            score = (0.60 * mean + 0.25 * p20) / 0.85
            return score, {
                "mean": round(mean, 6),
                "p20_nearest_rank": round(p20, 6),
            }
        if reducer_kind == PAIR_QUALITY:
            mean = sum(values) / len(values)
            p20 = _nearest_rank(values, 0.20)
            score = 0.70 * mean + 0.30 * p20
            return score, {
                "mean": round(mean, 6),
                "p20_nearest_rank": round(p20, 6),
            }
        if reducer_kind == IMPORTANCE_COVERAGE:
            total_importance = sum(item.importance for item in applicable)
            if total_importance > 0:
                score = sum(
                    item.importance * _required_local_score(item)
                    for item in applicable
                ) / total_importance
            else:
                score = sum(values) / len(values)
            return score, {
                "importance_weighted_coverage": round(score, 6),
                "applicable_importance": round(total_importance, 6),
            }
        raise ReductionError(f"unsupported reducer_kind {reducer_kind!r}")

    @staticmethod
    def _metadata(
        batch: ObservationBatch,
        spec: ReducerSpec,
        relevant: Sequence[AtomicObservation],
        applicable: Sequence[AtomicObservation],
        unavailable: Sequence[AtomicObservation],
        observability: float,
    ) -> dict[str, object]:
        return {
            "reducer_id": spec.reducer_id,
            "reducer_version": spec.version,
            "reducer_kind": spec.reducer_kind,
            "expected_scope": spec.expected_scope.value,
            "observability": round(observability, 6),
            "coverage": round(observability, 6),
            "minimum_observability": spec.minimum_observability,
            "required": spec.required,
            "lineage": {
                "batch_oracle_id": batch.oracle_id,
                "batch_version": batch.version,
                "input_metric_ids": spec.input_metric_ids,
                "observation_ids": tuple(
                    item.observation_id for item in relevant
                ),
                "applicable_observation_ids": tuple(
                    item.observation_id for item in applicable
                ),
                "unavailable_observation_ids": tuple(
                    item.observation_id for item in unavailable
                ),
                "na_observation_ids": tuple(
                    item.observation_id
                    for item in unavailable
                    if item.metric_status == MetricStatus.NA
                ),
                "error_observation_ids": tuple(
                    item.observation_id
                    for item in unavailable
                    if item.metric_status == MetricStatus.ERROR
                ),
            },
        }


# The longer name documents intent while the shorter class remains convenient.
ObservationReducerEngine = ReducerEngine


def reduce_observations(
    batch: ObservationBatch, spec: ReducerSpec
) -> OracleResult:
    """Functional entry point for deterministic observation reduction."""

    return ReducerEngine().reduce(batch, spec)


def _local_score(observation: AtomicObservation) -> float | None:
    if observation.metric_status == MetricStatus.SCORED:
        return observation.local_score
    if observation.metric_status == MetricStatus.PASS:
        return 1.0 if observation.local_score is None else observation.local_score
    if observation.metric_status == MetricStatus.FAIL:
        return 0.0 if observation.local_score is None else observation.local_score
    return None


def _required_local_score(observation: AtomicObservation) -> float:
    value = _local_score(observation)
    if value is None:  # guarded by the applicable filter
        raise ReductionError(
            f"observation {observation.observation_id!r} has no local score"
        )
    return value


def _observability(
    relevant: Sequence[AtomicObservation],
    applicable: Sequence[AtomicObservation],
) -> float:
    if not relevant:
        return 0.0
    total_importance = sum(item.importance for item in relevant)
    if total_importance > 0:
        return sum(item.importance for item in applicable) / total_importance
    return len(applicable) / len(relevant)


def _confidence(observations: Sequence[AtomicObservation]) -> float:
    if not observations:
        return 1.0
    total_importance = sum(item.importance for item in observations)
    if total_importance > 0:
        value = sum(
            item.confidence * item.importance for item in observations
        ) / total_importance
    else:
        value = sum(item.confidence for item in observations) / len(observations)
    return round(value, 6)


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ReductionError("a percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _maximum_severity(observations: Sequence[AtomicObservation]) -> Severity:
    return max(
        (item.severity for item in observations),
        key=_SEVERITY_ORDER.__getitem__,
        default=Severity.INFO,
    )


def _collect_evidence(
    observations: Iterable[AtomicObservation],
) -> tuple[Evidence, ...]:
    unique: dict[str, Evidence] = {}
    for observation in observations:
        for item in observation.evidence:
            unique.setdefault(item.evidence_id, item)
    return tuple(unique.values())
