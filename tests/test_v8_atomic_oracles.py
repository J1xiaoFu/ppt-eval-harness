from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.domain import (
    EvalCase,
    EvalProfile,
    EvaluationScope,
    MetricStatus,
    SceneType,
    Severity,
)
from ppt_eval.oracles.v8_atomic import (
    AltTextOracle,
    AuthorshipSpecificitySignalsOracle,
    CropGeometryRiskOracle,
    DocumentStructureOracle,
    DuplicateSlideOracle,
    EffectiveImageResolutionOracle,
    LanguageConsistencyOracle,
    MediaIntegrityOracle,
    PixelContrastProxyOracle,
    ReadingOrderProxyOracle,
    RenderAvailabilityParityOracle,
    SlideContentPresenceOracle,
    SlideEditabilityOracle,
    SlideGeometryIntegrityOracle,
    SlideReadingLoadOracle,
    SlideRoleClassifierOracle,
    SlideTypographyFunctionalOracle,
    TitleBodyAlignmentOracle,
    TransitionCoherenceProxyOracle,
    observe_assets,
    observe_chart_series,
    observe_requirements,
)
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


def _context(
    path: Path,
    *,
    scene: SceneType = SceneType.READY_MADE,
    request: str | None = None,
    assets: tuple[str, ...] = (),
    metadata: dict[str, object] | None = None,
) -> EvaluationContext:
    case = EvalCase(
        case_id="v8-case",
        scene=scene,
        pptx_path=str(path),
        request=request,
        assets=assets,
        metadata=metadata or {},
    )
    return EvaluationContext(case=case, profile=EvalProfile.default(scene))


def _ooxml() -> PptxAdapter:
    return PptxAdapter(backend="ooxml")


def test_page_observations_preserve_good_blank_and_dense_pages(tmp_path: Path) -> None:
    dense_text = "详细分析数据与行动建议" * 100
    path = build_pptx(
        tmp_path / "page-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "季度经营复盘",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            (),
            (
                {
                    "kind": "text",
                    "text": "详细分析",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
                {
                    "kind": "text",
                    "text": dense_text,
                    "x": 600_000,
                    "y": 1_300_000,
                    "w": 9_000_000,
                    "h": 4_500_000,
                    "font_pt": 9,
                },
            ),
        ),
    )
    context = _context(path)

    roles = SlideRoleClassifierOracle(_ooxml()).evaluate(context).observations
    assert [item.raw_value for item in roles] == ["cover", "content", "content"]
    assert all(item.scope == EvaluationScope.PAGE for item in roles)

    presence = SlideContentPresenceOracle(_ooxml()).evaluate(context).observations
    assert [item.local_score for item in presence] == [1.0, 0.0, 1.0]
    assert presence[1].unit_key == "page:2"
    assert presence[1].evidence[0].page_number == 2

    load = SlideReadingLoadOracle(_ooxml()).evaluate(context).observations
    assert load[1].metric_status == MetricStatus.NA
    assert load[2].local_score is not None and load[2].local_score < 0.5

    typography = SlideTypographyFunctionalOracle(_ooxml()).evaluate(context).observations
    assert typography[1].metric_status == MetricStatus.NA
    assert typography[2].critical is True
    assert typography[2].local_score == pytest.approx(0.625)


def test_geometry_defect_stays_bound_to_its_page(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "geometry-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "正常页面",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 5_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "越界正文",
                    "x": 11_500_000,
                    "y": 2_000_000,
                    "w": 2_000_000,
                    "h": 700_000,
                    "font_pt": 18,
                },
            ),
        ),
    )

    observations = SlideGeometryIntegrityOracle(_ooxml()).evaluate(_context(path)).observations

    assert len(observations) == 2
    assert observations[0].local_score == 1.0
    assert observations[1].local_score is not None and observations[1].local_score < 0.5
    assert observations[1].critical is True
    finding = next(item for item in observations[1].evidence if item.kind == "out_of_bounds")
    assert finding.page_number == 2
    assert finding.object_id is not None
    assert finding.bbox is not None


