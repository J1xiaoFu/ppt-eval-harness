"""Decision policy kept separate from metric implementation and aggregation."""

from __future__ import annotations

from typing import Iterable

from ppt_eval.domain import (
    CoverageStatus,
    EvalProfile,
    EvaluationDecision,
    MetricStatus,
    OracleResult,
    ScoreBreakdown,
    ScoreRole,
)


class DecisionPolicy:
    """Turn a calibrated score into a deterministic operational decision."""

    def decide(
        self,
        profile: EvalProfile,
        breakdown: ScoreBreakdown,
        results: Iterable[OracleResult],
    ) -> tuple[EvaluationDecision, tuple[str, ...]]:
        results = tuple(results)
        base_hard_failures = tuple(
            result.metric_id
            for result in results
            if result.score_role == ScoreRole.BASE_MULTIPLIER
            and result.metric_status == MetricStatus.FAIL
            and (result.multiplier is None or result.multiplier == 0.0)
            and result.confidence >= profile.hard_gate_min_confidence
        )
        if base_hard_failures:
            return EvaluationDecision.FAIL, tuple(
                f"hard_gate:{metric_id}" for metric_id in base_hard_failures
            )

        reasons: list[str] = []
        if breakdown.coverage != CoverageStatus.FULL:
            reasons.append(f"coverage:{breakdown.coverage.value}")
        reasons.extend(
            f"unresolved_metric:{metric_id}"
            for metric_id in breakdown.unresolved_metric_ids
        )
        reasons.extend(
            f"low_confidence_gate:{metric_id}"
            for metric_id in breakdown.low_confidence_gate_ids
        )
        if reasons:
            return EvaluationDecision.REVIEW, tuple(dict.fromkeys(reasons))

        scene_hard_failures = tuple(
            result.metric_id
            for result in results
            if result.score_role == ScoreRole.SCENE_MULTIPLIER
            and result.metric_status == MetricStatus.FAIL
            and (result.multiplier is None or result.multiplier == 0.0)
            and result.confidence >= profile.hard_gate_min_confidence
        )
        if scene_hard_failures:
            return EvaluationDecision.FAIL, tuple(
                f"hard_gate:{metric_id}" for metric_id in scene_hard_failures
            )

        score = breakdown.full_score
        if score is None:
            return EvaluationDecision.ERROR, ("score_unavailable",)
        if score >= profile.pass_threshold:
            return EvaluationDecision.PASS, ()
        if score >= profile.review_threshold:
            return EvaluationDecision.REVIEW, ("score_in_review_band",)
        return EvaluationDecision.FAIL, ("score_below_review_threshold",)
