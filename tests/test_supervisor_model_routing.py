"""Supervisor integration tests for explicit FLASH -> PLUS -> human routing."""

from __future__ import annotations

from datetime import datetime, timezone

from ppt_eval.application import (
    DagScheduler,
    InMemoryAuditLog,
    MetricDefinition,
    OracleDescriptor,
    OracleRegistry,
    RunSupervisor,
)
from ppt_eval.domain import (
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
    def __init__(
        self,
        oracle_id: str,
        results: tuple[OracleResult, ...],
        *,
        version: str = "test-1",
    ) -> None:
        self.oracle_id = oracle_id
        self.results = results
        self.version = version
        self.calls = 0

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.oracle_id,
            version=self.version,
            metrics=tuple(
                MetricDefinition(result.metric_id, result.score_role)
                for result in self.results
            ),
            deterministic=False,
        )

    def supports(self, context) -> bool:
        return True

    def evaluate(self, context):
        self.calls += 1
        return self.results


class RaisingAdvancedOracle(StaticOracle):
    def evaluate(self, context):
        self.calls += 1
        raise RuntimeError("provider transport failed")


def scored(
    metric_id: str,
    value: float,
    role: ScoreRole,
    *,
    confidence: float = 0.95,
    cost: float = 0.0,
    model: str | None = None,
) -> OracleResult:
    metadata = {}
    if model is not None:
        metadata = {
            "audit_type": "model",
            "response_schema_version": "1.0",
            "model": {
                "provider": "qwen",
                "model_id": model,
                "version": "2026-08-26",
            },
            "prompt": {
                "prompt_id": f"prompt-{metric_id}",
                "version": "1.0.0",
                "sha256": "a" * 64,
            },
        }
    return OracleResult(
        oracle_id=f"oracle.{metric_id}",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        score_role=role,
        normalized_score=value,
        confidence=confidence,
        cost=cost,
        metadata=metadata,
    )


def failed_gate(metric_id: str) -> OracleResult:
    return OracleResult(
        oracle_id=f"oracle.{metric_id}",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.FAIL,
        score_role=ScoreRole.BASE_MULTIPLIER,
        multiplier=0.0,
        confidence=1.0,
    )


def profile(*, routing: str | None = "FLASH_PLUS_HUMAN", gate: bool = False):
    return EvalProfile(
        profile_id="tiered-model-test",
        version="3.0",
        scene=SceneType.READY_MADE,
        base_weights={"base": 0.8, "flash_content": 0.2},
        scene_weights={},
        base_multiplier_metric_ids=("gate",) if gate else (),
        scene_multiplier_metric_ids=(),
        enabled_oracle_ids=("flash.audit",),
        metadata=(
            {"model_audit_routing": routing}
            if routing is not None
            else {}
        ),
    )


def evaluation_case() -> EvalCase:
    return EvalCase(
        case_id="case-tiered",
        scene=SceneType.READY_MADE,
        pptx_path="missing-but-static.pptx",
    )


def supervisor_for(
    flash_score: float,
    advanced: StaticOracle,
    *,
    gate: bool = False,
    audit: InMemoryAuditLog | None = None,
) -> RunSupervisor:
    baseline_results = [
        scored("base", 0.90, ScoreRole.BASE_ADDITIVE),
    ]
    if gate:
        baseline_results.append(failed_gate("gate"))
    baseline = StaticOracle("baseline_ppt_quality", tuple(baseline_results))
    flash = StaticOracle(
        "flash.audit",
        (
            scored(
                "flash_content",
                flash_score,
                ScoreRole.BASE_ADDITIVE,
                confidence=0.95,
                cost=0.01,
                model="qwen3.7-flash",
            ),
        ),
    )
    return RunSupervisor(
        DagScheduler(OracleRegistry((baseline, flash))),
        advanced_model_review=advanced,
        audit_log=audit,
        id_factory=lambda prefix: f"{prefix}-fixed",
        clock=lambda: datetime(2026, 8, 26, tzinfo=timezone.utc),
    )


def test_routing_requires_exact_profile_metadata_opt_in() -> None:
    advanced = StaticOracle(
        "advanced.model_review",
        (scored("advanced_content", 0.95, ScoreRole.DIAGNOSTIC),),
    )
    audit = InMemoryAuditLog()
    outcome = supervisor_for(0.70, advanced, audit=audit).run(
        evaluation_case(),
        profile(routing="DISABLED"),
    )

    assert outcome.report.decision == EvaluationDecision.PASS
    assert advanced.calls == 0
    assert not any(event.event_type == "MODEL_AUDIT_ROUTING" for event in audit.events)


