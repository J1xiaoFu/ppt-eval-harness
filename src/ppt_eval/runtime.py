from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ppt_eval.adapters import (
    LibreOfficeRenderer,
    ModelAuditProvider,
    ParsedPresentation,
    PowerPointRenderer,
    PptxAdapter,
    RenderResult,
)
from ppt_eval.application import DagScheduler, EvaluationService, RunSupervisor
from ppt_eval.config import default_profile
from ppt_eval.domain import AtomicObservation, EvalCase, EvalProfile
from ppt_eval.flywheel import (
    ActiveSampler,
    JsonlRecordStore,
    ParameterProposalService,
    feedback_from_mapping,
)
from ppt_eval.infrastructure import (
    DEFAULT_QWEN_KEY_FILE,
    DEFAULT_ZHIPU_KEY_FILE,
    JsonlAuditLog,
    JsonRunRepository,
    LocalArtifactStore,
    QwenAuditSettings,
    ZhipuAuditSettings,
    font_fingerprint,
    git_sha,
    to_primitive,
)
from ppt_eval.oracles import (
    AdvancedModelReviewOracle,
    ModelSourceAccessPolicy,
    build_default_registry,
)
from ppt_eval.oracles.model_audits import (
    GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    GROUNDED_STRUCTURED_DIMENSIONS_VLM_ORACLE_ID,
    MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
)
from ppt_eval.reporting import export_run_report

_RENDER_MANIFEST_NAME = "render-manifest.json"


class SlideRenderer(Protocol):
    """Small runtime port shared by PowerPoint, LibreOffice, and test fakes."""

    renderer_id: str

    @property
    def version(self) -> str: ...

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult: ...


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    reports: Path
    audit: Path
    artifacts: Path
    render_cache: Path

    @classmethod
    def under(cls, root: str | Path) -> "RuntimePaths":
        root_path = Path(root)
        return cls(
            root=root_path,
            reports=root_path / "runs",
            audit=root_path / "audit" / "events.jsonl",
            artifacts=root_path / "artifacts",
            render_cache=root_path / "artifacts" / "slide-renders",
        )


def normalized_report_payload(outcome: Any) -> dict[str, Any]:
    primitive = to_primitive(outcome.report)
    if not isinstance(primitive, dict):
        raise TypeError("normalized report must be a mapping")
    payload: dict[str, Any] = {str(key): value for key, value in primitive.items()}
    payload["scenario"] = payload.get("scene")
    payload["degradation_reasons"] = list(payload.get("review_reasons", ()))
    payload["manifest"] = to_primitive(outcome.manifest)
    payload["score_breakdown"] = to_primitive(outcome.score) if outcome.score else None
    return payload


