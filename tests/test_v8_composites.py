from __future__ import annotations

from pathlib import Path

import pytest

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application import EvaluationContext
from ppt_eval.domain import (
    EvalCase,
    EvalProfile,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreRole,
)
from ppt_eval.oracles.v8_composites import (
    V8AtomicObservationComposite,
    V8QualityReducerOracle,
    V8TieredVisualCriterionOracle,
)
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx
from tests.test_grounded_visual_audit import GroundedFakeProvider


def _deck(path: Path) -> Path:
    return build_pptx(
        path,
        (
            (
                {
                    "kind": "text",
                    "text": "市场分析",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "增长来自新客户",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
                {
                    "kind": "text",
                    "text": "收入增长 20%，新增客户贡献主要增量，下一步扩大渠道覆盖。",
                    "x": 700_000,
                    "y": 1_400_000,
                    "w": 8_000_000,
                    "h": 1_500_000,
                    "font_pt": 18,
                },
            ),
        ),
    )


def _vlm_result(metric_id: str, score: float) -> OracleResult:
    return OracleResult(
        oracle_id="grounded-vlm-test",
        metric_id=metric_id,
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        score_role=ScoreRole.BASE_ADDITIVE,
        raw_value=score,
        normalized_score=score,
        confidence=0.90,
        version="2.0.0",
    )


def _context(path: Path, scene: SceneType = SceneType.READY_MADE) -> EvaluationContext:
    return EvaluationContext(
        case=EvalCase(
            case_id="v8-composite",
            scene=scene,
            pptx_path=str(path),
            request="必须包含市场分析" if scene == SceneType.TEXT_TO_PPT else None,
        ),
        profile=EvalProfile.default(scene),
        memo={},
    )


def _model_results() -> list[OracleResult]:
    return [
        _vlm_result("structured_vlm_composition_layout", 0.88),
        _vlm_result("structured_vlm_typography_legibility", 0.86),
        _vlm_result("structured_vlm_color_contrast", 0.84),
        _vlm_result("structured_vlm_imagery_data_visualization", 0.80),
        _vlm_result("structured_vlm_cross_slide_consistency", 0.82),
        _vlm_result("structured_vlm_render_integrity", 0.95),
    ]


def test_v8_reducer_outputs_quality_attributes_without_holistic_judges(
    tmp_path: Path,
) -> None:
    context = _context(_deck(tmp_path / "quality.pptx"))
    batch = V8AtomicObservationComposite(PptxAdapter(backend="ooxml")).evaluate(
        context
    )
    context.memo["ppt_eval.atomic_observations"] = list(batch.observations)
    context.memo["ppt_eval.oracle_results"] = _model_results()

    results = V8QualityReducerOracle().evaluate(context)
    indexed = {item.metric_id: item for item in results}

    assert set(indexed) == {
        "v8_functional_integrity",
        "content_structure",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity",
    }
    assert all("llm" not in metric_id and "vlm" not in metric_id for metric_id in indexed)
    assert indexed["composition_craft"].normalized_score <= 0.88
    assert indexed["composition_craft"].metadata["fusion_mode"] == (
        "MODEL_POSITIVE_SIGNAL_WITH_DETERMINISTIC_CAP"
    )
    assert indexed["v8_functional_integrity"].multiplier == 1.0


def test_missing_model_aesthetics_is_na_not_a_neutral_score(tmp_path: Path) -> None:
    context = _context(_deck(tmp_path / "missing-model.pptx"))
    batch = V8AtomicObservationComposite(PptxAdapter(backend="ooxml")).evaluate(
        context
    )
    context.memo["ppt_eval.atomic_observations"] = list(batch.observations)
    context.memo["ppt_eval.oracle_results"] = []

    indexed = {
        item.metric_id: item for item in V8QualityReducerOracle().evaluate(context)
    }

    assert indexed["composition_craft"].metric_status == MetricStatus.NA
    assert indexed["palette_craft"].metric_status == MetricStatus.NA
    assert indexed["composition_craft"].normalized_score is None


def test_text_scene_reuses_requirement_atoms_without_critical_double_score(
    tmp_path: Path,
) -> None:
    context = _context(_deck(tmp_path / "text-scene.pptx"), SceneType.TEXT_TO_PPT)
    batch = V8AtomicObservationComposite(PptxAdapter(backend="ooxml")).evaluate(
        context
    )
    context.memo["ppt_eval.atomic_observations"] = list(batch.observations)
    context.memo["ppt_eval.oracle_results"] = _model_results()

    indexed = {
        item.metric_id: item for item in V8QualityReducerOracle().evaluate(context)
    }
    requirement_atoms = [
        item for item in batch.observations if item.metric_id == "requirement_satisfaction"
    ]

    assert len(requirement_atoms) == 1
    assert indexed["instruction"].score_role == ScoreRole.SCENE_ADDITIVE
    assert indexed["instruction"].normalized_score == pytest.approx(1.0)
    assert "critical_instruction_compliance" not in indexed


def test_visual_advanced_fallback_is_same_criterion_only(tmp_path: Path) -> None:
    deck = build_pptx(
        tmp_path / "rule-disagreement.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "Out of bounds",
                    "x": 11_800_000,
                    "y": 1_000_000,
                    "w": 2_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
        ),
    )
    image = tmp_path / "slide.png"
    image.write_bytes(PNG_1X1)
    context = _context(deck)
    context = EvaluationContext(
        case=context.case,
        profile=context.profile,
        artifacts={"slide_images": (image,)},
        memo={},
    )
    observations = V8AtomicObservationComposite(
        PptxAdapter(backend="ooxml")
    ).evaluate(context)
    context.memo["ppt_eval.atomic_observations"] = list(observations.observations)
    flash = GroundedFakeProvider()
    advanced = GroundedFakeProvider()
    oracle = V8TieredVisualCriterionOracle(
        "composition_layout",
        flash,
        advanced,
        PptxAdapter(backend="ooxml"),
    )

    result = oracle.evaluate(context)

    assert len(flash.requests) == 1
    assert len(advanced.requests) == 1
    assert result.metric_id == "structured_vlm_composition_layout"
    assert result.metadata["selected_tier"] == "ADVANCED"
    assert result.metadata["escalation_reason"] == "RULE_MODEL_DISAGREEMENT"
    assert advanced.requests[0].context["criterion_id"] == "composition_layout"
