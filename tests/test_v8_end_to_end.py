from __future__ import annotations

import json
from pathlib import Path

import pytest

from ppt_eval.config import default_profile, profile_for_version
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx
from tests.test_grounded_visual_audit import GroundedFakeProvider


def test_profile83_explicit_replay_skips_adaptive_visual_nodes(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "legacy-replay.pptx")
    image = tmp_path / "legacy-replay.png"
    image.write_bytes(PNG_1X1)
    provider = GroundedFakeProvider()
    provider.context_cache_enabled = True
    runtime = LocalEvaluationRuntime(tmp_path / "legacy-var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="profile-83-explicit-replay",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile_for_version(SceneType.READY_MADE, "8.3"),
        artifacts={"slide_images": (image,)},
    )

    assert report["profile_version"] == "8.3"
    assert "visual_audit_artifacts" not in report
    assert report["visual_audit_summary"] == {}
    assert all(
        request.metric_id != "visual_atlas_scout_routing"
        for request in provider.requests
    )
    structured = next(
        item
        for item in report["results"]
        if item["metric_id"] == "structured_vlm_composition_layout"
    )
    assert structured["metadata"]["routing_attempts"][0][
        "context_cache_enabled"
    ] is False
    assert "request_budget" not in structured["metadata"]
    assert all(
        "model_request_count" not in attempt
        for attempt in structured["metadata"]["routing_attempts"]
    )
    assert structured["version"] == "8.3.0"
    assert structured["metadata"]["prompt"]["version"] == "2.0.0"
    assert all(
        request.prompt.version in {"1.0.0", "2.0.0", "2.1.0"}
        for request in provider.requests
    )
    reducer_results = [
        item
        for item in report["results"]
        if item["oracle_id"] == "v8.quality_reducers"
    ]
    assert reducer_results
    assert {item["version"] for item in reducer_results} == {"8.3.0"}
    assert report["manifest"]["oracle_versions"]["v8.atomic_observations"] == (
        "8.3.0"
    )
    assert report["manifest"]["oracle_versions"][
        "v8.visual.composition_layout"
    ] == "8.3.0"
    assert report["manifest"]["oracle_versions"]["v8.quality_reducers"] == (
        "8.3.0"
    )
    observations = json.loads(
        Path(report["observation_artifact"]["uri"]).read_text(encoding="utf-8")
    )
    assert observations
    assert {item["version"] for item in observations} == {"2.1.0"}