def test_object_oracles_separate_editability_media_crop_and_alt_text(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "media-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "可编辑封面",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            (
                {
                    "kind": "image",
                    "x": 0,
                    "y": 0,
                    "w": 12_192_000,
                    "h": 6_858_000,
                    "alt": "",
                },
            ),
        ),
        image_bytes=PNG_1X1,
    )
    context = _context(path, scene=SceneType.MULTIMODAL)

    editability = SlideEditabilityOracle(_ooxml()).evaluate(context).observations
    assert editability[0].local_score == 1.0
    assert editability[1].local_score == 0.10
    assert editability[1].metadata["raster_only"] is True

    media = MediaIntegrityOracle(_ooxml()).evaluate(context).observations
    crop = CropGeometryRiskOracle(_ooxml()).evaluate(context).observations
    alt = AltTextOracle(_ooxml()).evaluate(context).observations
    assert len(media) == len(crop) == len(alt) == 1
    assert media[0].scope == EvaluationScope.OBJECT
    assert media[0].local_score == 1.0
    assert crop[0].local_score == 1.0
    assert crop[0].metadata["semantic_crop_observed"] is False
    assert alt[0].local_score == 0.0
    assert alt[0].evidence[0].page_number == 2


def test_document_structure_and_title_body_alignment_are_distinct(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "structure-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "项目封面",
                    "x": 700_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "增长结论",
                    "x": 700_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
                {
                    "kind": "text",
                    "text": "增长来自新客户，下一步扩大转化渠道并控制交付风险。" * 4,
                    "x": 750_000,
                    "y": 1_500_000,
                    "w": 8_000_000,
                    "h": 1_500_000,
                    "font_pt": 18,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "谢谢",
                    "x": 2_000_000,
                    "y": 2_000_000,
                    "w": 5_000_000,
                    "h": 1_000_000,
                    "font_pt": 30,
                },
            ),
        ),
    )
    context = _context(path)

    structure = DocumentStructureOracle(_ooxml()).evaluate(context).observations
    alignment = TitleBodyAlignmentOracle(_ooxml()).evaluate(context).observations

    assert structure[0].local_score == 1.0
    assert structure[1].local_score == 1.0
    assert structure[2].metric_status == MetricStatus.NA
    assert alignment[0].metric_status == MetricStatus.NA
    assert alignment[1].metric_status == MetricStatus.SCORED
    assert alignment[1].metadata["topical_overlap_is_score_affecting"] is False
    assert alignment[2].metric_status == MetricStatus.NA


def test_duplicate_and_transition_proxies_emit_slide_pair_atoms(tmp_path: Path) -> None:
    repeated = (
        {
            "kind": "text",
            "text": "市场机会",
            "x": 600_000,
            "y": 250_000,
            "w": 7_000_000,
            "h": 700_000,
            "font_pt": 28,
        },
        {
            "kind": "text",
            "text": "市场规模 100 亿，客户需求持续增长。",
            "x": 600_000,
            "y": 1_500_000,
            "w": 8_000_000,
            "h": 1_000_000,
            "font_pt": 18,
        },
    )
    path = build_pptx(
        tmp_path / "pair-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "年度计划",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            repeated,
            repeated,
        ),
    )
    context = _context(path)

    duplicates = DuplicateSlideOracle(_ooxml()).evaluate(context).observations
    transitions = TransitionCoherenceProxyOracle(_ooxml()).evaluate(context).observations

    assert any(item.unit_key == "pages:2-3" for item in duplicates)
    assert all(item.scope == EvaluationScope.SLIDE_PAIR for item in duplicates)
    assert len(transitions) == 2
    assert transitions[1].unit_key == "pages:2-3"
    assert transitions[1].confidence == 0.45


