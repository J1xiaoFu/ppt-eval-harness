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
    ModelUsage,
    PptxAdapter,
    PromptSpec,
)
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.config import default_profile
from ppt_eval.domain import (
    AtomicObservation,
    EvalCase,
    EvaluationScope,
    Evidence,
    MetricStatus,
    SceneType,
    Severity,
)
from ppt_eval.oracles import (
    GROUNDED_VLM_DEFECT_CODES,
    GROUNDED_VLM_POSITIVE_SIGNALS,
    V8_GROUNDED_VISUAL_CRITERION_IDS,
    V8_GROUNDED_VLM_CRITERION_PROMPTS,
    GroundedSingleCriterionVlmOracle,
)
from ppt_eval.oracles.model_audits import _sum_model_usage
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

BASE_VISUAL_CRITERIA = tuple(
    item
    for item in V8_GROUNDED_VISUAL_CRITERION_IDS
    if item != "authorship_specificity"
)


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
        if criterion_id in {"cross_slide_consistency", "authorship_specificity"}
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
            "model_id": "qwen3.8-flash",
            "version": "test",
        },
        "prompt": dict(request.prompt.reference()),
        "usage": {"input_tokens": 120, "output_tokens": 80, "cost": 0.004},
        "evidence": evidence,
    }


def _single_page_context(tmp_path) -> EvaluationContext:
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
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": (image,)},
        memo={},
    )


def _evaluate_current_visual_criteria(
    tmp_path,
    provider: GroundedFakeProvider,
):
    context = _single_page_context(tmp_path)
    return tuple(
        GroundedSingleCriterionVlmOracle(
            criterion_id,
            provider,
            PptxAdapter(backend="ooxml"),
        ).evaluate(context)
        for criterion_id in BASE_VISUAL_CRITERIA
    )


def _multi_page_context(
    tmp_path,
    *,
    page_count: int,
    observations: tuple[AtomicObservation, ...],
) -> EvaluationContext:
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
        for page_number in range(1, page_count + 1)
    )
    deck = build_pptx(tmp_path / "multi-page.pptx", slides=slides)
    images = []
    for page_number in range(1, page_count + 1):
        image = tmp_path / f"slide-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    return EvaluationContext(
        case=EvalCase(
            case_id="multi-page-grounded",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
        memo={"ppt_eval.atomic_observations": observations},
    )


def _rule_observation(
    metric_id: str,
    page_number: int,
    *,
    severity: Severity = Severity.CRITICAL,
    critical: bool = True,
    key_unit: bool = True,
    suffix: str = "",
) -> AtomicObservation:
    return AtomicObservation(
        observation_id=f"obs-{metric_id}-{page_number}{suffix}",
        oracle_id=f"v8.{metric_id}",
        metric_id=metric_id,
        scope=EvaluationScope.PAGE,
        unit_key=f"page:{page_number}",
        local_score=0.2,
        raw_value=0.2,
        confidence=0.95,
        severity=severity,
        critical=critical,
        key_unit=key_unit,
        evidence=(
            Evidence(
                evidence_id=f"evidence-{metric_id}-{page_number}{suffix}",
                kind="rule_candidate",
                message=f"Page {page_number} requires visual confirmation.",
                page_number=page_number,
            ),
        ),
    )


