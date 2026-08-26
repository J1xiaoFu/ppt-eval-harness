from __future__ import annotations

import math
from pathlib import Path

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.domain import EvalCase, EvalProfile, OracleResult, SceneType
from ppt_eval.oracles import LayoutOracle, TemplateResidueOracle
from ppt_eval.oracles.baseline import template_residue_score
from tests.fixtures.pptx_factory import build_pptx


def _context(path: Path) -> EvaluationContext:
    return EvaluationContext(
        case=EvalCase(
            case_id=f"regression-{path.stem}",
            scene=SceneType.READY_MADE,
            pptx_path=str(path),
        ),
        profile=EvalProfile.default(SceneType.READY_MADE),
    )


def _evaluate_template_residue(path: Path) -> OracleResult:
    return TemplateResidueOracle(PptxAdapter(backend="ooxml")).evaluate(_context(path))


def _text(text: str, y: int, *, x: int = 800_000) -> dict[str, object]:
    return {
        "kind": "text",
        "text": text,
        "x": x,
        "y": y,
        "w": 4_300_000,
        "h": 420_000,
        "font_pt": 18,
    }


def test_template_residue_detects_strong_markers_without_flagging_real_fields(
    tmp_path: Path,
) -> None:
    clean = build_pptx(
        tmp_path / "clean-real-fields.pptx",
        ((
            _text("市场分析", 300_000),
            _text("日期：2026年8月26日", 1_000_000),
            _text("报告人：王小明", 1_600_000),
            _text("XXL 产品线与 TBD 方案均已正式确认", 2_200_000),
        ),),
    )
    residue = build_pptx(
        tmp_path / "template-residue.pptx",
        ((
            _text("市场分析", 300_000),
            _text("20xx.xx.xx", 1_000_000),
            _text("报告人名称", 1_600_000),
            _text("TBD", 2_200_000),
            _text("Lorem ipsum dolor sit amet", 2_800_000),
        ),),
    )

    clean_result = _evaluate_template_residue(clean)
    residue_result = _evaluate_template_residue(residue)

    assert clean_result.normalized_score == 1.0
    assert clean_result.evidence == ()
    assert math.isclose(residue_result.normalized_score or 0.0, 0.1)
    assert residue_result.metadata["residue_objects"] == 4
    assert residue_result.metadata["reason_counts"] == {
        "date_placeholder": 1,
        "explicit_todo_token": 1,
        "lorem_ipsum": 1,
        "presenter_placeholder": 1,
    }
    assert all(item.page_number == 1 for item in residue_result.evidence)
    assert all(item.object_id and item.bbox for item in residue_result.evidence)

    # Evidence ids are derived from metric/page/object rather than random UUIDs.
    repeated = _evaluate_template_residue(residue)
    assert [item.evidence_id for item in repeated.evidence] == [
        item.evidence_id for item in residue_result.evidence
    ]


def test_template_residue_score_is_monotonic_in_unresolved_object_count() -> None:
    scores = [
        template_residue_score(findings, min(findings, 3), 3)
        for findings in range(6)
    ]

    assert scores[0] == 1.0
    assert all(left >= right for left, right in zip(scores, scores[1:]))
    assert math.isclose(scores[-1], 0.1)
    try:
        template_residue_score(-1, 0, 1)
    except ValueError as exc:
        assert "cannot be negative" in str(exc)
    else:
        raise AssertionError("negative finding count must be rejected")


def _real_style_objects(*, add_text_collision: bool) -> tuple[dict[str, object], ...]:
    objects: list[dict[str, object]] = [
        {
            "kind": "shape",
            "name": "Background",
            "x": 0,
            "y": 0,
            # Deliberate background bleed extends well beyond rounding tolerance.
            "w": 12_400_000,
            "h": 6_858_000,
        },
        {
            "kind": "shape",
            "name": "Rectangle Card",
            "x": 700_000,
            "y": 1_300_000,
            "w": 4_700_000,
            "h": 3_600_000,
        },
        _text("市场趋势", 1_600_000, x=1_000_000),
        # A heading/body pair with roughly 50% bbox overlap models line-height
        # padding observed in the real Slides-Align decks; it is not occlusion.
        _text("卡片内的正文标签", 1_810_000, x=1_000_000),
        {
            "kind": "connector",
            "name": "Timeline connector",
            "x": 6_000_000,
            "y": 3_300_000,
            "w": 4_000_000,
            "h": 20_000,
        },
        {
            "kind": "shape",
            "name": "Oval node 1",
            "x": 6_500_000,
            "y": 3_100_000,
            "w": 400_000,
            "h": 400_000,
        },
        {
            "kind": "image",
            "name": "Footer accent image",
            "x": 0,
            "y": 6_500_000,
            "w": 12_192_000,
            # Sub-percent semantic-media overshoot is parser/export tolerance.
            "h": 360_000,
        },
        {
            "kind": "shape",
            "name": "Oval node 2",
            "x": 8_500_000,
            "y": 3_100_000,
            "w": 400_000,
            "h": 400_000,
        },
    ]
    if add_text_collision:
        objects.extend(
            (
                _text("被遮挡的关键结论 A", 5_100_000, x=1_000_000),
                _text("被遮挡的关键结论 B", 5_180_000, x=1_100_000),
            )
        )
    return tuple(objects)


def test_layout_ignores_real_style_composition_but_preserves_text_collisions(
    tmp_path: Path,
) -> None:
    composed = build_pptx(
        tmp_path / "timeline-card-composition.pptx",
        (_real_style_objects(add_text_collision=False),),
    )
    collision = build_pptx(
        tmp_path / "timeline-card-with-text-collision.pptx",
        (_real_style_objects(add_text_collision=True),),
    )
    oracle = LayoutOracle(PptxAdapter(backend="ooxml"))

    composed_result = oracle.evaluate(_context(composed))
    collision_result = oracle.evaluate(_context(collision))

    assert composed_result.normalized_score == 1.0
    assert composed_result.metadata["overlaps"] == 0
    assert composed_result.metadata["ignored_intentional_overlaps"] >= 2
    assert composed_result.metadata["ignored_outside_tolerance"] == 1
    assert composed_result.metadata["ignored_intentional_outside"] == 1
    assert not [item for item in composed_result.evidence if item.kind == "overlap"]

    overlap_findings = [item for item in collision_result.evidence if item.kind == "overlap"]
    assert collision_result.normalized_score is not None
    assert composed_result.normalized_score is not None
    assert collision_result.normalized_score < composed_result.normalized_score
    assert collision_result.metadata["overlaps"] == 1
    assert len(overlap_findings) == 1
    assert overlap_findings[0].payload["classification"] == "text_text"
    assert overlap_findings[0].object_id is not None
    assert overlap_findings[0].bbox is not None


def test_layout_keeps_meaningful_semantic_media_overflow(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "semantic-media-overflow.pptx",
        ((
            _text("交付版市场分析", 300_000),
            {
                "kind": "image",
                "name": "Required product screenshot",
                "x": 11_500_000,
                "y": 1_000_000,
                "w": 2_000_000,
                "h": 2_000_000,
            },
        ),),
    )

    result = LayoutOracle(PptxAdapter(backend="ooxml")).evaluate(_context(path))

    findings = [item for item in result.evidence if item.kind == "out_of_bounds"]
    assert result.metadata["outside"] == 1
    assert result.metadata["ignored_intentional_outside"] == 0
    assert len(findings) == 1
    assert findings[0].object_id is not None
    assert findings[0].bbox is not None
