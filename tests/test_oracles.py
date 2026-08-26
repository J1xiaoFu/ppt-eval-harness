from __future__ import annotations

import tempfile
from pathlib import Path

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application import DagScheduler, RunSupervisor
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.domain import (
    CoverageStatus,
    EvalCase,
    EvalProfile,
    ExecutionStatus,
    MetricStatus,
    SceneType,
    ScoreRole,
)
from ppt_eval.oracles import (
    SCENE_ORACLE_IDS,
    BaselinePptQualityOracle,
    MultimodalQualityOracle,
    ProjectSummaryQualityOracle,
    TextGenerationQualityOracle,
    build_default_registry,
)
from ppt_eval.oracles.scenarios import compression_quality_score
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

BASE_METRICS = {
    "file_deliverability",
    "critical_content_visibility",
    "internal_data_consistency",
    "content_clarity",
    "template_residue",
    "narrative",
    "visual_hierarchy",
    "layout",
    "typography",
    "style_consistency",
    "multimedia_quality",
    "editability",
    "compatibility",
    "accessibility",
}


def _context(
    path: Path,
    scene: SceneType,
    *,
    request: str | None = None,
    audience: str | None = None,
    source_materials: tuple[str, ...] = (),
    assets: tuple[str, ...] = (),
    metadata=None,
) -> EvaluationContext:
    case = EvalCase(
        case_id="case-1",
        scene=scene,
        pptx_path=str(path),
        request=request,
        audience=audience,
        source_materials=source_materials,
        assets=assets,
        metadata=metadata or {},
    )
    return EvaluationContext(case=case, profile=EvalProfile.default(scene))


def test_baseline_composite_covers_every_default_base_metric() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = build_pptx(Path(directory) / "baseline.pptx")
        context = _context(path, SceneType.READY_MADE)
        oracle = BaselinePptQualityOracle(PptxAdapter(backend="ooxml"))
        results = oracle.evaluate(context)

    assert {result.metric_id for result in results} == BASE_METRICS
    assert len(results) == len(BASE_METRICS)
    assert all(result.execution_status == ExecutionStatus.SUCCESS for result in results)
    assert {result.metric_id: result.score_role for result in results}["file_deliverability"] == ScoreRole.BASE_MULTIPLIER
    assert {result.metric_id: result.score_role for result in results}["layout"] == ScoreRole.BASE_ADDITIVE
    assert context.memo.get("ppt_eval.parsed_presentation") is not None


def test_unreadable_ppt_is_a_quality_failure_not_a_zeroed_additive_metric() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "broken.pptx"
        path.write_bytes(b"not a zip package")
        results = BaselinePptQualityOracle(PptxAdapter(backend="ooxml")).evaluate(
            _context(path, SceneType.TEXT_TO_PPT, request="必须包含结论")
        )

    file_result = next(item for item in results if item.metric_id == "file_deliverability")
    assert file_result.metric_status == MetricStatus.FAIL
    assert file_result.multiplier == 0.0
    assert file_result.execution_status == ExecutionStatus.SUCCESS
    additive = [item for item in results if item.score_role == ScoreRole.BASE_ADDITIVE]
    assert all(item.metric_status == MetricStatus.ERROR for item in additive)
    assert all(item.normalized_score is None for item in additive)


def test_layout_evidence_points_to_page_object_and_bbox() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = build_pptx(
            Path(directory) / "outside.pptx",
            ((
                {"kind": "text", "text": "标题", "x": 500_000, "y": 200_000, "w": 4_000_000, "h": 500_000, "font_pt": 28},
                {"kind": "text", "text": "越界对象", "x": 11_500_000, "y": 2_000_000, "w": 2_000_000, "h": 600_000, "font_pt": 18},
            ),),
        )
        results = BaselinePptQualityOracle(PptxAdapter(backend="ooxml")).evaluate(
            _context(path, SceneType.READY_MADE)
        )
    result = next(item for item in results if item.metric_id == "layout")
    finding = next(item for item in result.evidence if item.kind == "out_of_bounds")
    assert finding.page_number == 1
    assert finding.object_id is not None
    assert finding.bbox is not None and finding.bbox[0] > 0.9


def test_text_scene_has_hard_instruction_additives_and_offline_fact_na() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = build_pptx(Path(directory) / "text.pptx")
        context = _context(
            path,
            SceneType.TEXT_TO_PPT,
            request="必须包含项目汇报。展示销售额 100 万元。",
            audience="公司领导",
        )
        results = TextGenerationQualityOracle(PptxAdapter(backend="ooxml")).evaluate(context)

    by_metric = {item.metric_id: item for item in results}
    assert set(by_metric) == {
        "critical_instruction_compliance",
        "instruction_coverage",
        "audience_fit",
        "fact_quality",
    }
    assert by_metric["critical_instruction_compliance"].score_role == ScoreRole.SCENE_MULTIPLIER
    assert by_metric["critical_instruction_compliance"].metric_status == MetricStatus.PASS
    assert by_metric["critical_instruction_compliance"].multiplier == 1.0
    assert by_metric["instruction_coverage"].normalized_score is not None
    assert by_metric["instruction_coverage"].evidence
    assert by_metric["fact_quality"].metric_status == MetricStatus.NA
    assert by_metric["fact_quality"].metadata["reason_code"] == "NO_TRUSTED_FACTS"


