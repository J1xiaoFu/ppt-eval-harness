from __future__ import annotations

from pathlib import Path

from ppt_eval.application import (
    EvaluationContext,
    MetricDefinition,
    OracleDescriptor,
    ProfileCompiler,
)
from ppt_eval.domain import (
    AtomicObservation,
    EvalCase,
    EvalProfile,
    EvaluationScope,
    MetricStatus,
    ObservationBatch,
    ReducerSpec,
    SceneType,
    ScoreRole,
)
from ppt_eval.runtime import LocalEvaluationRuntime
from ppt_eval.scoring import PAGE_QUALITY, ReducerEngine
from tests.fixtures.pptx_factory import build_pptx


class _ObservationOracle:
    oracle_id = "test.v8.observe"

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name="Observation test Oracle",
            version="8.0",
            metrics=(MetricDefinition("slide_test_quality"),),
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> ObservationBatch:
        del context
        return ObservationBatch(
            oracle_id=self.oracle_id,
            version="8.0",
            observations=(
                AtomicObservation(
                    observation_id="test-page-1",
                    oracle_id=self.oracle_id,
                    metric_id="slide_test_quality",
                    scope=EvaluationScope.PAGE,
                    unit_key="page:1",
                    local_score=0.90,
                    key_unit=True,
                ),
                AtomicObservation(
                    observation_id="test-page-2",
                    oracle_id=self.oracle_id,
                    metric_id="slide_test_quality",
                    scope=EvaluationScope.PAGE,
                    unit_key="page:2",
                    local_score=0.70,
                ),
            ),
        )


class _ReducerOracle:
    oracle_id = "test.v8.reduce"

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name="Reducer test Oracle",
            version="8.0",
            metrics=(
                MetricDefinition("page_quality", ScoreRole.BASE_ADDITIVE),
            ),
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext):
        observations = context.memo.get("ppt_eval.atomic_observations", ())
        batch = ObservationBatch(
            oracle_id=self.oracle_id,
            observations=tuple(observations),
            version="8.0",
        )
        spec = ReducerSpec(
            reducer_id="page-quality-reducer",
            version="8.0",
            input_metric_ids=("slide_test_quality",),
            expected_scope=EvaluationScope.PAGE,
            reducer_kind=PAGE_QUALITY,
            output_oracle_id=self.oracle_id,
            output_metric_id="page_quality",
            output_score_role=ScoreRole.BASE_ADDITIVE,
        )
        return ReducerEngine().reduce(batch, spec)


def _profile() -> EvalProfile:
    return EvalProfile(
        profile_id="v8-pipeline-test",
        version="8.0",
        scene=SceneType.READY_MADE,
        base_weights={"page_quality": 1.0},
        scene_weights={},
        required_metric_ids=("page_quality", "file_deliverability"),
        enabled_oracle_ids=(),
        lambda_base=1.0,
        metadata={
            "pipeline_nodes": (
                {
                    "node_id": "observe:pages",
                    "oracle_id": _ObservationOracle.oracle_id,
                    "kind": "OBSERVE",
                    "dependencies": ("baseline_ppt_quality",),
                },
                {
                    "node_id": "reduce:pages",
                    "oracle_id": _ReducerOracle.oracle_id,
                    "kind": "REDUCE",
                    "dependencies": ("observe:pages",),
                },
            )
        },
    )


def test_profile_compiler_builds_multistage_v8_dag() -> None:
    dag = ProfileCompiler().compile(_profile())

    assert [node.node_id for node in dag.nodes] == [
        "baseline_ppt_quality",
        "observe:pages",
        "reduce:pages",
    ]
    assert dag.nodes[-1].dependencies == ("observe:pages",)
    assert dag.nodes[-1].kind.value == "REDUCE"


def test_runtime_persists_full_observation_artifact(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "two-pages.pptx", slides=(({},), ({},)))
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    runtime.registry.register(_ObservationOracle())
    runtime.registry.register(_ReducerOracle())

    report = runtime.evaluate(
        EvalCase(
            case_id="v8-observation-artifact",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        _profile(),
    )

    result = next(item for item in report["results"] if item["metric_id"] == "page_quality")
    assert result["metric_status"] == MetricStatus.SCORED.value
    assert report["observation_summary"]["count"] == 2
    assert report["observation_summary"]["by_scope"] == {"PAGE": 2}
    artifact = report["observation_artifact"]
    assert Path(artifact["uri"]).is_file()
    assert report["manifest"]["artifact_hashes"]["atomic_observations"] == artifact["sha256"]
