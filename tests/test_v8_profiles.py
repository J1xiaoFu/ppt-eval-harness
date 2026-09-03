from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import pytest

from ppt_eval.application import ProfileCompiler
from ppt_eval.config import default_profile, profile_for_version, profile_from_mapping
from ppt_eval.domain import SceneType
from ppt_eval.oracles import build_default_registry

PROFILE_IDS = {
    SceneType.TEXT_TO_PPT: "text-generation-v8",
    SceneType.PROJECT_SUMMARY: "project-summary-v8",
    SceneType.MULTIMODAL: "multimodal-generation-v8",
    SceneType.READY_MADE: "finished-deck-v8",
}

MINIMAL_V8_PROFILE = {
    "profile_id": "minimal-v8",
    "version": "8.3",
    "scenario": "FINISHED_DECK",
    "base_weights": {"quality": 1.0},
    "scene_weights": {},
    "base_multiplier_metric_ids": [],
    "scene_multiplier_metric_ids": [],
    "required_metric_ids": ["quality"],
    "pipeline_nodes": [
        {
            "node_id": "observe:minimal",
            "oracle_id": "v8.atomic_observations",
            "kind": "OBSERVE",
            "dependencies": ["baseline_ppt_quality"],
        }
    ],
}


def test_v8_is_default_for_all_four_scenes() -> None:
    for scene, profile_id in PROFILE_IDS.items():
        profile = default_profile(scene)
        assert profile.profile_id == profile_id
        assert profile.version == "8.4"
        assert profile.metadata["runtime_wired"] is True
        assert profile.metadata["model_audit_routing"] == (
            "ADAPTIVE_ATOMIC_FLASH_ADVANCED_HUMAN"
        )
        assert profile.metadata["flash_provider"] == (
            "qwen-dashscope-openai-compatible"
        )
        assert profile.metadata["flash_model"] == "qwen3.8-flash"
        assert profile.metadata["advanced_provider"] == (
            "zhipu-bigmodel-openai-compatible"
        )
        assert profile.metadata["advanced_model"] == "glm-5.3-flash"
        assert profile.metadata["vlm_dimension_min_confidence"] == 0.60
        assert profile.cost_budget == 1.0
        assert profile.metadata["visual_audit"]["maximum_model_requests"] == 64
        assert profile.metadata["visual_audit"]["maximum_scout_batches"] == 4
        assert math.isclose(sum(profile.base_weights.values()), 1.0)
        if profile.scene_weights:
            assert math.isclose(sum(profile.scene_weights.values()), 1.0)


def test_bundled_v8_defaults_do_not_depend_on_working_directory(
    tmp_path: Path,
) -> None:
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        profiles = [default_profile(scene) for scene in SceneType]
    finally:
        os.chdir(previous)

    assert [profile.profile_id for profile in profiles] == [
        PROFILE_IDS[scene] for scene in SceneType
    ]
    assert all(profile.version == "8.4" for profile in profiles)


def test_main_rejects_legacy_profile_versions_and_fields() -> None:
    legacy_version = {**MINIMAL_V8_PROFILE, "version": "7.0"}
    with pytest.raises(ValueError, match="Profile 8.4.*8.3 replay"):
        profile_from_mapping(legacy_version)

    legacy_field = {**MINIMAL_V8_PROFILE, "optional_oracles": []}
    with pytest.raises(ValueError, match="legacy Profile fields"):
        profile_from_mapping(legacy_field)


def test_v8_pipeline_exposes_each_visual_criterion_as_its_own_dag_node() -> None:
    profile = default_profile(SceneType.READY_MADE)
    dag = ProfileCompiler().compile(profile)
    oracle_ids = [node.oracle_id for node in dag.nodes]

    assert oracle_ids[0] == "baseline_ppt_quality"
    assert oracle_ids[1] == "v8.atomic_observations"
    assert [item for item in oracle_ids if item.startswith("v8.visual.")] == [
        "v8.visual.page_index",
        "v8.visual.atlas_scout",
        "v8.visual.selection",
        "v8.visual.initial.composition_layout",
        "v8.visual.initial.typography_legibility",
        "v8.visual.initial.color_contrast",
        "v8.visual.initial.imagery_data_visualization",
        "v8.visual.initial.cross_slide_consistency",
        "v8.visual.initial.render_integrity",
        "v8.visual.initial.authorship_specificity",
        "v8.visual.composition_layout",
        "v8.visual.typography_legibility",
        "v8.visual.color_contrast",
        "v8.visual.imagery_data_visualization",
        "v8.visual.cross_slide_consistency",
        "v8.visual.render_integrity",
        "v8.visual.authorship_specificity",
        "v8.visual.coverage",
    ]
    assert [item for item in oracle_ids if item.startswith("v8.raster_text.")] == [
        "v8.raster_text.raster_content_structure",
        "v8.raster_text.raster_language_consistency",
    ]
    assert oracle_ids[-1] == "v8.quality_reducers"
    node_by_id = {node.node_id: node for node in dag.nodes}
    assert node_by_id["audit:v8-raster-content"].dependencies == (
        "audit:v8-initial-composition",
        "audit:v8-initial-typography",
        "audit:v8-initial-color",
        "audit:v8-initial-imagery",
        "audit:v8-initial-system",
        "audit:v8-initial-render",
        "audit:v8-initial-authorship",
    )
    assert node_by_id["audit:v8-composition"].dependencies == (
        "audit:v8-raster-language",
    )
    assert all(
        node_by_id[node_id].mandatory
        for node_id in (
            "audit:v8-composition",
            "audit:v8-typography",
            "audit:v8-color",
            "audit:v8-imagery",
            "audit:v8-system",
            "audit:v8-render",
            "audit:v8-authorship",
        )
    )
    assert set(dag.nodes[-1].dependencies) == {
        "fuse:v8-visual-coverage",
        "audit:v8-raster-content",
        "audit:v8-raster-language",
    }


def test_profile_83_remains_an_explicit_bundled_replay_contract() -> None:
    for scene in SceneType:
        current = default_profile(scene)
        replay = profile_for_version(scene, "8.3")

        assert current.version == "8.4"
        assert replay.version == "8.3"
        assert replay.base_weights == current.base_weights
        assert replay.scene_weights == current.scene_weights
        assert replay.cost_budget is None
        replay_oracles = {
            str(item["oracle_id"]) for item in replay.metadata["pipeline_nodes"]
        }
        assert "v8.visual.page_index" not in replay_oracles
        assert "v8.visual.atlas_scout" not in replay_oracles
        assert "v8.visual.selection" not in replay_oracles
        assert "v8.visual.coverage" not in replay_oracles


def test_profile83_files_are_byte_exact_087_release_snapshots() -> None:
    expected = {
        "finished_deck_v83.json": "e7f26334e5efc2e21dc1038dc70e49cac457a11bd5e7b5dca488a59221aee997",
        "multimodal_generation_v83.json": "110a660b8f34198c2a1df50d685c10e954ded3ce602aba971efe2b3610ba4780",
        "project_summary_v83.json": "7f0a5ac209e2ab3002965ef2f72c84a5989f072feeb4350c84174298c2dde320",
        "text_generation_v83.json": "dcd060e0a75a391b5d2d7ed5ee07570605e32d62258916ea1ad0f75fdbf7eba3",
    }
    profile_root = Path(__file__).parents[1] / "src" / "ppt_eval" / "profiles"

    assert {
        name: hashlib.sha256((profile_root / name).read_bytes()).hexdigest()
        for name in expected
    } == expected


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
