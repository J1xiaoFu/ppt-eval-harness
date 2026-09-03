from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ppt_eval.adapters import PptxAdapter
from ppt_eval.application import EvaluationContext
from ppt_eval.config import default_profile
from ppt_eval.domain import (
    AtomicObservation,
    EvalCase,
    EvaluationScope,
    Evidence,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoreRole,
    Severity,
)
from ppt_eval.oracles.v8_composites import (
    V8AtomicObservationComposite,
    V8QualityReducerOracle,
    V8RasterTextObservationOracle,
    V8TieredVisualCriterionOracle,
    _model_routing_usage,
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
        profile=default_profile(scene),
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
        _vlm_result("structured_vlm_authorship_specificity", 0.75),
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
        "language_consistency",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity",
        "authorship_specificity_v2",
    }
    assert all("llm" not in metric_id and "vlm" not in metric_id for metric_id in indexed)
    assert indexed["composition_craft"].normalized_score <= 0.88
    assert indexed["composition_craft"].metadata["fusion_mode"] == (
        "MODEL_POSITIVE_SIGNAL_WITH_DETERMINISTIC_CAP"
    )
    assert indexed["authorship_specificity_v2"].metadata["fusion_mode"] == (
        "SINGLE_CONSTRUCT_RULE_MODEL_FUSION"
    )
    assert indexed["authorship_specificity_v2"].confidence > 0.60
    assert indexed["authorship_specificity"].score_role == ScoreRole.DIAGNOSTIC
    assert indexed["v8_functional_integrity"].multiplier == 1.0
    gate_metrics = set(
        indexed["v8_functional_integrity"].metadata["gate_eligible_metric_ids"]
    )
    assert "authorship_specificity_signals" not in gate_metrics
    assert "language_consistency" not in gate_metrics


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
    assert indexed["authorship_specificity_v2"].metric_status == MetricStatus.NA
    assert indexed["composition_craft"].normalized_score is None


def test_raster_text_model_emits_atomic_fallback_without_double_scoring(
    tmp_path: Path,
) -> None:
    deck = build_pptx(
        tmp_path / "raster-only.pptx",
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
            for _ in range(10)
        ),
    )
    images = []
    for page_number in range(1, 11):
        image = tmp_path / f"raster-slide-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    context = EvaluationContext(
        case=EvalCase(
            case_id="raster-text-recovery",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
        memo={},
    )
    deterministic = V8AtomicObservationComposite(
        PptxAdapter(backend="ooxml")
    ).evaluate(context)
    context.memo["ppt_eval.atomic_observations"] = list(
        deterministic.observations
    )
    provider = GroundedFakeProvider()
    recovered = [
        V8RasterTextObservationOracle(
            criterion_id,
            provider,
            None,
            PptxAdapter(backend="ooxml"),
        ).evaluate(context)
        for criterion_id in (
            "raster_content_structure",
            "raster_language_consistency",
        )
    ]

    assert len(provider.requests) == 2
    assert all(
        output.results[0].metric_status == MetricStatus.SCORED
        for output in recovered
    )
    recovered_observations = tuple(
        observation for output in recovered for observation in output.observations
    )
    assert {item.metric_id for item in recovered_observations} == {
        "raster_content_structure_vlm",
        "raster_language_consistency_vlm",
    }
    assert len(recovered_observations) == 20
    content_observations = tuple(
        item
        for item in recovered_observations
        if item.metric_id == "raster_content_structure_vlm"
    )
    language_observations = tuple(
        item
        for item in recovered_observations
        if item.metric_id == "raster_language_consistency_vlm"
    )
    assert sum(item.metric_status == MetricStatus.SCORED for item in content_observations) == 4
    assert sum(item.metric_status == MetricStatus.SCORED for item in language_observations) == 8
    assert all(
        item.scope == EvaluationScope.PAGE for item in recovered_observations
    )
    assert all(
        item.evidence and item.metadata["cost_accounted_by_result"]
        for item in recovered_observations
    )

    context.memo["ppt_eval.atomic_observations"].extend(recovered_observations)
    context.memo["ppt_eval.oracle_results"] = [
        *_model_results(),
        *(result for output in recovered for result in output.results),
    ]
    indexed = {
        item.metric_id: item for item in V8QualityReducerOracle().evaluate(context)
    }

    assert indexed["content_structure"].normalized_score == pytest.approx(0.82)
    assert indexed["language_consistency"].normalized_score == pytest.approx(0.82)
    assert indexed["content_structure"].metadata["fusion_mode"] == (
        "RASTER_VLM_ATOMIC_FALLBACK"
    )
    assert 0.4 <= indexed["content_structure"].metadata["observability"] < 0.5
    assert indexed["content_structure"].metadata[
        "minimum_observability"
    ] == pytest.approx(indexed["content_structure"].metadata["observability"])
    assert indexed["content_structure"].metadata["deck_page_coverage"] == pytest.approx(0.4)
    assert indexed["language_consistency"].metadata["no_double_score"] is True


