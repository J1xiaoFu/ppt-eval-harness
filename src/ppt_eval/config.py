from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from ppt_eval.domain import EvalCase, EvalProfile, SceneType

_SCENE_ALIASES = {
    "TEXT_GENERATION": SceneType.TEXT_TO_PPT,
    "TEXT_TO_PPT": SceneType.TEXT_TO_PPT,
    "PROJECT_SUMMARY": SceneType.PROJECT_SUMMARY,
    "MULTIMODAL_GENERATION": SceneType.MULTIMODAL,
    "MULTIMODAL": SceneType.MULTIMODAL,
    "FINISHED_DECK": SceneType.READY_MADE,
    "READY_MADE": SceneType.READY_MADE,
}

_DEFAULT_PROFILE_NAMES = {
    SceneType.TEXT_TO_PPT: "text_generation_v8.json",
    SceneType.PROJECT_SUMMARY: "project_summary_v8.json",
    SceneType.MULTIMODAL: "multimodal_generation_v8.json",
    SceneType.READY_MADE: "finished_deck_v8.json",
}

_PROFILE_NAMES_BY_VERSION = {
    "8.4": _DEFAULT_PROFILE_NAMES,
    "8.3": {
        SceneType.TEXT_TO_PPT: "text_generation_v83.json",
        SceneType.PROJECT_SUMMARY: "project_summary_v83.json",
        SceneType.MULTIMODAL: "multimodal_generation_v83.json",
        SceneType.READY_MADE: "finished_deck_v83.json",
    },
}
CURRENT_PROFILE_VERSION = "8.4"
REPLAY_PROFILE_VERSIONS = ("8.3",)

_REQUIRED_V8_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "version",
        "base_weights",
        "scene_weights",
        "base_multiplier_metric_ids",
        "scene_multiplier_metric_ids",
        "required_metric_ids",
        "pipeline_nodes",
    }
)
_LEGACY_PROFILE_FIELDS = frozenset({"required_oracles", "optional_oracles"})


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
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("case manifest must contain a JSON object")
    base = manifest_path.parent
    normalized = dict(payload)
    normalized["pptx_path"] = _manifest_path(
        payload["pptx_path"],
        base=base,
        explicit_only=False,
    )
    normalized["source_materials"] = [
        _manifest_path(item, base=base, explicit_only=True)
        for item in payload.get("source_materials", ())
    ]
    normalized["assets"] = [
        _manifest_path(item, base=base, explicit_only=True)
        for item in payload.get("assets", ())
    ]
    payload = normalized
    return case_from_mapping(payload)


def _manifest_path(
    value: object,
    *,
    base: Path,
    explicit_only: bool,
) -> str:
    text = str(value)
    parsed = urlsplit(text)
    if parsed.scheme in {"http", "https", "urn", "data", "file"}:
        return text
    candidate = Path(text)
    if candidate.is_absolute() or PureWindowsPath(text).is_absolute():
        return text
    explicit = text.startswith(("./", "../", ".\\", "..\\"))
    if explicit_only and not explicit:
        return text
    return str((base / candidate).resolve())


def profile_from_mapping(payload: Mapping[str, Any]) -> EvalProfile:
    missing = _REQUIRED_V8_PROFILE_FIELDS - set(payload)
    if missing:
        raise ValueError(
            "v8 profile is missing required fields: " + ", ".join(sorted(missing))
        )
    legacy = _LEGACY_PROFILE_FIELDS & set(payload)
    if legacy:
        raise ValueError(
            "legacy Profile fields are not supported on main: "
            + ", ".join(sorted(legacy))
            + "; use archive/v8.3-pre-release for historical replay"
        )
    scene = parse_scene(str(payload.get("scene") or payload.get("scenario")))
    profile_id = str(payload["profile_id"])
    version = str(payload["version"])
    try:
        major_version = int(version.split(".", 1)[0])
    except ValueError as exc:
        raise ValueError("profile version must begin with an integer major version") from exc
    if major_version != 8 or version not in _PROFILE_NAMES_BY_VERSION:
        raise ValueError(
            "main accepts only Profile 8.4 and the explicit read-only 8.3 replay "
            "contract; use archive/v8.3-pre-release for older historical replay"
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
        base_weights=dict(payload["base_weights"]),
        scene_weights=dict(payload["scene_weights"]),
        base_multiplier_metric_ids=tuple(payload["base_multiplier_metric_ids"]),
        scene_multiplier_metric_ids=tuple(payload["scene_multiplier_metric_ids"]),
        required_metric_ids=tuple(
            str(item) for item in payload["required_metric_ids"]
        ),
        enabled_oracle_ids=enabled_oracles,
        lambda_base=float(payload.get("lambda_base", 1.0)),
        hard_gate_min_confidence=float(payload.get("hard_gate_min_confidence", 0.90)),
        pass_threshold=float(payload.get("pass_threshold", 80.0)),
        review_threshold=float(payload.get("review_threshold", 60.0)),
        metric_review_thresholds=dict(
            payload.get("metric_review_thresholds") or {}
        ),
        aggregation_strategy=str(
            payload.get("aggregation_strategy") or "FLAT_WEIGHTED_MEAN"
        ),
        base_metric_constructs={
            str(key): str(value)
            for key, value in dict(payload.get("base_metric_constructs") or {}).items()
        },
        base_construct_weights=dict(payload.get("base_construct_weights") or {}),
        scene_metric_constructs={
            str(key): str(value)
            for key, value in dict(payload.get("scene_metric_constructs") or {}).items()
        },
        scene_construct_weights=dict(payload.get("scene_construct_weights") or {}),
        max_retries=int(payload.get("max_retries", 1)),
        oracle_timeout_seconds=float(payload.get("oracle_timeout_seconds", 60.0)),
        cost_budget=payload.get("cost_budget"),
        metadata={
            **dict(payload.get("metadata", {})),
            "pipeline_nodes": tuple(payload["pipeline_nodes"]),
        },
    )


def load_profile(path: str | Path) -> EvalProfile:
    return profile_from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


def profile_path_for_scene(scene: SceneType, root: str | Path) -> Path:
    return Path(root) / _DEFAULT_PROFILE_NAMES[scene]


def default_profile(
    scene: SceneType,
    root: str | Path | None = None,
) -> EvalProfile:
    return profile_for_version(scene, CURRENT_PROFILE_VERSION, root=root)


def profile_for_version(
    scene: SceneType,
    version: str,
    *,
    root: str | Path | None = None,
) -> EvalProfile:
    """Load the current Profile or an explicitly requested immutable replay."""

    try:
        profile_names = _PROFILE_NAMES_BY_VERSION[version]
    except KeyError as exc:
        raise ValueError(f"unsupported Profile replay version {version!r}") from exc
    profile_name = profile_names[scene]
    if root is not None:
        path = Path(root) / profile_name
        if not path.is_file():
            raise FileNotFoundError(path)
        return load_profile(path)
    resource = files("ppt_eval.profiles").joinpath(profile_name)
    if not resource.is_file():
        raise RuntimeError(
            f"bundled Profile {version} is missing for {scene.value}"
        )
    return profile_from_mapping(json.loads(resource.read_text(encoding="utf-8")))
