"""Dependency-free tests for conservative FLASH -> PLUS -> human routing."""

from __future__ import annotations

import unittest

from ppt_eval.application.model_escalation import (
    ModelAuditEscalationPolicy,
    ModelAuditTier,
)
from ppt_eval.domain import (
    CoverageStatus,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
)


def scored(
    metric_id: str,
    score: float,
    *,
    confidence: float = 0.95,
) -> OracleResult:
    return OracleResult(
        oracle_id=f"model.{metric_id}",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        normalized_score=score,
        confidence=confidence,
    )


class ModelAuditEscalationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ModelAuditEscalationPolicy()

    def decide(
        self,
        *,
        provisional: EvaluationDecision = EvaluationDecision.PASS,
        coverage: CoverageStatus = CoverageStatus.FULL,
        reasons: tuple[str, ...] = (),
        flash: tuple[OracleResult, ...] = (),
        advanced: tuple[OracleResult, ...] | None = None,
    ):
        return self.policy.decide(
            provisional_decision=provisional,
            coverage=coverage,
            provisional_reasons=reasons,
            flash_results=flash,
            advanced_results=advanced,
        )

    def test_hard_gate_fail_cannot_be_overridden_by_models(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.FAIL,
            reasons=("hard_gate:file_deliverability",),
            flash=(scored("flash", 0.99),),
            advanced=(scored("plus", 0.99),),
        )

        self.assertEqual(outcome.route, ModelAuditTier.FLASH_BASELINE)
        self.assertFalse(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.FAIL)
        self.assertEqual(outcome.escalation_reasons, ("deterministic_hard_gate",))

    def test_incomplete_coverage_goes_to_human_and_is_not_model_upgraded(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            coverage=CoverageStatus.DEGRADED,
            reasons=("coverage:DEGRADED",),
            flash=(scored("flash", 0.99),),
            advanced=(scored("plus", 0.99),),
        )

        self.assertEqual(outcome.route, ModelAuditTier.HUMAN_REVIEW)
        self.assertFalse(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.REVIEW)
        self.assertEqual(
            outcome.human_review_reasons,
            ("coverage_not_full:DEGRADED",),
        )

    def test_flash_agreement_keeps_provisional_pass_without_plus(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.PASS,
            flash=(scored("content", 0.91), scored("visual", 0.86)),
        )

        self.assertEqual(outcome.route, ModelAuditTier.FLASH_BASELINE)
        self.assertFalse(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.PASS)
        self.assertEqual(outcome.flash_recommendation, EvaluationDecision.PASS)

    def test_score_band_review_requests_plus_on_first_pass(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("score_in_review_band",),
            flash=(scored("flash", 0.91),),
            advanced=None,
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertTrue(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.REVIEW)
        self.assertIn("score_band_review", outcome.escalation_reasons)
        self.assertEqual(outcome.human_review_reasons, ())

    def test_metric_floor_review_requests_plus_on_first_pass(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("metric_floor_review:template_residue",),
            flash=(scored("content", 0.91), scored("visual", 0.88)),
            advanced=None,
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertTrue(outcome.should_call_advanced)
        self.assertIn("metric_floor:template_residue", outcome.escalation_reasons)

    def test_high_confidence_unanimous_plus_resolves_score_band(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("score_in_review_band",),
            flash=(scored("flash", 0.91),),
            advanced=(scored("plus_content", 0.92), scored("plus_visual", 0.87)),
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertFalse(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.PASS)
        self.assertEqual(outcome.plus_recommendation, EvaluationDecision.PASS)

    def test_explicitly_missing_plus_result_goes_to_human(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("score_in_review_band",),
            flash=(scored("flash", 0.91),),
            advanced=(),
        )

        self.assertEqual(outcome.route, ModelAuditTier.HUMAN_REVIEW)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.REVIEW)
        self.assertEqual(outcome.human_review_reasons, ("plus_advanced_missing",))

    def test_low_confidence_plus_result_goes_to_human(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("score_in_review_band",),
            flash=(scored("flash", 0.91),),
            advanced=(scored("content", 0.95, confidence=0.84),),
        )

        self.assertEqual(outcome.route, ModelAuditTier.HUMAN_REVIEW)
        self.assertEqual(
            outcome.human_review_reasons,
            ("plus_advanced_low_confidence:content",),
        )

    def test_plus_metric_disagreement_goes_to_human(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("score_in_review_band",),
            flash=(scored("flash", 0.91),),
            advanced=(scored("content", 0.93), scored("visual", 0.40)),
        )

        self.assertEqual(outcome.route, ModelAuditTier.HUMAN_REVIEW)
        self.assertEqual(
            outcome.human_review_reasons,
            ("plus_advanced_disagreement",),
        )

    def test_flash_provisional_disagreement_requests_plus(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.PASS,
            flash=(scored("content", 0.42), scored("visual", 0.35)),
            advanced=None,
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertTrue(outcome.should_call_advanced)
        self.assertIn("flash_provisional_disagreement", outcome.escalation_reasons)

    def test_flash_internal_disagreement_is_high_confusion_and_requests_plus(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.PASS,
            flash=(scored("content", 0.92), scored("visual", 0.30)),
            advanced=None,
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertTrue(outcome.should_call_advanced)
        self.assertIn("flash_baseline_disagreement", outcome.escalation_reasons)

    def test_flash_gray_band_is_high_confusion_and_requests_plus(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.PASS,
            flash=(scored("content", 0.72),),
            advanced=None,
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertIn(
            "flash_baseline_uncertain_score:content",
            outcome.escalation_reasons,
        )

    def test_non_score_review_reason_bypasses_plus_and_goes_to_human(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.REVIEW,
            reasons=("low_confidence_gate:critical_content_visibility",),
            flash=(scored("flash", 0.95),),
        )

        self.assertEqual(outcome.route, ModelAuditTier.HUMAN_REVIEW)
        self.assertEqual(
            outcome.human_review_reasons,
            (
                "not_model_resolvable:low_confidence_gate:critical_content_visibility",
            ),
        )

    def test_plus_can_resolve_score_disagreement_to_fail_but_not_a_hard_gate(self) -> None:
        outcome = self.decide(
            provisional=EvaluationDecision.PASS,
            reasons=(),
            flash=(scored("flash", 0.30),),
            advanced=(scored("plus", 0.20),),
        )

        self.assertEqual(outcome.route, ModelAuditTier.PLUS_ADVANCED)
        self.assertFalse(outcome.should_call_advanced)
        self.assertEqual(outcome.final_recommendation, EvaluationDecision.FAIL)


if __name__ == "__main__":
    unittest.main()
