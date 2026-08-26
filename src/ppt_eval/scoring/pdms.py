"""PPT-PDMS scoring: hard non-compensable gates around soft metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod
from typing import Iterable, Mapping

from ppt_eval.domain import (
    CONSTRUCT_WEIGHTED_MEAN,
    CoverageStatus,
    EvalProfile,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreBreakdown,
    ScoreRole,
)


class ScoringError(ValueError):
    """A profile/result set violates the scoring contract."""


class DuplicateMetricResultError(ScoringError):
    """Raised rather than silently punishing the same defect twice."""


@dataclass(frozen=True, slots=True)
class _AdditivePart:
    value: float | None
    unresolved: tuple[str, ...]
    applicable: tuple[str, ...]
    construct_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _MultiplierPart:
    value: float
    unresolved: tuple[str, ...]
    applicable: tuple[str, ...]
    low_confidence: tuple[str, ...]


class PptPdmsAggregator:
    """Pure deterministic implementation of the versioned PPT-PDMS formula."""

    def aggregate(
        self, profile: EvalProfile, results: Iterable[OracleResult]
    ) -> ScoreBreakdown:
        indexed = self._index_score_results(results)
        required = frozenset(profile.required_metric_ids or ())
        if profile.aggregation_strategy == CONSTRUCT_WEIGHTED_MEAN:
            base_additive = self._construct_additive(
                profile.base_weights,
                indexed,
                required,
                ScoreRole.BASE_ADDITIVE,
                profile.base_metric_constructs,
                profile.base_construct_weights,
            )
            scene_additive = self._construct_additive(
                profile.scene_weights,
                indexed,
                required,
                ScoreRole.SCENE_ADDITIVE,
                profile.scene_metric_constructs,
                profile.scene_construct_weights,
            )
        else:
            base_additive = self._additive(
                profile.base_weights, indexed, required, ScoreRole.BASE_ADDITIVE
            )
            scene_additive = self._additive(
                profile.scene_weights, indexed, required, ScoreRole.SCENE_ADDITIVE
            )
        base_multiplier = self._multipliers(
            profile.base_multiplier_metric_ids,
            indexed,
            profile.hard_gate_min_confidence,
            required,
            ScoreRole.BASE_MULTIPLIER,
        )
        scene_multiplier = self._multipliers(
            profile.scene_multiplier_metric_ids,
            indexed,
            profile.hard_gate_min_confidence,
            required,
            ScoreRole.SCENE_MULTIPLIER,
        )

        base_complete = not (
            base_additive.unresolved or base_multiplier.unresolved
        ) and (base_additive.value is not None or base_multiplier.value == 0.0)
        base_score: float | None = None
        if base_additive.value is not None:
            base_score = 100.0 * base_multiplier.value * base_additive.value
        elif base_multiplier.value == 0.0:
            # An unreadable/empty deliverable is a deterministic quality result,
            # even though no inner visual/content metric can be computed.
            base_score = 0.0

        has_scene_metrics = bool(
            profile.scene_weights or profile.scene_multiplier_metric_ids
        )
        scene_complete = not (
            scene_additive.unresolved or scene_multiplier.unresolved
        ) and (not profile.scene_weights or scene_additive.value is not None)
        scene_signal = bool(
            scene_additive.applicable or scene_multiplier.applicable
        )

        if base_score is None:
            coverage = CoverageStatus.UNASSESSABLE
        elif profile.scene == SceneType.READY_MADE or not has_scene_metrics:
            coverage = (
                CoverageStatus.FULL if base_complete else CoverageStatus.DEGRADED
            )
        elif not scene_signal:
            coverage = CoverageStatus.BASE_ONLY
        elif base_complete and scene_complete:
            coverage = CoverageStatus.FULL
        else:
            coverage = CoverageStatus.DEGRADED

        full_score: float | None = None
        if profile.scene == SceneType.READY_MADE and coverage == CoverageStatus.FULL:
            full_score = base_score
        elif (
            coverage == CoverageStatus.FULL
            and base_additive.value is not None
            and scene_additive.value is not None
        ):
            lambda_base = profile.lambda_base
            if lambda_base is None:  # guarded by EvalProfile validation
                raise ScoringError("profile lambda_base is unavailable")
            inner = (
                float(lambda_base) * base_additive.value
                + (1.0 - float(lambda_base)) * scene_additive.value
            )
            full_score = (
                100.0
                * base_multiplier.value
                * scene_multiplier.value
                * inner
            )

        unresolved = tuple(
            dict.fromkeys(
                base_additive.unresolved
                + base_multiplier.unresolved
                + scene_additive.unresolved
                + scene_multiplier.unresolved
            )
        )
        low_confidence = tuple(
            dict.fromkeys(
                base_multiplier.low_confidence + scene_multiplier.low_confidence
            )
        )
        return ScoreBreakdown(
            base_additive=self._rounded(base_additive.value),
            scene_additive=self._rounded(scene_additive.value),
            base_multiplier=self._rounded(base_multiplier.value) or 0.0,
            scene_multiplier=self._rounded(scene_multiplier.value) or 0.0,
            base_score=self._rounded(base_score),
            full_score=self._rounded(full_score),
            coverage=coverage,
            base_complete=base_complete,
            scene_complete=scene_complete,
            unresolved_metric_ids=unresolved,
            low_confidence_gate_ids=low_confidence,
            base_construct_scores={
                key: round(value, 6)
                for key, value in base_additive.construct_scores.items()
            },
            scene_construct_scores={
                key: round(value, 6)
                for key, value in scene_additive.construct_scores.items()
            },
        )

    @staticmethod
    def _index_score_results(
        results: Iterable[OracleResult],
    ) -> Mapping[str, OracleResult]:
        indexed: dict[str, OracleResult] = {}
        for result in results:
            if result.score_role == ScoreRole.DIAGNOSTIC:
                continue
            if result.metric_id in indexed:
                raise DuplicateMetricResultError(
                    f"multiple score-affecting results for metric {result.metric_id!r}"
                )
            indexed[result.metric_id] = result
        return indexed

    @staticmethod
    def _additive(
        weights: Mapping[str, float],
        indexed: Mapping[str, OracleResult],
        required: frozenset[str],
        expected_role: ScoreRole,
    ) -> _AdditivePart:
        numerator = 0.0
        denominator = 0.0
        unresolved: list[str] = []
        applicable: list[str] = []
        for metric_id, weight in weights.items():
            if weight <= 0:
                continue
            result = indexed.get(metric_id)
            if result is None or result.metric_status == MetricStatus.ERROR:
                unresolved.append(metric_id)
                continue
            if result.metric_status == MetricStatus.NA:
                if metric_id in required:
                    unresolved.append(metric_id)
                continue
            if result.score_role != expected_role:
                raise ScoringError(
                    f"metric {metric_id!r} is configured as additive but returned "
                    f"role {result.score_role.value}"
                )
            if result.metric_status == MetricStatus.PASS:
                score = 1.0
            elif result.metric_status == MetricStatus.FAIL:
                score = 0.0
            elif result.metric_status == MetricStatus.SCORED:
                if result.normalized_score is None:  # guarded by the contract
                    raise ScoringError(f"metric {metric_id!r} has no score")
                score = result.normalized_score
            else:
                unresolved.append(metric_id)
                continue
            numerator += weight * score
            denominator += weight
            applicable.append(metric_id)
        value = numerator / denominator if denominator else None
        return _AdditivePart(value, tuple(unresolved), tuple(applicable))

    @classmethod
    def _construct_additive(
        cls,
        weights: Mapping[str, float],
        indexed: Mapping[str, OracleResult],
        required: frozenset[str],
        expected_role: ScoreRole,
        assignments: Mapping[str, str],
        construct_weights: Mapping[str, float],
    ) -> _AdditivePart:
        if not weights:
            return _AdditivePart(None, (), (), {})
        scores: dict[str, float] = {}
        unresolved: list[str] = []
        applicable: list[str] = []
        for construct_id in construct_weights:
            group_weights = {
                metric_id: weight
                for metric_id, weight in weights.items()
                if weight > 0 and assignments.get(metric_id) == construct_id
            }
            part = cls._additive(
                group_weights,
                indexed,
                required,
                expected_role,
            )
            unresolved.extend(part.unresolved)
            applicable.extend(part.applicable)
            if part.value is None:
                unresolved.append(f"construct:{construct_id}")
            else:
                scores[construct_id] = part.value

        numerator = 0.0
        denominator = 0.0
        for construct_id, weight in construct_weights.items():
            value = scores.get(construct_id)
            if value is None or weight <= 0:
                continue
            numerator += weight * value
            denominator += weight
        value = numerator / denominator if denominator else None
        return _AdditivePart(
            value,
            tuple(dict.fromkeys(unresolved)),
            tuple(dict.fromkeys(applicable)),
            scores,
        )

    @staticmethod
    def _multipliers(
        metric_ids: tuple[str, ...],
        indexed: Mapping[str, OracleResult],
        minimum_confidence: float,
        required: frozenset[str],
        expected_role: ScoreRole,
    ) -> _MultiplierPart:
        values: list[float] = []
        unresolved: list[str] = []
        applicable: list[str] = []
        low_confidence: list[str] = []
        for metric_id in metric_ids:
            result = indexed.get(metric_id)
            if result is None or result.metric_status == MetricStatus.ERROR:
                unresolved.append(metric_id)
                continue
            if result.metric_status == MetricStatus.NA:
                if metric_id in required:
                    unresolved.append(metric_id)
                continue
            if result.score_role != expected_role:
                raise ScoringError(
                    f"metric {metric_id!r} is configured as a multiplier but returned "
                    f"role {result.score_role.value}"
                )
            multiplier = result.multiplier
            if multiplier is None:
                if result.metric_status == MetricStatus.PASS:
                    multiplier = 1.0
                elif result.metric_status == MetricStatus.FAIL:
                    multiplier = 0.0
                else:
                    unresolved.append(metric_id)
                    continue
            if multiplier < 1.0 and result.confidence < minimum_confidence:
                # A subjective or uncertain judge can request review, never
                # activate a non-compensable outer gate.
                multiplier = 1.0
                unresolved.append(metric_id)
                low_confidence.append(metric_id)
            values.append(multiplier)
            applicable.append(metric_id)
        return _MultiplierPart(
            prod(values) if values else 1.0,
            tuple(unresolved),
            tuple(applicable),
            tuple(low_confidence),
        )

    @staticmethod
    def _rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 6)