def test_project_scene_without_sources_is_explicit_na() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = build_pptx(Path(directory) / "project.pptx")
        results = ProjectSummaryQualityOracle(PptxAdapter(backend="ooxml")).evaluate(
            _context(path, SceneType.PROJECT_SUMMARY)
        )
    assert {item.metric_id for item in results} == {
        "critical_source_consistency",
        "source_faithfulness",
        "key_point_recall",
        "numeric_accuracy",
        "compression_quality",
        "traceability",
    }
    assert all(item.metric_status == MetricStatus.NA for item in results)
    assert all(item.evidence for item in results)


def test_multimodal_asset_hash_match_has_object_level_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        asset = root / "input.png"
        asset.write_bytes(PNG_1X1)
        path = build_pptx(
            root / "media.pptx",
            ((
                {"kind": "text", "text": "多模态汇报", "font_pt": 28},
                {"kind": "image", "x": 2_000_000, "y": 1_500_000, "w": 5_000_000, "h": 3_000_000, "alt": "产品截图"},
            ),),
            image_bytes=PNG_1X1,
        )
        results = MultimodalQualityOracle(PptxAdapter(backend="ooxml")).evaluate(
            _context(path, SceneType.MULTIMODAL, assets=(str(asset),))
        )

    asset_result = next(item for item in results if item.metric_id == "asset_compliance")
    assert asset_result.metric_status == MetricStatus.SCORED
    assert asset_result.normalized_score == 1.0
    matched = next(item for item in asset_result.evidence if item.kind == "matched_asset")
    assert matched.page_number == 1
    assert matched.object_id is not None
    assert matched.bbox is not None
    assert next(item for item in results if item.metric_id == "chart_data_accuracy").metric_status == MetricStatus.NA


def test_registry_and_scene_catalog_use_harness_ids() -> None:
    registry = build_default_registry(PptxAdapter(backend="ooxml"))
    assert registry.contains("baseline_ppt_quality")
    assert SCENE_ORACLE_IDS[SceneType.TEXT_TO_PPT] == ("scenario.instruction_alignment",)
    assert SCENE_ORACLE_IDS[SceneType.PROJECT_SUMMARY] == ("scenario.source_faithfulness",)
    assert SCENE_ORACLE_IDS[SceneType.MULTIMODAL] == ("scenario.asset_compliance",)
    descriptor = registry.get("baseline_ppt_quality").describe()
    assert {metric.metric_id for metric in descriptor.metrics} == BASE_METRICS


def test_compression_quality_score_is_continuous_at_registered_boundaries() -> None:
    epsilon = 1e-8
    for boundary in (0.03, 0.20, 0.45):
        below = compression_quality_score(boundary - epsilon)
        at = compression_quality_score(boundary)
        above = compression_quality_score(boundary + epsilon)

        assert abs(at - below) < 1e-6
        assert abs(above - at) < 1e-6


def test_compression_quality_score_has_expected_direction_and_anchor_values() -> None:
    assert compression_quality_score(0.0) == 0.0
    assert compression_quality_score(0.03) == 0.5
    assert compression_quality_score(0.20) == 1.0
    assert compression_quality_score(0.45) == 0.85
    assert compression_quality_score(1.20) == 0.0
    assert compression_quality_score(2.0) == 0.0

    under_target = [
        compression_quality_score(value) for value in (0.0, 0.01, 0.03, 0.10, 0.20)
    ]
    over_target = [
        compression_quality_score(value) for value in (0.20, 0.30, 0.45, 0.80, 1.20)
    ]
    assert under_target == sorted(under_target)
    assert over_target == sorted(over_target, reverse=True)


def test_all_four_scenes_preserve_baseline_when_specialty_evidence_is_missing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = build_pptx(Path(directory) / "fallback.pptx")
        for scene in SceneType:
            case = EvalCase(case_id=f"case-{scene.value}", scene=scene, pptx_path=str(path))
            profile = EvalProfile.default(scene, version="1.0")
            outcome = RunSupervisor(
                DagScheduler(build_default_registry(PptxAdapter(backend="ooxml")))
            ).run(case, profile)
            metric_ids = {item.metric_id for item in outcome.report.results}
            assert BASE_METRICS <= metric_ids
            assert outcome.report.base_score is not None
            if scene == SceneType.READY_MADE:
                assert outcome.report.coverage == CoverageStatus.FULL
            else:
                assert outcome.report.coverage in {
                    CoverageStatus.BASE_ONLY,
                    CoverageStatus.DEGRADED,
                }
                assert outcome.report.full_score is None
