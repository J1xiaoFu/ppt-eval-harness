from __future__ import annotations

import builtins
import json
import mimetypes
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
from ppt_eval.application import (
    DagScheduler,
    EvaluationService,
    RunSupervisor,
    audit_task_sort_key,
    build_attention_projection,
    build_review_task_summary,
    normalize_review_payload,
)
from ppt_eval.config import default_profile
from ppt_eval.domain import AtomicObservation, EvalCase, EvalProfile
from ppt_eval.flywheel import (
    JsonlRecordStore,
    ParameterProposalService,
    feedback_from_mapping,
)
from ppt_eval.infrastructure import (
    JsonlAuditLog,
    JsonRunRepository,
    LocalArtifactStore,
    QwenAuditSettings,
    ZhipuAuditSettings,
    font_fingerprint,
    git_sha,
    sha256_file,
    to_primitive,
)
from ppt_eval.oracles import build_default_registry
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
        vlm_provider: ModelAuditProvider | None = None,
        advanced_vlm_provider: ModelAuditProvider | None = None,
        slide_renderer: SlideRenderer | None = None,
        review_rendering: bool = False,
    ) -> None:
        self.paths = RuntimePaths.under(root)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.repository = JsonRunRepository(self.paths.reports)
        self.audit_log = JsonlAuditLog(self.paths.audit)
        self.artifacts = LocalArtifactStore(self.paths.artifacts)
        self._git_sha = git_sha(Path.cwd())
        self._font_fingerprint = font_fingerprint()
        self.registry = build_default_registry(
            vlm_provider=vlm_provider,
            advanced_vlm_provider=advanced_vlm_provider,
        )
        self._vlm_enabled = vlm_provider is not None
        self._slide_renderer = slide_renderer
        self._review_rendering = bool(review_rendering)
        self.feedback_store = JsonlRecordStore(self.paths.root / "feedback" / "records.jsonl")
        self.proposal_store = JsonlRecordStore(self.paths.root / "proposals" / "events.jsonl")
        self.proposals = ParameterProposalService(self.proposal_store, self._audit_proposal)
        supervisor = RunSupervisor(
            DagScheduler(self.registry),
            audit_log=self.audit_log,
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
        _validate_runtime_profile(profile)
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
        artifact_hashes = dict(outcome.manifest.artifact_hashes)
        input_artifacts, input_hashes = self._persist_case_inputs(
            case,
            run_id=outcome.report.run_id,
        )
        artifact_hashes.update(input_hashes)
        source_reference = input_artifacts.get("source_pptx")
        source_artifact = (
            source_reference if isinstance(source_reference, Mapping) else None
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
            artifact_hashes["atomic_observations"] = str(
                observation_artifact["sha256"]
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
        render_result = prepared_artifacts.get("render_result")
        render_manifest_artifact: Mapping[str, Any] | None = None
        render_cache_key = str(outcome.manifest.input_hash or "")
        if isinstance(render_result, RenderResult):
            render_manifest_path = (
                self.paths.render_cache
                / render_cache_key
                / _RENDER_MANIFEST_NAME
            )
            if render_manifest_path.is_file():
                render_manifest_artifact = self.artifacts.put(
                    render_manifest_path,
                    media_type="application/vnd.ppt-eval.slide-render-manifest+json",
                )
                artifact_hashes["slide_render_manifest"] = str(
                    render_manifest_artifact["sha256"]
                )
                self.audit_log.append(
                    run_id=outcome.report.run_id,
                    event_type="SLIDE_RENDER_MANIFEST_STORED",
                    actor="local-runtime",
                    payload={
                        "sha256": render_manifest_artifact["sha256"],
                        "slide_count": len(render_result.slide_images),
                        "renderer_id": render_result.renderer_id,
                        "renderer_version": render_result.renderer_version,
                    },
                )
        outcome = replace(
            outcome,
            manifest=replace(outcome.manifest, artifact_hashes=artifact_hashes),
        )
        payload = normalized_report_payload(outcome)
        if source_artifact is not None:
            payload["source_artifact"] = dict(source_artifact)
        payload["input_artifacts"] = input_artifacts
        if observation_artifact is not None:
            payload["observation_artifact"] = dict(observation_artifact)
            payload["observation_summary"] = _observation_summary(
                outcome.observations
            )
        if isinstance(render_result, RenderResult):
            payload["render_artifact"] = {
                "cache_key": render_cache_key,
                "renderer_id": render_result.renderer_id,
                "renderer_version": render_result.renderer_version,
                "slide_count": len(render_result.slide_images),
                "warnings": list(render_result.warnings),
                "manifest_sha256": (
                    render_manifest_artifact["sha256"]
                    if render_manifest_artifact is not None
                    else None
                ),
            }
            if render_manifest_artifact is not None:
                payload["slide_render_manifest_artifact"] = dict(
                    render_manifest_artifact
                )
        with self._lock:
            self.repository.save(payload)
        return payload

    def _persist_case_inputs(
        self,
        case: EvalCase,
        *,
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Bind every file-backed evaluation input to CAS after an outcome exists."""

        configured = case.metadata.get("artifact_hashes")
        expected_hashes = configured if isinstance(configured, Mapping) else {}
        artifact_hashes: dict[str, str] = {}

        def persist(
            path_value: str,
            *,
            role: str,
            index: int | None,
            media_type: str | None = None,
            original_name: str | None = None,
        ) -> dict[str, Any] | None:
            artifact_role = role if index is None else f"{role}/{index}"
            expected = str(expected_hashes.get(artifact_role) or "")
            source = Path(path_value)
            try:
                available = source.is_file()
            except OSError:
                available = False
            if not available:
                if expected:
                    raise ValueError(
                        "run input artifact is unavailable before persistence"
                    )
                return None
            resolved_media_type = media_type or (
                mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            )
            stored = self.artifacts.put(
                source,
                media_type=resolved_media_type,
                original_name=_safe_artifact_name(
                    original_name or source.name,
                    fallback=(
                        "presentation.pptx"
                        if index is None
                        else f"{role}-{index}.bin"
                    ),
                ),
                expected_sha256=expected or None,
            )
            digest = str(stored["sha256"])
            artifact_hashes[artifact_role] = digest
            return {
                **dict(stored),
                "role": role,
                "index": index,
            }

        source_pptx = persist(
            case.pptx_path,
            role="source_pptx",
            index=None,
            media_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
            original_name=str(case.metadata.get("source_pptx_original_name") or ""),
        )
        source_materials = [
            reference
            for index, value in enumerate(case.source_materials, start=1)
            if (
                reference := persist(
                    value,
                    role="source_material",
                    index=index,
                )
            )
            is not None
        ]
        assets = [
            reference
            for index, value in enumerate(case.assets, start=1)
            if (
                reference := persist(
                    value,
                    role="asset",
                    index=index,
                )
            )
            is not None
        ]
        input_artifacts = {
            "schema_version": "1.0",
            "source_pptx": source_pptx,
            "source_materials": source_materials,
            "assets": assets,
        }
        if source_pptx is not None:
            self.audit_log.append(
                run_id=run_id,
                event_type="SOURCE_PRESENTATION_STORED",
                actor="local-runtime",
                payload={
                    "sha256": source_pptx["sha256"],
                    "size_bytes": source_pptx["size_bytes"],
                    "media_type": source_pptx["media_type"],
                },
            )
        self.audit_log.append(
            run_id=run_id,
            event_type="RUN_INPUT_ARTIFACTS_STORED",
            actor="local-runtime",
            payload={
                "source_material_hashes": [
                    item["sha256"] for item in source_materials
                ],
                "asset_hashes": [item["sha256"] for item in assets],
            },
        )
        return input_artifacts, artifact_hashes

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
        if not self._vlm_enabled and not self._review_rendering:
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
        if self._review_rendering:
            return "slide_images" not in artifacts and "render_result" not in artifacts
        if not any(
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

    def reviews(self, run_id: str) -> builtins.list[dict[str, Any]]:
        self.repository.get(run_id)
        return self.repository.list_reviews(run_id)

    def observations(self, run_id: str) -> builtins.list[dict[str, Any]]:
        report = self.repository.get(run_id)
        reference = report.get("observation_artifact")
        if not isinstance(reference, Mapping):
            return []
        path, _metadata = self.review_artifact(run_id, "atomic_observations")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, builtins.list):
            raise ValueError("observation artifact must contain a JSON array")
        return [
            {str(key): value for key, value in item.items()}
            for item in payload
            if isinstance(item, Mapping)
        ]

    def review_slide_paths(self, run_id: str) -> tuple[Path, ...]:
        report = self.repository.get(run_id)
        reference = report.get("render_artifact")
        if not isinstance(reference, Mapping):
            return ()
        manifest = report.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        input_hash = str(manifest.get("input_hash") or "")
        cache_key = str(reference.get("cache_key") or "")
        slide_count = reference.get("slide_count")
        if (
            cache_key != input_hash
            or len(cache_key) != 64
            or any(character not in "0123456789abcdef" for character in cache_key)
            or isinstance(slide_count, bool)
            or not isinstance(slide_count, int)
            or slide_count < 1
        ):
            return ()
        result = _load_render_cache(
            self.paths.render_cache / cache_key,
            expected_input_hash=input_hash,
            expected_slide_count=slide_count,
        )
        return result.slide_images if result is not None else ()

    def review_artifact(
        self,
        run_id: str,
        role: str,
    ) -> tuple[Path, Mapping[str, Any]]:
        report = self.repository.get(run_id)
        role_to_field = {
            "source_pptx": "source_artifact",
            "atomic_observations": "observation_artifact",
            "slide_render_manifest": "slide_render_manifest_artifact",
        }
        field = role_to_field.get(role)
        if field is None:
            raise KeyError(role)
        reference = report.get(field)
        if not isinstance(reference, Mapping):
            raise FileNotFoundError(role)
        digest = str(reference.get("sha256") or "")
        manifest = report.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        artifact_hashes = manifest.get("artifact_hashes")
        artifact_hashes = artifact_hashes if isinstance(artifact_hashes, Mapping) else {}
        if artifact_hashes.get(role) != digest:
            raise ValueError("artifact hash does not match the run manifest")
        path = self.artifacts.resolve(digest)
        if sha256_file(path) != digest:
            raise ValueError("artifact content hash verification failed")
        metadata = {
            "role": role,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "media_type": str(reference.get("media_type") or "application/octet-stream"),
            "original_name": str(reference.get("original_name") or role),
        }
        return path, metadata

    def review_input_artifact(
        self,
        run_id: str,
        role: str,
        index: int,
    ) -> tuple[Path, Mapping[str, Any]]:
        if role not in {"source_material", "asset"}:
            raise KeyError(role)
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise KeyError(index)
        report = self.repository.get(run_id)
        inputs = report.get("input_artifacts")
        inputs = inputs if isinstance(inputs, Mapping) else {}
        field = "source_materials" if role == "source_material" else "assets"
        values = inputs.get(field)
        if not isinstance(values, list) or index > len(values):
            raise FileNotFoundError(f"{role}/{index}")
        reference = values[index - 1]
        if not isinstance(reference, Mapping):
            raise ValueError("stored input artifact reference is invalid")
        if reference.get("role") != role or reference.get("index") != index:
            raise ValueError("stored input artifact lineage is invalid")
        digest = str(reference.get("sha256") or "")
        manifest = report.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        hashes = manifest.get("artifact_hashes")
        hashes = hashes if isinstance(hashes, Mapping) else {}
        if hashes.get(f"{role}/{index}") != digest:
            raise ValueError("input artifact hash does not match the run manifest")
        path = self.artifacts.resolve(digest)
        if sha256_file(path) != digest:
            raise ValueError("input artifact content hash verification failed")
        return path, {
            "role": role,
            "index": index,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "media_type": str(
                reference.get("media_type") or "application/octet-stream"
            ),
            "original_name": _safe_artifact_name(
                reference.get("original_name"),
                fallback=f"{role}-{index}.bin",
            ),
        }

    def review_inputs(
        self, run_id: str
    ) -> dict[str, builtins.list[dict[str, Any]]]:
        report = self.repository.get(run_id)
        inputs = report.get("input_artifacts")
        inputs = inputs if isinstance(inputs, Mapping) else {}
        result: dict[str, builtins.list[dict[str, Any]]] = {
            "source_materials": [],
            "assets": [],
        }
        for role, field in (
            ("source_material", "source_materials"),
            ("asset", "assets"),
        ):
            values = inputs.get(field)
            if not isinstance(values, list):
                continue
            for index, reference in enumerate(values, start=1):
                raw = reference if isinstance(reference, Mapping) else {}
                try:
                    _path, metadata = self.review_input_artifact(
                        run_id,
                        role,
                        index,
                    )
                    item = {**dict(metadata), "available": True}
                except (OSError, ValueError, FileNotFoundError):
                    item = {
                        "role": role,
                        "index": index,
                        "sha256": str(raw.get("sha256") or "") or None,
                        "original_name": _safe_artifact_name(
                            raw.get("original_name"),
                            fallback=f"{role}-{index}.bin",
                        ),
                        "media_type": str(
                            raw.get("media_type") or "application/octet-stream"
                        ),
                        "size_bytes": raw.get("size_bytes"),
                        "available": False,
                    }
                result[field].append(item)
        return result

    def list_review_tasks(
        self,
        *,
        view: str = "queue",
        query: str = "",
        decision: str | None = None,
        coverage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if view not in {"queue", "all", "completed"}:
            raise ValueError("view must be queue, all, or completed")
        if limit < 1 or limit > 200 or offset < 0:
            raise ValueError("invalid pagination")
        query_text = query.strip().casefold()
        summaries: list[dict[str, Any]] = []
        for report in self.repository.list():
            run_id = str(report.get("run_id") or "")
            reviews = self.repository.list_reviews(run_id)
            projection_report = report
            try:
                observations = self.observations(run_id)
            except (OSError, ValueError, json.JSONDecodeError):
                observations = []
                projection_report = _with_integrity_error(
                    report, "ATOMIC_OBSERVATION_ARTIFACT_INVALID"
                )
            page_count = len(self.review_slide_paths(run_id))
            summary = build_review_task_summary(
                projection_report,
                observations=observations,
                reviews=reviews,
                page_count=page_count,
            )
            if view == "queue" and (
                summary["review_state"] == "RESOLVED" or summary["priority"] == "P3"
            ):
                continue
            if view == "completed" and summary["review_state"] != "RESOLVED":
                continue
            if decision and summary["decision"] != decision:
                continue
            if coverage and summary["coverage"] != coverage:
                continue
            if query_text and query_text not in (
                f"{summary['case_id']} {summary['run_id']} {summary['scenario']}"
            ).casefold():
                continue
            summaries.append(summary)
        summaries.sort(key=audit_task_sort_key)
        total = len(summaries)
        return {
            "items": summaries[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "triage_policy_version": "audit-attention@1.0.0",
        }

    def review_task(self, run_id: str) -> dict[str, Any]:
        report = self.repository.get(run_id)
        observation_integrity = True
        try:
            observations = self.observations(run_id)
        except (OSError, ValueError, json.JSONDecodeError):
            observations = []
            observation_integrity = False
            report = _with_integrity_error(
                report, "ATOMIC_OBSERVATION_ARTIFACT_INVALID"
            )
        reviews = self.repository.list_reviews(run_id)
        slides = self.review_slide_paths(run_id)
        summary = build_review_task_summary(
            report,
            observations=observations,
            reviews=reviews,
            page_count=len(slides),
        )
        attention = build_attention_projection(report, observations)
        results = [
            {str(key): value for key, value in item.items()}
            for item in report.get("results", ())
            if isinstance(item, Mapping)
        ]
        gate_results = [
            item
            for item in results
            if str(item.get("score_role") or "").endswith("MULTIPLIER")
            or str(item.get("metric_id") or "").endswith("integrity")
        ]
        model_routes = []
        for item in results:
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            attempts = metadata.get("routing_attempts")
            if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
                continue
            model_routes.append(
                {
                    "metric_id": item.get("metric_id"),
                    "criterion_id": metadata.get("criterion_id"),
                    "selected_tier": metadata.get("selected_tier"),
                    "escalation_reason": metadata.get("escalation_reason"),
                    "sampled_pages": list(metadata.get("sampled_pages", ())),
                    "forced_rule_pages": list(metadata.get("forced_rule_pages", ())),
                    "attempts": [
                        {
                            "tier": attempt.get("tier"),
                            "selected": attempt.get("selected"),
                            "execution_status": attempt.get("execution_status"),
                            "metric_status": attempt.get("metric_status"),
                            "confidence": attempt.get("confidence"),
                            "error_code": attempt.get("error_code"),
                        }
                        for attempt in attempts
                        if isinstance(attempt, Mapping)
                    ],
                }
            )
        manifest = report.get("manifest")
        manifest = dict(manifest) if isinstance(manifest, Mapping) else {}
        artifacts: dict[str, dict[str, Any]] = {
            "report": {"available": True},
            "atomic_observations": {
                "available": observation_integrity
                and isinstance(report.get("observation_artifact"), Mapping),
                "sha256": _artifact_digest(report, "observation_artifact"),
            },
            "source_pptx": {
                "available": isinstance(report.get("source_artifact"), Mapping),
                "sha256": _artifact_digest(report, "source_artifact"),
            },
            "slide_render_manifest": {
                "available": isinstance(
                    report.get("slide_render_manifest_artifact"), Mapping
                ),
                "sha256": _artifact_digest(
                    report, "slide_render_manifest_artifact"
                ),
            },
        }
        return {
            **summary,
            "triage_policy_version": attention["policy_version"],
            "report_hash": manifest.get("result_hash"),
            "observation_hash": artifacts["atomic_observations"]["sha256"],
            "review_reasons": list(report.get("review_reasons", ())),
            "issues": attention["items"],
            "slides": [
                {"page_number": index, "available": True}
                for index in range(1, len(slides) + 1)
            ],
            "results": results,
            "gate_results": gate_results,
            "model_routes": model_routes,
            "manifest": manifest,
            "artifacts": artifacts,
            "inputs": self.review_inputs(run_id),
            "reviews": reviews,
            "audit_integrity": {
                "chain_valid": self.audit_log.verify()[0],
                "observation_artifact_valid": observation_integrity,
            },
        }

    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload["run_id"])
        report = self.repository.get(run_id)
        try:
            observations = self.observations(run_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if str(payload.get("verdict") or "").upper() != "REQUEST_MORE_EVIDENCE":
                raise ValueError(
                    "invalid observation artifact permits only REQUEST_MORE_EVIDENCE"
                ) from exc
            observations = []
            report = _with_integrity_error(
                report, "ATOMIC_OBSERVATION_ARTIFACT_INVALID"
            )
        attention = build_attention_projection(report, observations)
        normalized = normalize_review_payload(
            report,
            payload,
            valid_issue_ids=[str(item["issue_id"]) for item in attention["items"]],
        )
        if normalized["verdict"] in {
            "CONFIRM_SYSTEM_DECISION",
            "OVERRIDE_DECISION",
        }:
            required_issue_ids = {
                str(item["issue_id"])
                for item in attention["items"]
                if item.get("priority") in {"P0", "P1"}
            }
            resolved_issue_ids = {
                str(item["issue_id"])
                for item in normalized["issue_resolutions"]
            }
            missing = sorted(required_issue_ids - resolved_issue_ids)
            if missing:
                raise ValueError(
                    "all P0/P1 attention issues must be resolved before final review"
                )
        existing_review_ids = {
            str(item.get("review_id"))
            for item in self.repository.list_reviews(run_id)
            if item.get("review_id")
        }
        record = self.repository.add_review(normalized)
        if str(record.get("review_id")) not in existing_review_ids:
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
        "schema_version": "1.1",
        "input_hash": input_hash,
        "renderer_id": result.renderer_id,
        "renderer_version": result.renderer_version,
        "slide_count": len(result.slide_images),
        "slide_images": [
            {
                "name": path.name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in result.slide_images
        ],
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
        schema_version = payload.get("schema_version")
        if schema_version not in {"1.0", "1.1"}:
            return None
        if payload.get("input_hash") != expected_input_hash:
            return None
        if payload.get("slide_count") != expected_slide_count:
            return None
        renderer_id = payload.get("renderer_id")
        renderer_version = payload.get("renderer_version")
        entries = payload.get("slide_images")
        warnings = payload.get("warnings", ())
        if not isinstance(renderer_id, str) or not renderer_id.strip():
            return None
        if not isinstance(renderer_version, str) or not renderer_version.strip():
            return None
        if (
            isinstance(entries, (str, bytes))
            or not isinstance(entries, list)
            or not entries
        ):
            return None
        if len(entries) != expected_slide_count:
            return None
        names = [
            item if isinstance(item, str) else item.get("name")
            for item in entries
            if isinstance(item, (str, Mapping))
        ]
        if len(names) != len(entries) or not all(isinstance(name, str) for name in names):
            return None
        string_names = [str(name) for name in names]
        if len(string_names) != len({name.casefold() for name in string_names}):
            return None
        if isinstance(warnings, (str, bytes)) or not isinstance(warnings, list):
            return None
        images: list[Path] = []
        cache_root = cache_dir.resolve()
        for index, name in enumerate(string_names):
            if Path(name).name != name:
                return None
            image = (cache_dir / name).resolve()
            if not image.is_relative_to(cache_root):
                return None
            if not image.is_file() or image.stat().st_size <= 0:
                return None
            if schema_version == "1.1":
                entry = entries[index]
                if not isinstance(entry, Mapping):
                    return None
                expected_sha256 = entry.get("sha256")
                expected_size = entry.get("size_bytes")
                if (
                    not isinstance(expected_sha256, str)
                    or sha256_file(image) != expected_sha256
                    or isinstance(expected_size, bool)
                    or not isinstance(expected_size, int)
                    or image.stat().st_size != expected_size
                ):
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
) -> LocalEvaluationRuntime:
    """Create the opt-in infrastructure-aware runtime used by CLI and API.

    This is deliberately separate from ``LocalEvaluationRuntime.__init__`` so
    direct construction remains offline and deterministic.  Presence of
    ``DASHSCOPE_API_KEY`` or the ignored local key file enables the
    qwen3.8-flash primary tier.  The independent ``ZAI_API_KEY`` (or ignored
    local BigModel key file) enables glm-5.3-flash criterion-isomorphic
    fallback.  Each provider has its own kill switch, endpoint and timeout.
    Current VLM requests contain rendered pages plus bounded case text; local
    source files are never opened or uploaded by the model-audit path.
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
    flash = qwen_settings.provider(
        protected_secrets=protected_model_credentials,
    )
    advanced = zhipu_settings.provider(
        protected_secrets=protected_model_credentials,
    )
    data_root = root if root is not None else env.get("PPT_EVAL_DATA_DIR", "var")
    return LocalEvaluationRuntime(
        data_root,
        vlm_provider=flash,
        advanced_vlm_provider=advanced,
        slide_renderer=slide_renderer,
        review_rendering=_environment_flag(
            env.get("PPT_EVAL_REVIEW_RENDERING_ENABLED"),
            default=False,
        ),
    )


def _validate_runtime_profile(profile: EvalProfile) -> None:
    """Keep the release runtime on the single supported write contract."""

    if profile.version != "8.3":
        raise ValueError(
            "the release runtime accepts only Profile version 8.3; "
            "use archive/v8.3-pre-release for historical replay"
        )
    pipeline_nodes = profile.metadata.get("pipeline_nodes")
    if (
        isinstance(pipeline_nodes, (str, bytes))
        or not isinstance(pipeline_nodes, Sequence)
        or not pipeline_nodes
    ):
        raise ValueError("Profile 8.3 requires a non-empty pipeline_nodes DAG")


def _environment_flag(value: object, *, default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("PPT_EVAL_REVIEW_RENDERING_ENABLED must be a boolean")


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


def _artifact_digest(report: Mapping[str, Any], field: str) -> str | None:
    reference = report.get(field)
    if not isinstance(reference, Mapping):
        return None
    digest = reference.get("sha256")
    return str(digest) if isinstance(digest, str) and digest else None


def _safe_artifact_name(value: object, *, fallback: str) -> str:
    name = Path(str(value or "")).name.strip()
    if (
        not name
        or len(name) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or "/" in name
        or "\\" in name
    ):
        return fallback
    return name


def _with_integrity_error(
    report: Mapping[str, Any], error_code: str
) -> dict[str, Any]:
    copied = {str(key): value for key, value in report.items()}
    errors = [str(item) for item in report.get("errors", ())]
    copied["errors"] = list(dict.fromkeys([*errors, error_code]))
    return copied


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
