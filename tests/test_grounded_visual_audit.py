from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

import pytest

from ppt_eval.adapters import (
    ModelAuditContractError,
    ModelAuditModality,
    ModelAuditRequest,
    ModelAuditResponse,
    ModelImageInput,
    PptxAdapter,
    PromptSpec,
)
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.config import load_profile
from ppt_eval.domain import EvalCase, MetricStatus, SceneType
from ppt_eval.infrastructure import to_primitive
from ppt_eval.oracles import (
    GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    GROUNDED_VLM_CRITERION_PROMPTS,
    GROUNDED_VLM_DEFECT_CODES,
    GROUNDED_VLM_POSITIVE_SIGNALS,
    STRUCTURED_VLM_VISUAL_CRITERION_IDS,
    GroundedStructuredDimensionsModelAuditOracle,
    GroundedStructuredVlmVisualDimensionsAuditOracle,
    build_default_registry,
)
from scripts.benchmarks.evaluate_slides_align_sample import (
    _structured_projection_contract,
)
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


class GroundedFakeProvider:
    def __init__(
        self,
        mutate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.mutate = mutate
        self.requests: list[ModelAuditRequest] = []

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        payload = deepcopy(_grounded_response(request))
        if self.mutate is not None:
            self.mutate(payload)
        return payload


def _grounded_response(request: ModelAuditRequest) -> dict[str, Any]:
    criterion_id = str(request.context["criterion_id"])
    positive = sorted(GROUNDED_VLM_POSITIVE_SIGNALS[criterion_id])[:2]
    pages = (
        (request.images[0].page_number,)
        if criterion_id == "cross_slide_consistency"
        else tuple(image.page_number for image in request.images)
    )
    evidence = [
        {
            "evidence_id": f"grounded-{criterion_id}-{page_number}",
            "kind": "criterion_summary",
            "message": f"Visible observations for {criterion_id} on page {page_number}.",
            "page_number": page_number,
            "payload": {
                "criterion_id": criterion_id,
                "criterion_score": 0.82,
                "criterion_confidence": 0.84,
                "defect_codes": [],
                "affected_page_numbers": [],
                "severity": "NONE",
                "positive_quality_signals": positive,
            }
        }
        for page_number in pages
    ]
    return {
        "score": 0.01,
        "confidence": 0.86,
        "model": {
            "provider": "fake",
            "model_id": "qwen3.7-flash",
            "version": "test",
        },
        "prompt": dict(request.prompt.reference()),
        "usage": {"input_tokens": 120, "output_tokens": 80, "cost": 0.004},
        "evidence": evidence,
    }


def _context(tmp_path) -> EvaluationContext:
    deck = build_pptx(tmp_path / "grounded.pptx")
    image = tmp_path / "slide-1.png"
    image.write_bytes(PNG_1X1)
    return EvaluationContext(
        case=EvalCase(
            case_id="grounded-visual",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
            request="Explain the decision clearly",
            audience="Executive reviewers",
        ),
        profile=load_profile(
            "configs/profiles/finished_deck_v7_grounded_visual_candidate.json"
        ),
        artifacts={"slide_images": (image,)},
        memo={},
    )


def _evaluate(tmp_path, provider: GroundedFakeProvider):
    return GroundedStructuredVlmVisualDimensionsAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_context(tmp_path))


def test_grounded_oracle_projects_six_validated_dimensions(tmp_path) -> None:
    provider = GroundedFakeProvider()

    results = _evaluate(tmp_path, provider)

    assert len(provider.requests) == 6
    request = provider.requests[0]
    assert request.audit_id == "grounded_vlm_composition_layout_audit_oracle"
    assert request.prompt.prompt_id == "ppt-vlm-grounded-composition-layout-audit"
    assert request.prompt.version == "2.0.0"
    assert request.context["evaluation_scope"] == "SUPPLIED_RENDERED_PAGES_ONLY"
    assert request.context["sampled_page_roles"] == {"1": "SINGLE_SLIDE"}
    assert request.context["request"] == "Explain the decision clearly"
    assert request.context["audience"] == "Executive reviewers"
    assert len(results) == 6
    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert all(result.normalized_score == pytest.approx(0.82) for result in results)
    assert all(result.metadata["criterion_observability"] == "FULL" for result in results)
    assert all(result.metadata["observability_owner"] == "HARNESS" for result in results)
    assert all(result.metadata["defect_codes"] == [] for result in results)
    assert all(len(result.metadata["positive_quality_signals"]) == 2 for result in results)
    assert all(result.metadata["atomic_criterion_validated"] is True for result in results)
    assert sum(result.cost for result in results) == pytest.approx(0.024)


