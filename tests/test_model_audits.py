from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ppt_eval.adapters import ModelAuditModality, ModelAuditRequest, PptxAdapter
from ppt_eval.application import DagScheduler, RunSupervisor
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.config import default_profile, load_profile, profile_from_mapping
from ppt_eval.domain import (
    CONSTRUCT_WEIGHTED_MEAN,
    CoverageStatus,
    EvalCase,
    EvalProfile,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    SceneType,
)
from ppt_eval.oracles import (
    HighCostModelAuditOracle,
    LlmContentQualityAuditOracle,
    VlmVisualQualityAuditOracle,
    build_default_registry,
)
from ppt_eval.scoring import PptPdmsAggregator
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

MODEL_METRICS = {
    "llm_content_quality_audit",
    "vlm_visual_quality_audit",
    "llm_scenario_compliance_audit",
}


class FakeProvider:
    def __init__(self, scores: Mapping[str, float] | None = None) -> None:
        self.scores = dict(scores or {})
        self.requests: list[ModelAuditRequest] = []
        self.responses: list[Mapping[str, Any]] = []

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        response = valid_response(
            request,
            score=self.scores.get(request.metric_id, 0.82),
        )
        self.responses.append(response)
        return response


class RaisingProvider:
    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        raise TimeoutError("provider timed out")


class StaticPayloadProvider:
    def __init__(self, mutate) -> None:
        self.mutate = mutate

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        payload = deepcopy(valid_response(request))
        self.mutate(payload)
        return payload


def valid_response(
    request: ModelAuditRequest,
    *,
    score: float = 0.82,
) -> Mapping[str, Any]:
    slide = request.slides[0]
    objects = slide.get("objects", ())
    finding: dict[str, Any] = {
        "evidence_id": f"model-{request.metric_id}-slide-1",
        "kind": "model_audit_finding",
        "message": "The first slide supports the model audit conclusion.",
        "page_number": 1,
        "payload": {"criterion": request.metric_id},
    }
    if objects:
        finding["object_id"] = objects[0]["object_id"]
        finding["bbox"] = objects[0]["bbox"]
    return {
        "score": score,
        "confidence": 0.88,
        "model": {
            "provider": "fake",
            "model_id": "audit-model",
            "version": "2026-08-26",
        },
        "prompt": dict(request.prompt.reference()),
        "usage": {"input_tokens": 120, "output_tokens": 30, "cost": 0.001},
        "evidence": [finding],
    }


def context_for(
    deck,
    scene: SceneType,
    *,
    artifacts: Mapping[str, Any] | None = None,
) -> EvaluationContext:
    case = EvalCase(
        case_id=f"case-{scene.value}",
        scene=scene,
        pptx_path=str(deck),
        request="Create a concise project update for executives.",
        audience="executives",
        source_materials=("Revenue increased to 120 in Q2.",),
        assets=("required-product-image.png",),
    )
    return EvaluationContext(
        case=case,
        profile=default_profile(scene),
        artifacts=artifacts or {},
        memo={},
    )