def test_major_contestable_gate_page_keeps_v83_budgeted_risk_sampling(
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
        for page_number in range(1, 27)
    )
    deck = build_pptx(tmp_path / "gate-risk-slide-21.pptx", slides=slides)
    images = []
    for page_number in range(1, 27):
        image = tmp_path / f"slide-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    candidate = AtomicObservation(
        observation_id="obs-page-21-out-of-bounds",
        oracle_id="v8.slide_geometry_integrity",
        metric_id="slide_geometry_integrity",
        scope=EvaluationScope.PAGE,
        unit_key="page:21",
        local_score=0.2,
        raw_value=0.2,
        confidence=0.95,
        severity=Severity.MAJOR,
        critical=False,
        key_unit=True,
        evidence=(
            Evidence(
                evidence_id="page-21-out-of-bounds",
                kind="out_of_bounds",
                message="Page 21 geometry requires visual confirmation.",
                page_number=21,
            ),
        ),
    )
    provider = GroundedFakeProvider()
    context = EvaluationContext(
        case=EvalCase(
            case_id="gate-risk-slide-21",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
        memo={"ppt_eval.atomic_observations": [candidate]},
    )

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["sampled_pages"] == [1, 9, 21, 26]
    assert result.metadata["sampling_limit"] == 4
    assert result.metadata["sampling_strategy"] == (
        "contestable_gate_risk_role_and_exploration"
    )
    assert result.metadata["base_sampled_pages"] == [1, 9, 21, 26]
    assert result.metadata["forced_rule_pages"] == []
    assert result.metadata["forced_overflow_count"] == 0
    assert result.metadata["sampling_limit_extended_by_forced_pages"] is False
    assert result.metadata["selection_reason"] == "BASE_SAMPLE_ONLY"
    assert result.metadata["sampling_strategy_version"] == "2.0.0"
    assert provider.requests[0].context["sampled_page_roles"] == {
        "1": "COVER_OR_OPENING",
        "9": "BODY",
        "21": "BODY",
        "26": "ENDING_OR_APPENDIX",
    }


def test_critical_rule_page_extends_sampling_without_displacing_base_pages(
    tmp_path,
) -> None:
    context = _multi_page_context(
        tmp_path,
        page_count=26,
        observations=(
            _rule_observation(
                "slide_geometry_integrity",
                21,
                critical=False,
                key_unit=False,
            ),
        ),
    )
    provider = GroundedFakeProvider()

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["base_sampled_pages"] == [1, 9, 17, 26]
    assert result.metadata["forced_rule_pages"] == [21]
    assert result.metadata["sampled_pages"] == [1, 9, 17, 21, 26]
    assert result.metadata["sampling_limit"] == 4
    assert result.metadata["forced_overflow_pages"] == [21]
    assert result.metadata["forced_overflow_count"] == 1
    assert result.metadata["sampling_limit_extended_by_forced_pages"] is True
    assert result.metadata["sampling_limit_semantics"] == (
        "BASE_EXPLORATION_BUDGET_EXCLUDES_FORCED_RULE_PAGES"
    )
    assert result.metadata["sampling_limit_is_total_page_cap"] is False
    assert result.metadata["effective_sample_count"] == 5
    assert result.metadata["sampling_strategy"] == (
        "forced_rule_critical_plus_risk_role_and_exploration"
    )
    assert result.metadata["selection_reason"] == (
        "BASE_SAMPLE_PLUS_ISOMORPHIC_RULE_CRITICAL"
    )
    assert result.metadata["page_selection_reasons"]["21"] == [
        "FORCED_ISOMORPHIC_RULE_CRITICAL"
    ]
    assert result.metadata["forced_rule_metrics_by_page"] == {
        "21": ["slide_geometry_integrity"]
    }
    assert [image.page_number for image in provider.requests[0].images] == [
        1,
        9,
        17,
        21,
        26,
    ]


def test_all_critical_rule_pages_survive_overflow_and_are_deduplicated(
    tmp_path,
) -> None:
    forced_pages = (3, 7, 11, 15, 19, 23)
    observations = tuple(
        _rule_observation(
            "slide_geometry_integrity",
            page_number,
            critical=page_number in {3, 11, 19},
        )
        for page_number in forced_pages
    ) + (
        _rule_observation(
            "slide_geometry_integrity",
            7,
            critical=False,
            suffix="-duplicate",
        ),
    )
    context = _multi_page_context(
        tmp_path,
        page_count=30,
        observations=observations,
    )
    provider = GroundedFakeProvider()

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metadata["base_sampled_pages"] == [1, 10, 20, 30]
    assert result.metadata["forced_rule_pages"] == list(forced_pages)
    assert result.metadata["sampled_pages"] == [
        1,
        3,
        7,
        10,
        11,
        15,
        19,
        20,
        23,
        30,
    ]
    assert result.metadata["forced_overflow_count"] == 6
    assert result.metadata["effective_sample_count"] == 10
    assert len(provider.requests[0].images) == 10


def test_critical_rule_pages_are_forced_only_into_isomorphic_criterion(
    tmp_path,
) -> None:
    criterion_cases = (
        ("composition_layout", "slide_geometry_integrity", 3),
        ("typography_legibility", "slide_typography_functional", 5),
        ("color_contrast", "slide_pixel_contrast", 9),
        ("imagery_data_visualization", "effective_image_resolution", 11),
    )
    rule_pages = {
        "slide_geometry_integrity": 3,
        "slide_typography_functional": 5,
        "slide_pixel_contrast": 9,
        "effective_image_resolution": 11,
    }
    observations = tuple(
        _rule_observation(metric_id, page_number, critical=False)
        for metric_id, page_number in rule_pages.items()
    )

    for criterion_id, owned_metric_id, forced_page in criterion_cases:
        criterion_path = tmp_path / criterion_id
        criterion_path.mkdir()
        context = _multi_page_context(
            criterion_path,
            page_count=20,
            observations=observations,
        )
        provider = GroundedFakeProvider()

        result = GroundedSingleCriterionVlmOracle(
            criterion_id,
            provider,
            PptxAdapter(backend="ooxml"),
        ).evaluate(context)

        assert result.metadata["forced_rule_pages"] == [forced_page]
        assert result.metadata["forced_rule_metrics_by_page"] == {
            str(forced_page): [owned_metric_id]
        }
        assert all(
            page_number not in result.metadata["forced_rule_pages"]
            for metric_id, page_number in rule_pages.items()
            if metric_id != owned_metric_id
        )


def test_unmapped_visual_criterion_ignores_unrelated_critical_rules(
    tmp_path,
) -> None:
    context = _multi_page_context(
        tmp_path,
        page_count=20,
        observations=(
            _rule_observation("slide_geometry_integrity", 3),
            _rule_observation("slide_typography_functional", 5),
            _rule_observation("slide_pixel_contrast", 9),
            _rule_observation("effective_image_resolution", 11),
        ),
    )
    provider = GroundedFakeProvider()

    result = GroundedSingleCriterionVlmOracle(
        "cross_slide_consistency",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metadata["forced_rule_pages"] == []
    assert result.metadata["sampling_limit"] == 8
    assert result.metadata["sampled_pages"] == [1, 3, 6, 9, 11, 14, 17, 20]
    assert result.metadata["selection_reason"] == "BASE_SAMPLE_ONLY"


def test_grounded_prompt_encodes_aesthetic_anti_bias_boundaries() -> None:
    instructions = " ".join(
        " ".join(prompt.instructions.split())
        for criterion_id, prompt in V8_GROUNDED_VLM_CRITERION_PROMPTS.items()
        if criterion_id in BASE_VISUAL_CRITERIA
    )

    assert "Monochrome, minimal, and dark themes are not defects" in instructions
    assert "Do not reward decoration, gradients, icons, or image count" in instructions
    assert "Absence of defects is acceptable hygiene, not automatic excellence" in instructions
    assert "Misspelled, nonsensical, or source-garbled text is a content issue" in instructions
    assert "Never cite or claim to see an unsupplied page" in instructions


def test_v8_authorship_is_a_seventh_isomorphic_criterion() -> None:
    assert len(BASE_VISUAL_CRITERIA) == 6
    assert V8_GROUNDED_VISUAL_CRITERION_IDS == (
        *BASE_VISUAL_CRITERIA,
        "authorship_specificity",
    )
    assert set(BASE_VISUAL_CRITERIA).issubset(V8_GROUNDED_VLM_CRITERION_PROMPTS)
    prompt = V8_GROUNDED_VLM_CRITERION_PROMPTS["authorship_specificity"]

    assert prompt.version == "2.1.0"
    assert "Do not infer whether AI produced the deck" in prompt.instructions
    assert "one appropriate taxonomy/checklist/process layout" in prompt.instructions
    assert "systemic across at least two supplied pages" in prompt.instructions
    assert "mechanical_cardization" in GROUNDED_VLM_DEFECT_CODES[
        "authorship_specificity"
    ]
    assert "functional_visual_encoding" in GROUNDED_VLM_POSITIVE_SIGNALS[
        "authorship_specificity"
    ]


def test_v8_authorship_abstains_on_a_single_slide_without_provider_cost(
    tmp_path,
) -> None:
    provider = GroundedFakeProvider()
    result = GroundedSingleCriterionVlmOracle(
        "authorship_specificity",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_single_page_context(tmp_path))

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["reason_code"] == "AUTHORSHIP_SYSTEMIC_SCOPE_UNOBSERVABLE"
    assert provider.requests == []


def test_grounded_oracle_normalizes_redundant_vendor_kind_labels(tmp_path) -> None:
    def use_vendor_specific_kinds(payload: dict[str, Any]) -> None:
        for item in payload["evidence"]:
            item["kind"] = f"vendor_{item['payload']['criterion_id']}"

    results = _evaluate_current_visual_criteria(
        tmp_path, GroundedFakeProvider(use_vendor_specific_kinds)
    )

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
    results = _evaluate_current_visual_criteria(tmp_path, provider)

    assert len(provider.requests) == 6
    assert all(result.metric_status == MetricStatus.SCORED for result in results)
    assert all(result.normalized_score == pytest.approx(0.79) for result in results)
    assert all(
        result.metadata["score_adjustments"] == ["POSITIVE_SIGNAL_CAP_0_79"]
        for result in results
    )


def test_grounded_oracle_preserves_extended_usage_in_routing_metadata(
    tmp_path,
) -> None:
    def add_usage_telemetry(payload: dict[str, Any]) -> None:
        payload["usage"].update(
            image_tokens=96,
            cached_tokens=48,
            cache_creation_input_tokens=24,
            request_bytes=4096,
            cost_known=True,
        )

    context = _single_page_context(tmp_path)
    provider = GroundedFakeProvider(add_usage_telemetry)
    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["usage"] == {
        "input_tokens": 120,
        "output_tokens": 80,
        "total_tokens": 200,
        "cost": 0.004,
        "image_tokens": 96,
        "cached_tokens": 48,
        "cache_creation_input_tokens": 24,
        "request_bytes": 4096,
        "cost_known": True,
    }


def test_grounded_oracle_records_non_sensitive_provider_runtime_metadata(
    tmp_path,
) -> None:
    context = _single_page_context(tmp_path)
    provider = GroundedFakeProvider()
    provider.image_transport_mode = "signed-url"
    provider.context_cache_enabled = True

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["image_transport_mode"] == "signed-url"
    assert result.metadata["context_cache_enabled"] is True


def test_criterion_retry_aggregates_extended_usage_telemetry(tmp_path) -> None:
    class RetryProvider:
        def __init__(self) -> None:
            self.call_count = 0

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.call_count += 1
            payload = _grounded_response(request)
            payload["usage"].update(
                image_tokens=10 * self.call_count,
                cached_tokens=4 * self.call_count,
                cache_creation_input_tokens=2 * self.call_count,
                request_bytes=100 * self.call_count,
                cost_known=True,
            )
            if self.call_count == 1:
                payload["evidence"][0]["payload"].pop(
                    "positive_quality_signals"
                )
            return payload

    context = _single_page_context(tmp_path)
    provider = RetryProvider()
    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert provider.call_count == 2
    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["criterion_retry_count"] == 1
    assert result.metadata["usage"] == {
        "input_tokens": 240,
        "output_tokens": 160,
        "total_tokens": 400,
        "cost": 0.008,
        "image_tokens": 30,
        "cached_tokens": 12,
        "cache_creation_input_tokens": 6,
        "request_bytes": 300,
        "cost_known": True,
    }


def test_sum_model_usage_omits_optional_total_if_any_attempt_omits_it() -> None:
    first = ModelUsage(
        input_tokens=100,
        output_tokens=20,
        cost=0.01,
        image_tokens=70,
        cached_tokens=30,
        cache_creation_input_tokens=10,
        request_bytes=4096,
        cost_known=True,
    )
    second = ModelUsage(
        input_tokens=80,
        output_tokens=15,
        cost=0.02,
        cost_known=True,
    )

    usage = _sum_model_usage(first, second).to_mapping()

    assert usage["input_tokens"] == 180
    assert usage["output_tokens"] == 35
    assert usage["cost"] == pytest.approx(0.03)
    assert usage["cost_known"] is True
    assert "image_tokens" not in usage
    assert "cached_tokens" not in usage
    assert "cache_creation_input_tokens" not in usage
    assert "request_bytes" not in usage


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

    results = _evaluate_current_visual_criteria(
        tmp_path, GroundedFakeProvider(cite_unsupplied_page)
    )

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

    results = _evaluate_current_visual_criteria(
        tmp_path, GroundedFakeProvider(add_unlocalized_render_defect)
    )
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
