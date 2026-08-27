from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ppt_eval.adapters import ModelAuditRequest, RenderResult
from ppt_eval.config import load_profile
from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from ppt_eval.oracles.model_audits import (
    GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
)
from ppt_eval.runtime import LocalEvaluationRuntime, build_runtime_from_environment
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


class RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[ModelAuditRequest] = []

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        return {
            "score": 0.8,
            "confidence": 0.9,
            "model": {
                "provider": "fake",
                "model_id": "runtime-test",
                "version": "1",
            },
            "prompt": dict(request.prompt.reference()),
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0},
            "evidence": [
                {
                    "evidence_id": f"{request.metric_id}-page-1",
                    "kind": "model_audit_finding",
                    "message": "Grounded on the first rendered page.",
                    "page_number": 1,
                    "payload": {},
                }
            ],
        }


class RecordingRenderer:
    renderer_id = "fake-powerpoint"
    version = "1.2.3"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        self.calls += 1
        output = Path(output_dir)
        image = output / "Slide1.PNG"
        image.write_bytes(PNG_1X1)
        return RenderResult(self.renderer_id, self.version, (image,))


class SecretRaisingRenderer:
    renderer_id = "failing-renderer"
    version = "1"

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        raise RuntimeError("renderer failed with sk-should-never-be-persisted")


def _shadow_profile() -> EvalProfile:
    return EvalProfile(
        profile_id="runtime-render-test",
        version="2.0",
        scene=SceneType.READY_MADE,
        base_weights={"content_clarity": 1.0},
        scene_weights={},
        base_multiplier_metric_ids=(),
        scene_multiplier_metric_ids=(),
        required_metric_ids=("content_clarity",),
        enabled_oracle_ids=("baseline_ppt_quality", MODEL_AUDIT_COMPOSITE_ID),
        lambda_base=1.0,
    )


def test_direct_runtime_is_offline_even_when_a_local_secret_exists(tmp_path) -> None:
    key_file = tmp_path / "api" / "qwen3.7_flash_api.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-local-test-secret-value", encoding="utf-8")

    runtime = LocalEvaluationRuntime(tmp_path / "var")
    composite = runtime.registry.get(MODEL_AUDIT_COMPOSITE_ID)

    assert all(child.provider is None for child in composite.children)
    assert runtime.advanced_model_review is None


def test_structured_profile_enables_automatic_render_inputs(tmp_path) -> None:
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=RecordingProvider(),
        slide_renderer=RecordingRenderer(),
    )
    profile = load_profile(
        "configs/profiles/finished_deck_v5_structured_visual_candidate.json"
    )

    assert STRUCTURED_MODEL_AUDIT_COMPOSITE_ID in profile.enabled_oracle_ids
    assert runtime._should_render_model_inputs(profile, {}) is True
    assert runtime._should_render_model_inputs(
        profile,
        {"slide_images": (tmp_path / "caller.png",)},
    ) is False


def test_structured_dimensions_profile_enables_automatic_render_inputs(
    tmp_path,
) -> None:
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=RecordingProvider(),
        slide_renderer=RecordingRenderer(),
    )
    profile = load_profile(
        "configs/profiles/finished_deck_v6_structured_visual_dimensions_candidate.json"
    )

    assert (
        STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID
        in profile.enabled_oracle_ids
    )
    assert runtime._should_render_model_inputs(profile, {}) is True
    assert runtime._should_render_model_inputs(
        profile,
        {"render_result": object()},
    ) is False


def test_grounded_visual_profile_enables_automatic_render_inputs(tmp_path) -> None:
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=RecordingProvider(),
        slide_renderer=RecordingRenderer(),
    )
    profile = load_profile(
        "configs/profiles/finished_deck_v7_grounded_visual_candidate.json"
    )

    assert (
        GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID
        in profile.enabled_oracle_ids
    )
    assert runtime._should_render_model_inputs(profile, {}) is True


def test_environment_factory_wires_flash_baseline_and_qwen38_advanced_review(
    tmp_path,
) -> None:
    secret = "sk-runtime-factory-test-secret"
    runtime = build_runtime_from_environment(
        tmp_path / "var",
        environment={"DASHSCOPE_API_KEY": secret},
        workspace_root=tmp_path,
    )

    baseline = runtime.registry.get(MODEL_AUDIT_COMPOSITE_ID)
    baseline_models = {
        child.provider.model for child in baseline.children if child.provider is not None
    }
    advanced = runtime.advanced_model_review
    assert advanced is not None
    advanced_models = {
        child.provider.model for child in advanced.children if child.provider is not None
    }

    assert baseline_models == {"qwen3.7-flash"}
    assert advanced_models == {"qwen3.8-flash"}
    assert secret not in repr(runtime)
    assert secret not in repr(baseline.children[0].provider)


def test_automatic_vlm_render_uses_input_hash_cache(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    provider = RecordingProvider()
    renderer = RecordingRenderer()
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=provider,
        slide_renderer=renderer,
    )
    case = EvalCase(
        case_id="cached-render",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )

    first = runtime.evaluate(case, _shadow_profile())
    second = runtime.evaluate(case, _shadow_profile())

    assert first["decision"] != "ERROR"
    assert second["decision"] != "ERROR"
    assert renderer.calls == 1
    assert len(provider.requests) == 2
    image_path = Path(provider.requests[0].images[0].uri)
    assert image_path.parent.name == first["manifest"]["input_hash"]
    assert image_path.is_file()
    manifest = json.loads(
        (image_path.parent / "render-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["input_hash"] == first["manifest"]["input_hash"]
    assert manifest["slide_count"] == 1
    assert first["manifest"]["renderer_versions"]["model_audit_slides/fake-powerpoint"] == (
        "1.2.3"
    )

    # A hash-matching cache entry is still rejected when its page coverage is
    # inconsistent with the parsed deck.
    manifest["slide_count"] = 999
    (image_path.parent / "render-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    runtime.evaluate(case, _shadow_profile())
    assert renderer.calls == 2


def test_automatic_render_failure_degrades_without_leaking_diagnostics(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    provider = RecordingProvider()
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=provider,
        slide_renderer=SecretRaisingRenderer(),
    )
    report = runtime.evaluate(
        EvalCase(
            case_id="render-failure",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        _shadow_profile(),
    )

    visual = next(
        result
        for result in report["results"]
        if result["metric_id"] == "vlm_visual_quality_audit"
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["decision"] != "ERROR"
    assert visual["metric_status"] == "NA"
    assert visual["metadata"]["reason_code"] == "RENDERED_SLIDES_UNAVAILABLE"
    assert provider.requests == []
    assert "sk-should-never-be-persisted" not in serialized
    assert report["manifest"]["renderer_versions"]["model_audit_slides"] == "unavailable"


def test_automatic_native_render_refuses_active_or_external_content(tmp_path) -> None:
    deck = build_pptx(
        tmp_path / "unsafe-render.pptx",
        external_relationship=True,
        active_content=True,
    )
    provider = RecordingProvider()
    renderer = RecordingRenderer()
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=provider,
        slide_renderer=renderer,
    )

    report = runtime.evaluate(
        EvalCase(
            case_id="unsafe-native-render",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        _shadow_profile(),
    )

    visual = next(
        result
        for result in report["results"]
        if result["metric_id"] == "vlm_visual_quality_audit"
    )
    assert renderer.calls == 0
    assert provider.requests == []
    assert visual["metric_status"] == "NA"
    assert visual["metadata"]["reason_code"] == "RENDERED_SLIDES_UNAVAILABLE"
    assert report["manifest"]["renderer_versions"]["model_audit_slides"] == (
        "unavailable"
    )