def test_v8_is_default_and_emits_atomic_training_contract(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "v8-default.pptx")
    image = tmp_path / "slide-1.png"
    image.write_bytes(PNG_1X1)
    provider = GroundedFakeProvider()
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=provider,
    )

    profile = default_profile(SceneType.READY_MADE)
    report = runtime.evaluate(
        EvalCase(
            case_id="v8-default",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile,
        artifacts={"slide_images": (image,)},
    )

    assert profile.profile_id == "finished-deck-v8"
    assert profile.version == "8.4"
    assert len(provider.requests) == 6
    metric_ids = {item["metric_id"] for item in report["results"]}
    assert {
        "content_structure",
        "language_consistency",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
        "authorship_specificity",
        "v8_functional_integrity",
    } <= metric_ids
    assert "llm_content_quality_audit" not in metric_ids
    assert "vlm_visual_quality_audit" not in metric_ids
    assert report["observation_summary"]["count"] > 0
    assert Path(report["observation_artifact"]["uri"]).is_file()
    decisions = {
        item["track"]: item for item in report["training_eligibility"]["decisions"]
    }
    assert decisions["content"]["status"] == "REVIEW"
    assert decisions["full_deck"]["status"] == "REVIEW"


def test_v8_cross_provider_fallback_persists_complete_lineage(tmp_path: Path) -> None:
    deck = build_pptx(
        tmp_path / "cross-provider.pptx",
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
    image = tmp_path / "slide-1.png"
    image.write_bytes(PNG_1X1)

    def identify_qwen(payload) -> None:
        payload["model"] = {
            "provider": "qwen-dashscope-openai-compatible",
            "model_id": "qwen3.8-flash",
            "version": "qwen3.8-flash",
        }

    def identify_glm(payload) -> None:
        payload["model"] = {
            "provider": "zhipu-bigmodel-openai-compatible",
            "model_id": "glm-5.3-flash",
            "version": "glm-5.3-flash",
        }
        for item in payload["evidence"]:
            item["payload"]["criterion_score"] = 0.60

    primary = GroundedFakeProvider(identify_qwen)
    fallback = GroundedFakeProvider(identify_glm)
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=primary,
        advanced_vlm_provider=fallback,
    )
    report = runtime.evaluate(
        EvalCase(
            case_id="cross-provider-lineage",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": (image,)},
    )

    result = next(
        item
        for item in report["results"]
        if item["metric_id"] == "structured_vlm_composition_layout"
    )
    attempts = result["metadata"]["routing_attempts"]
    assert result["metadata"]["selected_tier"] == "ADVANCED"
    assert [item["model"]["model_id"] for item in attempts] == [
        "qwen3.8-flash",
        "glm-5.3-flash",
    ]
    assert result["metadata"]["routing_usage"]["total_tokens"] == 400
    assert result["cost"] == 0.0
    initial = next(
        item
        for item in report["results"]
        if item["metric_id"]
        == "provisional_structured_vlm_composition_layout"
    )
    assert initial["cost"] == 0.008
    assert result["metadata"]["initial_seed_cost_accounted_separately"] is True
    versions = report["manifest"]["model_versions"]
    assert versions["structured_vlm_composition_layout#flash"].startswith(
        "qwen-dashscope-openai-compatible/qwen3.8-flash@"
    )
    assert versions["structured_vlm_composition_layout#advanced"].startswith(
        "zhipu-bigmodel-openai-compatible/glm-5.3-flash@"
    )
    assert report["manifest"]["cost"] == pytest.approx(0.001 + 0.004 * (
        len(primary.requests) - 1 + len(fallback.requests)
    ))
    assert report["training_eligibility"]["critical_issue_codes"] == []
    assert runtime.audit_log.verify() == (True, None)


def test_v8_raster_deck_recovers_required_text_metrics_as_atomic_observations(
    tmp_path: Path,
) -> None:
    deck = build_pptx(
        tmp_path / "raster-recovery.pptx",
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
            for _ in range(2)
        ),
    )
    images = []
    for page_number in (1, 2):
        image = tmp_path / f"raster-render-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    provider = GroundedFakeProvider()
    fallback = GroundedFakeProvider()
    runtime = LocalEvaluationRuntime(
        tmp_path / "raster-var",
        vlm_provider=provider,
        advanced_vlm_provider=fallback,
    )

    report = runtime.evaluate(
        EvalCase(
            case_id="raster-recovery",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
    )

    assert len(provider.requests) == 9
    assert fallback.requests
    indexed = {item["metric_id"]: item for item in report["results"]}
    for metric_id in ("content_structure", "language_consistency"):
        assert indexed[metric_id]["metric_status"] == "SCORED"
        assert indexed[metric_id]["normalized_score"] == 0.82
        assert indexed[metric_id]["metadata"]["fusion_mode"] == (
            "RASTER_VLM_ATOMIC_FALLBACK"
        )
    assert report["coverage"] == "FULL"
    assert {
        "raster_content_structure_vlm",
        "raster_language_consistency_vlm",
    } <= set(report["observation_summary"]["metric_ids"])
    assert report["manifest"]["cost"] == pytest.approx(0.001 + 0.004 * (
        len(provider.requests) - 1 + len(fallback.requests)
    ))
    assert (
        "structured_vlm_raster_content_structure"
        in report["manifest"]["model_versions"]
    )
    assert runtime.audit_log.verify() == (True, None)
