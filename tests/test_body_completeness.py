from __future__ import annotations

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.domain import EvalCase, EvalProfile, MetricStatus, SceneType, ScoreRole
from ppt_eval.oracles import BodyCompletenessOracle
from tests.fixtures.pptx_factory import build_pptx


def _context(deck) -> EvaluationContext:
    return EvaluationContext(
        case=EvalCase(
            case_id="body-completeness",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=EvalProfile.default(SceneType.READY_MADE, version="1.0"),
    )


def _title(text: str) -> dict[str, object]:
    return {
        "kind": "text",
        "text": text,
        "x": 500_000,
        "y": 250_000,
        "w": 8_000_000,
        "h": 700_000,
        "font_pt": 28,
    }


def test_body_completeness_flags_title_only_content_but_exempts_cover_and_close(
    tmp_path,
) -> None:
    deck = build_pptx(
        tmp_path / "body-signal.pptx",
        (
            (_title("Market Analysis"),),
            (
                _title("Findings"),
                {
                    "kind": "text",
                    "text": "Detailed evidence and recommendations " * 3,
                    "x": 700_000,
                    "y": 1_500_000,
                    "w": 8_000_000,
                    "h": 2_000_000,
                    "font_pt": 18,
                },
            ),
            (_title("Recommendations"),),
            (_title("Thank You"),),
        ),
    )

    result = BodyCompletenessOracle(PptxAdapter(backend="ooxml")).evaluate(
        _context(deck)
    )

    assert result.score_role == ScoreRole.DIAGNOSTIC
    assert result.metric_status == MetricStatus.SCORED
    assert result.normalized_score == 0.5
    assert result.metadata["assessed_pages"] == 2
    assert result.metadata["incomplete_pages"] == [3]
    assert result.metadata["exempt_pages"] == [1, 4]
    assert result.evidence[0].page_number == 3
    assert result.evidence[0].kind == "body_content_missing"


def test_large_semantic_visual_counts_as_body_signal(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "visual-body.pptx",
        (
            (_title("Cover"),),
            (
                _title("Visual evidence"),
                {
                    "kind": "image",
                    "x": 1_000_000,
                    "y": 1_300_000,
                    "w": 7_000_000,
                    "h": 4_000_000,
                    "alt": "market chart",
                },
            ),
        ),
    )

    result = BodyCompletenessOracle(PptxAdapter(backend="ooxml")).evaluate(
        _context(deck)
    )

    assert result.normalized_score == 1.0
    assert result.evidence == ()


def test_raster_only_deck_is_unobservable_not_zero(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "raster-body.pptx",
        tuple(
            (
                {
                    "kind": "image",
                    "x": 0,
                    "y": 0,
                    "w": 12_192_000,
                    "h": 6_858_000,
                },
            )
            for _ in range(4)
        ),
    )

    result = BodyCompletenessOracle(PptxAdapter(backend="ooxml")).evaluate(
        _context(deck)
    )

    assert result.metric_status == MetricStatus.NA
    assert result.normalized_score is None
    assert result.metadata["reason_code"] == "TEXT_OBSERVABILITY_INSUFFICIENT"
    assert result.metadata["text_page_ratio"] == 0.0
