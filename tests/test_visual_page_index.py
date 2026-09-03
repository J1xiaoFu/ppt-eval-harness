from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ppt_eval.adapters.pptx import (
    BoundingBox,
    ParsedPresentation,
    ParsedSlide,
    SlideObject,
    ZipPreflightReport,
)
from ppt_eval.application.visual_index import VisualPageIndexBuilder
from ppt_eval.domain import (
    AtomicObservation,
    EvaluationScope,
    Evidence,
    ExecutionStatus,
    MetricStatus,
    ScoutFinding,
    ScoutResult,
    Severity,
    VisualAuditRound,
    VisualCoverageCertificate,
    VisualSelectionItem,
    VisualSelectionPlan,
)

_DECK_SHA = "d" * 64
_COMMON_ASSET_SHA = "a" * 64
_OUTLIER_ASSET_SHA = "b" * 64


def _object(
    object_id: str,
    *,
    kind: str = "shape",
    text: str = "",
    x: float = 0.08,
    y: float = 0.10,
    width: float = 0.84,
    height: float = 0.16,
    media_sha256: str | None = None,
    alt_text: str = "",
) -> SlideObject:
    return SlideObject(
        object_id=object_id,
        name=f"Object {object_id}",
        kind=kind,
        bbox=BoundingBox(x=x, y=y, width=width, height=height),
        text=text,
        media_sha256=media_sha256,
        metadata={"alt_text": alt_text},
    )


def _slide(
    page_number: int,
    *,
    asset_sha256: str | None = None,
    text: str | None = None,
) -> ParsedSlide:
    objects = [
        _object(
            f"title-{page_number}",
            text=text or f"Market insight {page_number}",
        )
    ]
    if asset_sha256 is not None:
        objects.append(
            _object(
                f"image-{page_number}",
                kind="picture",
                x=0.10,
                y=0.32,
                width=0.80,
                height=0.65,
                media_sha256=asset_sha256,
                alt_text="Market evidence illustration",
            )
        )
    return ParsedSlide(
        page_number=page_number,
        slide_id=f"slide-{page_number}",
        objects=tuple(objects),
    )