def test_grounded_atomic_oracle_aggregates_one_observation_per_sampled_page(
    tmp_path,
) -> None:
    slides = tuple(
        (
            {
                "kind": "text",
                "text": f"Slide {page_number}",
                "x": 600_000,
                "y": 300_000,
                "w": 8_000_000,
                "h": 800_000,
                "font_pt": 28,
            },
        )
        for page_number in range(1, 6)
    )
    deck = build_pptx(tmp_path / "five-slides.pptx", slides=slides)
    images = []
    for page_number in range(1, 6):
        image = tmp_path / f"slide-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    provider = GroundedFakeProvider()
    context = EvaluationContext(
        case=EvalCase(
            case_id="five-slide-grounded",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=load_profile(
            "configs/profiles/finished_deck_v7_grounded_visual_candidate.json"
        ),
        artifacts={"slide_images": tuple(images)},
        memo={},
    )

    results = GroundedStructuredVlmVisualDimensionsAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)
    indexed = {result.metric_id: result for result in results}
    composition = indexed["structured_vlm_composition_layout"]
    cross_slide = indexed["structured_vlm_cross_slide_consistency"]

    assert composition.metadata["sampled_pages"] == [1, 2, 3, 5]
    assert composition.metadata["observation_count"] == 4
    assert composition.metadata["page_scores"] == {
        "1": 0.82,
        "2": 0.82,
        "3": 0.82,
        "5": 0.82,
    }
    assert composition.normalized_score == pytest.approx(0.82)
    assert cross_slide.metadata["sampled_pages"] == [1, 2, 3, 4, 5]
    assert cross_slide.metadata["observation_count"] == 1


def test_grounded_prompt_encodes_aesthetic_anti_bias_boundaries() -> None:
    instructions = " ".join(
        " ".join(prompt.instructions.split())
        for prompt in GROUNDED_VLM_CRITERION_PROMPTS.values()
    )

    assert "Monochrome, minimal, and dark themes are not defects" in instructions
    assert "Do not reward decoration, gradients, icons, or image count" in instructions
    assert "Absence of defects is acceptable hygiene, not automatic excellence" in instructions
    assert "Misspelled, nonsensical, or source-garbled text is a content issue" in instructions
    assert "Never cite or claim to see an unsupplied page" in instructions


def test_grounded_oracle_normalizes_redundant_vendor_kind_labels(tmp_path) -> None:
    def use_vendor_specific_kinds(payload: dict[str, Any]) -> None:
        for item in payload["evidence"]:
            item["kind"] = f"vendor_{item['payload']['criterion_id']}"

    results = _evaluate(tmp_path, GroundedFakeProvider(use_vendor_specific_kinds))

    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert all(result.evidence[0].kind == "criterion_summary" for result in results)
    assert all(
        result.metadata["criterion_kind_normalized"] is True
        for result in results
    )


def test_grounded_oracle_caps_high_score_without_positive_evidence(tmp_path) -> None:
    def remove_positive_signals(payload: dict[str, Any]) -> None:
        payload["evidence"][0]["payload"]["positive_quality_signals"] = []

    provider = GroundedFakeProvider(remove_positive_signals)
    results = _evaluate(tmp_path, provider)

    assert len(provider.requests) == 6
    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert all(result.normalized_score == pytest.approx(0.79) for result in results)
    assert all(
        result.metadata["score_adjustments"] == ["POSITIVE_SIGNAL_CAP_0_79"]
        for result in results
    )


def test_grounded_oracle_rejects_affected_page_that_was_not_rendered(tmp_path) -> None:
    def cite_unsupplied_page(payload: dict[str, Any]) -> None:
        summary = payload["evidence"][0]
        if summary["payload"]["criterion_id"] != "composition_layout":
            return
        summary["payload"].update(
            defect_codes=["poor_visual_hierarchy"],
            affected_page_numbers=[2],
            severity="MINOR",
            criterion_score=0.70,
        )

    results = _evaluate(tmp_path, GroundedFakeProvider(cite_unsupplied_page))

    indexed = {result.metric_id: result for result in results}
    assert indexed["structured_vlm_composition_layout"].metric_status == MetricStatus.ERROR
    assert "may reference only supplied rendered pages" in (
        indexed["structured_vlm_composition_layout"].error_message or ""
    )
    assert all(
        result.metric_status == MetricStatus.SCORED
        for metric_id, result in indexed.items()
        if metric_id != "structured_vlm_composition_layout"
    )


