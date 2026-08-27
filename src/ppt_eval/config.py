from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from ppt_eval.domain.models import (
    DEFAULT_BASE_MULTIPLIERS,
    DEFAULT_SCENE_MULTIPLIERS,
)

_SCENE_ALIASES = {
    "TEXT_GENERATION": SceneType.TEXT_TO_PPT,
    "TEXT_TO_PPT": SceneType.TEXT_TO_PPT,
    "PROJECT_SUMMARY": SceneType.PROJECT_SUMMARY,
    "MULTIMODAL_GENERATION": SceneType.MULTIMODAL,
    "MULTIMODAL": SceneType.MULTIMODAL,
    "FINISHED_DECK": SceneType.READY_MADE,
    "READY_MADE": SceneType.READY_MADE,
}


def parse_scene(value: str | SceneType) -> SceneType:
    if isinstance(value, SceneType):
        return value
    normalized = value.strip()
    if normalized.upper() in _SCENE_ALIASES:
        return _SCENE_ALIASES[normalized.upper()]
    return SceneType(normalized.lower())


def case_from_mapping(payload: Mapping[str, Any]) -> EvalCase:
    return EvalCase(
        case_id=str(payload["case_id"]),
        scene=parse_scene(str(payload.get("scene") or payload.get("scenario"))),
        pptx_path=str(payload["pptx_path"]),
        request=payload.get("request"),
        audience=payload.get("audience"),
        source_materials=tuple(str(item) for item in payload.get("source_materials", ())),
        assets=tuple(str(item) for item in payload.get("assets", ())),
        metadata=dict(payload.get("metadata", {})),
    )


def load_case(path: str | Path) -> EvalCase:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return case_from_mapping(payload)


def profile_from_mapping(payload: Mapping[str, Any]) -> EvalProfile:
    scene = parse_scene(str(payload.get("scene") or payload.get("scenario")))
    profile_id = str(payload.get("profile_id") or f"default-{scene.value}")
    version = str(payload.get("version") or "1.0")
    defaults = EvalProfile.default(scene, version=version)
    try:
        major_version = int(version.split(".", 1)[0])
    except ValueError:
        major_version = 1
    optional_oracles = (
        tuple(payload.get("optional_oracles") or ()) if major_version >= 2 else ()
    )
    configured_oracles = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                *(payload.get("required_oracles") or ()),
                *optional_oracles,
            )
            if str(item)
        )
    )
    enabled_oracles = tuple(
        str(item)
        for item in (payload.get("enabled_oracle_ids") or ())
        if str(item)
    )
    return EvalProfile(
        profile_id=profile_id,
        version=version,
        scene=scene,
        base_weights=dict(payload.get("base_weights") or defaults.base_weights),
        scene_weights=dict(payload.get("scene_weights") or defaults.scene_weights),
        base_multiplier_metric_ids=tuple(
            payload["base_multiplier_metric_ids"]
            if "base_multiplier_metric_ids" in payload
            else DEFAULT_BASE_MULTIPLIERS
        ),
        scene_multiplier_metric_ids=tuple(
            payload["scene_multiplier_metric_ids"]
            if "scene_multiplier_metric_ids" in payload
            else DEFAULT_SCENE_MULTIPLIERS[scene]
        ),
        required_metric_ids=(
            tuple(str(item) for item in payload["required_metric_ids"])
            if "required_metric_ids" in payload
            else None
        ),
        enabled_oracle_ids=tuple(
            enabled_oracles
            or configured_oracles
            or defaults.enabled_oracle_ids
        ),
        lambda_base=float(payload.get("lambda_base", defaults.lambda_base)),
        hard_gate_min_confidence=float(payload.get("hard_gate_min_confidence", 0.90)),
        pass_threshold=float(payload.get("pass_threshold", 80.0)),
        review_threshold=float(payload.get("review_threshold", 60.0)),
        metric_review_thresholds=dict(
            payload.get("metric_review_thresholds")
            or defaults.metric_review_thresholds
        ),
        aggregation_strategy=str(
            payload.get("aggregation_strategy") or defaults.aggregation_strategy
        ),
        base_metric_constructs={
            str(key): str(value)
            for key, value in dict(
                payload.get("base_metric_constructs")
                or defaults.base_metric_constructs
            ).items()
        },
        base_construct_weights=dict(
            payload.get("base_construct_weights")
            or defaults.base_construct_weights
        ),
        scene_metric_constructs={
            str(key): str(value)
            for key, value in dict(
                payload.get("scene_metric_constructs")
                or defaults.scene_metric_constructs
            ).items()
        },
        scene_construct_weights=dict(
            payload.get("scene_construct_weights")
            or defaults.scene_construct_weights
        ),
        max_retries=int(payload.get("max_retries", 1)),
        oracle_timeout_seconds=float(payload.get("oracle_timeout_seconds", 60.0)),
        cost_budget=payload.get("cost_budget"),
        metadata={
            **dict(defaults.metadata),
            **dict(payload.get("metadata", {})),
            **(
                {"pipeline_nodes": tuple(payload.get("pipeline_nodes", ()))}
                if "pipeline_nodes" in payload
                else {}
            ),
            "optional_oracles": tuple(payload.get("optional_oracles", ())),
            "optional_oracles_executed": major_version >= 2,
        },
    )


def load_profile(path: str | Path) -> EvalProfile:
    return profile_from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


def profile_path_for_scene(scene: SceneType, root: str | Path = "configs/profiles") -> Path:
    names = {
        SceneType.TEXT_TO_PPT: "text_generation_v8.json",
        SceneType.PROJECT_SUMMARY: "project_summary_v8.json",
        SceneType.MULTIMODAL: "multimodal_generation_v8.json",
        SceneType.READY_MADE: "finished_deck_v8.json",
    }
    return Path(root) / names[scene]


def default_profile(scene: SceneType, root: str | Path = "configs/profiles") -> EvalProfile:
    path = profile_path_for_scene(scene, root)
    return load_profile(path) if path.is_file() else EvalProfile.default(scene)
