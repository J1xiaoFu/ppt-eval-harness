from __future__ import annotations

import pytest

from ppt_eval.domain import (
    AtomicObservation,
    DagNodeKind,
    EvaluationScope,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    ReducerSpec,
    ScoreRole,
    Severity,
)
from ppt_eval.scoring import (
    IMPORTANCE_COVERAGE,
    PAGE_QUALITY,
    PAIR_QUALITY,
    ReducerEngine,
    ReductionError,
    reduce_observations,
)


def observation(
    index: int,
    score: float | None,
    *,
    scope: EvaluationScope = EvaluationScope.PAGE,
    importance: float = 1.0,
    key_unit: bool = False,
    critical: bool = False,
    severity: Severity = Severity.INFO,
) -> AtomicObservation:
    return AtomicObservation(
        observation_id=f"obs-{index}",
        oracle_id="atomic.visual",
        metric_id="local_quality",
        scope=scope,
        unit_key=f"unit-{index}",
        metric_status=MetricStatus.NA if score is None else MetricStatus.SCORED,
        local_score=score,
        importance=importance,
        key_unit=key_unit,
        critical=critical,
        severity=severity,
    )


def spec(
    kind: str,
    scope: EvaluationScope,
    *,
    required: bool = True,
) -> ReducerSpec:
    return ReducerSpec(
        reducer_id=f"reducer.{kind.lower()}",
        version="8.0",
        input_metric_ids=("local_quality",),
        expected_scope=scope,
        reducer_kind=kind,
        output_oracle_id="reduced.visual",
        output_metric_id="visual_quality",
        output_score_role=ScoreRole.BASE_ADDITIVE,
        required=required,
    )


def batch(*items: AtomicObservation) -> ObservationBatch:
    return ObservationBatch("atomic.visual", items, version="8.0", duration_ms=12)


def test_evaluation_scope_contains_all_v8_atomic_units() -> None:
    assert {item.value for item in EvaluationScope} == {
        "PACKAGE",
        "OBJECT",
        "PAGE",
        "SLIDE_PAIR",
        "CLAIM",
        "REQUIREMENT",
        "ASSET",
        "CHART_SERIES",
        "DECK",
    }


def test_v8_dag_node_kinds_preserve_legacy_persisted_values() -> None:
    assert {item.value for item in DagNodeKind} == {
        "ACQUIRE",
        "OBSERVE",
        "SELECT",
        "AUDIT",
        "FUSE",
        "REDUCE",
        "SCORE",
        "BASELINE",
        "SCENE",
    }
    assert DagNodeKind("BASELINE") is DagNodeKind.BASELINE
    assert DagNodeKind("SCENE") is DagNodeKind.SCENE


def test_observation_contract_validates_score_range_and_batch_identity() -> None:
    with pytest.raises(ValueError, match="require local_score"):
        AtomicObservation(
            observation_id="missing-score",
            oracle_id="atomic.visual",
            metric_id="local_quality",
            scope=EvaluationScope.PAGE,
            unit_key="page-1",
        )
    with pytest.raises(ValueError, match="between zero and one"):
        observation(1, 1.1)
    with pytest.raises(ValueError, match="must be unique"):
        batch(observation(1, 0.8), observation(1, 0.6))


def test_page_quality_uses_fixed_mean_p20_and_key_min_formula() -> None:
    items = (
        observation(1, 1.0),
        observation(2, 0.8),
        observation(3, 0.6),
        observation(4, 0.4, key_unit=True),
        observation(5, 0.2),
    )

    result = reduce_observations(batch(*items), spec(PAGE_QUALITY, EvaluationScope.PAGE))

    assert result.metric_status == MetricStatus.SCORED
    assert result.normalized_score == pytest.approx(0.47)
    assert result.metadata["components"] == {
        "mean": 0.6,
        "p20_nearest_rank": 0.2,
        "key_min": 0.4,
    }


def test_pair_quality_uses_nearest_rank_low_tail() -> None:
    items = tuple(
        observation(index, value, scope=EvaluationScope.SLIDE_PAIR)
        for index, value in enumerate((1.0, 0.8, 0.6, 0.4, 0.2), start=1)
    )

    result = ReducerEngine().reduce(
        batch(*items), spec(PAIR_QUALITY, EvaluationScope.SLIDE_PAIR)
    )

    assert result.normalized_score == pytest.approx(0.48)
    assert result.metadata["components"] == {
        "mean": 0.6,
        "p20_nearest_rank": 0.2,
    }


def test_importance_coverage_is_importance_weighted() -> None:
    items = (
        observation(1, 1.0, scope=EvaluationScope.REQUIREMENT, importance=3.0),
        observation(2, 0.0, scope=EvaluationScope.REQUIREMENT, importance=1.0),
    )

    result = reduce_observations(
        batch(*items), spec(IMPORTANCE_COVERAGE, EvaluationScope.REQUIREMENT)
    )

    assert result.normalized_score == pytest.approx(0.75)


def test_required_low_observability_returns_success_na_with_lineage() -> None:
    items = (
        observation(1, 0.9, scope=EvaluationScope.CLAIM, importance=1.0),
        observation(2, None, scope=EvaluationScope.CLAIM, importance=1.0),
    )

    result = reduce_observations(
        batch(*items), spec(IMPORTANCE_COVERAGE, EvaluationScope.CLAIM)
    )

    assert result.execution_status == ExecutionStatus.SUCCESS
    assert result.metric_status == MetricStatus.NA
    assert result.normalized_score is None
    assert result.metadata["observability"] == 0.5
    lineage = result.metadata["lineage"]
    assert isinstance(lineage, dict)
    assert lineage["observation_ids"] == ("obs-1", "obs-2")
    assert lineage["applicable_observation_ids"] == ("obs-1",)
    assert lineage["na_observation_ids"] == ("obs-2",)


def test_optional_low_observability_reduces_available_observations() -> None:
    items = (
        observation(1, 0.9, scope=EvaluationScope.ASSET, importance=1.0),
        observation(2, None, scope=EvaluationScope.ASSET, importance=4.0),
    )

    result = reduce_observations(
        batch(*items),
        spec(IMPORTANCE_COVERAGE, EvaluationScope.ASSET, required=False),
    )

    assert result.metric_status == MetricStatus.SCORED
    assert result.normalized_score == pytest.approx(0.9)
    assert result.metadata["observability"] == 0.2


def test_critical_unit_caps_the_output_without_erasing_raw_score() -> None:
    for key_unit, critical in ((True, False), (False, True)):
        items = (
            observation(
                1,
                0.9,
                key_unit=key_unit,
                critical=critical,
                severity=Severity.CRITICAL,
            ),
            observation(2, 0.9),
            observation(3, 0.9),
        )

        result = reduce_observations(
            batch(*items), spec(PAGE_QUALITY, EvaluationScope.PAGE)
        )

        assert result.raw_value == pytest.approx(0.9)
        assert result.normalized_score == pytest.approx(0.34)
        assert result.severity == Severity.CRITICAL
        assert result.metadata["critical_cap_applied"] is True
        assert result.metadata["critical_observation_ids"] == ("obs-1",)


def test_configured_metric_with_wrong_scope_is_a_contract_error() -> None:
    wrong = observation(1, 0.8, scope=EvaluationScope.OBJECT)

    with pytest.raises(ReductionError, match="wrong scope"):
        reduce_observations(
            batch(wrong), spec(PAGE_QUALITY, EvaluationScope.PAGE)
        )