def test_plus_results_cost_and_provenance_are_appended_without_rescoring() -> None:
    advanced = StaticOracle(
        "advanced.model_review",
        (
            scored(
                "advanced_content",
                0.93,
                ScoreRole.DIAGNOSTIC,
                cost=0.20,
                model="qwen3.7-plus",
            ),
            scored(
                "advanced_visual",
                0.88,
                ScoreRole.DIAGNOSTIC,
                cost=0.30,
                model="qwen3.7-plus",
            ),
        ),
        version="2.1.0",
    )
    audit = InMemoryAuditLog(
        id_factory=lambda prefix: f"{prefix}-{len(prefix)}",
        clock=lambda: datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    outcome = supervisor_for(0.70, advanced, audit=audit).run(
        evaluation_case(),
        profile(),
        run_id="run-routing",
    )

    assert advanced.calls == 1
    assert outcome.report.decision == EvaluationDecision.PASS
    assert outcome.report.base_score == 86.0
    assert outcome.score is not None and outcome.score.base_score == 86.0
    assert outcome.manifest.state == SupervisorState.FINALIZE
    assert outcome.manifest.cost == 0.51
    assert outcome.manifest.oracle_versions["advanced.model_review"] == "2.1.0"
    assert outcome.manifest.model_versions["advanced_content"] == (
        "qwen/qwen3.7-plus@2026-08-26"
    )
    assert outcome.manifest.prompt_versions["advanced_visual"].startswith(
        "prompt-advanced_visual@1.0.0#"
    )
    advanced_results = tuple(
        result
        for result in outcome.report.results
        if result.metric_id.startswith("advanced_")
    )
    assert len(advanced_results) == 2
    assert all(result.score_role == ScoreRole.DIAGNOSTIC for result in advanced_results)

    routing_events = tuple(
        event for event in audit.events if event.event_type == "MODEL_AUDIT_ROUTING"
    )
    assert [event.payload["stage"] for event in routing_events] == ["FLASH", "PLUS"]
    assert routing_events[0].payload["should_call_advanced"] is True
    assert routing_events[1].payload["route"] == "PLUS_ADVANCED"
    assert routing_events[1].payload["final_recommendation"] == "PASS"
    assert routing_events[1].payload["advanced_call_status"] == "COMPLETED"
    assert "flash_baseline_uncertain_score:flash_content" in (
        routing_events[1].payload["escalation_reasons"]
    )
    assert audit.verify_chain()


def test_uncertain_plus_result_routes_to_human_review() -> None:
    advanced = StaticOracle(
        "advanced.model_review",
        (
            scored(
                "advanced_content",
                0.72,
                ScoreRole.DIAGNOSTIC,
                confidence=0.95,
            ),
        ),
    )
    outcome = supervisor_for(0.70, advanced).run(evaluation_case(), profile())

    assert advanced.calls == 1
    assert outcome.report.decision == EvaluationDecision.REVIEW
    assert outcome.manifest.state == SupervisorState.REVIEW
    assert "plus_advanced_uncertain_score:advanced_content" in (
        outcome.report.review_reasons
    )


def test_deterministic_hard_gate_prevents_plus_and_remains_fail() -> None:
    advanced = StaticOracle(
        "advanced.model_review",
        (scored("advanced_content", 0.99, ScoreRole.DIAGNOSTIC),),
    )
    audit = InMemoryAuditLog()
    outcome = supervisor_for(0.95, advanced, gate=True, audit=audit).run(
        evaluation_case(),
        profile(gate=True),
    )

    assert advanced.calls == 0
    assert outcome.report.decision == EvaluationDecision.FAIL
    assert outcome.report.full_score == 0.0
    assert "hard_gate:gate" in outcome.report.review_reasons
    routing = next(
        event for event in audit.events if event.event_type == "MODEL_AUDIT_ROUTING"
    )
    assert routing.payload["route"] == "FLASH_BASELINE"
    assert routing.payload["escalation_reasons"] == ["deterministic_hard_gate"]


def test_plus_exception_becomes_diagnostic_error_and_human_review() -> None:
    template_result = scored(
        "advanced_content",
        0.95,
        ScoreRole.DIAGNOSTIC,
    )
    advanced = RaisingAdvancedOracle(
        "advanced.model_review",
        (template_result,),
    )
    outcome = supervisor_for(0.70, advanced).run(evaluation_case(), profile())

    assert advanced.calls == 1
    assert outcome.report.decision == EvaluationDecision.REVIEW
    assert outcome.manifest.state == SupervisorState.REVIEW
    result = next(
        item for item in outcome.report.results if item.metric_id == "advanced_content"
    )
    assert result.error_code == "ADVANCED_REVIEW_EXCEPTION"
    assert outcome.report.errors
    assert "plus_advanced_error:advanced_content" in outcome.report.review_reasons
