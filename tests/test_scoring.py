from __future__ import annotations

from contextlib import contextmanager

from ppt_eval.domain import (
    CoverageStatus,
    EvalProfile,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreRole,
)
from ppt_eval.scoring import (
    DecisionPolicy,
    DuplicateMetricResultError,
    PptPdmsAggregator,
)


@contextmanager
def expect_raises(exception_type, message: str | None = None):
    try:
        yield
    except exception_type as exc:
        if message is not None:
            assert message in str(exc)
    else:
        raise AssertionError(f"expected {exception_type.__name__}")


def scored(metric_id: str, value: float, role: ScoreRole) -> OracleResult:
    return OracleResult(
        oracle_id=f"{metric_id}_oracle",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        score_role=role,
        normalized_score=value,
    )


def gate(
    metric_id: str, value: float, role: ScoreRole, confidence: float = 1.0
) -> OracleResult:
    return OracleResult(
        oracle_id=f"{metric_id}_oracle",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.PASS if value == 1 else MetricStatus.FAIL,
        score_role=role,
        multiplier=value,
        confidence=confidence,
    )


def profile(scene: SceneType = SceneType.TEXT_TO_PPT) -> EvalProfile:
    return EvalProfile(
        profile_id="test",
        version="1",
        scene=scene,
        base_weights={"base": 1.0},
        scene_weights={} if scene == SceneType.READY_MADE else {"scene": 1.0},
        base_multiplier_metric_ids=("base_gate",),
        scene_multiplier_metric_ids=("scene_gate",)
        if scene != SceneType.READY_MADE
        else (),
        lambda_base=0.55 if scene != SceneType.READY_MADE else 1.0,
    )


def test_pdms_uses_outer_products_and_inner_addition() -> None:
    results = (
        scored("base", 0.8, ScoreRole.BASE_ADDITIVE),
        gate("base_gate", 0.5, ScoreRole.BASE_MULTIPLIER),
        scored("scene", 0.6, ScoreRole.SCENE_ADDITIVE),
        gate("scene_gate", 1.0, ScoreRole.SCENE_MULTIPLIER),
    )

    score = PptPdmsAggregator().aggregate(profile(), results)

    assert score.coverage == CoverageStatus.FULL
    assert score.base_score == 40.0
    assert score.full_score == 35.5


def test_na_is_neutral_and_reweights_additive_metrics() -> None:
    ready = EvalProfile(
        profile_id="ready",
        version="1",
        scene=SceneType.READY_MADE,
        base_weights={"usable": 0.5, "not_applicable": 0.5},
        scene_weights={},
        base_multiplier_metric_ids=(),
        required_metric_ids=("usable",),
    )
    na = OracleResult(
        oracle_id="na",
        metric_id="not_applicable",
        execution_status=ExecutionStatus.SKIPPED,
        metric_status=MetricStatus.NA,
        score_role=ScoreRole.BASE_ADDITIVE,
    )

    score = PptPdmsAggregator().aggregate(
        ready, (scored("usable", 0.8, ScoreRole.BASE_ADDITIVE), na)
    )

    assert score.coverage == CoverageStatus.FULL
    assert score.base_score == 80.0
    assert score.full_score == 80.0


def test_required_na_is_neutral_to_arithmetic_but_degrades_coverage() -> None:
    ready = EvalProfile(
        profile_id="ready",
        version="1",
        scene=SceneType.READY_MADE,
        base_weights={"usable": 0.5, "required": 0.5},
        scene_weights={},
        base_multiplier_metric_ids=(),
        required_metric_ids=("usable", "required"),
    )
    na = OracleResult(
        oracle_id="required_oracle",
        metric_id="required",
        execution_status=ExecutionStatus.SKIPPED,
        metric_status=MetricStatus.NA,
        score_role=ScoreRole.BASE_ADDITIVE,
    )

    score = PptPdmsAggregator().aggregate(
        ready, (scored("usable", 0.8, ScoreRole.BASE_ADDITIVE), na)
    )

    assert score.base_score == 80.0
    assert score.full_score is None
    assert score.coverage == CoverageStatus.DEGRADED
    assert score.unresolved_metric_ids == ("required",)


def test_error_is_excluded_from_score_and_marks_degraded() -> None:
    ready = EvalProfile(
        profile_id="ready",
        version="1",
        scene=SceneType.READY_MADE,
        base_weights={"usable": 0.5, "broken": 0.5},
        scene_weights={},
        base_multiplier_metric_ids=(),
    )
    error = OracleResult.error(
        oracle_id="broken_oracle",
        metric_id="broken",
        score_role=ScoreRole.BASE_ADDITIVE,
        error_code="TIMEOUT",
        error_message="timed out",
    )

    score = PptPdmsAggregator().aggregate(
        ready, (scored("usable", 0.8, ScoreRole.BASE_ADDITIVE), error)
    )

    assert score.coverage == CoverageStatus.DEGRADED
    assert score.base_score == 80.0
    assert score.full_score is None
    assert score.unresolved_metric_ids == ("broken",)