def _text_rich_slide(page_number: int = 1) -> ParsedSlide:
    text = (
        "Quarterly market evidence, customer behavior, channel performance, "
        "conversion drivers, operational implications, and concrete next actions."
    )
    return ParsedSlide(
        page_number=page_number,
        slide_id=f"slide-{page_number}",
        objects=tuple(
            _object(
                f"text-{index}",
                text=f"{text} Section {index}.",
                x=0.08 + (index % 2) * 0.46,
                y=0.08 + (index // 2) * 0.42,
                width=0.40,
                height=0.34,
            )
            for index in range(4)
        ),
    )


def _presentation(slides: tuple[ParsedSlide, ...]) -> ParsedPresentation:
    return ParsedPresentation(
        source_name="deck.pptx",
        source_sha256=_DECK_SHA,
        width_emu=12_192_000,
        height_emu=6_858_000,
        slides=slides,
        media_hashes=tuple(
            sorted(
                {
                    item.media_sha256
                    for slide in slides
                    for item in slide.objects
                    if item.media_sha256 is not None
                }
            )
        ),
        preflight=ZipPreflightReport(
            archive_bytes=1,
            entry_count=1,
            total_uncompressed_bytes=1,
            max_observed_compression_ratio=1.0,
        ),
        parser_backend="unit-test",
    )


def _render(path: Path, color: tuple[int, int, int], *, accent: bool = True) -> Path:
    image = Image.new("RGB", (320, 180), color)
    if accent:
        draw = ImageDraw.Draw(image)
        draw.rectangle((25, 25, 260, 65), fill=(245, 245, 245))
        draw.line((30, 120, 285, 95), fill=(10, 10, 10), width=5)
    image.save(path)
    return path


def test_visual_contracts_are_json_friendly_and_non_scoring() -> None:
    finding = ScoutFinding(
        page_number=2,
        risk_code="placeholder_visual_suspected",
        confidence=0.81,
        suggested_criteria=("imagery_data_visualization",),
        atlas_id="atlas-1",
    )
    result = ScoutResult(
        scout_id="scout-1",
        findings=(finding,),
        covered_page_numbers=(1, 2),
        atlas_ids=("atlas-1",),
        usage={"image_tokens": 40},
    )
    payload = result.to_dict()
    assert json.loads(json.dumps(payload))["findings"][0]["risk_code"] == (
        "placeholder_visual_suspected"
    )
    assert "score" not in payload["findings"][0]
    assert "decision" not in payload["findings"][0]

    item = VisualSelectionItem(
        page_number=2,
        priority="P0",
        reasons=("rule_critical",),
        criteria=("render_integrity",),
        mandatory=True,
        consumes_exploration_budget=False,
    )
    plan = VisualSelectionPlan(
        plan_id="plan-1",
        deck_sha256=_DECK_SHA,
        items=(item,),
        common_page_local=(2,),
        common_cross_slide=(2,),
        high_resolution_budget=4,
        forced_page_numbers=(2,),
    )
    assert plan.version == "3.0.0"

    round_result = VisualAuditRound(
        round_number=1,
        page_numbers=(2,),
        criterion_pages={"render_integrity": (2,)},
        continue_audit=False,
        stopping_reason="all_mandatory_pages_resolved",
    )
    certificate = VisualCoverageCertificate(
        deck_sha256=_DECK_SHA,
        total_pages=2,
        atlas_covered_page_numbers=(1, 2),
        high_resolution_page_numbers=(2,),
        criterion_pages={"render_integrity": (2,)},
        covered_cluster_ids=("layout-a", "asset-a"),
        uncovered_cluster_ids=(),
        hard_gate_candidate_pages=(2,),
        resolved_hard_gate_pages=(2,),
        round_count=1,
        atlas_coverage_complete=True,
        semantic_coverage_complete=True,
        coverage_complete=True,
        stopping_reason=round_result.stopping_reason or "resolved",
    )
    assert json.loads(json.dumps(certificate.to_dict()))["coverage_complete"] is True


def test_visual_contracts_reject_invalid_routing_state() -> None:
    with pytest.raises(ValueError, match="P0 pages must be mandatory"):
        VisualSelectionItem(
            page_number=1,
            priority="P0",
            reasons=("critical",),
            criteria=(),
        )
    with pytest.raises(ValueError, match="Atlas-covered"):
        ScoutResult(
            scout_id="scout-bad",
            findings=(
                ScoutFinding(
                    page_number=2,
                    risk_code="semantic_mismatch_suspected",
                    confidence=0.7,
                    suggested_criteria=("imagery_data_visualization",),
                ),
            ),
            covered_page_numbers=(1,),
        )


def test_builder_extracts_render_and_object_tree_features(tmp_path: Path) -> None:
    slides = (
        _slide(1, asset_sha256=_COMMON_ASSET_SHA),
        _slide(2, asset_sha256=_COMMON_ASSET_SHA),
        _slide(3),
    )
    renders = {
        1: _render(tmp_path / "1.png", (30, 60, 100)),
        2: _render(tmp_path / "2.png", (30, 60, 100)),
        3: _render(tmp_path / "3.png", (245, 245, 245)),
    }
    index = VisualPageIndexBuilder().build(
        _presentation(slides),
        rendered_images=renders,
        ocr_text_by_page={1: "Market insight 1", 2: "Market insight 2", 3: None},
    )

    assert index.version == "1.0.0"
    assert index.rendered_page_numbers == (1, 2, 3)
    assert index.ocr_available is True
    assert len(index.pages) == 3
    first = index.pages[0]
    assert first.page_phash is not None and len(first.page_phash) == 16
    assert len(first.image_phashes) == 1
    assert len(first.color_histogram) == 24
    assert first.visual_entropy is not None
    assert first.edge_density is not None
    assert first.image_dominant is True
    assert first.duplicate_asset_hashes == (_COMMON_ASSET_SHA,)
    assert first.missing_alt_text_count == 0
    assert first.layout_cluster_id is not None
    assert first.asset_cluster_id is not None
    assert index.pages[2].ocr_text_character_count is None
    assert index.pages[2].image_text_dense is None


def test_object_pixel_parity_proxy_routes_only_near_certain_missing_render(
    tmp_path: Path,
) -> None:
    slide = _text_rich_slide()
    blank = _render(tmp_path / "blank.png", (255, 255, 255), accent=False)
    visible_monochrome = _render(
        tmp_path / "visible-monochrome.png",
        (8, 8, 8),
        accent=True,
    )

    missing = VisualPageIndexBuilder().build(
        _presentation((slide,)),
        rendered_images={1: blank},
    ).pages[0]
    rendered = VisualPageIndexBuilder().build(
        _presentation((slide,)),
        rendered_images={1: visible_monochrome},
    ).pages[0]
    minimal = VisualPageIndexBuilder().build(
        _presentation((_slide(1, text="A deliberately minimal monochrome title"),)),
        rendered_images={1: blank},
    ).pages[0]

    assert missing.object_pixel_parity_anomaly is True
    assert missing.metadata["object_pixel_parity_proxy"]["status"] == (
        "ANOMALY_SUSPECTED"
    )
    assert missing.metadata["object_pixel_parity_proxy"]["score_affecting"] is False
    assert rendered.object_pixel_parity_anomaly is False
    assert minimal.object_pixel_parity_anomaly is False


def test_builder_is_stable_when_parsed_slides_arrive_out_of_order(tmp_path: Path) -> None:
    slides = tuple(_slide(page, asset_sha256=_COMMON_ASSET_SHA) for page in range(1, 7))
    renders = {
        page: _render(tmp_path / f"stable-{page}.png", (50, 90, 120))
        for page in range(1, 7)
    }
    builder = VisualPageIndexBuilder()
    forward = builder.build(_presentation(slides), rendered_images=renders)
    reverse = builder.build(_presentation(tuple(reversed(slides))), rendered_images=renders)
    assert forward.to_dict() == reverse.to_dict()


@pytest.mark.parametrize("slide_count", [50, 100])
def test_long_deck_asset_outlier_is_not_diluted(slide_count: int) -> None:
    outlier_page = 27 if slide_count == 50 else 57
    slides = tuple(
        _slide(
            page,
            asset_sha256=_OUTLIER_ASSET_SHA if page == outlier_page else _COMMON_ASSET_SHA,
        )
        for page in range(1, slide_count + 1)
    )
    index = VisualPageIndexBuilder().build(_presentation(slides))

    assert len(index.pages) == slide_count
    assert tuple(page.page_number for page in index.pages) == tuple(range(1, slide_count + 1))
    outlier = index.pages[outlier_page - 1]
    assert outlier.asset_outlier is True
    cluster = next(item for item in index.asset_clusters if item.cluster_id == outlier.asset_cluster_id)
    assert cluster.member_page_numbers == (outlier_page,)
    assert cluster.medoid_page_number == outlier_page
    assert cluster.is_outlier is True
    assert outlier.duplicate_asset_hashes == ()
    assert index.pages[0].duplicate_asset_hashes == (_COMMON_ASSET_SHA,)


def test_no_image_and_missing_ocr_are_explicit_na_not_failure() -> None:
    index = VisualPageIndexBuilder().build(
        _presentation((_slide(1), _slide(2))),
    )

    assert index.ocr_available is False
    assert index.rendered_page_numbers == ()
    assert index.warnings == ()
    for page in index.pages:
        assert page.image_count == 0
        assert page.page_phash is None
        assert page.color_histogram == ()
        assert page.visual_entropy is None
        assert page.edge_density is None
        assert page.ocr_text_character_count is None
        assert page.image_text_dense is None
        assert page.metadata["ocr_status"] == "N/A"


def test_text_only_long_deck_does_not_create_one_asset_cluster_per_page() -> None:
    slides = tuple(
        _slide(page, text=f"Unique project finding number {page}")
        for page in range(1, 51)
    )
    index = VisualPageIndexBuilder().build(_presentation(slides))

    assert len(index.asset_clusters) <= 3
    assert all(page.asset_outlier is False for page in index.pages)


def test_rule_critical_and_unobservable_metrics_are_preserved_for_routing() -> None:
    critical = AtomicObservation(
        observation_id="critical-1",
        oracle_id="v8.geometry",
        metric_id="geometry_integrity",
        scope=EvaluationScope.PAGE,
        unit_key="page:2",
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.FAIL,
        severity=Severity.MAJOR,
        critical=True,
    )
    unobservable = AtomicObservation(
        observation_id="na-1",
        oracle_id="v8.pixel_contrast",
        metric_id="pixel_contrast",
        scope=EvaluationScope.PAGE,
        unit_key="page:2",
        execution_status=ExecutionStatus.SKIPPED,
        metric_status=MetricStatus.NA,
        evidence=(
            Evidence(
                evidence_id="ev-na-1",
                kind="missing_render",
                message="No page render",
                page_number=2,
            ),
        ),
    )
    index = VisualPageIndexBuilder().build(
        _presentation((_slide(1), _slide(2))),
        observations=(unobservable, critical),
    )

    page = index.pages[1]
    assert page.rule_severity == "CRITICAL"
    assert page.rule_risk_metric_ids == ("geometry_integrity",)
    assert page.unobservable_metric_ids == ("pixel_contrast",)


def test_builder_records_partial_render_failure_without_losing_all_pages(tmp_path: Path) -> None:
    valid = _render(tmp_path / "valid.png", (120, 40, 40))
    index = VisualPageIndexBuilder().build(
        _presentation((_slide(1), _slide(2), _slide(3))),
        rendered_images={1: valid, 2: tmp_path / "missing.png"},
    )

    assert index.rendered_page_numbers == (1,)
    assert index.warnings == (
        "render_missing:page:3",
        "render_unreadable:page:2",
    )
    assert len(index.pages) == 3