def test_editable_deck_skips_raster_text_model_calls(tmp_path: Path) -> None:
    context = _context(_deck(tmp_path / "editable-no-raster-call.pptx"))
    deterministic = V8AtomicObservationComposite(
        PptxAdapter(backend="ooxml")
    ).evaluate(context)
    context.memo["ppt_eval.atomic_observations"] = list(
        deterministic.observations
    )
    provider = GroundedFakeProvider()

    output = V8RasterTextObservationOracle(
        "raster_content_structure",
        provider,
        None,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert provider.requests == []
    assert output.observations == ()
    assert output.results[0].metric_status == MetricStatus.NA
    assert output.results[0].metadata["reason_code"] == (
        "RASTER_TEXT_RECOVERY_NOT_REQUIRED"
    )


def test_raster_text_double_provider_failure_returns_legal_na(tmp_path: Path) -> None:
    class FailingProvider:
        def audit(self, request):
            del request
            raise RuntimeError("provider unavailable")

    deck = build_pptx(
        tmp_path / "raster-failure.pptx",
        (
            (
                {
                    "kind": "image",
                    "x": 0,
                    "y": 0,
                    "w": 12_192_000,
                    "h": 6_858_000,
                },
            ),
        ),
    )
    image = tmp_path / "raster-failure.png"
    image.write_bytes(PNG_1X1)
    context = EvaluationContext(
        case=EvalCase(
            case_id="raster-provider-failure",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile=default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": (image,)},
        memo={},
    )
    deterministic = V8AtomicObservationComposite(
        PptxAdapter(backend="ooxml")
    ).evaluate(context)
    context.memo["ppt_eval.atomic_observations"] = list(
        deterministic.observations
    )

    output = V8RasterTextObservationOracle(
        "raster_content_structure",
        FailingProvider(),
        FailingProvider(),
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    result = output.results[0]
    assert result.execution_status == ExecutionStatus.SUCCESS
    assert result.metric_status == MetricStatus.NA
    assert output.observations == ()
    assert result.metadata["selected_tier"] == (
        "FLASH_UNRESOLVED_ADVANCED_FAILED"
    )
    assert [item["metric_status"] for item in result.metadata["routing_attempts"]] == [
        "ERROR",
        "ERROR",
    ]
    assert not any(
        item["selected"] for item in result.metadata["routing_attempts"]
    )


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
    flash.image_transport_mode = "base64"
    flash.context_cache_enabled = True
    advanced = GroundedFakeProvider()
    advanced.image_transport_mode = "signed-url"
    advanced.context_cache_enabled = False
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
    assert result.metric_status == MetricStatus.SCORED
    assert result.metadata["advanced_rule_disagreement"] is False
    assert result.cost == pytest.approx(0.008)
    attempts = result.metadata["routing_attempts"]
    assert [item["tier"] for item in attempts] == ["FLASH", "ADVANCED"]
    assert [item["selected"] for item in attempts] == [False, True]
    assert all(item["model"]["provider"] == "fake" for item in attempts)
    assert all(item["evidence"] for item in attempts)
    assert [item["image_transport_mode"] for item in attempts] == [
        "base64",
        "signed-url",
    ]
    assert [item["context_cache_enabled"] for item in attempts] == [True, False]
    assert result.metadata["image_transport_mode"] == "signed-url"
    assert result.metadata["context_cache_enabled"] is False
    assert result.metadata["routing_usage"] == {
        "input_tokens": 240,
        "output_tokens": 160,
        "total_tokens": 400,
        "reported_cost": pytest.approx(0.008),
        "cost_known": True,
        "attempt_count": 2,
        "usage_complete": True,
    }
    assert advanced.requests[0].context["criterion_id"] == "composition_layout"


def test_model_routing_usage_preserves_partial_usage_and_unknown_cost() -> None:
    usage = _model_routing_usage(
        (
            {
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "usage_complete": False,
                "cost": 0.0,
                "cost_known": False,
            },
        )
    )

    assert usage["total_tokens"] == 15
    assert usage["usage_complete"] is False
    assert usage["cost_known"] is False


def test_model_routing_usage_aggregates_optional_visual_telemetry() -> None:
    usage = _model_routing_usage(
        (
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "image_tokens": 70,
                    "cached_tokens": 30,
                    "cache_creation_input_tokens": 10,
                    "request_bytes": 4096,
                    "cost_known": True,
                },
                "usage_complete": True,
                "cost": 0.01,
                "cost_known": True,
            },
            {
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 15,
                    "image_tokens": 50,
                    "cached_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "request_bytes": 2048,
                    "cost_known": True,
                },
                "usage_complete": True,
                "cost": 0.02,
                "cost_known": True,
            },
        )
    )

    assert usage == {
        "input_tokens": 180,
        "output_tokens": 35,
        "total_tokens": 215,
        "reported_cost": pytest.approx(0.03),
        "cost_known": True,
        "attempt_count": 2,
        "usage_complete": True,
        "image_tokens": 120,
        "cached_tokens": 50,
        "cache_creation_input_tokens": 15,
        "request_bytes": 6144,
    }