def test_grounded_render_defect_without_bbox_degrades_only_render_metric(tmp_path) -> None:
    def add_unlocalized_render_defect(payload: dict[str, Any]) -> None:
        summary = payload["evidence"][0]
        if summary["payload"]["criterion_id"] != "render_integrity":
            return
        summary["payload"].update(
            criterion_score=0.50,
            defect_codes=["missing_glyph_boxes"],
            affected_page_numbers=[1],
            severity="MAJOR",
            positive_quality_signals=[],
        )

    results = _evaluate(tmp_path, GroundedFakeProvider(add_unlocalized_render_defect))
    indexed = {result.metric_id: result for result in results}
    render = indexed["structured_vlm_render_integrity"]

    assert render.metric_status == MetricStatus.NA
    assert render.normalized_score is None
    assert render.metadata["model_reported_score"] == pytest.approx(0.50)
    assert render.metadata["criterion_observability"] == "INSUFFICIENT"
    assert render.metadata["reason_code"] == (
        "RENDER_DEFECT_LOCALIZATION_INSUFFICIENT"
    )
    assert all(
        result.metric_status == MetricStatus.SCORED
        for metric_id, result in indexed.items()
        if metric_id != "structured_vlm_render_integrity"
    )
    assert sum(result.cost for result in results) == pytest.approx(0.024)


def test_vlm_response_rejects_page_not_present_in_uploaded_images(tmp_path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(PNG_1X1)
    sampled_pages = (1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 15)
    request = ModelAuditRequest(
        audit_id="visual-grounding-regression",
        metric_id="visual-grounding-regression",
        modality=ModelAuditModality.VLM,
        prompt=PromptSpec("test", "1.0", "Return grounded JSON."),
        case_id="case",
        scene="FINISHED_DECK",
        slides=tuple(
            {"page_number": page_number, "text": "", "objects": []}
            for page_number in range(1, 16)
        ),
        images=tuple(
            ModelImageInput.from_path(image_path, page_number=page_number)
            for page_number in sampled_pages
        ),
    )
    payload = {
        "score": 0.5,
        "confidence": 0.8,
        "model": {"provider": "fake", "model_id": "vlm", "version": "test"},
        "prompt": dict(request.prompt.reference()),
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.0},
        "evidence": [
            {
                "evidence_id": "hallucinated-page-10",
                "kind": "visual_finding",
                "message": "Claims to see an image that was never supplied.",
                "page_number": 10,
            }
        ],
    }

    with pytest.raises(ModelAuditContractError, match="not supplied as visual evidence"):
        ModelAuditResponse.from_mapping(payload, request=request)


def test_v7_profile_and_composite_are_versioned_without_overwriting_v6() -> None:
    profile = load_profile(
        "configs/profiles/finished_deck_v7_grounded_visual_candidate.json"
    )
    registry = build_default_registry(PptxAdapter(backend="ooxml"))
    descriptor = GroundedStructuredDimensionsModelAuditOracle(
        PptxAdapter(backend="ooxml")
    ).describe()

    assert profile.version == "7.0"
    assert profile.enabled_oracle_ids == (
        "baseline_ppt_quality",
        GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    )
    assert profile.metadata["production_approved"] is False
    assert profile.metadata["vlm_dimension_budget"] == {
        "structured_vlm_composition_layout": 0.20,
        "structured_vlm_typography_legibility": 0.15,
        "structured_vlm_color_contrast": 0.15,
        "structured_vlm_imagery_data_visualization": 0.25,
        "structured_vlm_cross_slide_consistency": 0.20,
        "structured_vlm_render_integrity": 0.05,
    }
    assert registry.contains(GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID)
    assert descriptor.version == "2.0.0"
    assert [metric.metric_id for metric in descriptor.metrics] == [
        "llm_content_quality_audit",
        *(f"structured_vlm_{item}" for item in STRUCTURED_VLM_VISUAL_CRITERION_IDS),
        "llm_scenario_compliance_audit",
    ]
    assert GROUNDED_VLM_DEFECT_CODES["render_integrity"] == {
        "missing_glyph_boxes",
        "corrupted_raster_or_image",
        "object_tree_content_missing_in_render",
        "visible_export_artifact",
    }


def test_benchmark_validates_atomic_visual_call_telemetry(tmp_path) -> None:
    profile = load_profile(
        "configs/profiles/finished_deck_v7_grounded_visual_candidate.json"
    )
    results = _evaluate(tmp_path, GroundedFakeProvider())
    entries = [to_primitive(result) for result in results]

    contract = _structured_projection_contract(entries, profile)

    assert contract["oracle_projection_contract_ok"] is True
    assert contract["oracle_projection_contract_failures"] == []
    assert contract["call_mode"] == "ATOMIC_CRITERION_CALLS"
    assert len(contract["request_fingerprints"]) == 6
    assert len(contract["response_fingerprints"]) == 6
    assert contract["usage_owner_metric_id"] is None
    assert contract["atomic_usage_cost_total"] == pytest.approx(0.024)
