from __future__ import annotations

from datetime import datetime, timezone

from ppt_eval.application import (
    DagScheduler,
    InMemoryAuditLog,
    MetricDefinition,
    OracleDescriptor,
    OracleRegistry,
    ProfileCompiler,
    RunSupervisor,
)
from ppt_eval.domain import (
    CoverageStatus,
    EvalCase,
    EvalProfile,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreRole,
    SupervisorState,
)


class StaticOracle:
    def __init__(self, oracle_id: str, results: tuple[OracleResult, ...]) -> None:
        self.oracle_id = oracle_id
        self.results = results

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.oracle_id,
            version="test",
            metrics=tuple(
                MetricDefinition(item.metric_id, item.score_role)
                for item in self.results
            ),
        )

    def supports(self, context) -> bool:
        return True

    def evaluate(self, context):
        context.memo[f"called:{self.oracle_id}"] = True
        return self.results


class FlakyOracle(StaticOracle):
    def __init__(self, oracle_id: str, results: tuple[OracleResult, ...]) -> None:
        super().__init__(oracle_id, results)
        self.calls = 0

    def evaluate(self, context):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return self.results


def scored(metric: str, value: float, role: ScoreRole) -> OracleResult:
    return OracleResult(
        oracle_id=f"{metric}_oracle",
        metric_id=metric,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        score_role=role,
        normalized_score=value,
    )


def ready_profile(**changes) -> EvalProfile:
    values = {
        "profile_id": "ready",
        "version": "1",
        "scene": SceneType.READY_MADE,
        "base_weights": {"base": 1.0},
        "scene_weights": {},
        "base_multiplier_metric_ids": (),
        "scene_multiplier_metric_ids": (),
        "enabled_oracle_ids": (),
    }
    values.update(changes)
    return EvalProfile(**values)


def case(scene: SceneType = SceneType.READY_MADE) -> EvalCase:
    return EvalCase(case_id="case-1", scene=scene, pptx_path="missing.pptx")


def test_profile_compiler_always_injects_unremovable_baseline() -> None:
    dag = ProfileCompiler().compile(ready_profile(enabled_oracle_ids=("extra",)))

    assert dag.nodes[0].oracle_id == "baseline_ppt_quality"
    assert dag.nodes[0].mandatory is True
    assert dag.nodes[1].dependencies == (dag.nodes[0].node_id,)


def test_supervisor_follows_fixed_state_machine_and_finalizes() -> None:
    baseline = StaticOracle(
        "baseline_ppt_quality",
        (scored("base", 0.9, ScoreRole.BASE_ADDITIVE),),
    )
    registry = OracleRegistry((baseline,))
    audit = InMemoryAuditLog(
        id_factory=lambda prefix: f"{prefix}-audit",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    supervisor = RunSupervisor(
        DagScheduler(registry),
        audit_log=audit,
        id_factory=lambda prefix: f"{prefix}-fixed",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    outcome = supervisor.run(case(), ready_profile(), run_id="run-fixed")

    assert outcome.report.decision == EvaluationDecision.PASS
    assert outcome.report.coverage == CoverageStatus.FULL
    assert outcome.manifest.state_history == (
        SupervisorState.OBSERVE,
        SupervisorState.PLAN,
        SupervisorState.ACT,
        SupervisorState.VERIFY,
        SupervisorState.FINALIZE,
    )
    assert audit.verify_chain()


def test_scene_oracle_error_degrades_to_base_only_and_review() -> None:
    baseline = StaticOracle(
        "baseline_ppt_quality",
        (scored("base", 0.9, ScoreRole.BASE_ADDITIVE),),
    )
    profile = EvalProfile(
        profile_id="text",
        version="1",
        scene=SceneType.TEXT_TO_PPT,
        base_weights={"base": 1.0},
        scene_weights={"scene": 1.0},
        base_multiplier_metric_ids=(),
        scene_multiplier_metric_ids=(),
        enabled_oracle_ids=("not_registered",),
    )
    supervisor = RunSupervisor(
        DagScheduler(OracleRegistry((baseline,))),
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    outcome = supervisor.run(case(SceneType.TEXT_TO_PPT), profile)

    assert outcome.report.base_score == 90.0
    assert outcome.report.full_score is None
    assert outcome.report.coverage == CoverageStatus.BASE_ONLY
    assert outcome.report.decision == EvaluationDecision.REVIEW
    assert outcome.manifest.state == SupervisorState.REVIEW


def test_missing_baseline_cannot_be_bypassed_by_successful_scene_oracle() -> None:
    scene_oracle = StaticOracle(
        "scene_quality",
        (scored("scene", 1.0, ScoreRole.SCENE_ADDITIVE),),
    )
    profile = EvalProfile(
        profile_id="text",
        version="1",
        scene=SceneType.TEXT_TO_PPT,
        base_weights={"base": 1.0},
        scene_weights={"scene": 1.0},
        base_multiplier_metric_ids=(),
        scene_multiplier_metric_ids=(),
        enabled_oracle_ids=("scene_quality",),
    )
    supervisor = RunSupervisor(
        DagScheduler(OracleRegistry((scene_oracle,))),
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    outcome = supervisor.run(case(SceneType.TEXT_TO_PPT), profile)

    assert outcome.report.coverage == CoverageStatus.UNASSESSABLE
    assert outcome.report.base_score is None
    assert outcome.report.full_score is None
    assert outcome.report.decision == EvaluationDecision.REVIEW


def test_scheduler_retries_bounded_transient_oracle_failure() -> None:
    baseline = FlakyOracle(
        "baseline_ppt_quality",
        (scored("base", 0.9, ScoreRole.BASE_ADDITIVE),),
    )
    supervisor = RunSupervisor(
        DagScheduler(OracleRegistry((baseline,))),
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    outcome = supervisor.run(case(), ready_profile(max_retries=1))

    assert baseline.calls == 2
    assert outcome.report.coverage == CoverageStatus.FULL


def test_case_profile_scene_mismatch_becomes_auditable_harness_error() -> None:
    supervisor = RunSupervisor(
        DagScheduler(OracleRegistry()),
        id_factory=lambda prefix: f"{prefix}-fixed",
    )

    outcome = supervisor.run(
        case(SceneType.READY_MADE),
        EvalProfile(
            profile_id="text",
            version="1",
            scene=SceneType.TEXT_TO_PPT,
            base_weights={"base": 1.0},
            scene_weights={"scene": 1.0},
            base_multiplier_metric_ids=(),
            scene_multiplier_metric_ids=(),
        ),
    )

    assert outcome.report.decision == EvaluationDecision.ERROR
    assert outcome.report.coverage == CoverageStatus.UNASSESSABLE
    assert outcome.manifest.state_history == (
        SupervisorState.OBSERVE,
        SupervisorState.REVIEW,
    )
    assert "does not match" in outcome.manifest.error