def test_model_routing_usage_omits_optional_total_if_any_attempt_omits_it() -> None:
    usage = _model_routing_usage(
        (
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "image_tokens": 70,
                    "cached_tokens": 30,
                    "cache_creation_input_tokens": 10,
                    "request_bytes": 4096,
                },
                "usage_complete": True,
                "cost": 0.01,
                "cost_known": True,
            },
            {
                "usage": {"input_tokens": 80, "output_tokens": 15},
                "usage_complete": True,
                "cost": 0.02,
                "cost_known": True,
            },
        )
    )

    assert usage["total_tokens"] == 215
    assert usage["usage_complete"] is True
    assert "image_tokens" not in usage
    assert "cached_tokens" not in usage
    assert "cache_creation_input_tokens" not in usage
    assert "request_bytes" not in usage


def test_authorship_persistent_rule_disagreement_routes_review(tmp_path: Path) -> None:
    cards = tuple(
        {
            "kind": "text",
            "text": "Core benefit and next steps",
            "x": 500_000 + (index % 2) * 5_000_000,
            "y": 500_000 + (index // 2) * 2_500_000,
            "w": 4_000_000,
            "h": 1_500_000,
            "font_pt": 20,
        }
        for index in range(4)
    )
    deck = build_pptx(
        tmp_path / "authorship-disagreement.pptx",
        (cards, tuple({**item, "text": "Our solution and next steps"} for item in cards)),
    )
    images = []
    for page_number in (1, 2):
        image = tmp_path / f"slide-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    base = _context(deck)
    context = EvaluationContext(
        case=base.case,
        profile=base.profile,
        artifacts={"slide_images": tuple(images)},
        memo={},
    )
    observations = V8AtomicObservationComposite(PptxAdapter(backend="ooxml")).evaluate(
        context
    )
    context.memo["ppt_eval.atomic_observations"] = list(observations.observations)
    result = V8TieredVisualCriterionOracle(
        "authorship_specificity",
        GroundedFakeProvider(),
        GroundedFakeProvider(),
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["selected_tier"] == "ADVANCED_RULE_DISAGREEMENT_REVIEW"
    assert result.metadata["advanced_rule_disagreement"] is True


def test_low_confidence_advanced_result_cannot_replace_flash(tmp_path: Path) -> None:
    image = tmp_path / "slide.png"
    image.write_bytes(PNG_1X1)
    image_two = tmp_path / "slide-2.png"
    image_two.write_bytes(PNG_1X1)
    base = _context(_deck(tmp_path / "advanced-confidence.pptx"))
    context = EvaluationContext(
        case=base.case,
        profile=base.profile,
        artifacts={"slide_images": (image, image_two)},
        memo={},
    )

    def set_confidence(value: float):
        def mutate(payload) -> None:
            payload["confidence"] = value
            for item in payload["evidence"]:
                item["payload"]["criterion_confidence"] = value

        return mutate

    flash = GroundedFakeProvider(set_confidence(0.50))
    advanced = GroundedFakeProvider(set_confidence(0.20))
    result = V8TieredVisualCriterionOracle(
        "composition_layout",
        flash,
        advanced,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.metadata["escalation_reason"] == "FLASH_LOW_CONFIDENCE"
    assert result.metadata["selected_tier"] == (
        "FLASH_UNRESOLVED_ADVANCED_FAILED"
    )
    assert result.metric_status == MetricStatus.NA
    assert result.confidence == pytest.approx(0.50)


def test_contestable_functional_gate_requires_isomorphic_vlm_confirmation() -> None:
    observation = AtomicObservation(
        observation_id="obs-geometry-major",
        oracle_id="v8.slide_geometry_integrity",
        metric_id="slide_geometry_integrity",
        scope=EvaluationScope.PAGE,
        unit_key="page:1",
        local_score=0.2,
        raw_value=0.2,
        confidence=0.9,
        severity=Severity.MAJOR,
        evidence=(
            Evidence(
                evidence_id="geometry-major",
                kind="out_of_bounds",
                message="Rule proposed a contestable geometry gate.",
                page_number=1,
            ),
        ),
    )
    reducer = V8QualityReducerOracle()
    unresolved = reducer._integrity_gate((observation,), ())
    rejected_model = replace(
        _vlm_result("structured_vlm_composition_layout", 0.90),
        evidence=(
            Evidence(
                evidence_id="model-unrelated",
                kind="criterion_summary",
                message="Model saw an unrelated hierarchy issue.",
                page_number=1,
                payload={
                    "defect_codes": ["poor_visual_hierarchy"],
                    "affected_page_numbers": [1],
                    "severity": "MAJOR",
                },
            ),
        ),
        metadata={
            "sampled_pages": [1],
            "total_pages": 1,
            "affected_page_numbers": [1],
            "defect_severity": "MAJOR",
            "defect_codes": ["poor_visual_hierarchy"],
        },
    )
    rejected = reducer._integrity_gate((observation,), (rejected_model,))
    confirmed_model = replace(
        rejected_model,
        normalized_score=0.45,
        raw_value=0.45,
        evidence=(
            Evidence(
                evidence_id="model-isomorphic",
                kind="criterion_summary",
                message="Model confirmed visible overflow on the candidate page.",
                page_number=1,
                payload={
                    "defect_codes": ["content_overflow_or_cutoff"],
                    "affected_page_numbers": [1],
                    "severity": "MAJOR",
                },
            ),
        ),
        metadata={
            "sampled_pages": [1],
            "total_pages": 1,
            "affected_page_numbers": [1],
            "defect_severity": "MAJOR",
            "defect_codes": ["content_overflow_or_cutoff"],
        },
    )
    confirmed = reducer._integrity_gate((observation,), (confirmed_model,))

    assert unresolved.metric_status == MetricStatus.NA
    assert unresolved.metadata["reason_code"] == "GATE_AUDIT_UNRESOLVED"
    assert rejected.multiplier == 1.0
    assert rejected.metadata["gate_verdicts"][0]["verdict"] == "REJECTED"
    assert confirmed.multiplier == 0.5
    assert confirmed.metadata["gate_verdicts"][0]["verdict"] == "CONFIRMED"


def test_gate_confirmation_requires_isomorphic_defect_on_same_candidate_page() -> None:
    candidate = AtomicObservation(
        observation_id="obs-page-21-overflow",
        oracle_id="v8.slide_geometry_integrity",
        metric_id="slide_geometry_integrity",
        scope=EvaluationScope.PAGE,
        unit_key="page:21",
        local_score=0.2,
        raw_value=0.2,
        confidence=0.9,
        severity=Severity.CRITICAL,
        critical=True,
        evidence=(
            Evidence(
                evidence_id="page-21-overflow",
                kind="out_of_bounds",
                message="Rule proposed overflow on page 21.",
                page_number=21,
            ),
        ),
    )
    model = replace(
        _vlm_result("structured_vlm_composition_layout", 0.5),
        evidence=(
            Evidence(
                evidence_id="matching-code-wrong-page",
                kind="criterion_summary",
                message="Overflow exists on another page.",
                page_number=1,
                payload={
                    "defect_codes": ["content_overflow_or_cutoff"],
                    "affected_page_numbers": [1],
                    "severity": "MAJOR",
                },
            ),
            Evidence(
                evidence_id="wrong-code-right-page",
                kind="criterion_summary",
                message="Page 21 has only an unrelated hierarchy issue.",
                page_number=21,
                payload={
                    "defect_codes": ["poor_visual_hierarchy"],
                    "affected_page_numbers": [21],
                    "severity": "MAJOR",
                },
            ),
        ),
        metadata={
            "sampled_pages": [1, 21],
            "total_pages": 26,
            "affected_page_numbers": [1, 21],
            "defect_severity": "MAJOR",
            "defect_codes": [
                "content_overflow_or_cutoff",
                "poor_visual_hierarchy",
            ],
        },
    )

    result = V8QualityReducerOracle()._integrity_gate((candidate,), (model,))

    assert result.multiplier == 1.0
    assert result.metadata["gate_verdicts"][0]["verdict"] == "REJECTED"