class LocalEvaluationRuntime:
    """Runnable composition root for CLI/API and local shadow evaluation."""

    def __init__(
        self,
        root: str | Path = "var",
        *,
        llm_provider: ModelAuditProvider | None = None,
        vlm_provider: ModelAuditProvider | None = None,
        advanced_llm_provider: ModelAuditProvider | None = None,
        advanced_vlm_provider: ModelAuditProvider | None = None,
        slide_renderer: SlideRenderer | None = None,
        model_source_roots: Sequence[str | Path] | str | Path = (),
        model_source_denied_paths: Sequence[str | Path] | str | Path = (),
    ) -> None:
        self.paths = RuntimePaths.under(root)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.repository = JsonRunRepository(self.paths.reports)
        self.audit_log = JsonlAuditLog(self.paths.audit)
        self.artifacts = LocalArtifactStore(self.paths.artifacts)
        self._git_sha = git_sha(Path.cwd())
        self._font_fingerprint = font_fingerprint()
        self.model_source_access_policy = ModelSourceAccessPolicy(
            allowed_roots=tuple(Path(item) for item in _path_values(model_source_roots)),
            denied_paths=tuple(
                Path(item) for item in _path_values(model_source_denied_paths)
            ),
        )
        self.registry = build_default_registry(
            llm_provider=llm_provider,
            vlm_provider=vlm_provider,
            advanced_vlm_provider=advanced_vlm_provider,
            model_source_access_policy=self.model_source_access_policy,
        )
        self.advanced_model_review = (
            AdvancedModelReviewOracle(
                llm_provider=advanced_llm_provider,
                vlm_provider=advanced_vlm_provider,
                source_access_policy=self.model_source_access_policy,
            )
            if advanced_llm_provider is not None or advanced_vlm_provider is not None
            else None
        )
        self._vlm_enabled = vlm_provider is not None
        self._slide_renderer = slide_renderer
        self.feedback_store = JsonlRecordStore(self.paths.root / "feedback" / "records.jsonl")
        self.proposal_store = JsonlRecordStore(self.paths.root / "proposals" / "events.jsonl")
        self.proposals = ParameterProposalService(self.proposal_store, self._audit_proposal)
        self.active_sampler = ActiveSampler()
        supervisor = RunSupervisor(
            DagScheduler(self.registry),
            audit_log=self.audit_log,
            advanced_model_review=self.advanced_model_review,
        )
        self.service = EvaluationService(supervisor)
        self._lock = threading.RLock()
        self._render_lock = threading.RLock()

    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile | None = None,
        *,
        artifacts: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        profile = profile or default_profile(case.scene)
        prepared_artifacts, render_versions = self._prepare_model_artifacts(
            case,
            profile,
            artifacts,
        )
        profile = replace(
            profile,
            metadata={
                **dict(profile.metadata),
                "git_sha": self._git_sha,
                "font_fingerprint": self._font_fingerprint,
                "renderer_versions": {
                    **dict(profile.metadata.get("renderer_versions", {})),
                    "pptx_object_tree": "1.0",
                    **render_versions,
                },
            },
        )
        outcome = self.service.evaluate(
            case,
            profile,
            artifacts=prepared_artifacts,
            run_id=run_id,
        )
        observation_artifact: Mapping[str, Any] | None = None
        if outcome.observations:
            observation_bytes = json.dumps(
                to_primitive(outcome.observations),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            observation_artifact = self.artifacts.put_bytes(
                observation_bytes,
                media_type="application/vnd.ppt-eval.observations+json",
                original_name=f"{outcome.report.run_id}.observations.json",
            )
            outcome = replace(
                outcome,
                manifest=replace(
                    outcome.manifest,
                    artifact_hashes={
                        **dict(outcome.manifest.artifact_hashes),
                        "atomic_observations": str(observation_artifact["sha256"]),
                    },
                ),
            )
            self.audit_log.append(
                run_id=outcome.report.run_id,
                event_type="ATOMIC_OBSERVATIONS_STORED",
                actor="local-runtime",
                payload={
                    "sha256": observation_artifact["sha256"],
                    "count": len(outcome.observations),
                    "media_type": observation_artifact["media_type"],
                },
            )
        payload = normalized_report_payload(outcome)
        if observation_artifact is not None:
            payload["observation_artifact"] = dict(observation_artifact)
            payload["observation_summary"] = _observation_summary(
                outcome.observations
            )
        with self._lock:
            self.repository.save(payload)
        return payload

    def _prepare_model_artifacts(
        self,
        case: EvalCase,
        profile: EvalProfile,
        artifacts: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        prepared = dict(artifacts or {})
        if not self._should_render_model_inputs(profile, prepared):
            return prepared, {}

        try:
            presentation = prepared.get("parsed_presentation")
            if not isinstance(presentation, ParsedPresentation):
                presentation = PptxAdapter().parse(case.pptx_path)
                prepared["parsed_presentation"] = presentation
            if (
                presentation.preflight.has_macros
                or presentation.preflight.has_external_relationships
            ):
                raise RuntimeError(
                    "native rendering is disabled for active or externally linked content"
                )
            render_result, cache_hit = self._render_for_model_audit(
                case,
                expected_slide_count=presentation.slide_count,
            )
        except Exception as exc:
            # Rendering is an optional evidence acquisition step.  Do not let
            # Office/COM/process failures abort deterministic evaluation, and
            # never persist exception text that might contain a local path or
            # a vendor diagnostic.  The VLM Oracle will return auditable N/A.
            prepared["model_audit_rendering"] = {
                "status": "UNAVAILABLE",
                "error_type": type(exc).__name__,
            }
            return prepared, {"model_audit_slides": "unavailable"}

        prepared["render_result"] = render_result
        prepared["slide_images"] = render_result.slide_images
        prepared["model_audit_rendering"] = {
            "status": "READY",
            "cache_hit": cache_hit,
            "renderer_id": render_result.renderer_id,
            "renderer_version": render_result.renderer_version,
            "slide_count": len(render_result.slide_images),
        }
        return prepared, {
            f"model_audit_slides/{render_result.renderer_id}": (
                render_result.renderer_version
            )
        }

    def _should_render_model_inputs(
        self,
        profile: EvalProfile,
        artifacts: Mapping[str, Any],
    ) -> bool:
        if not self._vlm_enabled:
            return False
        pipeline_nodes = profile.metadata.get("pipeline_nodes", ())
        pipeline_oracle_ids = {
            str(item.get("oracle_id"))
            for item in pipeline_nodes
            if isinstance(item, Mapping) and item.get("oracle_id")
        } if isinstance(pipeline_nodes, Sequence) and not isinstance(
            pipeline_nodes, (str, bytes)
        ) else set()
        configured_oracle_ids = set(profile.enabled_oracle_ids) | pipeline_oracle_ids
        if not {
            MODEL_AUDIT_COMPOSITE_ID,
            GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
            GROUNDED_STRUCTURED_DIMENSIONS_VLM_ORACLE_ID,
            STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
            STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
        }.intersection(configured_oracle_ids) and not any(
            oracle_id.startswith("v8.visual.") for oracle_id in configured_oracle_ids
        ):
            return False
        # Explicit caller-supplied rendering artifacts have authority, even if
        # they later fail validation in the Oracle contract.
        return "slide_images" not in artifacts and "render_result" not in artifacts

    def _render_for_model_audit(
        self,
        case: EvalCase,
        *,
        expected_slide_count: int,
    ) -> tuple[RenderResult, bool]:
        input_hash = RunSupervisor._input_hash(case)
        cache_dir = self.paths.render_cache / input_hash
        with self._render_lock:
            cached = _load_render_cache(
                cache_dir,
                expected_input_hash=input_hash,
                expected_slide_count=expected_slide_count,
            )
            if cached is not None:
                return cached, True

            self.paths.render_cache.mkdir(parents=True, exist_ok=True)
            failures: list[str] = []
            for renderer in self._renderer_candidates():
                temporary = Path(
                    tempfile.mkdtemp(
                        prefix=f".{input_hash[:12]}-",
                        dir=self.paths.render_cache,
                    )
                )
                try:
                    result = renderer.render(case.pptx_path, temporary)
                    if not result.slide_images:
                        raise RuntimeError("renderer did not produce slide images")
                    normalized = _normalize_render_output(result, temporary)
                    if len(normalized.slide_images) != expected_slide_count:
                        raise RuntimeError(
                            "renderer output does not cover every presentation page"
                        )
                    _write_render_manifest(
                        temporary,
                        input_hash=input_hash,
                        result=normalized,
                    )
                    if cache_dir.exists():
                        concurrent = _load_render_cache(
                            cache_dir,
                            expected_input_hash=input_hash,
                            expected_slide_count=expected_slide_count,
                        )
                        if concurrent is not None:
                            return concurrent, True
                        _remove_invalid_render_cache(
                            cache_dir,
                            expected_parent=self.paths.render_cache,
                        )
                    os.replace(temporary, cache_dir)
                    persisted = _load_render_cache(
                        cache_dir,
                        expected_input_hash=input_hash,
                        expected_slide_count=expected_slide_count,
                    )
                    if persisted is None:
                        raise RuntimeError("render cache failed integrity validation")
                    return persisted, False
                except Exception as exc:
                    failures.append(type(exc).__name__)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary, ignore_errors=True)

            summary = ",".join(failures) or "NO_RENDERER"
            raise RuntimeError(f"automatic slide rendering unavailable ({summary})")

    def _renderer_candidates(self) -> Sequence[SlideRenderer]:
        if self._slide_renderer is not None:
            return (self._slide_renderer,)
        if os.name == "nt":
            # Native PowerPoint has the highest fidelity on Windows.  A local
            # LibreOffice installation is a safe fallback, though its current
            # adapter may yield only a PDF and therefore an auditable VLM N/A.
            return (PowerPointRenderer(), LibreOfficeRenderer())
        return (LibreOfficeRenderer(),)

    def get(self, run_id: str) -> dict[str, Any]:
        return self.repository.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        return self.repository.list()

    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload["run_id"])
        self.repository.get(run_id)
        record = self.repository.add_review(payload)
        self.audit_log.append(
            run_id=run_id,
            event_type="HUMAN_REVIEW_RECORDED",
            actor=str(payload.get("reviewer_id") or "reviewer"),
            payload=record,
        )
        return record

    def export(self, run_id: str, output_dir: str | Path) -> tuple[Path, Path]:
        return export_run_report(self.get(run_id), output_dir)

    def add_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.repository.get(str(payload["run_id"]))
        record = feedback_from_mapping(payload)
        self.feedback_store.append(record)
        primitive = to_primitive(record)
        if not isinstance(primitive, dict):
            raise TypeError("feedback record must normalize to a mapping")
        normalized = {str(key): value for key, value in primitive.items()}
        self.audit_log.append(
            run_id=record.run_id,
            event_type="FEEDBACK_RECORDED",
            actor=str(payload.get("actor") or "feedback-pipeline"),
            payload=normalized,
        )
        return normalized

    def _audit_proposal(self, proposal: Any) -> None:
        self.audit_log.append(
            run_id=proposal.proposal_id,
            event_type=f"PARAMETER_PROPOSAL_{proposal.status.value}",
            actor="parameter-governance",
            payload=to_primitive(proposal),
        )