def test_authorship_specificity_is_explicitly_low_confidence_proxy(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "authorship-atoms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "Click to add title",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "2026 年收入增长 37%，负责人 Alice，交付日期 2026-09。",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 1_000_000,
                    "font_pt": 20,
                },
            ),
        ),
    )

    observations = AuthorshipSpecificitySignalsOracle(_ooxml()).evaluate(_context(path)).observations

    assert observations[0].local_score is not None
    assert observations[1].local_score is not None
    assert observations[0].local_score < observations[1].local_score
    assert all(item.confidence == 0.45 for item in observations)
    assert all(item.metadata["proxy_only"] is True for item in observations)


def test_repeated_card_grid_lowers_authorship_signal_without_penalizing_minimalism(
    tmp_path: Path,
) -> None:
    cards = tuple(
        {
            "kind": "text",
            "text": "核心优势 解决方案",
            "x": 500_000 + (index % 2) * 5_000_000,
            "y": 500_000 + (index // 2) * 2_500_000,
            "w": 4_000_000,
            "h": 1_500_000,
            "font_pt": 20,
        }
        for index in range(4)
    )
    path = build_pptx(
        tmp_path / "authorship-visual.pptx",
        (
            cards,
            (
                {
                    "kind": "text",
                    "text": "2026 年客户留存率提升 12%，主要来自续费流程缩短。",
                    "x": 900_000,
                    "y": 1_000_000,
                    "w": 9_000_000,
                    "h": 1_500_000,
                    "font_pt": 26,
                },
            ),
        ),
    )

    observations = AuthorshipSpecificitySignalsOracle(_ooxml()).evaluate(
        _context(path)
    ).observations

    assert observations[0].metadata["visual_and_text_signals"] is True
    assert observations[0].evidence[0].payload["mechanical_grid_signal"] is True
    assert observations[0].local_score < observations[1].local_score


def test_language_consistency_penalizes_partial_localization_but_allows_bilingual_policy(
    tmp_path: Path,
) -> None:
    path = build_pptx(
        tmp_path / "language-consistency.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "Market Analysis 汇报人",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "Define the market and quantify customer demand",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "感谢观看 THANKS",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
        ),
    )
    oracle = LanguageConsistencyOracle(_ooxml())

    undeclared = oracle.evaluate(_context(path)).observations[0]
    bilingual = oracle.evaluate(
        _context(path, metadata={"language_policy": "BILINGUAL"})
    ).observations[0]

    assert undeclared.scope == EvaluationScope.DECK
    assert undeclared.local_score is not None and undeclared.local_score < 0.80
    assert undeclared.metadata["minority_language_pages"] == [1, 3]
    assert undeclared.metadata["primary_owner"] == "language_consistency"
    assert bilingual.local_score == 1.0


def test_language_consistency_ignores_common_technical_acronyms(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "language-acronyms.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "使用 AI、SaaS、ROI 与 CRM 指标分析客户价值",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
        ),
    )

    observation = LanguageConsistencyOracle(_ooxml()).evaluate(_context(path)).observations[0]

    assert observation.local_score == 1.0
    assert observation.metadata["dominant_language"] == "ZH"


def test_systematic_mixed_language_requires_explicit_bilingual_policy(
    tmp_path: Path,
) -> None:
    mixed_slide = (
        {
            "kind": "text",
            "text": "市场分析 Market analysis decision framework",
            "x": 600_000,
            "y": 250_000,
            "w": 8_000_000,
            "h": 700_000,
            "font_pt": 28,
        },
    )
    path = build_pptx(
        tmp_path / "undeclared-systematic-bilingual.pptx",
        (mixed_slide, mixed_slide, mixed_slide),
    )
    oracle = LanguageConsistencyOracle(_ooxml())

    undeclared = oracle.evaluate(_context(path)).observations[0]
    declared = oracle.evaluate(
        _context(path, metadata={"allowed_languages": ["zh", "en"]})
    ).observations[0]

    assert undeclared.metadata["systematic_bilingual"] is True
    assert undeclared.local_score is not None and undeclared.local_score < 0.50
    assert declared.local_score == 1.0


