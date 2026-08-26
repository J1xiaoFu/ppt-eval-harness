"""Conservative routing policy for tiered model-assisted audits.

This module deliberately contains no provider SDK or network code.  It only
interprets already validated :class:`OracleResult` values and decides whether
the cheap FLASH baseline is sufficient, a PLUS advanced audit should run, or
the case must be handed to a human.

``advanced_results=None`` means PLUS has not been attempted yet.  An explicit
empty iterable means it was attempted but produced no result.  Keeping those
states distinct lets a caller use the same pure policy before and after the
advanced call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from ppt_eval.domain import (
    CoverageStatus,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
)


class ModelAuditTier(str, Enum):
    """The tier responsible for the next or final recommendation."""

    FLASH_BASELINE = "FLASH_BASELINE"
    PLUS_ADVANCED = "PLUS_ADVANCED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True, slots=True)
class ModelEscalationOutcome:
    """Auditable output of :class:`ModelAuditEscalationPolicy`.

    ``final_recommendation`` remains ``REVIEW`` while PLUS is requested or a
    human decision is needed.  Hard-gate ``FAIL`` and harness ``ERROR`` keep
    their original semantics.
    """

    route: ModelAuditTier
    should_call_advanced: bool
    final_recommendation: EvaluationDecision
    escalation_reasons: tuple[str, ...] = ()
    human_review_reasons: tuple[str, ...] = ()
    flash_recommendation: EvaluationDecision | None = None
    plus_recommendation: EvaluationDecision | None = None

    def __post_init__(self) -> None:
        if self.should_call_advanced and self.route != ModelAuditTier.PLUS_ADVANCED:
            raise ValueError(
                "should_call_advanced is only valid when PLUS_ADVANCED is requested"
            )
        if self.route == ModelAuditTier.HUMAN_REVIEW and not self.human_review_reasons:
            raise ValueError("HUMAN_REVIEW requires at least one reason")
        if self.route != ModelAuditTier.HUMAN_REVIEW and self.human_review_reasons:
            raise ValueError("human-review reasons are only valid for HUMAN_REVIEW")


@dataclass(frozen=True, slots=True)
class _AuditSummary:
    recommendation: EvaluationDecision | None
    issue_reasons: tuple[str, ...]
    applicable_count: int


class ModelAuditEscalationPolicy:
    """Route FLASH -> PLUS -> human without weakening deterministic safety.

    A model result becomes a PASS/FAIL vote only when it is a successful,
    scored result at or above the tier's confidence floor.  Scores between the
    fail and pass thresholds are intentional abstentions.  PLUS must be both
    high-confidence and unanimous; otherwise the policy hands off to a human.

    The policy never changes incomplete coverage to FULL and never lets any
    model result override a deterministic hard-gate failure.
    """

    def __init__(
        self,
        *,
        pass_score: float = 0.80,
        fail_score: float = 0.60,
        flash_min_confidence: float = 0.65,
        plus_min_confidence: float = 0.85,
    ) -> None:
        if not 0.0 <= fail_score < pass_score <= 1.0:
            raise ValueError("scores must satisfy 0 <= fail_score < pass_score <= 1")
        for name, value in (
            ("flash_min_confidence", flash_min_confidence),
            ("plus_min_confidence", plus_min_confidence),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
        self.pass_score = pass_score
        self.fail_score = fail_score
        self.flash_min_confidence = flash_min_confidence
        self.plus_min_confidence = plus_min_confidence

    def decide(
        self,
        *,
        provisional_decision: EvaluationDecision,
        coverage: CoverageStatus,
        provisional_reasons: Iterable[str],
        flash_results: Iterable[OracleResult],
        advanced_results: Iterable[OracleResult] | None = None,
    ) -> ModelEscalationOutcome:
        """Return a routing and final-decision recommendation.

        ``provisional_reasons`` is part of the safety contract.  In particular,
        the deterministic decision policy must expose hard gates as
        ``hard_gate:<metric_id>`` and score-band reviews as
        ``score_in_review_band``.
        """

        provisional_decision = EvaluationDecision(provisional_decision)
        coverage = CoverageStatus(coverage)
        reasons = tuple(dict.fromkeys(str(reason) for reason in provisional_reasons))
        flash = self._summarize(
            flash_results,
            tier="flash_baseline",
            min_confidence=self.flash_min_confidence,
        )

        # A deterministic gate is authoritative even when models strongly
        # disagree.  This check intentionally precedes coverage handling.
        if any(reason.startswith("hard_gate:") for reason in reasons):
            return ModelEscalationOutcome(
                route=ModelAuditTier.FLASH_BASELINE,
                should_call_advanced=False,
                final_recommendation=EvaluationDecision.FAIL,
                escalation_reasons=("deterministic_hard_gate",),
                flash_recommendation=flash.recommendation,
            )

        # Models can add evidence, but they cannot manufacture missing
        # deterministic evidence or upgrade CoverageStatus.
        if coverage != CoverageStatus.FULL:
            recommendation = (
                EvaluationDecision.ERROR
                if provisional_decision == EvaluationDecision.ERROR
                else EvaluationDecision.REVIEW
            )
            return self._human(
                recommendation,
                f"coverage_not_full:{coverage.value}",
                flash_recommendation=flash.recommendation,
            )

        if provisional_decision == EvaluationDecision.ERROR:
            return self._human(
                EvaluationDecision.ERROR,
                "provisional_execution_error",
                flash_recommendation=flash.recommendation,
            )

        model_resolvable_review_reasons = tuple(
            reason
            for reason in reasons
            if reason == "score_in_review_band"
            or reason.startswith("metric_floor_review:")
        )
        non_score_review_reasons = tuple(
            reason for reason in reasons if reason not in model_resolvable_review_reasons
        )
        if provisional_decision == EvaluationDecision.REVIEW and (
            not model_resolvable_review_reasons or non_score_review_reasons
        ):
            unresolved = non_score_review_reasons or ("unspecified_review_reason",)
            return self._human(
                EvaluationDecision.REVIEW,
                *(f"not_model_resolvable:{reason}" for reason in unresolved),
                flash_recommendation=flash.recommendation,
            )

        flash_disagrees = (
            provisional_decision in (EvaluationDecision.PASS, EvaluationDecision.FAIL)
            and flash.recommendation is not None
            and flash.recommendation != provisional_decision
        )
        flash_confused = flash.recommendation is None

        escalation_reasons: list[str] = []
        if "score_in_review_band" in model_resolvable_review_reasons:
            escalation_reasons.append("score_band_review")
        escalation_reasons.extend(
            f"metric_floor:{reason.split(':', 1)[1]}"
            for reason in model_resolvable_review_reasons
            if reason.startswith("metric_floor_review:")
        )
        if flash_disagrees:
            escalation_reasons.append("flash_provisional_disagreement")
        if flash_confused:
            escalation_reasons.extend(flash.issue_reasons or ("flash_baseline_missing",))

        if not escalation_reasons:
            return ModelEscalationOutcome(
                route=ModelAuditTier.FLASH_BASELINE,
                should_call_advanced=False,
                final_recommendation=provisional_decision,
                flash_recommendation=flash.recommendation,
            )

        # None means the caller is at the first routing pass and should invoke
        # PLUS.  An explicit iterable means PLUS has already been attempted.
        if advanced_results is None:
            return ModelEscalationOutcome(
                route=ModelAuditTier.PLUS_ADVANCED,
                should_call_advanced=True,
                final_recommendation=EvaluationDecision.REVIEW,
                escalation_reasons=tuple(dict.fromkeys(escalation_reasons)),
                flash_recommendation=flash.recommendation,
            )

        plus = self._summarize(
            advanced_results,
            tier="plus_advanced",
            min_confidence=self.plus_min_confidence,
        )
        if plus.recommendation is None:
            return self._human(
                EvaluationDecision.REVIEW,
                *(plus.issue_reasons or ("plus_advanced_missing",)),
                escalation_reasons=tuple(dict.fromkeys(escalation_reasons)),
                flash_recommendation=flash.recommendation,
            )

        return ModelEscalationOutcome(
            route=ModelAuditTier.PLUS_ADVANCED,
            should_call_advanced=False,
            final_recommendation=plus.recommendation,
            escalation_reasons=tuple(dict.fromkeys(escalation_reasons)),
            flash_recommendation=flash.recommendation,
            plus_recommendation=plus.recommendation,
        )

    def _summarize(
        self,
        results: Iterable[OracleResult],
        *,
        tier: str,
        min_confidence: float,
    ) -> _AuditSummary:
        votes: list[EvaluationDecision] = []
        issues: list[str] = []
        applicable_count = 0

        for result in tuple(results):
            if (
                result.metric_status == MetricStatus.NA
                and result.metadata.get("reason_code") == "SCENE_NOT_APPLICABLE"
            ):
                continue
            applicable_count += 1
            metric_id = result.metric_id

            if (
                result.execution_status != ExecutionStatus.SUCCESS
                or result.metric_status == MetricStatus.ERROR
            ):
                issues.append(f"{tier}_error:{metric_id}")
                continue
            if result.metric_status != MetricStatus.SCORED or result.normalized_score is None:
                issues.append(f"{tier}_unavailable:{metric_id}")
                continue
            if result.confidence < min_confidence:
                issues.append(f"{tier}_low_confidence:{metric_id}")
                continue

            if result.normalized_score >= self.pass_score:
                votes.append(EvaluationDecision.PASS)
            elif result.normalized_score < self.fail_score:
                votes.append(EvaluationDecision.FAIL)
            else:
                issues.append(f"{tier}_uncertain_score:{metric_id}")

        if applicable_count == 0:
            issues.append(f"{tier}_missing")
        if votes and len(set(votes)) > 1:
            issues.append(f"{tier}_disagreement")

        recommendation = None
        if votes and not issues and len(set(votes)) == 1:
            recommendation = votes[0]
        return _AuditSummary(
            recommendation=recommendation,
            issue_reasons=tuple(dict.fromkeys(issues)),
            applicable_count=applicable_count,
        )

    @staticmethod
    def _human(
        recommendation: EvaluationDecision,
        *reasons: str,
        escalation_reasons: tuple[str, ...] = (),
        flash_recommendation: EvaluationDecision | None = None,
    ) -> ModelEscalationOutcome:
        return ModelEscalationOutcome(
            route=ModelAuditTier.HUMAN_REVIEW,
            should_call_advanced=False,
            final_recommendation=recommendation,
            escalation_reasons=escalation_reasons,
            human_review_reasons=tuple(dict.fromkeys(reasons)),
            flash_recommendation=flash_recommendation,
        )


__all__ = [
    "ModelAuditEscalationPolicy",
    "ModelAuditTier",
    "ModelEscalationOutcome",
]
