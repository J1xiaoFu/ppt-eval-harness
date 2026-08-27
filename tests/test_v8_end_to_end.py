from __future__ import annotations

from pathlib import Path

from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx
from tests.test_grounded_visual_audit import GroundedFakeProvider


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
    assert profile.version == "8.0"
    assert len(provider.requests) == 6
    metric_ids = {item["metric_id"] for item in report["results"]}
    assert {
        "content_structure",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
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


def test_explicit_v3_profile_remains_loadable_after_v8_default() -> None:
    from ppt_eval.config import load_profile

    legacy = load_profile("configs/profiles/finished_deck_v3.json")

    assert legacy.profile_id == "finished-deck-v3"
    assert legacy.version == "3.1"
    assert "high_cost.model_audits" in legacy.enabled_oracle_ids
