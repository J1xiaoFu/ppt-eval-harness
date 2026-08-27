from __future__ import annotations

import math

from ppt_eval.application import ProfileCompiler
from ppt_eval.config import default_profile, load_profile
from ppt_eval.domain import SceneType
from ppt_eval.oracles import build_default_registry

PROFILE_IDS = {
    SceneType.TEXT_TO_PPT: "text-generation-v8",
    SceneType.PROJECT_SUMMARY: "project-summary-v8",
    SceneType.MULTIMODAL: "multimodal-generation-v8",
    SceneType.READY_MADE: "finished-deck-v8",
}


def test_v8_is_default_for_all_four_scenes() -> None:
    for scene, profile_id in PROFILE_IDS.items():
        profile = default_profile(scene)
        assert profile.profile_id == profile_id
        assert profile.version == "8.1"
        assert profile.metadata["runtime_wired"] is True
        assert profile.metadata["model_audit_routing"] == (
            "ATOMIC_FLASH_ADVANCED_HUMAN"
        )
        assert profile.metadata["flash_provider"] == (
            "qwen-dashscope-openai-compatible"
        )
        assert profile.metadata["flash_model"] == "qwen3.8-flash"
        assert profile.metadata["advanced_provider"] == (
            "zhipu-bigmodel-openai-compatible"
        )
        assert profile.metadata["advanced_model"] == "glm-5.3-flash"
        assert math.isclose(sum(profile.base_weights.values()), 1.0)
        if profile.scene_weights:
            assert math.isclose(sum(profile.scene_weights.values()), 1.0)


def test_v8_pipeline_exposes_each_visual_criterion_as_its_own_dag_node() -> None:
    profile = default_profile(SceneType.READY_MADE)
    dag = ProfileCompiler().compile(profile)
    oracle_ids = [node.oracle_id for node in dag.nodes]

    assert oracle_ids[0] == "baseline_ppt_quality"
    assert oracle_ids[1] == "v8.atomic_observations"
    assert [item for item in oracle_ids if item.startswith("v8.visual.")] == [
        "v8.visual.composition_layout",
        "v8.visual.typography_legibility",
        "v8.visual.color_contrast",
        "v8.visual.imagery_data_visualization",
        "v8.visual.cross_slide_consistency",
        "v8.visual.render_integrity",
    ]
    assert oracle_ids[-1] == "v8.quality_reducers"
    assert set(dag.nodes[-1].dependencies) == {
        "audit:v8-composition",
        "audit:v8-typography",
        "audit:v8-color",
        "audit:v8-imagery",
        "audit:v8-system",
        "audit:v8-render",
    }


def test_v8_formula_contains_no_legacy_holistic_model_metrics() -> None:
    forbidden = {
        "llm_content_quality_audit",
        "llm_scenario_compliance_audit",
        "vlm_visual_quality_audit",
        "structured_vlm_visual_audit",
    }
    for scene in SceneType:
        profile = default_profile(scene)
        assert forbidden.isdisjoint(profile.base_weights)
        assert forbidden.isdisjoint(profile.scene_weights)
        assert forbidden.isdisjoint(profile.required_metric_ids or ())


def test_default_registry_contains_every_v8_pipeline_oracle() -> None:
    registry = build_default_registry()
    profile = default_profile(SceneType.READY_MADE)
    pipeline_ids = {
        str(item["oracle_id"]) for item in profile.metadata["pipeline_nodes"]
    }

    assert all(registry.contains(oracle_id) for oracle_id in pipeline_ids)


def test_v1_through_v7_profiles_remain_explicitly_loadable() -> None:
    paths = (
        "configs/profiles/finished_deck.json",
        "configs/profiles/finished_deck_v2.json",
        "configs/profiles/finished_deck_v3.json",
        "configs/profiles/finished_deck_v4_construct_candidate.json",
        "configs/profiles/finished_deck_v5_structured_visual_candidate.json",
        "configs/profiles/finished_deck_v6_structured_visual_dimensions_candidate.json",
        "configs/profiles/finished_deck_v7_grounded_visual_candidate.json",
    )

    assert [load_profile(path).version for path in paths] == [
        "1.0",
        "2.0",
        "3.1",
        "4.1",
        "5.0",
        "6.0",
        "7.0",
    ]