def test_placeholder_numbers_do_not_masquerade_as_authorship_specificity(
    tmp_path: Path,
) -> None:
    path = build_pptx(
        tmp_path / "authorship-placeholders.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "Competitor A Speed 8/10 Cost ?/10; a SaaS firm saved millions.",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 900_000,
                    "font_pt": 24,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "Acme renewal conversion rose 12% in 2026 after checkout time fell from 9 to 4 minutes.",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 8_000_000,
                    "h": 900_000,
                    "font_pt": 24,
                },
            ),
        ),
    )

    observations = AuthorshipSpecificitySignalsOracle(_ooxml()).evaluate(
        _context(path)
    ).observations

    assert observations[0].evidence[0].payload["placeholder_authorship_hits"] >= 2
    assert observations[0].local_score < observations[1].local_score
    assert all(item.severity != Severity.MAJOR for item in observations)


def test_repeated_section_silhouette_is_an_authorship_signal(tmp_path: Path) -> None:
    repeated = tuple(
        {
            "kind": "text",
            "text": text,
            "x": 900_000,
            "y": 1_500_000,
            "w": 8_000_000,
            "h": 900_000,
            "font_pt": 30,
        }
        for text in ("PART 01",)
    )
    path = build_pptx(
        tmp_path / "repeated-silhouette.pptx",
        (
            repeated,
            tuple({**item, "text": "PART 02"} for item in repeated),
            tuple({**item, "text": "PART 03"} for item in repeated),
            (
                {
                    "kind": "text",
                    "text": "Revenue grew 18% after the named enterprise segment launched.",
                    "x": 900_000,
                    "y": 500_000,
                    "w": 8_000_000,
                    "h": 1_800_000,
                    "font_pt": 30,
                },
                {
                    "kind": "text",
                    "text": "The result changes the channel investment decision.",
                    "x": 1_500_000,
                    "y": 3_000_000,
                    "w": 6_000_000,
                    "h": 1_000_000,
                    "font_pt": 20,
                },
            ),
        ),
    )

    observations = AuthorshipSpecificitySignalsOracle(_ooxml()).evaluate(
        _context(path)
    ).observations

    assert all(
        item.evidence[0].payload["repeated_silhouette_signal"] is True
        for item in observations[:3]
    )
    assert max(item.local_score or 0.0 for item in observations[:3]) < (
        observations[3].local_score or 0.0
    )


def test_requirement_helper_expands_page_scopes_without_deck_aggregation(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "requirements.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "公开经营摘要",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "收入 100 万元",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            ),
        ),
    )
    context = _context(
        path,
        scene=SceneType.TEXT_TO_PPT,
        request="第2页必须包含收入。每页不得包含机密。",
    )

    observations = observe_requirements(context, adapter=_ooxml()).observations

    assert len(observations) == 3
    assert {item.unit_key for item in observations} == {
        "requirement:1/page:2",
        "requirement:2/page:1",
        "requirement:2/page:2",
    }
    assert all(item.scope == EvaluationScope.REQUIREMENT for item in observations)
    assert all(item.key_unit and item.critical is False for item in observations)


def test_asset_helper_observes_required_asset_once(tmp_path: Path) -> None:
    asset = tmp_path / "logo.png"
    asset.write_bytes(PNG_1X1)
    path = build_pptx(
        tmp_path / "assets.pptx",
        (
            (
                {
                    "kind": "image",
                    "x": 2_000_000,
                    "y": 1_000_000,
                    "w": 5_000_000,
                    "h": 3_000_000,
                    "alt": "公司标识",
                },
            ),
        ),
        image_bytes=PNG_1X1,
    )
    context = _context(
        path,
        scene=SceneType.MULTIMODAL,
        metadata={"required_assets": [str(asset)]},
    )

    observations = observe_assets(context, adapter=_ooxml()).observations

    assert len(observations) == 1
    assert observations[0].scope == EvaluationScope.ASSET
    assert observations[0].local_score == 1.0
    assert observations[0].key_unit is True
    assert observations[0].critical is False
    assert observations[0].evidence[0].page_number == 1