def test_low_confidence_hard_failure_cannot_activate_gate() -> None:
    ready = profile(SceneType.READY_MADE)
    results = (
        scored("base", 0.8, ScoreRole.BASE_ADDITIVE),
        gate("base_gate", 0.0, ScoreRole.BASE_MULTIPLIER, confidence=0.5),
    )

    score = PptPdmsAggregator().aggregate(ready, results)

    assert score.base_multiplier == 1.0
    assert score.base_score == 80.0
    assert score.coverage == CoverageStatus.DEGRADED
    assert score.low_confidence_gate_ids == ("base_gate",)


def test_specialty_failure_keeps_base_score_but_not_fake_full_score() -> None:
    results = (
        scored("base", 0.9, ScoreRole.BASE_ADDITIVE),
        gate("base_gate", 1.0, ScoreRole.BASE_MULTIPLIER),
    )

    score = PptPdmsAggregator().aggregate(profile(), results)

    assert score.coverage == CoverageStatus.BASE_ONLY
    assert score.base_score == 90.0
    assert score.full_score is None


def test_incomplete_specialty_remains_review_even_with_scene_gate_failure() -> None:
    configured = profile()
    results = (
        scored("base", 0.9, ScoreRole.BASE_ADDITIVE),
        gate("base_gate", 1.0, ScoreRole.BASE_MULTIPLIER),
        gate("scene_gate", 0.0, ScoreRole.SCENE_MULTIPLIER),
    )
    score = PptPdmsAggregator().aggregate(configured, results)

    decision, reasons = DecisionPolicy().decide(configured, score, results)

    assert score.coverage == CoverageStatus.DEGRADED
    assert decision == EvaluationDecision.REVIEW
    assert "coverage:DEGRADED" in reasons


def test_common_zero_gate_is_a_final_failure_even_if_inner_metrics_unavailable() -> None:
    configured = profile(SceneType.READY_MADE)
    results = (gate("base_gate", 0.0, ScoreRole.BASE_MULTIPLIER),)
    score = PptPdmsAggregator().aggregate(configured, results)

    decision, reasons = DecisionPolicy().decide(configured, score, results)

    assert decision == EvaluationDecision.FAIL
    assert reasons == ("hard_gate:base_gate",)


def test_zero_deliverability_is_a_quality_conclusion_without_inner_score() -> None:
    ready = profile(SceneType.READY_MADE)

    score = PptPdmsAggregator().aggregate(
        ready, (gate("base_gate", 0.0, ScoreRole.BASE_MULTIPLIER),)
    )

    assert score.base_score == 0.0
    assert score.full_score is None
    assert score.coverage == CoverageStatus.DEGRADED


def test_duplicate_score_metric_is_rejected_to_prevent_double_penalty() -> None:
    duplicated = (
        scored("base", 0.8, ScoreRole.BASE_ADDITIVE),
        scored("base", 0.7, ScoreRole.BASE_ADDITIVE),
    )

    with expect_raises(DuplicateMetricResultError):
        PptPdmsAggregator().aggregate(profile(SceneType.READY_MADE), duplicated)


def test_profile_rejects_same_metric_inside_and_outside_formula() -> None:
    with expect_raises(ValueError, "both additive and multiplicative"):
        EvalProfile(
            profile_id="invalid",
            version="1",
            scene=SceneType.READY_MADE,
            base_weights={"same": 1.0},
            scene_weights={},
            base_multiplier_metric_ids=("same",),
        )


def test_worsening_additive_or_gate_cannot_improve_score() -> None:
    aggregator = PptPdmsAggregator()
    good = aggregator.aggregate(
        profile(),
        (
            scored("base", 0.9, ScoreRole.BASE_ADDITIVE),
            gate("base_gate", 1.0, ScoreRole.BASE_MULTIPLIER),
            scored("scene", 0.8, ScoreRole.SCENE_ADDITIVE),
            gate("scene_gate", 1.0, ScoreRole.SCENE_MULTIPLIER),
        ),
    )
    worse = aggregator.aggregate(
        profile(),
        (
            scored("base", 0.7, ScoreRole.BASE_ADDITIVE),
            gate("base_gate", 0.5, ScoreRole.BASE_MULTIPLIER),
            scored("scene", 0.8, ScoreRole.SCENE_ADDITIVE),
            gate("scene_gate", 1.0, ScoreRole.SCENE_MULTIPLIER),
        ),
    )

    assert worse.base_score < good.base_score
    assert worse.full_score < good.full_score