def test_unconfigured_model_composite_returns_auditable_optional_na(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    results = HighCostModelAuditOracle(PptxAdapter(backend="ooxml")).evaluate(
        context_for(deck, SceneType.READY_MADE)
    )

    assert {item.metric_id for item in results} == MODEL_METRICS
    assert all(item.execution_status == ExecutionStatus.SUCCESS for item in results)
    assert all(item.metric_status == MetricStatus.NA for item in results)
    by_metric = {item.metric_id: item for item in results}
    assert by_metric["llm_content_quality_audit"].metadata["reason_code"] == (
        "MODEL_PROVIDER_UNCONFIGURED"
    )
    assert by_metric["vlm_visual_quality_audit"].evidence
    assert by_metric["llm_scenario_compliance_audit"].metadata["reason_code"] == (
        "SCENE_NOT_APPLICABLE"
    )


def test_fake_llm_and_vlm_providers_produce_scored_grounded_metadata(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    llm = FakeProvider({"llm_content_quality_audit": 0.75, "llm_scenario_compliance_audit": 0.9})
    vlm = FakeProvider({"vlm_visual_quality_audit": 0.65})
    oracle = HighCostModelAuditOracle(
        PptxAdapter(backend="ooxml"),
        llm_provider=llm,
        vlm_provider=vlm,
    )

    results = oracle.evaluate(
        context_for(
            deck,
            SceneType.TEXT_TO_PPT,
            artifacts={"slide_images": (image,)},
        )
    )

    assert [item.metric_id for item in results] == [
        "llm_content_quality_audit",
        "vlm_visual_quality_audit",
        "llm_scenario_compliance_audit",
    ]
    assert all(item.metric_status == MetricStatus.SCORED for item in results)
    assert [item.normalized_score for item in results] == [0.75, 0.65, 0.9]
    assert all(item.confidence == 0.88 for item in results)
    assert all(item.cost == 0.001 for item in results)
    assert all(item.evidence[0].page_number == 1 for item in results)
    assert all(item.metadata["model"]["provider"] == "fake" for item in results)
    assert all(item.metadata["prompt"]["sha256"] for item in results)
    assert all(item.metadata["usage"]["total_tokens"] == 150 for item in results)
    assert all(item.metadata["request_fingerprint"] for item in results)
    assert all(len(item.metadata["response_fingerprint"]) == 64 for item in results)
    assert len(llm.requests) == 2 and len(vlm.requests) == 1
    assert vlm.requests[0].images[0].sha256
    assert all(request.context["input_trust"] == "UNTRUSTED_DATA" for request in llm.requests)
    canonical = json.dumps(
        llm.responses[0],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert results[0].metadata["response_fingerprint"] == hashlib.sha256(
        canonical
    ).hexdigest()


def test_raster_only_content_uses_rendered_semantic_fallback(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "raster-only.pptx",
        (
            (
                {
                    "kind": "image",
                    "x": 0,
                    "y": 0,
                    "w": 12_192_000,
                    "h": 6_858_000,
                    "alt": "rendered slide",
                },
            ),
        ),
    )
    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    text_provider = FakeProvider()
    visual_provider = FakeProvider({"llm_content_quality_audit": 0.73})
    oracle = LlmContentQualityAuditOracle(
        text_provider,
        PptxAdapter(backend="ooxml"),
        visual_fallback_provider=visual_provider,
    )

    result = oracle.evaluate(
        context_for(
            deck,
            SceneType.READY_MADE,
            artifacts={"slide_images": (image,)},
        )
    )

    assert result.metric_status == MetricStatus.SCORED
    assert result.normalized_score == 0.73
    assert text_provider.requests == []
    assert len(visual_provider.requests) == 1
    request = visual_provider.requests[0]
    assert request.modality == ModelAuditModality.VLM
    assert request.prompt.prompt_id == "ppt-vlm-semantic-content-recovery-audit"
    assert result.metadata["content_input_mode"] == "RENDERED_SEMANTIC_FALLBACK"
    assert result.metadata["text_page_ratio"] == 0.0
    assert result.metadata["sampled_pages"] == [1]


def test_raster_only_content_without_visual_provider_is_explicit_na(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "raster-no-provider.pptx",
        (({"kind": "image", "x": 0, "y": 0, "w": 1_000_000, "h": 1_000_000},),),
    )
    text_provider = FakeProvider()
    result = LlmContentQualityAuditOracle(
        text_provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context_for(deck, SceneType.READY_MADE))

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["reason_code"] == "SEMANTIC_INPUT_UNOBSERVABLE"
    assert text_provider.requests == []


def test_configured_vlm_without_complete_rendered_slides_is_optional_na(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "two-slides.pptx",
        (
            ({"kind": "text", "text": "Slide one"},),
            ({"kind": "text", "text": "Slide two"},),
        ),
    )
    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    provider = FakeProvider()

    result = VlmVisualQualityAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(
        context_for(
            deck,
            SceneType.READY_MADE,
            artifacts={"slide_images": (image,)},
        )
    )

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["reason_code"] == "RENDERED_SLIDES_INCOMPLETE"
    assert provider.requests == []


def test_long_deck_vlm_upload_is_deterministically_sampled(tmp_path) -> None:
    slide_count = 15
    deck = build_pptx(
        tmp_path / "long-deck.pptx",
        tuple(
            ({"kind": "text", "text": f"Slide {page}"},)
            for page in range(1, slide_count + 1)
        ),
    )
    images = []
    for page in range(1, slide_count + 1):
        image = tmp_path / f"slide-{page}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    provider = FakeProvider()

    result = VlmVisualQualityAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(
        context_for(
            deck,
            SceneType.READY_MADE,
            artifacts={"slide_images": tuple(images)},
        )
    )

    request = provider.requests[0]
    sampled_pages = [image.page_number for image in request.images]
    assert result.metric_status == MetricStatus.SCORED
    assert sampled_pages == [1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 15]
    assert request.context["total_pages"] == slide_count
    assert request.context["sampled_pages"] == sampled_pages
    assert request.context["sampling_strategy"] == "deterministic_even_coverage"
    assert result.metadata["total_pages"] == slide_count
    assert result.metadata["sampled_pages"] == sampled_pages

    # The exact same canonical subset is also accepted when a caller has
    # pre-sampled a long deck; arbitrary partial coverage is rejected above.
    canonical_images = tuple(images[page - 1] for page in sampled_pages)
    sampled_provider = FakeProvider()
    sampled_result = VlmVisualQualityAuditOracle(
        sampled_provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(
        context_for(
            deck,
            SceneType.READY_MADE,
            artifacts={
                "slide_images": tuple(
                    {
                        "page_number": page,
                        "path": path,
                    }
                    for page, path in zip(sampled_pages, canonical_images, strict=True)
                )
            },
        )
    )
    assert sampled_result.metric_status == MetricStatus.SCORED
    assert [
        image.page_number for image in sampled_provider.requests[0].images
    ] == sampled_pages


def test_provider_exception_is_execution_error_not_quality_failure(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    result = LlmContentQualityAuditOracle(
        RaisingProvider(),
        PptxAdapter(backend="ooxml"),
    ).evaluate(context_for(deck, SceneType.READY_MADE))

    assert result.execution_status == ExecutionStatus.ERROR
    assert result.metric_status == MetricStatus.ERROR
    assert result.normalized_score is None
    assert result.error_code == "MODEL_PROVIDER_ERROR"
    assert "TimeoutError" in (result.error_message or "")
    assert result.metadata["request_fingerprint"]


def test_provider_response_contract_rejects_invalid_required_fields(tmp_path) -> None:
    cases = (
        (lambda payload: payload.update(score=1.1), "score must be between"),
        (lambda payload: payload.update(confidence=float("nan")), "confidence must be a finite"),
        (lambda payload: payload["model"].pop("version"), "model is missing required"),
        (lambda payload: payload["prompt"].update(sha256="0" * 64), "does not match"),
        (lambda payload: payload["usage"].update(input_tokens=-1), "non-negative integer"),
        (lambda payload: payload.update(evidence=[]), "at least one item"),
        (
            lambda payload: payload["evidence"][0].update(page_number=999),
            "exceeds slide count",
        ),
        (
            lambda payload: payload.update(decision="PASS"),
            "contains unknown fields: decision",
        ),
    )
    deck = build_pptx(tmp_path / "deck.pptx")
    for mutate, expected_message in cases:
        result = LlmContentQualityAuditOracle(
            StaticPayloadProvider(mutate),
            PptxAdapter(backend="ooxml"),
        ).evaluate(context_for(deck, SceneType.READY_MADE))

        assert result.execution_status == ExecutionStatus.ERROR
        assert result.metric_status == MetricStatus.ERROR
        assert result.error_code == "MODEL_RESPONSE_INVALID"
        assert expected_message in (result.error_message or "")


def test_four_v3_default_profiles_score_flash_and_declare_escalation_route(
    tmp_path,
) -> None:
    profile_names = {
        SceneType.TEXT_TO_PPT: "text_generation_v3.json",
        SceneType.PROJECT_SUMMARY: "project_summary_v3.json",
        SceneType.MULTIMODAL: "multimodal_generation_v3.json",
        SceneType.READY_MADE: "finished_deck_v3.json",
    }
    for scene in SceneType:
        profile = load_profile(Path("configs/profiles") / profile_names[scene])
        fallback = default_profile(scene, root=tmp_path / "missing-profiles")

        assert profile.profile_id.endswith("-v3")
        assert profile.base_weights == fallback.base_weights
        assert profile.scene_weights == fallback.scene_weights
        assert profile.required_metric_ids == fallback.required_metric_ids
        assert profile.metric_review_thresholds == fallback.metric_review_thresholds
        for candidate in (profile, fallback):
            assert candidate.version == "3.1"
            assert "high_cost.model_audits" in candidate.enabled_oracle_ids
            assert candidate.base_weights["template_residue"] == 0.08
            assert candidate.base_weights["llm_content_quality_audit"] == 0.08
            assert candidate.base_weights["vlm_visual_quality_audit"] == 0.12
            assert "template_residue" in (candidate.required_metric_ids or ())
            assert "llm_content_quality_audit" in (
                candidate.required_metric_ids or ()
            )
            assert "vlm_visual_quality_audit" in (
                candidate.required_metric_ids or ()
            )
            assert candidate.metric_review_thresholds["template_residue"] == 0.85
            assert candidate.metric_review_thresholds["layout"] == 0.65
            assert candidate.metric_review_thresholds["typography"] == 0.70
            assert (
                candidate.metric_review_thresholds["llm_content_quality_audit"]
                == 0.70
            )
            assert (
                candidate.metric_review_thresholds["vlm_visual_quality_audit"]
                == 0.70
            )
            assert candidate.metadata["lifecycle"] == "PRE_RESEARCH"
            assert (
                candidate.metadata["model_audit_routing"]
                == "FLASH_ADVANCED_HUMAN"
            )
            assert candidate.metadata["flash_model"] == "qwen3.7-flash"
            assert candidate.metadata["advanced_model"] == "qwen3.8-flash"

            if scene == SceneType.READY_MADE:
                assert "llm_scenario_compliance_audit" not in candidate.scene_weights
                assert "llm_scenario_compliance_audit" not in (
                    candidate.required_metric_ids or ()
                )
                assert "multimedia_quality" not in (
                    candidate.required_metric_ids or ()
                )
            else:
                assert (
                    candidate.scene_weights["llm_scenario_compliance_audit"]
                    == 0.10
                )
                assert "llm_scenario_compliance_audit" in (
                    candidate.required_metric_ids or ()
                )
                assert (
                    candidate.metric_review_thresholds[
                        "llm_scenario_compliance_audit"
                    ]
                    == 0.70
                )


def test_explicit_v1_default_profile_keeps_historical_pre_model_semantics() -> None:
    for scene in SceneType:
        profile = EvalProfile.default(scene, version="1.0")

        assert MODEL_METRICS.isdisjoint(profile.base_weights)
        assert MODEL_METRICS.isdisjoint(profile.scene_weights)
        assert "high_cost.model_audits" not in profile.enabled_oracle_ids


def test_optional_oracle_execution_is_versioned_for_v1_replay() -> None:
    payload = {
        "profile_id": "compatibility-test",
        "scenario": "FINISHED_DECK",
        "required_oracles": ["baseline_ppt_quality"],
        "optional_oracles": ["high_cost.model_audits"],
    }

    v1 = profile_from_mapping({**payload, "version": "1.0"})
    v2 = profile_from_mapping({**payload, "version": "2.0"})

    assert v1.enabled_oracle_ids == ("baseline_ppt_quality",)
    assert v1.metadata["optional_oracles_executed"] is False
    assert v2.enabled_oracle_ids == (
        "baseline_ppt_quality",
        "high_cost.model_audits",
    )
    assert v2.metadata["optional_oracles_executed"] is True


def test_minimal_v3_mapping_inherits_tiered_routing_metadata() -> None:
    profile = profile_from_mapping(
        {
            "profile_id": "minimal-v3",
            "version": "3.0",
            "scenario": "FINISHED_DECK",
            "metadata": {"owner": "local-research"},
        }
    )

    assert profile.metadata["model_audit_routing"] == "FLASH_PLUS_HUMAN"
    assert profile.metadata["lifecycle"] == "PRE_RESEARCH"
    assert profile.metadata["flash_model"] == "qwen3.7-flash"
    assert profile.metadata["plus_model"] == "qwen3.7-plus"
    assert profile.metadata["owner"] == "local-research"


def test_v1_profile_files_remain_explicitly_loadable_for_replay() -> None:
    legacy_paths = {
        SceneType.READY_MADE: "configs/profiles/finished_deck.json",
        SceneType.TEXT_TO_PPT: "configs/profiles/text_generation.json",
        SceneType.PROJECT_SUMMARY: "configs/profiles/project_summary.json",
        SceneType.MULTIMODAL: "configs/profiles/multimodal_generation.json",
    }
    for scene, path in legacy_paths.items():
        profile = load_profile(path)

        assert profile.scene == scene
        assert profile.version == "1.0"
        assert profile.profile_id.endswith("-v1")
        assert "high_cost.model_audits" not in profile.enabled_oracle_ids
        assert MODEL_METRICS.isdisjoint(profile.base_weights)
        assert MODEL_METRICS.isdisjoint(profile.scene_weights)
        if scene == SceneType.READY_MADE:
            assert "multimedia_quality" not in (profile.required_metric_ids or ())


def test_v2_profile_files_remain_explicitly_loadable_in_shadow_mode() -> None:
    v2_paths = {
        SceneType.READY_MADE: "configs/profiles/finished_deck_v2.json",
        SceneType.TEXT_TO_PPT: "configs/profiles/text_generation_v2.json",
        SceneType.PROJECT_SUMMARY: "configs/profiles/project_summary_v2.json",
        SceneType.MULTIMODAL: "configs/profiles/multimodal_generation_v2.json",
    }
    v1_paths = {
        SceneType.READY_MADE: "configs/profiles/finished_deck.json",
        SceneType.TEXT_TO_PPT: "configs/profiles/text_generation.json",
        SceneType.PROJECT_SUMMARY: "configs/profiles/project_summary.json",
        SceneType.MULTIMODAL: "configs/profiles/multimodal_generation.json",
    }

    for scene in SceneType:
        v1 = load_profile(v1_paths[scene])
        v2 = load_profile(v2_paths[scene])

        assert v2.scene == scene
        assert v2.version == "2.0"
        assert v2.profile_id.endswith("-v2")
        assert v2.base_weights == v1.base_weights
        assert v2.scene_weights == v1.scene_weights
        assert "high_cost.model_audits" in v2.enabled_oracle_ids
        assert MODEL_METRICS.isdisjoint(v2.base_weights)
        assert MODEL_METRICS.isdisjoint(v2.scene_weights)
        assert MODEL_METRICS.isdisjoint(v2.required_metric_ids or ())
        assert v2.metric_review_thresholds == {}


def test_v3_flash_scores_affect_formula_while_v1_replay_stays_available(
    tmp_path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    case = EvalCase(
        case_id="ready",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )
    profile = load_profile("configs/profiles/finished_deck_v3.json")
    adapter = PptxAdapter(backend="ooxml")

    without_provider = RunSupervisor(
        DagScheduler(build_default_registry(adapter))
    ).run(case, profile)

    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    low_model = FakeProvider(
        {
            "llm_content_quality_audit": 0.10,
            "vlm_visual_quality_audit": 0.10,
        }
    )
    with_provider = RunSupervisor(
        DagScheduler(
            build_default_registry(
                adapter,
                llm_provider=low_model,
                vlm_provider=low_model,
            )
        )
    ).run(case, profile, artifacts={"slide_images": (image,)})

    legacy_profile = load_profile("configs/profiles/finished_deck.json")
    legacy = RunSupervisor(
        DagScheduler(build_default_registry(adapter))
    ).run(case, legacy_profile)

    assert without_provider.report.coverage == CoverageStatus.DEGRADED
    assert without_provider.report.base_score is not None
    assert without_provider.report.full_score is None
    assert without_provider.report.decision == EvaluationDecision.REVIEW
    assert with_provider.report.coverage == CoverageStatus.FULL
    assert with_provider.report.full_score is not None
    assert legacy.report.full_score is not None
    assert with_provider.report.full_score < legacy.report.full_score
    assert with_provider.report.decision == EvaluationDecision.REVIEW
    assert "metric_floor_review:llm_content_quality_audit" in (
        with_provider.report.review_reasons
    )
    assert "metric_floor_review:vlm_visual_quality_audit" in (
        with_provider.report.review_reasons
    )
    assert with_provider.manifest.cost == 0.002
    model_results = [
        item for item in with_provider.report.results if item.metric_id in MODEL_METRICS
    ]
    assert sum(item.metric_status == MetricStatus.SCORED for item in model_results) == 2


def test_required_flash_provider_error_is_visible_and_routes_review(
    tmp_path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    case = EvalCase(
        case_id="ready",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )
    profile = load_profile("configs/profiles/finished_deck_v3.json")
    adapter = PptxAdapter(backend="ooxml")
    failed_flash = RunSupervisor(
        DagScheduler(
            build_default_registry(adapter, llm_provider=RaisingProvider())
        )
    ).run(case, profile)

    llm_result = next(
        item
        for item in failed_flash.report.results
        if item.metric_id == "llm_content_quality_audit"
    )
    assert llm_result.error_code == "MODEL_PROVIDER_ERROR"
    assert failed_flash.report.errors
    assert failed_flash.report.base_score is not None
    assert failed_flash.report.full_score is None
    assert failed_flash.report.coverage == CoverageStatus.DEGRADED
    assert failed_flash.report.decision == EvaluationDecision.REVIEW
    assert "unresolved_metric:llm_content_quality_audit" in (
        failed_flash.report.review_reasons
    )


def test_experimental_profile_is_unvalidated_and_can_score_fake_model_results(
    tmp_path,
) -> None:
    profile = load_profile(
        "configs/profiles/experimental_text_generation_model_scoring.json"
    )
    assert profile.metadata["lifecycle"] == "EXPERIMENTAL"
    assert profile.metadata["calibration_status"] == "UNVALIDATED"
    assert profile.metadata["production_approved"] is False
    assert profile.base_weights["llm_content_quality_audit"] == 0.03
    assert profile.scene_weights["llm_scenario_compliance_audit"] == 0.04

    deck = build_pptx(tmp_path / "deck.pptx")
    image = tmp_path / "slide1.png"
    image.write_bytes(PNG_1X1)
    llm = FakeProvider(
        {
            "llm_content_quality_audit": 0.75,
            "llm_scenario_compliance_audit": 0.90,
        }
    )
    vlm = FakeProvider({"vlm_visual_quality_audit": 0.65})
    results = HighCostModelAuditOracle(
        PptxAdapter(backend="ooxml"),
        llm_provider=llm,
        vlm_provider=vlm,
    ).evaluate(
        context_for(
            deck,
            SceneType.TEXT_TO_PPT,
            artifacts={"slide_images": (image,)},
        )
    )
    model_only_profile = EvalProfile(
        profile_id="experimental-model-only-formula-test",
        version="test",
        scene=SceneType.TEXT_TO_PPT,
        base_weights={
            "llm_content_quality_audit": 0.03,
            "vlm_visual_quality_audit": 0.03,
        },
        scene_weights={"llm_scenario_compliance_audit": 0.04},
        base_multiplier_metric_ids=(),
        scene_multiplier_metric_ids=(),
        required_metric_ids=(),
        lambda_base=0.5,
    )
    breakdown = PptPdmsAggregator().aggregate(model_only_profile, results)

    assert breakdown.base_additive == 0.7
    assert breakdown.scene_additive == 0.9
    assert breakdown.full_score == 80.0


def test_construct_candidate_profile_caps_visual_model_without_becoming_default() -> None:
    candidate = load_profile(
        "configs/profiles/finished_deck_v4_construct_candidate.json"
    )
    default = default_profile(SceneType.READY_MADE)

    assert candidate.aggregation_strategy == CONSTRUCT_WEIGHTED_MEAN
    assert candidate.metadata["lifecycle"] == "EXPERIMENTAL"
    assert candidate.metadata["production_approved"] is False
    assert default.profile_id == "finished-deck-v8"
    assert default.aggregation_strategy != CONSTRUCT_WEIGHTED_MEAN
    visual_metrics = {
        metric_id
        for metric_id, construct in candidate.base_metric_constructs.items()
        if construct == "visual"
    }
    visual_weight = sum(candidate.base_weights[item] for item in visual_metrics)
    vlm_share = candidate.base_weights["vlm_visual_quality_audit"] / visual_weight
    assert round(vlm_share, 8) == 0.10
    assert candidate.base_construct_weights == {
        "content": 0.265625,
        "visual": 0.5,
        "delivery": 0.171875,
        "handoff": 0.0625,
    }


def test_manifest_uses_validated_actual_model_and_prompt_over_profile_declaration(
    tmp_path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    provider = FakeProvider({"llm_content_quality_audit": 0.9})
    profile = load_profile("configs/profiles/finished_deck_v3.json")
    profile = replace(
        profile,
        metadata={
            **dict(profile.metadata),
            "model_versions": {
                "llm_content_quality_audit": "declared/model@old",
                "unrelated": "declared/other@1",
            },
            "prompt_versions": {
                "llm_content_quality_audit": "declared-prompt@old",
            },
        },
    )
    case = EvalCase(
        case_id="ready",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )

    outcome = RunSupervisor(
        DagScheduler(
            build_default_registry(
                PptxAdapter(backend="ooxml"),
                llm_provider=provider,
            )
        )
    ).run(case, profile)

    assert outcome.manifest.model_versions["llm_content_quality_audit"] == (
        "fake/audit-model@2026-08-26"
    )
    assert outcome.manifest.model_versions["unrelated"] == "declared/other@1"
    prompt = outcome.manifest.prompt_versions["llm_content_quality_audit"]
    assert prompt.startswith("ppt-llm-content-quality-audit@1.0.0#")
    assert len(prompt.rsplit("#", 1)[1]) == 64
    assert outcome.manifest.cost == 0.001