def _normalize_render_output(result: RenderResult, output_dir: Path) -> RenderResult:
    root = output_dir.resolve()
    images: list[Path] = []
    for image in result.slide_images:
        path = Path(image).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise RuntimeError("renderer returned an image outside its output directory")
        if path.stat().st_size <= 0:
            raise RuntimeError("renderer returned an empty slide image")
        images.append(path)
    if len(images) != len({path.name.casefold() for path in images}):
        raise RuntimeError("renderer returned duplicate slide image names")
    return RenderResult(
        renderer_id=str(result.renderer_id),
        renderer_version=str(result.renderer_version),
        slide_images=tuple(images),
        document_path=(
            Path(result.document_path).resolve()
            if result.document_path is not None
            else None
        ),
        warnings=tuple(str(item) for item in result.warnings),
    )


def _write_render_manifest(
    output_dir: Path,
    *,
    input_hash: str,
    result: RenderResult,
) -> None:
    payload = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "renderer_id": result.renderer_id,
        "renderer_version": result.renderer_version,
        "slide_count": len(result.slide_images),
        "slide_images": [path.name for path in result.slide_images],
        "warnings": list(result.warnings),
    }
    (output_dir / _RENDER_MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _load_render_cache(
    cache_dir: Path,
    *,
    expected_input_hash: str,
    expected_slide_count: int,
) -> RenderResult | None:
    manifest_path = cache_dir / _RENDER_MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != "1.0":
            return None
        if payload.get("input_hash") != expected_input_hash:
            return None
        if payload.get("slide_count") != expected_slide_count:
            return None
        renderer_id = payload.get("renderer_id")
        renderer_version = payload.get("renderer_version")
        names = payload.get("slide_images")
        warnings = payload.get("warnings", ())
        if not isinstance(renderer_id, str) or not renderer_id.strip():
            return None
        if not isinstance(renderer_version, str) or not renderer_version.strip():
            return None
        if isinstance(names, (str, bytes)) or not isinstance(names, list) or not names:
            return None
        if len(names) != expected_slide_count:
            return None
        if len(names) != len(
            {name.casefold() for name in names if isinstance(name, str)}
        ):
            return None
        if isinstance(warnings, (str, bytes)) or not isinstance(warnings, list):
            return None
        images: list[Path] = []
        cache_root = cache_dir.resolve()
        for name in names:
            if not isinstance(name, str) or Path(name).name != name:
                return None
            image = (cache_dir / name).resolve()
            if not image.is_relative_to(cache_root):
                return None
            if not image.is_file() or image.stat().st_size <= 0:
                return None
            images.append(image)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return RenderResult(
        renderer_id=renderer_id.strip(),
        renderer_version=renderer_version.strip(),
        slide_images=tuple(images),
        warnings=tuple(str(item) for item in warnings),
    )


def _remove_invalid_render_cache(cache_dir: Path, *, expected_parent: Path) -> None:
    resolved = cache_dir.resolve()
    parent = expected_parent.resolve()
    if resolved.parent != parent or len(resolved.name) != 64:
        raise RuntimeError("refusing to remove an unexpected render cache path")
    if any(character not in "0123456789abcdef" for character in resolved.name):
        raise RuntimeError("refusing to remove an invalid render cache key")
    shutil.rmtree(resolved)


def build_runtime_from_environment(
    root: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    workspace_root: str | Path | None = None,
    slide_renderer: SlideRenderer | None = None,
    model_source_roots: Sequence[str | Path] | str | Path | None = None,
) -> LocalEvaluationRuntime:
    """Create the opt-in infrastructure-aware runtime used by CLI/API/worker.

    This is deliberately separate from ``LocalEvaluationRuntime.__init__`` so
    direct construction remains offline and deterministic.  Presence of
    ``DASHSCOPE_API_KEY`` or the ignored local key file enables the
    qwen3.8-flash primary tier.  The independent ``ZAI_API_KEY`` (or ignored
    local BigModel key file) enables glm-5.3-flash criterion-isomorphic
    fallback.  Each provider has its own kill switch, endpoint and timeout.
    Local ``source_materials`` files remain unavailable to remote model audits
    unless their parent root is explicitly supplied through
    ``model_source_roots`` or ``PPT_EVAL_MODEL_SOURCE_ROOTS``.
    """

    env = dict(os.environ if environment is None else environment)
    project_root = _find_workspace_root(workspace_root)
    qwen_settings = QwenAuditSettings.from_environment(
        env,
        workspace_root=project_root,
    )
    zhipu_settings = ZhipuAuditSettings.from_environment(
        env,
        workspace_root=project_root,
    )
    protected_model_credentials = tuple(
        secret
        for secret in (qwen_settings.api_key, zhipu_settings.api_key)
        if secret
    )
    flash, _legacy_qwen_advanced = qwen_settings.providers(
        protected_secrets=protected_model_credentials,
    )
    advanced = zhipu_settings.provider(
        protected_secrets=protected_model_credentials,
    )
    data_root = root if root is not None else env.get("PPT_EVAL_DATA_DIR", "var")
    source_roots = _configured_model_source_roots(
        model_source_roots,
        environment=env,
        workspace_root=project_root,
    )
    key_file_value = str(
        env.get("PPT_EVAL_DASHSCOPE_API_KEY_FILE") or DEFAULT_QWEN_KEY_FILE
    ).strip()
    key_file = Path(key_file_value)
    if not key_file.is_absolute():
        key_file = project_root / key_file
    zhipu_key_file_value = str(
        env.get("PPT_EVAL_ZHIPU_API_KEY_FILE") or DEFAULT_ZHIPU_KEY_FILE
    ).strip()
    zhipu_key_file = Path(zhipu_key_file_value)
    if not zhipu_key_file.is_absolute():
        zhipu_key_file = project_root / zhipu_key_file
    return LocalEvaluationRuntime(
        data_root,
        llm_provider=flash,
        vlm_provider=flash,
        advanced_llm_provider=advanced,
        advanced_vlm_provider=advanced,
        slide_renderer=slide_renderer,
        model_source_roots=source_roots,
        model_source_denied_paths=(
            project_root / "api",
            project_root / ".git",
            project_root / ".env",
            key_file,
            zhipu_key_file,
        ),
    )


def _configured_model_source_roots(
    explicit: Sequence[str | Path] | str | Path | None,
    *,
    environment: Mapping[str, str],
    workspace_root: Path,
) -> tuple[Path, ...]:
    if explicit is None:
        raw = str(environment.get("PPT_EVAL_MODEL_SOURCE_ROOTS") or "").strip()
        values: Sequence[str | Path] = tuple(
            part.strip() for part in raw.split(os.pathsep) if part.strip()
        )
    else:
        values = _path_values(explicit)

    roots: list[Path] = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = workspace_root / path
        roots.append(path.resolve(strict=False))
    return tuple(roots)


def _path_values(
    values: Sequence[str | Path] | str | Path,
) -> tuple[str | Path, ...]:
    if isinstance(values, (str, Path)):
        return (values,)
    return tuple(values)


def _find_workspace_root(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value).resolve()
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "ppt_eval"
        ).is_dir():
            return candidate
    return start


def _observation_summary(
    observations: Sequence[AtomicObservation],
) -> Mapping[str, Any]:
    by_scope: dict[str, int] = {}
    by_status: dict[str, int] = {}
    metric_ids: set[str] = set()
    for observation in observations:
        scope = observation.scope.value
        status = observation.metric_status.value
        by_scope[scope] = by_scope.get(scope, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        metric_ids.add(observation.metric_id)
    return {
        "count": len(observations),
        "metric_ids": sorted(metric_ids),
        "by_scope": dict(sorted(by_scope.items())),
        "by_status": dict(sorted(by_status.items())),
    }


_runtime: LocalEvaluationRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> LocalEvaluationRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = build_runtime_from_environment()
        return _runtime


__all__ = [
    "LocalEvaluationRuntime",
    "RuntimePaths",
    "SlideRenderer",
    "build_runtime_from_environment",
    "get_runtime",
    "normalized_report_payload",
]