def test_chart_helper_binds_expectation_to_chart_object_values(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "chart.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "经营数据",
                    "x": 600_000,
                    "y": 250_000,
                    "w": 7_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
                {
                    "kind": "chart",
                    "x": 1_000_000,
                    "y": 1_300_000,
                    "w": 8_000_000,
                    "h": 4_000_000,
                },
            ),
        ),
    )
    context = _context(path, scene=SceneType.MULTIMODAL)

    observations = observe_chart_series(
        context,
        adapter=_ooxml(),
        critical_expectations=("销售额 2026 100",),
    ).observations

    assert len(observations) == 1
    assert observations[0].scope == EvaluationScope.CHART_SERIES
    assert observations[0].local_score == 1.0
    assert observations[0].key_unit is True
    assert observations[0].critical is False
    assert observations[0].evidence[0].page_number == 1
    assert observations[0].evidence[0].object_id is not None
    assert observations[0].evidence[0].payload["binding_scope"] == "chart_object_values"


def test_reading_order_and_render_parity_are_page_scoped(tmp_path: Path) -> None:
    path = build_pptx(
        tmp_path / "reading-render.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "Lower object first in XML",
                    "x": 500_000,
                    "y": 2_000_000,
                    "w": 5_000_000,
                    "h": 700_000,
                    "font_pt": 20,
                },
                {
                    "kind": "text",
                    "text": "Visual title",
                    "x": 500_000,
                    "y": 200_000,
                    "w": 5_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
            (
                {
                    "kind": "text",
                    "text": "Second page",
                    "x": 500_000,
                    "y": 200_000,
                    "w": 5_000_000,
                    "h": 700_000,
                    "font_pt": 30,
                },
            ),
        ),
    )
    base = _context(path)
    context = EvaluationContext(
        case=base.case,
        profile=base.profile,
        artifacts={"slide_images": ({"page_number": 1, "path": "opaque"},)},
        memo={},
    )

    order = ReadingOrderProxyOracle(_ooxml()).evaluate(context).observations
    parity = RenderAvailabilityParityOracle(_ooxml()).evaluate(context).observations

    assert order[0].scope == EvaluationScope.PAGE
    assert order[0].local_score == 0.0
    assert parity[0].local_score == 1.0
    assert parity[1].local_score == 0.0
    assert parity[1].critical is True
    assert parity[1].evidence[0].page_number == 2


def test_pixel_contrast_and_effective_resolution_emit_functional_atoms(
    tmp_path: Path,
) -> None:
    path = build_pptx(
        tmp_path / "pixel-rules.pptx",
        (
            (
                {
                    "kind": "text",
                    "text": "High contrast",
                    "x": 1_219_200,
                    "y": 685_800,
                    "w": 6_096_000,
                    "h": 685_800,
                    "font_pt": 30,
                },
                {
                    "kind": "image",
                    "x": 1_219_200,
                    "y": 2_057_400,
                    "w": 8_534_400,
                    "h": 3_429_000,
                },
            ),
        ),
        image_bytes=PNG_1X1,
    )
    render = Image.new("RGB", (1600, 900), "white")
    drawer = ImageDraw.Draw(render)
    drawer.rectangle((160, 90, 320, 180), fill="black")
    render_path = tmp_path / "render.png"
    render.save(render_path)
    base = _context(path)
    context = EvaluationContext(
        case=base.case,
        profile=base.profile,
        artifacts={"slide_images": (render_path,)},
        memo={},
    )

    contrast = PixelContrastProxyOracle(_ooxml()).evaluate(context).observations
    resolution_context = EvaluationContext(
        case=base.case,
        profile=base.profile,
        artifacts=context.artifacts,
        memo={},
    )
    resolution = EffectiveImageResolutionOracle(
        PptxAdapter(backend="python-pptx")
    ).evaluate(resolution_context).observations

    assert contrast[0].metric_status == MetricStatus.SCORED
    assert contrast[0].local_score is not None and contrast[0].local_score > 0.9
    assert contrast[0].evidence[0].payload["ratio"] > 10
    assert resolution[0].scope == EvaluationScope.OBJECT
    assert resolution[0].local_score is not None and resolution[0].local_score < 0.01
    assert resolution[0].critical is True
