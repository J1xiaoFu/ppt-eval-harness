"""Run the pinned 22-deck Slides-Align slice with the current Profile 8.4.

The benchmark is intentionally separate from the production review queue:
human ranks are read only here, for within-topic diagnostic statistics, and
are never placed in the evaluation request or used to alter a score.  Every
deck gets an isolated runtime directory and the official, manifest-pinned PNG
renders are supplied as model evidence.

Typical live run from the repository root::

    python scripts/benchmarks/evaluate_slides_align_profile84.py \
        --suite-root var/datasets/slides_align_three_topics \
        --output-dir var/benchmarks/slides-align-profile84 \
        --workers 3 --resume

The script never reads or prints credentials itself.  Provider discovery and
credential loading stay inside ``build_runtime_from_environment``.  Unless
``--allow-offline`` is explicit, the run fails closed when the primary visual
provider is not configured.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ppt_eval.config import load_profile  # noqa: E402
from ppt_eval.domain import EvalCase, EvalProfile, SceneType  # noqa: E402
from ppt_eval.infrastructure import JsonlAuditLog, git_sha, to_primitive  # noqa: E402
from ppt_eval.runtime import build_runtime_from_environment  # noqa: E402

BENCHMARK_ID = "slides-align-profile84-live"
BENCHMARK_VERSION = "1.2.0"
CASE_RECORD_SCHEMA_VERSION = "1.0"
COMPARISON_SCHEMA_VERSION = "1.0"
EXPECTED_PROFILE_VERSION = "8.4"
EXPECTED_PROFILE_ID = "finished-deck-v8"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PPTX_SLIDE_RE = re.compile(r"ppt/slides/slide([1-9][0-9]*)\.xml")
_SAFE_OUTPUT_COMPONENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
_KNOWN_DECISIONS = frozenset({"PASS", "FAIL", "REVIEW", "ERROR"})
_KNOWN_COVERAGES = frozenset({"FULL", "DEGRADED", "BASE_ONLY", "UNASSESSABLE"})
_MODEL_RESPONSE_LEGALITY_THRESHOLD = 0.98


@dataclass(frozen=True, slots=True)
class RenderSpec:
    page_number: int
    path: Path
    relative_path: str
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class CaseSpec:
    topic: str
    topic_slug: str
    dataset_id: str
    product: str
    human_rank: int
    case_id: str
    pptx_path: Path
    pptx_relative_path: str
    pptx_sha256: str
    pptx_bytes: int
    renders: tuple[RenderSpec, ...]

    @property
    def case_key(self) -> str:
        return f"{self.topic_slug}/{self.case_id}"

    @property
    def evaluation_case_id(self) -> str:
        # Deliberately independent of product name and human rank.
        digest = hashlib.sha256(
            f"{self.dataset_id}\n{self.pptx_sha256}".encode("utf-8")
        ).hexdigest()
        return f"slides-align-{digest[:24]}"

    @property
    def render_set_fingerprint(self) -> str:
        return _stable_hash(
            [
                {"page_number": item.page_number, "sha256": item.sha256}
                for item in self.renders
            ]
        )


@dataclass(frozen=True, slots=True)
class SuiteSpec:
    root: Path
    dataset_id: str
    revision: str
    cases: tuple[CaseSpec, ...]
    manifest_file_count: int
    manifest_bytes: int

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.topic for item in self.cases))


class EvaluationRuntime(Protocol):
    def evaluate(
        self,
        case: EvalCase,
        profile: EvalProfile,
        *,
        artifacts: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_clean_evaluation_checkout() -> None:
    """Reject live benchmark runs whose Git SHA does not describe the code."""

    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or completed.stdout.strip():
        raise RuntimeError("live benchmark requires a clean evaluation checkout")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _sha256(value: object, label: str) -> str:
    digest = str(value or "").lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _safe_output_component(value: object, label: str) -> str:
    text = str(value or "")
    if not _SAFE_OUTPUT_COMPONENT_RE.fullmatch(text):
        raise ValueError(
            f"{label} must contain only lowercase letters, digits, '_' or '-'"
        )
    return text


def _contained_path(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*parts).resolve()
    if not target.is_relative_to(resolved_root):
        raise ValueError("benchmark output path escapes --output-dir")
    return target


def _relative_file(root: Path, value: object, label: str) -> tuple[str, Path]:
    text = str(value or "")
    candidate = Path(text)
    if not text or candidate.is_absolute() or PureWindowsPath(text).is_absolute():
        raise ValueError(f"{label} must be a non-empty relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"{label} escapes its manifest root")
    normalized = candidate.as_posix()
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"{label} escapes its manifest root")
    return normalized, resolved


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable valid JSON") from exc
    return _mapping(payload, label)


def _validated_file_records(
    root: Path,
    records_value: object,
    *,
    verify_hashes: bool,
) -> dict[str, Mapping[str, Any]]:
    records = _sequence(records_value, "manifest.files")
    validated: dict[str, Mapping[str, Any]] = {}
    for index, raw_record in enumerate(records, start=1):
        record = _mapping(raw_record, f"manifest.files[{index}]")
        relative, path = _relative_file(
            root,
            record.get("path"),
            f"manifest.files[{index}].path",
        )
        if relative in validated:
            raise ValueError(f"manifest contains duplicate file path {relative}")
        expected_bytes = _positive_int(
            record.get("bytes"), f"manifest.files[{index}].bytes"
        )
        expected_sha = _sha256(
            record.get("sha256"), f"manifest.files[{index}].sha256"
        )
        if not path.is_file():
            raise ValueError(f"manifest file is missing: {relative}")
        if path.stat().st_size != expected_bytes:
            raise ValueError(f"manifest size mismatch: {relative}")
        if verify_hashes and _sha256_file(path) != expected_sha:
            raise ValueError(f"manifest SHA-256 mismatch: {relative}")
        validated[relative] = record
    return validated


def _assert_record_matches(
    declared: Mapping[str, Any],
    manifest_record: Mapping[str, Any],
    *,
    label: str,
) -> None:
    if _positive_int(declared.get("bytes"), f"{label}.bytes") != _positive_int(
        manifest_record.get("bytes"), f"manifest record for {label}.bytes"
    ):
        raise ValueError(f"{label} byte count disagrees with manifest.files")
    if _sha256(declared.get("sha256"), f"{label}.sha256") != _sha256(
        manifest_record.get("sha256"), f"manifest record for {label}.sha256"
    ):
        raise ValueError(f"{label} digest disagrees with manifest.files")


def _pptx_slide_count(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as package:
            slide_numbers = sorted(
                int(match.group(1))
                for name in package.namelist()
                if (match := _PPTX_SLIDE_RE.fullmatch(name)) is not None
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"manifest PPTX is not a valid ZIP package: {path.name}") from exc
    # OOXML slide part numbers may retain gaps after slides are deleted.  The
    # presentation relationship list defines display order; for this manifest
    # preflight we need only prove that the package has the same number of
    # distinct slide parts as pinned renders.
    if not slide_numbers or len(slide_numbers) != len(set(slide_numbers)):
        raise ValueError(f"manifest PPTX has an invalid slide part set: {path.name}")
    return len(slide_numbers)


def _selected_topic_declarations(
    declarations: Sequence[Any], requested_topics: Sequence[str]
) -> tuple[Mapping[str, Any], ...]:
    normalized = tuple(value.strip().casefold() for value in requested_topics if value.strip())
    parsed = tuple(
        _mapping(value, f"manifest.topics[{index}]")
        for index, value in enumerate(declarations, start=1)
    )
    if not normalized:
        return tuple(sorted(parsed, key=lambda item: str(item.get("slug") or "")))
    aliases: dict[str, Mapping[str, Any]] = {}
    for item in parsed:
        topic = str(item.get("topic") or "")
        slug = str(item.get("slug") or "")
        if not topic or not slug:
            raise ValueError("each suite topic requires topic and slug")
        for alias in (topic.casefold(), slug.casefold()):
            if alias in aliases and aliases[alias] is not item:
                raise ValueError(f"ambiguous suite topic alias {alias}")
            aliases[alias] = item
    unknown = sorted(set(normalized) - set(aliases))
    if unknown:
        raise ValueError("unknown --topic value(s): " + ", ".join(unknown))
    selected = {str(aliases[value]["slug"]) for value in normalized}
    return tuple(
        sorted(
            (item for item in parsed if str(item["slug"]) in selected),
            key=lambda item: str(item["slug"]),
        )
    )


def _pinned_topic_ranking(
    *,
    suite_root: Path,
    topic_root: Path,
    local_directory: str,
    topic: str,
    topic_slug: str,
    suite_revision: str,
    topic_selection: Mapping[str, Any],
    topic_records: Mapping[str, Mapping[str, Any]],
    root_records: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, int]:
    subset_relative, subset_path = _relative_file(
        topic_root,
        topic_selection.get("human_ranking_subset"),
        f"topic {topic_slug} human_ranking_subset",
    )
    topic_record = topic_records.get(subset_relative)
    root_record = root_records.get(f"{local_directory}/{subset_relative}")
    if topic_record is None or root_record is None:
        raise ValueError(f"topic {topic_slug} ranking subset is not hash-pinned")
    _assert_record_matches(
        topic_record,
        root_record,
        label=f"topic {topic_slug} ranking subset",
    )
    # root_records were already checked byte-for-byte.  Re-resolve here to make
    # the trust boundary explicit and to reject a later path swap.
    if not subset_path.resolve().is_relative_to(suite_root.resolve()):
        raise ValueError(f"topic {topic_slug} ranking subset escapes suite root")
    if _sha256_file(subset_path) != _sha256(
        root_record.get("sha256"), f"topic {topic_slug} ranking subset SHA-256"
    ):
        raise ValueError(f"topic {topic_slug} ranking subset changed after preflight")

    payload = _read_json(subset_path, f"topic {topic_slug} ranking subset")
    if str(payload.get("schema_version")) != "1.0":
        raise ValueError(f"topic {topic_slug} ranking subset schema is unsupported")
    if str(payload.get("source_revision") or "") != suite_revision:
        raise ValueError(f"topic {topic_slug} ranking revision is inconsistent")
    if str(payload.get("topic") or "") != topic:
        raise ValueError(f"topic {topic_slug} ranking topic is inconsistent")

    results = _sequence(payload.get("results"), f"topic {topic_slug} ranking results")
    declared_ranked = _positive_int(
        payload.get("ranked_product_count"),
        f"topic {topic_slug} ranked_product_count",
    )
    if len(results) != declared_ranked:
        raise ValueError(f"topic {topic_slug} ranking result count is inconsistent")
    selected: dict[str, int] = {}
    all_products: set[str] = set()
    all_ranks: set[int] = set()
    for index, value in enumerate(results, start=1):
        result = _mapping(value, f"topic {topic_slug} ranking results[{index}]")
        product = str(result.get("product") or "")
        rank = _positive_int(
            result.get("rank"), f"topic {topic_slug} ranking results[{index}].rank"
        )
        if not product or product in all_products or rank in all_ranks:
            raise ValueError(f"topic {topic_slug} ranking has duplicate/blank product or rank")
        all_products.add(product)
        all_ranks.add(rank)
        if result.get("selected") is True:
            selected[product] = rank
    if len(selected) != _positive_int(
        payload.get("paired_product_count"),
        f"topic {topic_slug} paired_product_count",
    ):
        raise ValueError(f"topic {topic_slug} selected ranking count is inconsistent")
    if not selected:
        raise ValueError(f"topic {topic_slug} has no selected pinned rankings")
    return selected


def validate_suite(
    suite_root: str | Path,
    *,
    requested_topics: Sequence[str] = (),
    verify_hashes: bool = True,
) -> SuiteSpec:
    """Validate the pinned suite and return immutable absolute-path case specs."""

    root = Path(suite_root).resolve()
    manifest = _read_json(root / "manifest.json", "suite manifest")
    if str(manifest.get("schema_version")) != "1.0":
        raise ValueError("unsupported suite manifest schema")
    source = _mapping(manifest.get("source"), "suite manifest source")
    revision = str(source.get("revision") or "")
    if not revision or source.get("revision_pinned") is not True:
        raise ValueError("suite source revision must be pinned")
    dataset_id = str(manifest.get("dataset_id") or "")
    if not dataset_id:
        raise ValueError("suite manifest requires dataset_id")

    root_records = _validated_file_records(
        root,
        manifest.get("files"),
        verify_hashes=verify_hashes,
    )
    integrity = _mapping(manifest.get("integrity"), "suite manifest integrity")
    declared_file_count = _positive_int(
        integrity.get("manifest_files"), "suite integrity.manifest_files"
    )
    if declared_file_count != len(root_records):
        raise ValueError("suite manifest file count is inconsistent")

    declarations = _sequence(manifest.get("topics"), "suite manifest topics")
    selected = _selected_topic_declarations(declarations, requested_topics)
    cases: list[CaseSpec] = []
    topic_artifact_bytes = 0
    topic_render_count = 0
    all_topic_case_count = 0
    for declaration_value in declarations:
        declaration = _mapping(declaration_value, "suite topic declaration")
        topic_artifact_bytes += _positive_int(
            declaration.get("artifact_bytes"), "suite topic artifact_bytes"
        )
        topic_render_count += _positive_int(
            declaration.get("rendered_slides"), "suite topic rendered_slides"
        )
        all_topic_case_count += _positive_int(
            declaration.get("paired_products"), "suite topic paired_products"
        )
    if _positive_int(
        integrity.get("artifact_bytes"), "suite integrity.artifact_bytes"
    ) != topic_artifact_bytes:
        raise ValueError("suite artifact byte total is inconsistent")
    if _positive_int(
        integrity.get("rendered_slide_count"),
        "suite integrity.rendered_slide_count",
    ) != topic_render_count:
        raise ValueError("suite rendered slide total is inconsistent")
    if _positive_int(
        integrity.get("paired_deck_count"), "suite integrity.paired_deck_count"
    ) != all_topic_case_count:
        raise ValueError("suite paired deck total is inconsistent")

    for declaration in selected:
        topic = str(declaration.get("topic") or "")
        slug = _safe_output_component(
            declaration.get("slug"), "suite topic slug"
        )
        local_directory, topic_root = _relative_file(
            root,
            declaration.get("local_directory"),
            f"topic {slug} local_directory",
        )
        manifest_relative, topic_manifest_path = _relative_file(
            root,
            declaration.get("manifest_path"),
            f"topic {slug} manifest_path",
        )
        expected_manifest = f"{local_directory}/manifest.json"
        if manifest_relative != expected_manifest:
            raise ValueError(f"topic {slug} manifest path is inconsistent")
        topic_manifest = _read_json(topic_manifest_path, f"topic {slug} manifest")
        topic_dataset_id = str(topic_manifest.get("dataset_id") or "")
        if not topic_dataset_id:
            raise ValueError(f"topic {slug} manifest requires dataset_id")
        topic_selection = _mapping(
            topic_manifest.get("selection"), f"topic {slug} selection"
        )
        if str(topic_selection.get("topic") or "") != topic:
            raise ValueError(f"topic {slug} selection name is inconsistent")
        topic_records = _validated_file_records(
            topic_root,
            topic_manifest.get("files"),
            verify_hashes=False,
        )
        for relative, topic_record in topic_records.items():
            root_record = root_records.get(f"{local_directory}/{relative}")
            if root_record is None:
                raise ValueError(
                    f"topic {slug} file is absent from suite manifest: {relative}"
                )
            _assert_record_matches(
                topic_record,
                root_record,
                label=f"topic {slug} file {relative}",
            )
        topic_integrity = _mapping(
            topic_manifest.get("integrity"), f"topic {slug} integrity"
        )
        if len(topic_records) != _positive_int(
            topic_integrity.get("manifest_files"),
            f"topic {slug} integrity.manifest_files",
        ):
            raise ValueError(f"topic {slug} manifest file count is inconsistent")
        pinned_ranking = _pinned_topic_ranking(
            suite_root=root,
            topic_root=topic_root,
            local_directory=local_directory,
            topic=topic,
            topic_slug=slug,
            suite_revision=revision,
            topic_selection=topic_selection,
            topic_records=topic_records,
            root_records=root_records,
        )
        artifacts = _sequence(
            topic_manifest.get("artifacts"), f"topic {slug} artifacts"
        )
        if len(artifacts) != _positive_int(
            declaration.get("paired_products"), f"topic {slug} paired_products"
        ):
            raise ValueError(f"topic {slug} artifact count is inconsistent")
        seen_products: set[str] = set()
        seen_ranks: set[int] = set()
        topic_bytes = 0
        topic_pages = 0
        for artifact_index, artifact_value in enumerate(artifacts, start=1):
            artifact = _mapping(
                artifact_value, f"topic {slug} artifacts[{artifact_index}]"
            )
            product = str(artifact.get("product_label") or "")
            declared_human_rank = _positive_int(
                artifact.get("human_rank"),
                f"topic {slug} artifacts[{artifact_index}].human_rank",
            )
            pinned_human_rank = pinned_ranking.get(product)
            if pinned_human_rank is None or pinned_human_rank != declared_human_rank:
                raise ValueError(
                    f"topic {slug} artifact product/rank disagrees with pinned ranking"
                )
            human_rank = pinned_human_rank
            if not product or product in seen_products or human_rank in seen_ranks:
                raise ValueError(f"topic {slug} has duplicate/blank product or rank")
            seen_products.add(product)
            seen_ranks.add(human_rank)

            pptx = _mapping(
                artifact.get("pptx"), f"topic {slug} artifacts[{artifact_index}].pptx"
            )
            pptx_relative, pptx_path = _relative_file(
                topic_root,
                pptx.get("local_path"),
                f"topic {slug} PPTX path",
            )
            pptx_record = topic_records.get(pptx_relative)
            if pptx_record is None:
                raise ValueError(f"topic {slug} PPTX is absent from topic manifest files")
            _assert_record_matches(pptx, pptx_record, label=f"topic {slug} PPTX")
            root_pptx_record = root_records.get(f"{local_directory}/{pptx_relative}")
            if root_pptx_record is None:
                raise ValueError(f"topic {slug} PPTX is absent from suite manifest files")
            _assert_record_matches(pptx, root_pptx_record, label=f"topic {slug} PPTX")

            rendered = _mapping(
                artifact.get("rendered_slides"),
                f"topic {slug} artifacts[{artifact_index}].rendered_slides",
            )
            render_values = _sequence(
                rendered.get("files"), f"topic {slug} rendered slide files"
            )
            declared_render_count = _positive_int(
                rendered.get("count"), f"topic {slug} rendered slide count"
            )
            if len(render_values) != declared_render_count:
                raise ValueError(f"topic {slug} render count is inconsistent")
            render_specs: list[RenderSpec] = []
            for page_number, render_value in enumerate(render_values, start=1):
                render = _mapping(
                    render_value, f"topic {slug} render page {page_number}"
                )
                render_relative, render_path = _relative_file(
                    topic_root,
                    render.get("local_path"),
                    f"topic {slug} render page {page_number} path",
                )
                render_record = topic_records.get(render_relative)
                if render_record is None:
                    raise ValueError(
                        f"topic {slug} render page {page_number} is absent from topic files"
                    )
                _assert_record_matches(
                    render,
                    render_record,
                    label=f"topic {slug} render page {page_number}",
                )
                root_render_record = root_records.get(
                    f"{local_directory}/{render_relative}"
                )
                if root_render_record is None:
                    raise ValueError(
                        f"topic {slug} render page {page_number} is absent from suite files"
                    )
                _assert_record_matches(
                    render,
                    root_render_record,
                    label=f"topic {slug} render page {page_number}",
                )
                render_specs.append(
                    RenderSpec(
                        page_number=page_number,
                        path=render_path,
                        relative_path=f"{local_directory}/{render_relative}",
                        sha256=_sha256(
                            render.get("sha256"),
                            f"topic {slug} render page {page_number} SHA-256",
                        ),
                        byte_count=_positive_int(
                            render.get("bytes"),
                            f"topic {slug} render page {page_number} bytes",
                        ),
                    )
                )
            if _pptx_slide_count(pptx_path) != declared_render_count:
                raise ValueError(f"topic {slug} PPTX/render page count mismatch")
            artifact_bytes = _positive_int(
                artifact.get("artifact_bytes"),
                f"topic {slug} artifacts[{artifact_index}].artifact_bytes",
            )
            calculated_bytes = _positive_int(
                pptx.get("bytes"), f"topic {slug} PPTX bytes"
            ) + sum(item.byte_count for item in render_specs)
            if artifact_bytes != calculated_bytes:
                raise ValueError(f"topic {slug} artifact byte total is inconsistent")
            topic_bytes += artifact_bytes
            topic_pages += declared_render_count
            case_id = _safe_output_component(
                Path(pptx_relative).stem,
                f"topic {slug} PPTX case id",
            )
            cases.append(
                CaseSpec(
                    topic=topic,
                    topic_slug=slug,
                    dataset_id=topic_dataset_id,
                    product=product,
                    human_rank=human_rank,
                    case_id=case_id,
                    pptx_path=pptx_path,
                    pptx_relative_path=f"{local_directory}/{pptx_relative}",
                    pptx_sha256=_sha256(
                        pptx.get("sha256"), f"topic {slug} PPTX SHA-256"
                    ),
                    pptx_bytes=_positive_int(
                        pptx.get("bytes"), f"topic {slug} PPTX bytes"
                    ),
                    renders=tuple(render_specs),
                )
            )
        if set(pinned_ranking) != seen_products:
            raise ValueError(f"topic {slug} artifacts do not match pinned selected products")
        available_ranks = declaration.get("available_human_ranks")
        if available_ranks is not None:
            declared_available = sorted(
                _positive_int(value, f"topic {slug} available_human_ranks")
                for value in _sequence(
                    available_ranks, f"topic {slug} available_human_ranks"
                )
            )
            if declared_available != sorted(seen_ranks):
                raise ValueError(f"topic {slug} available human ranks are inconsistent")
        if topic_bytes != _positive_int(
            topic_integrity.get("artifact_bytes"), f"topic {slug} artifact bytes"
        ):
            raise ValueError(f"topic {slug} byte total disagrees with its manifest")
        if topic_pages != _positive_int(
            topic_integrity.get("rendered_slide_count"),
            f"topic {slug} rendered slide count",
        ):
            raise ValueError(f"topic {slug} page total disagrees with its manifest")

    case_keys = [item.case_key for item in cases]
    evaluation_ids = [item.evaluation_case_id for item in cases]
    if len(case_keys) != len(set(case_keys)):
        raise ValueError("suite contains duplicate case output identities")
    if len(evaluation_ids) != len(set(evaluation_ids)):
        raise ValueError("suite contains duplicate anonymous evaluation identities")

    return SuiteSpec(
        root=root,
        dataset_id=dataset_id,
        revision=revision,
        cases=tuple(
            sorted(cases, key=lambda item: (item.topic_slug, item.human_rank))
        ),
        manifest_file_count=len(root_records),
        manifest_bytes=sum(
            _positive_int(item.get("bytes"), "suite manifest file bytes")
            for item in root_records.values()
        ),
    )


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks, with larger values receiving larger ranks."""

    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = average
        index = end
    return result


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_norm = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_norm = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return numerator / (left_norm * right_norm)


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    return pearson(average_ranks(left), average_ranks(right))


def pairwise_accuracy(
    human_ranks: Sequence[int], scores: Sequence[float]
) -> float | None:
    if len(human_ranks) != len(scores):
        raise ValueError("human ranks and scores must have equal length")
    credits: list[float] = []
    for left in range(len(scores)):
        for right in range(left + 1, len(scores)):
            human_direction = human_ranks[right] - human_ranks[left]
            score_direction = scores[left] - scores[right]
            if score_direction == 0:
                credits.append(0.5)
            else:
                credits.append(
                    1.0 if human_direction * score_direction > 0 else 0.0
                )
    return sum(credits) / len(credits) if credits else None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_reference_valid(
    reference_value: object,
    *,
    runtime_root: Path,
    expected_digest: object,
) -> bool:
    if not isinstance(reference_value, Mapping):
        return False
    reference = {str(key): value for key, value in reference_value.items()}
    digest = str(reference.get("sha256") or "")
    if not _SHA256_RE.fullmatch(digest) or digest != str(expected_digest or ""):
        return False
    uri = str(reference.get("uri") or "")
    if not uri:
        return False
    path = Path(uri).resolve()
    root = runtime_root.resolve()
    return (
        path.is_relative_to(root)
        and path.is_file()
        and _sha256_file(path) == digest
    )


def verify_runtime_evidence(
    report: Mapping[str, Any], runtime_root: Path
) -> Mapping[str, Any]:
    run_id = str(report.get("run_id") or "")
    audit_path = runtime_root / "audit" / "events.jsonl"
    chain_valid = False
    completed_event_present = False
    failed_event_present = False
    result_hash_linked = False
    if audit_path.is_file() and run_id:
        audit_log = JsonlAuditLog(audit_path)
        chain_valid = audit_log.verify() == (True, None)
        if chain_valid:
            events = tuple(
                event for event in audit_log.read() if event.get("run_id") == run_id
            )
            completed_events = tuple(
                event for event in events if event.get("event_type") == "RUN_COMPLETED"
            )
            failed_event_present = any(
                event.get("event_type") == "RUN_FAILED" for event in events
            )
            completed_event_present = len(completed_events) == 1
            report_manifest = report.get("manifest")
            report_manifest = (
                report_manifest if isinstance(report_manifest, Mapping) else {}
            )
            manifest_result_hash = str(report_manifest.get("result_hash") or "")
            if completed_event_present and _SHA256_RE.fullmatch(manifest_result_hash):
                event_payload = completed_events[0].get("payload")
                event_payload = (
                    event_payload if isinstance(event_payload, Mapping) else {}
                )
                result_hash_linked = (
                    str(event_payload.get("result_hash") or "")
                    == manifest_result_hash
                )
    manifest_value = report.get("manifest")
    manifest = (
        {str(key): value for key, value in manifest_value.items()}
        if isinstance(manifest_value, Mapping)
        else {}
    )
    artifact_hashes_value = manifest.get("artifact_hashes")
    artifact_hashes = (
        {str(key): value for key, value in artifact_hashes_value.items()}
        if isinstance(artifact_hashes_value, Mapping)
        else {}
    )
    observation_valid = _artifact_reference_valid(
        report.get("observation_artifact"),
        runtime_root=runtime_root,
        expected_digest=artifact_hashes.get("atomic_observations"),
    )
    visual_references_value = report.get("visual_audit_artifacts")
    visual_references = (
        {str(key): value for key, value in visual_references_value.items()}
        if isinstance(visual_references_value, Mapping)
        else {}
    )
    expected_visual_roles = (
        "visual_page_index",
        "atlas_scout",
        "visual_selection_plan",
        "visual_audit_rounds",
        "visual_coverage_certificate",
    )
    visual_contracts_valid = all(
        _artifact_reference_valid(
            visual_references.get(role),
            runtime_root=runtime_root,
            expected_digest=artifact_hashes.get(role),
        )
        for role in expected_visual_roles
    )
    return {
        "audit_chain_valid": chain_valid,
        "completed_event_present": completed_event_present,
        "failed_event_present": failed_event_present,
        "result_hash_linked": result_hash_linked,
        "observation_artifact_valid": observation_valid,
        "visual_contract_artifacts_valid": visual_contracts_valid,
        "valid": bool(
            chain_valid
            and completed_event_present
            and not failed_event_present
            and result_hash_linked
            and observation_valid
            and visual_contracts_valid
        ),
    }


def _checkpoint_path(output_dir: Path, case: CaseSpec) -> Path:
    slug = _safe_output_component(case.topic_slug, "case topic_slug")
    case_id = _safe_output_component(case.case_id, "case_id")
    return _contained_path(output_dir, "checkpoints", slug, f"{case_id}.json")


def _runtime_root(output_dir: Path, case: CaseSpec) -> Path:
    evaluation_case_id = _safe_output_component(
        case.evaluation_case_id, "evaluation_case_id"
    )
    # Topic, product and human rank deliberately stay outside the runtime path.
    return _contained_path(output_dir, "runtime", evaluation_case_id)


def _stored_report_path(runtime_root: Path, run_id: str) -> Path:
    safe_run_id = _safe_output_component(run_id, "run_id")
    return _contained_path(runtime_root, "runs", f"{safe_run_id}.json")


def _checkpoint_identity(
    suite: SuiteSpec,
    case: CaseSpec,
    profile: EvalProfile,
) -> Mapping[str, Any]:
    return {
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_id": suite.dataset_id,
        "dataset_revision": suite.revision,
        "topic": case.topic,
        "topic_slug": case.topic_slug,
        "case_id": case.case_id,
        "evaluation_case_id": case.evaluation_case_id,
        "product": case.product,
        "human_rank": case.human_rank,
        "pptx_sha256": case.pptx_sha256,
        "render_set_fingerprint": case.render_set_fingerprint,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_fingerprint": _stable_hash(to_primitive(profile)),
        "evaluation_git_sha": git_sha(REPOSITORY_ROOT),
    }


def _load_resumable_case(
    suite: SuiteSpec,
    case: CaseSpec,
    profile: EvalProfile,
    output_dir: Path,
) -> Mapping[str, Any] | None:
    checkpoint_path = _checkpoint_path(output_dir, case)
    if not checkpoint_path.is_file():
        return None
    try:
        checkpoint = _read_json(checkpoint_path, "case checkpoint")
    except ValueError:
        return None
    if checkpoint.get("schema_version") != CASE_RECORD_SCHEMA_VERSION:
        return None
    if checkpoint.get("status") != "COMPLETED":
        return None
    for key, expected in _checkpoint_identity(suite, case, profile).items():
        if checkpoint.get(key) != expected:
            return None
    run_id = str(checkpoint.get("run_id") or "")
    if not run_id:
        return None
    runtime_root = _runtime_root(output_dir, case)
    report_path = _stored_report_path(runtime_root, run_id)
    if not report_path.is_file():
        return None
    if _sha256_file(report_path) != str(checkpoint.get("report_file_sha256") or ""):
        return None
    try:
        report = _read_json(report_path, "resumed EvaluationReport")
    except ValueError:
        return None
    if (
        report.get("run_id") != run_id
        or report.get("case_id") != case.evaluation_case_id
        or report.get("profile_id") != profile.profile_id
        or report.get("profile_version") != profile.version
    ):
        return None
    integrity = verify_runtime_evidence(report, runtime_root)
    if integrity.get("valid") is not True:
        return None
    composite_scores, _statuses, duplicate_composites = _composite_scores(
        report, profile
    )
    if _report_legality(
        report,
        checkpoint,
        case,
        profile,
        composite_scores,
        duplicate_composites,
    ).get("valid") is not True:
        return None
    normalized_checkpoint = dict(checkpoint)
    normalized_checkpoint["report_relative_path"] = report_path.relative_to(
        output_dir.resolve()
    ).as_posix()
    normalized_checkpoint["runtime_relative_path"] = runtime_root.relative_to(
        output_dir.resolve()
    ).as_posix()
    return {
        "checkpoint": normalized_checkpoint,
        "report": dict(report),
        "integrity": dict(integrity),
        "resumed": True,
    }


def _safe_error_code(exc: BaseException) -> str:
    """Return only a type-derived code; exception text may contain credentials."""

    name = re.sub(r"[^A-Za-z0-9]+", "_", type(exc).__name__).strip("_")
    return (name or "EvaluationFailure").upper()


def _runtime_has_primary_visual_provider(runtime: object) -> bool:
    # Provider construction and secret loading remain owned by the runtime.
    # This boolean is intentionally the only provider state the benchmark reads.
    return bool(getattr(runtime, "_vlm_enabled", False))


def evaluation_case(
    suite: SuiteSpec,
    case: CaseSpec,
    *,
    anonymous_pptx_path: Path | None = None,
) -> EvalCase:
    """Build the production input without benchmark labels or product identity."""

    return EvalCase(
        case_id=case.evaluation_case_id,
        scene=SceneType.READY_MADE,
        pptx_path=str(anonymous_pptx_path or case.pptx_path),
        metadata={
            "benchmark_id": BENCHMARK_ID,
            "benchmark_version": BENCHMARK_VERSION,
            "dataset_id": suite.dataset_id,
            "dataset_revision": suite.revision,
            "artifact_hashes": {"source_pptx": case.pptx_sha256},
        },
    )


def _anonymous_pptx_snapshot(case: CaseSpec, runtime_root: Path) -> Path:
    """Freeze a hash-named input so Oracle context cannot expose rank/product paths."""

    target = _contained_path(
        runtime_root,
        "inputs",
        f"{_safe_output_component(case.evaluation_case_id, 'evaluation_case_id')}.pptx",
    )
    if target.is_file() and _sha256_file(target) == case.pptx_sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(case.pptx_path, temporary)
        if _sha256_file(temporary) != case.pptx_sha256:
            raise ValueError("PPTX changed after manifest verification")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _anonymous_render_snapshots(
    case: CaseSpec, runtime_root: Path
) -> tuple[Path, ...]:
    """Freeze official renders under rank/product-free, hash-derived names."""

    snapshots: list[Path] = []
    for render in case.renders:
        target = _contained_path(
            runtime_root,
            "inputs",
            "rendered-pages",
            f"page-{render.page_number:04d}-{render.sha256[:16]}.png",
        )
        if target.is_file() and _sha256_file(target) == render.sha256:
            snapshots.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(render.path, temporary)
            if _sha256_file(temporary) != render.sha256:
                raise ValueError("official render changed after manifest verification")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        snapshots.append(target)
    return tuple(snapshots)


def invoke_evaluation(
    runtime: EvaluationRuntime,
    suite: SuiteSpec,
    case: CaseSpec,
    profile: EvalProfile,
    *,
    anonymous_pptx_path: Path,
) -> Mapping[str, Any]:
    """Invoke the runtime with label-free EvalCase and pinned official renders."""

    runtime_root = anonymous_pptx_path.parent.parent
    anonymous_renders = _anonymous_render_snapshots(case, runtime_root)
    model_images = tuple(
        {
            "page_number": item.page_number,
            "path": str(anonymous_path),
            "media_type": "image/png",
            "sha256": item.sha256,
        }
        for item, anonymous_path in zip(case.renders, anonymous_renders)
    )
    return runtime.evaluate(
        evaluation_case(suite, case, anonymous_pptx_path=anonymous_pptx_path),
        profile,
        artifacts={"slide_images": model_images},
    )


def evaluate_case(
    suite: SuiteSpec,
    case: CaseSpec,
    profile: EvalProfile,
    output_dir: Path,
    *,
    resume: bool,
    require_live_provider: bool,
) -> Mapping[str, Any]:
    if resume:
        resumed = _load_resumable_case(suite, case, profile, output_dir)
        if resumed is not None:
            return resumed

    runtime_root = _runtime_root(output_dir, case)
    try:
        runtime = build_runtime_from_environment(
            runtime_root,
            workspace_root=REPOSITORY_ROOT,
        )
        if require_live_provider and not _runtime_has_primary_visual_provider(runtime):
            raise RuntimeError("primary visual provider is not configured")
        anonymous_pptx = _anonymous_pptx_snapshot(case, runtime_root)
        report = invoke_evaluation(
            runtime,
            suite,
            case,
            profile,
            anonymous_pptx_path=anonymous_pptx,
        )
        if not isinstance(report, Mapping):
            raise TypeError("runtime returned a non-mapping report")
        normalized_report = {str(key): value for key, value in report.items()}
        run_id = str(normalized_report.get("run_id") or "")
        if not run_id:
            raise ValueError("runtime report has no run_id")
        report_path = _stored_report_path(runtime_root, run_id)
        if not report_path.is_file():
            raise FileNotFoundError("runtime did not persist its EvaluationReport")
        integrity = verify_runtime_evidence(normalized_report, runtime_root)
        checkpoint_status = (
            "COMPLETED" if integrity.get("valid") is True else "INVALID"
        )
        checkpoint = {
            "schema_version": CASE_RECORD_SCHEMA_VERSION,
            "status": checkpoint_status,
            **_checkpoint_identity(suite, case, profile),
            "run_id": run_id,
            "report_relative_path": report_path.relative_to(output_dir).as_posix(),
            "runtime_relative_path": runtime_root.relative_to(output_dir).as_posix(),
            "report_file_sha256": _sha256_file(report_path),
            "audit_integrity": dict(integrity),
            **(
                {"error_code": "AUDIT_INTEGRITY_INVALID"}
                if checkpoint_status != "COMPLETED"
                else {}
            ),
        }
        _atomic_write_json(_checkpoint_path(output_dir, case), checkpoint)
        return {
            "checkpoint": checkpoint,
            "report": normalized_report,
            "integrity": dict(integrity),
            "resumed": False,
        }
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        checkpoint = {
            "schema_version": CASE_RECORD_SCHEMA_VERSION,
            "status": "ERROR",
            **_checkpoint_identity(suite, case, profile),
            "error_code": _safe_error_code(exc),
            "runtime_relative_path": runtime_root.relative_to(output_dir).as_posix(),
        }
        _atomic_write_json(_checkpoint_path(output_dir, case), checkpoint)
        return {
            "checkpoint": checkpoint,
            "report": None,
            "integrity": {"valid": False},
            "resumed": False,
        }


def _numeric_score(report: Mapping[str, Any], name: str) -> float | None:
    value = report.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _configured_composite_metric_ids(profile: EvalProfile) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *profile.base_weights,
                *profile.scene_weights,
                *profile.base_multiplier_metric_ids,
                *profile.scene_multiplier_metric_ids,
            )
        )
    )


def _report_results(report: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    value = report.get("results")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def _result_status_counts(report: Mapping[str, Any]) -> Mapping[str, Mapping[str, int]]:
    results = _report_results(report)
    metric = Counter(str(item.get("metric_status") or "UNKNOWN") for item in results)
    execution = Counter(
        str(item.get("execution_status") or "UNKNOWN") for item in results
    )
    return {
        "metric_status": dict(sorted(metric.items())),
        "execution_status": dict(sorted(execution.items())),
    }


def _composite_scores(
    report: Mapping[str, Any], profile: EvalProfile
) -> tuple[Mapping[str, float | None], Mapping[str, str], tuple[str, ...]]:
    metric_ids = _configured_composite_metric_ids(profile)
    matches: dict[str, list[Mapping[str, Any]]] = {metric_id: [] for metric_id in metric_ids}
    for result in _report_results(report):
        metric_id = str(result.get("metric_id") or "")
        if metric_id in matches:
            matches[metric_id].append(result)
    scores: dict[str, float | None] = {}
    statuses: dict[str, str] = {}
    duplicate_ids: list[str] = []
    for metric_id in metric_ids:
        candidates = matches[metric_id]
        if len(candidates) > 1:
            duplicate_ids.append(metric_id)
        candidate = candidates[-1] if candidates else {}
        statuses[metric_id] = str(candidate.get("metric_status") or "MISSING")
        raw_score = candidate.get("normalized_score")
        if raw_score is None:
            raw_score = candidate.get("multiplier")
        if (
            isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and math.isfinite(float(raw_score))
        ):
            scores[metric_id] = float(raw_score)
        else:
            scores[metric_id] = None
    return scores, statuses, tuple(sorted(duplicate_ids))


def _read_visual_contract(
    report: Mapping[str, Any], runtime_root: Path, role: str
) -> Mapping[str, Any]:
    references = report.get("visual_audit_artifacts")
    references = references if isinstance(references, Mapping) else {}
    reference = references.get(role)
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    artifact_hashes = manifest.get("artifact_hashes")
    artifact_hashes = artifact_hashes if isinstance(artifact_hashes, Mapping) else {}
    if not _artifact_reference_valid(
        reference,
        runtime_root=runtime_root,
        expected_digest=artifact_hashes.get(role),
    ):
        return {}
    assert isinstance(reference, Mapping)
    try:
        return _read_json(Path(str(reference["uri"])).resolve(), role)
    except (KeyError, ValueError):
        return {}


def _visual_usage(
    report: Mapping[str, Any], runtime_root: Path
) -> Mapping[str, Any]:
    summary_value = report.get("visual_audit_summary")
    summary = dict(summary_value) if isinstance(summary_value, Mapping) else {}
    certificate = _read_visual_contract(
        report, runtime_root, "visual_coverage_certificate"
    )
    metadata = certificate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    persisted_usage = metadata.get("usage")
    persisted_usage = persisted_usage if isinstance(persisted_usage, Mapping) else {}
    merged = {str(key): value for key, value in persisted_usage.items()}
    for key, value in summary.items():
        if value is not None and key not in merged:
            merged[str(key)] = value
    return merged


def _model_response_legality(
    report: Mapping[str, Any], runtime_root: Path
) -> Mapping[str, Any]:
    valid_responses = 0
    logical_audits = 0
    scout = _read_visual_contract(report, runtime_root, "atlas_scout")
    scout_metadata = scout.get("audit_metadata")
    scout_metadata = scout_metadata if isinstance(scout_metadata, Mapping) else {}
    raw_scout_calls = scout_metadata.get("attempts")
    scout_calls = (
        tuple(item for item in raw_scout_calls if isinstance(item, Mapping))
        if isinstance(raw_scout_calls, Sequence)
        and not isinstance(raw_scout_calls, (str, bytes))
        else ()
    )
    raw_batch_count = scout_metadata.get("batch_count")
    batch_count = (
        int(raw_batch_count)
        if isinstance(raw_batch_count, int)
        and not isinstance(raw_batch_count, bool)
        and raw_batch_count >= 0
        else len(
            {
                item.get("batch_index")
                for item in scout_calls
                if isinstance(item.get("batch_index"), int)
            }
        )
    )
    if batch_count == 0 and scout_metadata.get("provider_attempt_count"):
        batch_count = 1
    for batch_index in range(1, batch_count + 1):
        logical_audits += 1
        batch_attempts = tuple(
            item for item in scout_calls if item.get("batch_index") == batch_index
        )
        if batch_attempts:
            valid_responses += int(
                any(item.get("outcome") == "valid" for item in batch_attempts)
            )
        elif scout_metadata.get("valid_response_count"):
            valid_responses += 1

    seen_logical_calls: set[str] = set()
    adaptive_request_fingerprints: set[str] = set()
    for result in _report_results(report):
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        adaptive_calls = metadata.get("adaptive_calls")
        if isinstance(adaptive_calls, Sequence) and not isinstance(
            adaptive_calls, (str, bytes)
        ):
            for index, raw_call in enumerate(adaptive_calls, start=1):
                if not isinstance(raw_call, Mapping):
                    continue
                call = {str(key): value for key, value in raw_call.items()}
                fingerprint = str(call.get("request_fingerprint") or "")
                dedupe_key = fingerprint or _stable_hash(
                    {
                        "metric_id": result.get("metric_id"),
                        "call_index": call.get("call_index", index),
                        "active_pages": call.get("active_page_numbers"),
                    }
                )
                if dedupe_key in seen_logical_calls:
                    continue
                seen_logical_calls.add(dedupe_key)
                if fingerprint:
                    adaptive_request_fingerprints.add(fingerprint)
                logical_audits += 1
                valid_responses += int(
                    call.get("metric_status") not in {"ERROR", None}
                    and bool(call.get("response_fingerprint"))
                )

    for result in _report_results(report):
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        attempts = metadata.get("routing_attempts")
        if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
            if _is_unreturned_model_node_failure(result):
                logical_audits += 1
            continue
        normalized_attempts = tuple(
            {str(key): value for key, value in item.items()}
            for item in attempts
            if isinstance(item, Mapping)
        )
        if not normalized_attempts:
            if _is_unreturned_model_node_failure(result):
                logical_audits += 1
            continue
        fingerprints = tuple(
            sorted(
                {
                    str(item.get("request_fingerprint"))
                    for item in normalized_attempts
                    if item.get("request_fingerprint")
                }
            )
        )
        if fingerprints and set(fingerprints) & adaptive_request_fingerprints:
            continue
        dedupe_key = _stable_hash(
            {
                "requests": fingerprints,
                "metric_id": result.get("metric_id"),
                "oracle_id": result.get("oracle_id"),
            }
        )
        if dedupe_key in seen_logical_calls:
            continue
        seen_logical_calls.add(dedupe_key)
        adaptive_request_fingerprints.update(fingerprints)
        logical_audits += 1
        selected = tuple(item for item in normalized_attempts if item.get("selected") is True)
        candidates = selected or normalized_attempts
        valid_responses += int(
            result.get("execution_status") == "SUCCESS"
            and result.get("metric_status") != "ERROR"
            and any(
                item.get("execution_status") == "SUCCESS"
                and item.get("metric_status") != "ERROR"
                and not item.get("error_code")
                and bool(item.get("response_fingerprint"))
                for item in candidates
            )
        )
    rate = valid_responses / logical_audits if logical_audits else None
    return {
        "valid_response_count": valid_responses,
        "response_attempt_count": logical_audits,
        "logical_audit_count": logical_audits,
        "legal_response_rate": rate,
        "threshold": _MODEL_RESPONSE_LEGALITY_THRESHOLD,
        "meets_threshold": bool(
            rate is not None and rate >= _MODEL_RESPONSE_LEGALITY_THRESHOLD
        ),
        "counting_contract": "POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2",
    }


def _is_unreturned_model_node_failure(result: Mapping[str, Any]) -> bool:
    oracle_id = str(result.get("oracle_id") or "")
    metric_id = str(result.get("metric_id") or "")
    is_model_node = (
        (
            oracle_id.startswith("v8.visual.")
            and metric_id.startswith(
                ("structured_vlm_", "provisional_structured_vlm_")
            )
        )
        or oracle_id.startswith("v8.raster_text.")
    )
    return bool(
        is_model_node
        and result.get("execution_status") == "ERROR"
        and result.get("error_code") in {
            "ORACLE_EXCEPTION",
            "COST_BUDGET_EXHAUSTED",
        }
    )


def _report_legality(
    report: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    case: CaseSpec,
    profile: EvalProfile,
    composite_scores: Mapping[str, float | None],
    duplicate_composites: Sequence[str],
) -> Mapping[str, Any]:
    reasons: list[str] = []
    if checkpoint.get("status") != "COMPLETED":
        reasons.append("checkpoint_not_completed")
    if str(report.get("schema_version") or "") != "1.0":
        reasons.append("report_schema_invalid")
    if report.get("case_id") != case.evaluation_case_id:
        reasons.append("case_id_mismatch")
    if report.get("profile_id") != profile.profile_id:
        reasons.append("profile_id_mismatch")
    if report.get("profile_version") != profile.version:
        reasons.append("profile_version_mismatch")
    run_id = str(report.get("run_id") or "")
    if not _SAFE_OUTPUT_COMPONENT_RE.fullmatch(run_id):
        reasons.append("run_id_invalid")
    if report.get("decision") not in _KNOWN_DECISIONS:
        reasons.append("decision_invalid")
    if report.get("coverage") not in _KNOWN_COVERAGES:
        reasons.append("coverage_invalid")
    if _numeric_score(report, "base_score") is None:
        reasons.append("base_score_not_finite")
    if not isinstance(report.get("results"), Sequence) or isinstance(
        report.get("results"), (str, bytes)
    ):
        reasons.append("results_invalid")
    if duplicate_composites:
        reasons.extend(f"duplicate_composite:{value}" for value in duplicate_composites)
    if report.get("coverage") == "FULL":
        reasons.extend(
            f"full_coverage_composite_missing:{metric_id}"
            for metric_id, score in composite_scores.items()
            if score is None
        )
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    for field, expected in (
        ("run_id", report.get("run_id")),
        ("case_id", case.evaluation_case_id),
        ("profile_id", profile.profile_id),
        ("profile_version", profile.version),
    ):
        if manifest.get(field) != expected:
            reasons.append(f"manifest_{field}_mismatch")
    if not _SHA256_RE.fullmatch(str(manifest.get("result_hash") or "")):
        reasons.append("manifest_result_hash_invalid")
    if manifest.get("git_sha") != checkpoint.get("evaluation_git_sha"):
        reasons.append("manifest_git_sha_mismatch")
    return {"valid": not reasons, "reasons": reasons}


def _case_summary(
    case: CaseSpec,
    outcome: Mapping[str, Any],
    profile: EvalProfile,
    *,
    runtime_root: Path,
) -> dict[str, Any]:
    checkpoint_value = outcome.get("checkpoint")
    checkpoint = (
        {str(key): value for key, value in checkpoint_value.items()}
        if isinstance(checkpoint_value, Mapping)
        else {}
    )
    report_value = outcome.get("report")
    if not isinstance(report_value, Mapping):
        return {
            "case_key": case.case_key,
            "case_id": case.case_id,
            "topic": case.topic,
            "topic_slug": case.topic_slug,
            "product": case.product,
            "human_rank": case.human_rank,
            "page_count": len(case.renders),
            "status": "ERROR",
            "error_code": checkpoint.get("error_code", "EVALUATION_FAILURE"),
            "decision": "ERROR",
            "coverage": "ERROR",
            "base_score": None,
            "full_score": None,
            "resumed": bool(outcome.get("resumed")),
            "report_relative_path": None,
            "audit_integrity": {"valid": False},
            "report_legality": {"valid": False, "reasons": ["report_missing"]},
            "visual_audit_summary": {},
            "visual_usage": {},
            "model_response_legality": {
                "valid_response_count": 0,
                "response_attempt_count": 0,
                "legal_response_rate": None,
                "threshold": _MODEL_RESPONSE_LEGALITY_THRESHOLD,
                "meets_threshold": False,
                "counting_contract": "POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2",
            },
            "result_status_counts": {
                "metric_status": {},
                "execution_status": {},
            },
            "composite_scores": {
                metric_id: None
                for metric_id in _configured_composite_metric_ids(profile)
            },
            "composite_metric_statuses": {
                metric_id: "MISSING"
                for metric_id in _configured_composite_metric_ids(profile)
            },
            "review_reasons": [],
        }
    report = {str(key): value for key, value in report_value.items()}
    visual_value = report.get("visual_audit_summary")
    visual = (
        {str(key): value for key, value in visual_value.items()}
        if isinstance(visual_value, Mapping)
        else {}
    )
    integrity_value = outcome.get("integrity")
    integrity = (
        {str(key): value for key, value in integrity_value.items()}
        if isinstance(integrity_value, Mapping)
        else {"valid": False}
    )
    manifest_value = report.get("manifest")
    manifest = (
        {str(key): value for key, value in manifest_value.items()}
        if isinstance(manifest_value, Mapping)
        else {}
    )
    composite_scores, composite_statuses, duplicate_composites = _composite_scores(
        report, profile
    )
    report_legality = _report_legality(
        report,
        checkpoint,
        case,
        profile,
        composite_scores,
        duplicate_composites,
    )
    visual_usage = _visual_usage(report, runtime_root)
    response_legality = _model_response_legality(report, runtime_root)
    return {
        "case_key": case.case_key,
        "case_id": case.case_id,
        "topic": case.topic,
        "topic_slug": case.topic_slug,
        "product": case.product,
        "human_rank": case.human_rank,
        "page_count": len(case.renders),
        "status": str(checkpoint.get("status") or "INVALID"),
        "run_id": report.get("run_id"),
        "profile_id": report.get("profile_id"),
        "profile_version": report.get("profile_version"),
        "decision": report.get("decision"),
        "coverage": report.get("coverage"),
        "base_score": _numeric_score(report, "base_score"),
        "full_score": _numeric_score(report, "full_score"),
        "duration_ms": manifest.get("duration_ms"),
        "resumed": bool(outcome.get("resumed")),
        "report_relative_path": checkpoint.get("report_relative_path"),
        "audit_integrity": integrity,
        "visual_audit_summary": visual,
        "visual_usage": visual_usage,
        "model_response_legality": response_legality,
        "report_legality": report_legality,
        "result_status_counts": _result_status_counts(report),
        "composite_scores": composite_scores,
        "composite_metric_statuses": composite_statuses,
        "review_reasons": list(report.get("review_reasons", ()))
        if isinstance(report.get("review_reasons"), Sequence)
        and not isinstance(report.get("review_reasons"), (str, bytes))
        else [],
    }


def _statistics(cases: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    usable = [
        item
        for item in cases
        if isinstance(item.get("human_rank"), int)
        and not isinstance(item.get("human_rank"), bool)
        and isinstance(item.get("base_score"), (int, float))
        and not isinstance(item.get("base_score"), bool)
        and math.isfinite(float(item["base_score"]))
    ]
    human_ranks = [int(item["human_rank"]) for item in usable]
    scores = [float(item["base_score"]) for item in usable]
    return {
        "case_count": len(usable),
        "spearman_base_vs_human": (
            spearman(scores, [-float(rank) for rank in human_ranks])
            if len(usable) > 1
            else None
        ),
        "pairwise_base_accuracy": (
            pairwise_accuracy(human_ranks, scores) if len(usable) > 1 else None
        ),
        "comparable_pairs": len(usable) * (len(usable) - 1) // 2,
    }


def _composite_diagnostics(
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    metric_ids = sorted(
        {
            str(metric_id)
            for item in cases
            if isinstance(item.get("composite_scores"), Mapping)
            for metric_id in item["composite_scores"]
        }
    )
    diagnostics: dict[str, Mapping[str, Any]] = {}
    for metric_id in metric_ids:
        usable: list[tuple[int, float]] = []
        for item in cases:
            scores = item.get("composite_scores")
            scores = scores if isinstance(scores, Mapping) else {}
            rank = item.get("human_rank")
            score = scores.get(metric_id)
            if (
                isinstance(rank, int)
                and not isinstance(rank, bool)
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(float(score))
            ):
                usable.append((rank, float(score)))
        values = [score for _rank, score in usable]
        ranks = [rank for rank, _score in usable]
        mean = sum(values) / len(values) if values else None
        variance = (
            sum((value - mean) ** 2 for value in values) / len(values)
            if mean is not None
            else None
        )
        diagnostics[metric_id] = {
            "status": "NOT_FOR_GATING_OR_WEIGHT_FIT",
            "case_count": len(usable),
            "mean": mean,
            "population_variance": variance,
            "spearman_vs_human": (
                spearman(values, [-float(rank) for rank in ranks])
                if len(usable) > 1
                else None
            ),
        }
    return diagnostics


def summarize_topic(
    topic: str,
    topic_slug: str,
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    ordered = tuple(sorted(cases, key=lambda item: int(item["human_rank"])))
    gate_reasons: list[str] = []
    for item in ordered:
        case_key = str(item.get("case_key") or item.get("case_id") or "unknown")
        if item.get("status") != "COMPLETED":
            gate_reasons.append(f"case_error:{case_key}")
            continue
        if item.get("profile_id") != EXPECTED_PROFILE_ID:
            gate_reasons.append(f"profile_id_mismatch:{case_key}")
        if item.get("profile_version") != EXPECTED_PROFILE_VERSION:
            gate_reasons.append(f"profile_mismatch:{case_key}")
        score = item.get("base_score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            gate_reasons.append(f"base_score_not_finite:{case_key}")
        report_legality = item.get("report_legality")
        if (
            not isinstance(report_legality, Mapping)
            or report_legality.get("valid") is not True
        ):
            gate_reasons.append(f"report_invalid:{case_key}")
        if item.get("coverage") != "FULL":
            gate_reasons.append(
                f"coverage_not_full:{case_key}:{item.get('coverage') or 'UNKNOWN'}"
            )
        visual_summary = item.get("visual_audit_summary")
        if (
            not isinstance(visual_summary, Mapping)
            or visual_summary.get("coverage_complete") is not True
        ):
            gate_reasons.append(f"visual_coverage_incomplete:{case_key}")
        if item.get("decision") == "ERROR":
            gate_reasons.append(f"decision_error:{case_key}")
        integrity = item.get("audit_integrity")
        if not isinstance(integrity, Mapping) or integrity.get("valid") is not True:
            gate_reasons.append(f"audit_integrity_invalid:{case_key}")
    eligible = not gate_reasons and len(ordered) > 1
    all_statistics = _statistics(ordered)
    full_statistics = _statistics(
        tuple(
            item
            for item in ordered
            if item.get("status") == "COMPLETED"
            and item.get("coverage") == "FULL"
            and isinstance(item.get("visual_audit_summary"), Mapping)
            and item["visual_audit_summary"].get("coverage_complete") is True
            and isinstance(item.get("audit_integrity"), Mapping)
            and item["audit_integrity"].get("valid") is True
        )
    )
    formal = dict(all_statistics) if eligible else {
        "case_count": len(ordered),
        "spearman_base_vs_human": None,
        "pairwise_base_accuracy": None,
        "comparable_pairs": 0,
    }
    return {
        "topic": topic,
        "topic_slug": topic_slug,
        "case_count": len(ordered),
        "rank_statistics_eligible": eligible,
        "rank_gate_reasons": gate_reasons,
        "statistics": formal,
        "exploratory_statistics": {
            "status": "NOT_FOR_GATING_OR_WEIGHT_FIT",
            "all_available_cases": dict(all_statistics),
            "full_cases_only": dict(full_statistics),
            "composite_metrics": dict(_composite_diagnostics(ordered)),
        },
        "cases": [dict(item) for item in ordered],
    }


def _mean_available(values: Iterable[object]) -> float | None:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    return sum(numbers) / len(numbers) if numbers else None


def _micro_pairwise(
    topic_summaries: Sequence[Mapping[str, Any]],
    *,
    section: str,
) -> tuple[float | None, int]:
    weighted = 0.0
    total_pairs = 0
    for topic in topic_summaries:
        container_value = topic.get(section)
        container = (
            container_value if isinstance(container_value, Mapping) else {}
        )
        if section == "exploratory_statistics":
            candidate = container.get("all_available_cases")
            stats = candidate if isinstance(candidate, Mapping) else {}
        else:
            stats = container
        pairs = stats.get("comparable_pairs")
        accuracy = stats.get("pairwise_base_accuracy")
        if (
            isinstance(pairs, int)
            and not isinstance(pairs, bool)
            and pairs > 0
            and isinstance(accuracy, (int, float))
            and not isinstance(accuracy, bool)
        ):
            weighted += pairs * float(accuracy)
            total_pairs += pairs
    return (weighted / total_pairs if total_pairs else None), total_pairs


def _aggregate_result_status_counts(
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Mapping[str, int]]:
    metric: Counter[str] = Counter()
    execution: Counter[str] = Counter()
    for item in cases:
        counts = item.get("result_status_counts")
        counts = counts if isinstance(counts, Mapping) else {}
        for target, key in ((metric, "metric_status"), (execution, "execution_status")):
            values = counts.get(key)
            if not isinstance(values, Mapping):
                continue
            for status, count in values.items():
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    target[str(status)] += count
    return {
        "metric_status": dict(sorted(metric.items())),
        "execution_status": dict(sorted(execution.items())),
    }


def _aggregate_visual_usage(
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "image_tokens",
        "cached_tokens",
        "cache_creation_input_tokens",
        "request_bytes",
        "request_count",
        "reported_cost",
    )
    result: dict[str, Any] = {
        "case_count": len(cases),
        "usage_complete_count": 0,
        "cost_known_count": 0,
        "field_reporting_counts": {},
    }
    usage_values: list[Mapping[str, Any]] = []
    for item in cases:
        usage = item.get("visual_usage")
        usage_values.append(usage if isinstance(usage, Mapping) else {})
    result["usage_complete_count"] = sum(
        usage.get("usage_complete") is True for usage in usage_values
    )
    result["cost_known_count"] = sum(
        usage.get("cost_known") is True for usage in usage_values
    )
    reporting: dict[str, int] = {}
    for field in fields:
        numbers = [
            float(usage[field])
            for usage in usage_values
            if isinstance(usage.get(field), (int, float))
            and not isinstance(usage.get(field), bool)
            and math.isfinite(float(usage[field]))
            and float(usage[field]) >= 0.0
        ]
        reporting[field] = len(numbers)
        suffix = "_when_reported"
        result[f"{field}{suffix}"] = sum(numbers) if numbers else None
        result[f"{field}_all_cases_complete"] = (
            sum(numbers) if len(numbers) == len(cases) and cases else None
        )
    result["field_reporting_counts"] = reporting
    result["known_reported_cost"] = (
        result["reported_cost_all_cases_complete"]
        if result["cost_known_count"] == len(cases) and cases
        else None
    )
    return result


def _aggregate_response_legality(
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    valid = 0
    logical_audits = 0
    for item in cases:
        value = item.get("model_response_legality")
        value = value if isinstance(value, Mapping) else {}
        raw_valid = value.get("valid_response_count")
        raw_attempts = value.get(
            "logical_audit_count",
            value.get("response_attempt_count"),
        )
        if (
            isinstance(raw_valid, int)
            and not isinstance(raw_valid, bool)
            and isinstance(raw_attempts, int)
            and not isinstance(raw_attempts, bool)
            and 0 <= raw_valid <= raw_attempts
        ):
            valid += raw_valid
            logical_audits += raw_attempts
    rate = valid / logical_audits if logical_audits else None
    return {
        "valid_response_count": valid,
        "response_attempt_count": logical_audits,
        "logical_audit_count": logical_audits,
        "legal_response_rate": rate,
        "threshold": _MODEL_RESPONSE_LEGALITY_THRESHOLD,
        "meets_threshold": bool(
            rate is not None and rate >= _MODEL_RESPONSE_LEGALITY_THRESHOLD
        ),
        "counting_contract": "POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2",
    }


def _aggregate_composite_diagnostics(
    topic_summaries: Sequence[Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    by_metric: dict[str, list[Mapping[str, Any]]] = {}
    for topic in topic_summaries:
        exploratory = topic.get("exploratory_statistics")
        exploratory = exploratory if isinstance(exploratory, Mapping) else {}
        values = exploratory.get("composite_metrics")
        values = values if isinstance(values, Mapping) else {}
        for metric_id, diagnostic in values.items():
            if isinstance(diagnostic, Mapping):
                by_metric.setdefault(str(metric_id), []).append(diagnostic)
    result: dict[str, Mapping[str, Any]] = {}
    for metric_id, diagnostics in sorted(by_metric.items()):
        result[metric_id] = {
            "status": "NOT_FOR_GATING_OR_WEIGHT_FIT",
            "topic_count": len(diagnostics),
            "macro_within_topic_spearman": _mean_available(
                item.get("spearman_vs_human") for item in diagnostics
            ),
            "mean_within_topic_population_variance": _mean_available(
                item.get("population_variance") for item in diagnostics
            ),
        }
    return result


def aggregate_suite(
    suite: SuiteSpec,
    profile: EvalProfile,
    topic_summaries: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    all_topics_eligible = bool(topic_summaries) and all(
        item.get("rank_statistics_eligible") is True for item in topic_summaries
    )
    formal_spearman = (
        _mean_available(
            _mapping(item.get("statistics"), "topic statistics").get(
                "spearman_base_vs_human"
            )
            for item in topic_summaries
        )
        if all_topics_eligible
        else None
    )
    formal_pairwise, formal_pairs = (
        _micro_pairwise(topic_summaries, section="statistics")
        if all_topics_eligible
        else (None, 0)
    )
    exploratory_spearman = _mean_available(
        _mapping(
            _mapping(item.get("exploratory_statistics"), "topic exploratory").get(
                "all_available_cases"
            ),
            "topic all-case exploratory",
        ).get("spearman_base_vs_human")
        for item in topic_summaries
    )
    exploratory_pairwise, exploratory_pairs = _micro_pairwise(
        topic_summaries, section="exploratory_statistics"
    )
    cases = [
        case
        for topic in topic_summaries
        for case in _sequence(topic.get("cases"), "topic cases")
        if isinstance(case, Mapping)
    ]
    decisions = Counter(str(item.get("decision") or "UNKNOWN") for item in cases)
    coverages = Counter(str(item.get("coverage") or "UNKNOWN") for item in cases)
    visual_usage = _aggregate_visual_usage(cases)
    response_legality = _aggregate_response_legality(cases)
    result_status_counts = _aggregate_result_status_counts(cases)
    reports_legal = sum(
        isinstance(item.get("report_legality"), Mapping)
        and item["report_legality"].get("valid") is True
        for item in cases
    )
    finite_scores = sum(
        isinstance(item.get("base_score"), (int, float))
        and not isinstance(item.get("base_score"), bool)
        and math.isfinite(float(item["base_score"]))
        for item in cases
    )
    completed_cases = sum(item.get("status") == "COMPLETED" for item in cases)
    audit_valid_cases = sum(
        isinstance(item.get("audit_integrity"), Mapping)
        and item["audit_integrity"].get("valid") is True
        for item in cases
    )
    validation_gate = {
        "expected_case_count": len(suite.cases),
        "completed_case_count": completed_cases,
        "legal_report_count": reports_legal,
        "finite_base_score_count": finite_scores,
        "audit_integrity_valid_count": audit_valid_cases,
        "all_topics_rank_eligible": all_topics_eligible,
        "model_response_legality_meets_98_percent": response_legality.get(
            "meets_threshold"
        )
        is True,
    }
    validation_gate["passed"] = bool(
        cases
        and len(cases) == len(suite.cases)
        and completed_cases == len(cases)
        and reports_legal == len(cases)
        and finite_scores == len(cases)
        and audit_valid_cases == len(cases)
        and response_legality.get("meets_threshold") is True
    )
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset_id": suite.dataset_id,
        "dataset_revision": suite.revision,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "evaluation_git_sha": git_sha(REPOSITORY_ROOT),
        "topic_count": len(topic_summaries),
        "case_count": len(cases),
        "rendered_slide_count": sum(
            int(item.get("page_count", 0)) for item in cases
        ),
        "topics": [dict(item) for item in topic_summaries],
        "aggregate": {
            "all_topics_rank_eligible": all_topics_eligible,
            "formal_macro_spearman_base_vs_human": formal_spearman,
            "formal_micro_pairwise_within_topics": formal_pairwise,
            "formal_within_topic_comparable_pairs": formal_pairs,
            "decision_counts": dict(sorted(decisions.items())),
            "coverage_counts": dict(sorted(coverages.items())),
            "completed_case_count": completed_cases,
            "legal_report_count": reports_legal,
            "finite_base_score_count": finite_scores,
            "audit_integrity_valid_count": audit_valid_cases,
            "result_status_counts": result_status_counts,
            "visual_usage": visual_usage,
            "model_response_legality": response_legality,
            "validation_gate": validation_gate,
            "exploratory_unqualified": {
                "status": "NOT_FOR_GATING_OR_WEIGHT_FIT",
                "macro_spearman_base_vs_human": exploratory_spearman,
                "micro_pairwise_within_topics": exploratory_pairwise,
                "within_topic_comparable_pairs": exploratory_pairs,
                "composite_metrics": _aggregate_composite_diagnostics(
                    topic_summaries
                ),
            },
        },
        "methodology": {
            "human_rank_visible_to_oracles": False,
            "global_rank_statistics_prohibited": True,
            "rank_scope": "WITHIN_TOPIC_ONLY",
            "formal_statistics_require_full_coverage": True,
            "degraded_cases_suppress_formal_topic_statistics": True,
            "rank_statistics_are_release_gate": False,
            "weight_fitting_used": False,
            "official_renders_injected": True,
            "official_render_paths_anonymized": True,
            "human_rank_source": "ROOT_MANIFEST_HASH_PINNED_TOPIC_SUBSET",
            "manifest_hashes_verified": True,
            "resume_bound_to_evaluation_git_sha": True,
            "model_response_legality_threshold": (
                _MODEL_RESPONSE_LEGALITY_THRESHOLD
            ),
            "composite_statistics_usage": "DIAGNOSTIC_ONLY",
            "composite_variance_definition": "WITHIN_TOPIC_POPULATION_VARIANCE",
        },
    }


def _relative_href(target: Path, document_dir: Path) -> str:
    try:
        return Path(os.path.relpath(target, document_dir)).as_posix()
    except ValueError as exc:
        raise ValueError(
            "--suite-root and --output-dir must be on the same filesystem volume"
        ) from exc


def _html_href(value: str) -> str:
    return html.escape(quote(value, safe="/._~-"), quote=True)


def _output_reference_path(output_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError("report reference must be relative to --output-dir")
    return _contained_path(output_dir, *candidate.parts)


def _display_number(value: object, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    return f"{float(value):.1%}" if percent else f"{float(value):.3f}"


def _html_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--ink:#15221d;--muted:#65756d;--paper:#f3f1eb;--card:#fff;--line:#d7ded9;--accent:#156b5b;--warn:#a84b2a;--bad:#9c2736}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1320px;margin:auto;padding:38px 26px 80px}}h1{{font:700 38px/1.15 Georgia,serif;margin:0 0 10px}}h2{{font:700 26px Georgia,serif;margin:0}}h3{{margin:20px 0 8px}}
a{{color:var(--accent)}}code{{font-size:.92em}}.lead{{color:var(--muted);font-size:17px;max-width:980px}}
.stats{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0}}.stat,.case,.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px}}
.stat{{padding:15px}}.stat b{{display:block;color:var(--accent);font-size:24px}}.panel,.case{{padding:21px;margin:18px 0}}
.warning{{border-left:4px solid var(--warn);background:#fff3ec;padding:10px 14px}}.error{{color:var(--bad)}}.muted{{color:var(--muted)}}
.badge{{display:inline-block;border-radius:999px;background:#e5f1ed;color:var(--accent);font-weight:700;padding:3px 9px;margin:4px 5px 4px 0}}.badge.warn{{background:#fff0e8;color:var(--warn)}}.badge.bad{{background:#fdecef;color:var(--bad)}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted)}}
.case-head{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}}.gallery{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}}.slide{{display:block;text-decoration:none;color:var(--ink);border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fafafa}}.slide img{{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#111}}.slide span{{display:block;padding:5px 8px;font-size:12px}}
details{{margin-top:12px}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;max-height:420px;overflow:auto;background:#14201b;color:#eff8f3;border-radius:8px;padding:13px}}
@media(max-width:900px){{.stats{{grid-template-columns:1fr 1fr}}.gallery{{grid-template-columns:repeat(2,1fr)}}.case-head{{display:block}}}}@media(max-width:560px){{.stats,.gallery{{grid-template-columns:1fr}}}}
</style></head><body><main>{body}</main></body></html>"""


def build_suite_html(payload: Mapping[str, Any]) -> str:
    aggregate = _mapping(payload.get("aggregate"), "suite aggregate")
    visual_usage = _mapping(aggregate.get("visual_usage"), "suite visual usage")
    response_legality = _mapping(
        aggregate.get("model_response_legality"), "suite model response legality"
    )
    status_counts = _mapping(
        aggregate.get("result_status_counts"), "suite result status counts"
    )
    metric_status_counts = _mapping(
        status_counts.get("metric_status"), "suite metric status counts"
    )
    operations_json = html.escape(
        json.dumps(
            {
                "validation_gate": aggregate.get("validation_gate"),
                "model_response_legality": response_legality,
                "visual_usage": visual_usage,
                "result_status_counts": status_counts,
                "composite_diagnostics": _mapping(
                    aggregate.get("exploratory_unqualified"),
                    "suite exploratory diagnostics",
                ).get("composite_metrics"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    topic_sections: list[str] = []
    for raw_topic in _sequence(payload.get("topics"), "suite topics"):
        topic = _mapping(raw_topic, "suite topic")
        stats = _mapping(topic.get("statistics"), "topic statistics")
        exploratory = _mapping(
            topic.get("exploratory_statistics"), "topic exploratory statistics"
        )
        diagnostic = _mapping(
            exploratory.get("all_available_cases"), "topic diagnostic statistics"
        )
        rows = "".join(
            "<tr>"
            f"<td>{int(case.get('human_rank', 0))}</td>"
            f"<td>{html.escape(str(case.get('product') or ''))}</td>"
            f"<td>{_display_number(case.get('base_score'))}</td>"
            f"<td>{html.escape(str(case.get('decision') or 'UNKNOWN'))}</td>"
            f"<td>{html.escape(str(case.get('coverage') or 'UNKNOWN'))}</td>"
            "</tr>"
            for case in _sequence(topic.get("cases"), "topic cases")
            if isinstance(case, Mapping)
        )
        topic_href = f"topics/{str(topic.get('topic_slug'))}/index.html"
        gate = "正式统计可用" if topic.get("rank_statistics_eligible") else "正式统计已抑制"
        topic_sections.append(
            f"""<section class="panel"><div class="case-head"><div><h2>{html.escape(str(topic.get('topic')))}</h2>
<span class="badge">{int(topic.get('case_count', 0))} decks</span>
<span class="badge {'warn' if not topic.get('rank_statistics_eligible') else ''}">{gate}</span>
<span class="badge">正式 Spearman {_display_number(stats.get('spearman_base_vs_human'))}</span>
<span class="badge warn">诊断 Spearman {_display_number(diagnostic.get('spearman_base_vs_human'))}</span></div>
<a href="{_html_href(topic_href)}">打开幻灯片与逐例结果 →</a></div>
<table><thead><tr><th>人评 rank</th><th>产品</th><th>8.4 base score</th><th>Decision</th><th>Coverage</th></tr></thead><tbody>{rows}</tbody></table></section>"""
        )
    diagnostic_suite = _mapping(
        aggregate.get("exploratory_unqualified"), "suite exploratory"
    )
    body = f"""<h1>Slides-Align / Profile 8.4 真实切片</h1>
<p class="lead">固定 revision <code>{html.escape(str(payload.get('dataset_revision')))}</code>；{int(payload.get('topic_count', 0))} 个同主题组，{int(payload.get('case_count', 0))} 份 PPTX / {int(payload.get('rendered_slide_count', 0))} 页。模型看不到人评 rank，统计只在各主题内部计算。</p>
<div class="stats"><div class="stat"><b>{_display_number(aggregate.get('formal_macro_spearman_base_vs_human'))}</b>正式 Macro Spearman</div>
<div class="stat"><b>{_display_number(diagnostic_suite.get('macro_spearman_base_vs_human'))}</b>诊断 Macro Spearman</div>
<div class="stat"><b>{html.escape(str(aggregate.get('audit_integrity_valid_count')))}/{int(payload.get('case_count', 0))}</b>审计链与 artifact</div>
<div class="stat"><b>{html.escape(str(visual_usage.get('usage_complete_count')))}/{int(payload.get('case_count', 0))}</b>usage 完整</div>
<div class="stat"><b>{_display_number(response_legality.get('legal_response_rate'), percent=True)}</b>模型合法响应率</div>
<div class="stat"><b>{html.escape(str(metric_status_counts.get('ERROR', 0)))}</b>OracleResult ERROR</div>
<div class="stat"><b>{html.escape(str(metric_status_counts.get('NA', 0)))}</b>OracleResult N/A</div>
<div class="stat"><b>{_display_number(visual_usage.get('reported_cost_when_reported'))}</b>已报告模型成本</div></div>
<p class="warning"><b>统计边界：</b>任一 topic 中只要存在 ERROR、DEGRADED、Profile 错配或审计链无效，该 topic 的正式 Spearman/Pairwise 即为 N/A；诊断值只用于发现 Oracle/Profile 偏差，不得门禁、拟合权重或跨主题混排。</p>
<details><summary>合法响应、usage / cost 与 Composite 诊断</summary><pre>{operations_json}</pre></details>
{''.join(topic_sections)}
<p class="muted">机器结果：<a href="comparison.json">comparison.json</a></p>"""
    return _html_shell("Slides-Align Profile 8.4", body)


def _result_rows(report: Mapping[str, Any]) -> str:
    rows: list[str] = []
    results = report.get("results")
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        for value in results:
            if not isinstance(value, Mapping):
                continue
            score = value.get("normalized_score")
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(str(value.get('metric_id') or 'unknown'))}</code></td>"
                f"<td>{html.escape(str(value.get('metric_status') or 'UNKNOWN'))}</td>"
                f"<td>{_display_number(score)}</td>"
                f"<td>{html.escape(str(value.get('severity') or 'INFO'))}</td>"
                "</tr>"
            )
    return "".join(rows)


def build_topic_html(
    topic_payload: Mapping[str, Any],
    *,
    case_specs: Mapping[str, CaseSpec],
    output_dir: Path,
    document_dir: Path,
) -> str:
    stats = _mapping(topic_payload.get("statistics"), "topic statistics")
    exploratory = _mapping(
        topic_payload.get("exploratory_statistics"), "topic exploratory"
    )
    diagnostic = _mapping(
        exploratory.get("all_available_cases"), "topic diagnostic"
    )
    composite_diagnostics = _mapping(
        exploratory.get("composite_metrics"), "topic composite diagnostics"
    )
    composite_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(metric_id))}</code></td>"
        f"<td>{int(value.get('case_count', 0))}</td>"
        f"<td>{_display_number(value.get('mean'))}</td>"
        f"<td>{_display_number(value.get('population_variance'))}</td>"
        f"<td>{_display_number(value.get('spearman_vs_human'))}</td>"
        "</tr>"
        for metric_id, raw_value in sorted(composite_diagnostics.items())
        if isinstance(raw_value, Mapping)
        for value in ({str(key): item for key, item in raw_value.items()},)
    )
    reasons = _sequence(topic_payload.get("rank_gate_reasons"), "rank gate reasons")
    cases_html: list[str] = []
    for case_value in _sequence(topic_payload.get("cases"), "topic cases"):
        case = _mapping(case_value, "topic case")
        spec = case_specs[str(case.get("case_key"))]
        report: Mapping[str, Any] = {}
        report_href = case.get("report_relative_path")
        report_path: Path | None = None
        if isinstance(report_href, str) and report_href:
            report_path = _output_reference_path(output_dir, report_href)
            if report_path.is_file():
                report = _read_json(report_path, "stored EvaluationReport")
        slides = "".join(
            f'<a class="slide" target="_blank" rel="noopener" href="{_html_href(_relative_href(item.path, document_dir))}">'
            f'<img loading="lazy" src="{_html_href(_relative_href(item.path, document_dir))}" alt="{html.escape(spec.product)} page {item.page_number}">'
            f"<span>第 {item.page_number} 页</span></a>"
            for item in spec.renders
        )
        report_link = (
            f'<a href="{_html_href(_relative_href(report_path, document_dir))}">EvaluationReport JSON</a>'
            if report_path is not None and report_path.is_file()
            else '<span class="error">EvaluationReport 不可用</span>'
        )
        pptx_link = _relative_href(spec.pptx_path, document_dir)
        visual = case.get("visual_audit_summary")
        visual = visual if isinstance(visual, Mapping) else {}
        integrity = case.get("audit_integrity")
        integrity = integrity if isinstance(integrity, Mapping) else {}
        coverage_class = (
            "bad"
            if case.get("status") != "COMPLETED" or case.get("decision") == "ERROR"
            else "warn" if case.get("coverage") != "FULL" else ""
        )
        error_note = (
            f'<p class="error"><b>运行失败：</b>{html.escape(str(case.get("error_code") or "EVALUATION_FAILURE"))}</p>'
            if case.get("status") != "COMPLETED"
            else ""
        )
        case_json = json.dumps(
            {
                "visual_audit_summary": visual,
                "review_reasons": case.get("review_reasons", []),
                "audit_integrity": integrity,
            },
            ensure_ascii=False,
            indent=2,
        )
        cases_html.append(
            f"""<section class="case"><div class="case-head"><div><h2>{html.escape(spec.product)}</h2>
<span class="badge">人评 #{spec.human_rank}</span><span class="badge">8.4 {_display_number(case.get('base_score'))}</span>
<span class="badge {coverage_class}">{html.escape(str(case.get('decision') or 'ERROR'))} / {html.escape(str(case.get('coverage') or 'ERROR'))}</span></div>
<div>{report_link}<br><a href="{_html_href(pptx_link)}">原始 PPTX</a></div></div>{error_note}
<details open><summary>全部 {len(spec.renders)} 页官方渲染</summary><div class="gallery">{slides}</div></details>
<details><summary>Composite / OracleResult</summary><table><thead><tr><th>Metric</th><th>Status</th><th>Score</th><th>Severity</th></tr></thead><tbody>{_result_rows(report) or '<tr><td colspan="4">无结果</td></tr>'}</tbody></table></details>
<details><summary>视觉覆盖、Review 原因与审计完整性</summary><pre>{html.escape(case_json)}</pre></details></section>"""
        )
    gate_html = (
        "<p class=\"warning\"><b>正式排名统计已抑制：</b>"
        + html.escape(" | ".join(str(value) for value in reasons))
        + "</p>"
        if reasons
        else "<p><span class=\"badge\">正式排名统计可用</span></p>"
    )
    body = f"""<p><a href="../../index.html">← 返回 Suite</a></p>
<h1>{html.escape(str(topic_payload.get('topic')))}</h1>
<p class="lead">仅在本主题内对照人评顺序；不跨主题排序。</p>
<div class="stats"><div class="stat"><b>{_display_number(stats.get('spearman_base_vs_human'))}</b>正式 Spearman</div>
<div class="stat"><b>{_display_number(stats.get('pairwise_base_accuracy'), percent=True)}</b>正式 Pairwise</div>
<div class="stat"><b>{_display_number(diagnostic.get('spearman_base_vs_human'))}</b>诊断 Spearman</div>
<div class="stat"><b>{_display_number(diagnostic.get('pairwise_base_accuracy'), percent=True)}</b>诊断 Pairwise</div></div>
<details><summary>Composite 逐项 Spearman 与维度方差（仅诊断）</summary><table><thead><tr><th>Composite metric</th><th>样本</th><th>均值</th><th>总体方差</th><th>Spearman</th></tr></thead><tbody>{composite_rows or '<tr><td colspan="5">无可用数据</td></tr>'}</tbody></table></details>
{gate_html}{''.join(cases_html)}
<p class="muted"><a href="comparison.json">本主题机器结果</a></p>"""
    return _html_shell(f"Slides-Align · {topic_payload.get('topic')}", body)


def _write_outputs(
    payload: Mapping[str, Any],
    suite: SuiteSpec,
    output_dir: Path,
) -> None:
    _atomic_write_json(output_dir / "comparison.json", payload)
    (output_dir / "index.html").write_text(
        build_suite_html(payload), encoding="utf-8"
    )
    case_specs = {case.case_key: case for case in suite.cases}
    for topic_value in _sequence(payload.get("topics"), "suite topics"):
        topic = _mapping(topic_value, "suite topic")
        slug = _safe_output_component(topic.get("topic_slug"), "topic output slug")
        topic_dir = _contained_path(output_dir, "topics", slug)
        topic_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(topic_dir / "comparison.json", topic)
        (topic_dir / "index.html").write_text(
            build_topic_html(
                topic,
                case_specs=case_specs,
                output_dir=output_dir,
                document_dir=topic_dir,
            ),
            encoding="utf-8",
        )


def run_benchmark(
    suite: SuiteSpec,
    profile: EvalProfile,
    output_dir: str | Path,
    *,
    workers: int,
    resume: bool,
    require_live_provider: bool,
) -> Mapping[str, Any]:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer between 1 and 8")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    outcomes: dict[str, Mapping[str, Any]] = {}
    futures: dict[Future[Mapping[str, Any]], CaseSpec] = {}
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="slides-align-profile84"
    )
    try:
        for case in suite.cases:
            future = executor.submit(
                evaluate_case,
                suite,
                case,
                profile,
                output,
                resume=resume,
                require_live_provider=require_live_provider,
            )
            futures[future] = case
        for future in as_completed(futures):
            case = futures[future]
            outcome = future.result()
            outcomes[case.case_key] = outcome
            summary = _case_summary(
                case,
                outcome,
                profile,
                runtime_root=_runtime_root(output, case),
            )
            marker = "RESUME" if outcome.get("resumed") else str(summary["status"])
            print(
                f"[{len(outcomes)}/{len(suite.cases)}] {marker} {case.case_key} "
                f"decision={summary.get('decision')} coverage={summary.get('coverage')}",
                flush=True,
            )
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    summaries = {
        case.case_key: _case_summary(
            case,
            outcomes[case.case_key],
            profile,
            runtime_root=_runtime_root(output, case),
        )
        for case in suite.cases
    }
    topic_payloads: list[Mapping[str, Any]] = []
    for topic in suite.topics:
        topic_cases = [case for case in suite.cases if case.topic == topic]
        topic_payloads.append(
            summarize_topic(
                topic,
                topic_cases[0].topic_slug,
                tuple(summaries[case.case_key] for case in topic_cases),
            )
        )
    payload = aggregate_suite(suite, profile, topic_payloads)
    _write_outputs(payload, suite, output)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the pinned Slides-Align real slice with Profile 8.4 and "
            "produce within-topic diagnostic statistics."
        )
    )
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=REPOSITORY_ROOT / "var" / "datasets" / "slides_align_three_topics",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "var" / "benchmarks" / "slides-align-profile84",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=SOURCE_ROOT / "ppt_eval" / "profiles" / "finished_deck_v8.json",
    )
    parser.add_argument(
        "--topic",
        action="append",
        default=[],
        help="topic name or slug; repeat to select multiple topics",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse only hash/profile/audit-validated completed case checkpoints",
    )
    parser.add_argument(
        "--allow-offline",
        action="store_true",
        help="allow an intentionally model-free diagnostic run",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify every manifest file/hash and exit before constructing a runtime",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = validate_suite(
            args.suite_root,
            requested_topics=tuple(args.topic),
            verify_hashes=True,
        )
        print(
            "manifest verified: "
            f"{suite.manifest_file_count} files / {suite.manifest_bytes} bytes; "
            f"selected {len(suite.cases)} decks / "
            f"{sum(len(case.renders) for case in suite.cases)} pages",
            flush=True,
        )
        if args.verify_only:
            return 0
        profile = load_profile(args.profile.resolve())
        if profile.profile_id != EXPECTED_PROFILE_ID or profile.version != EXPECTED_PROFILE_VERSION:
            raise ValueError(
                f"benchmark requires {EXPECTED_PROFILE_ID}@{EXPECTED_PROFILE_VERSION}"
            )
        if profile.scene != SceneType.READY_MADE:
            raise ValueError("benchmark profile must target the ready_made scene")
        require_clean_evaluation_checkout()
        payload = run_benchmark(
            suite,
            profile,
            args.output_dir,
            workers=args.workers,
            resume=args.resume,
            require_live_provider=not args.allow_offline,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"benchmark failed: {_safe_error_code(exc)}", file=sys.stderr)
        return 2
    aggregate = _mapping(payload.get("aggregate"), "suite aggregate")
    print(
        json.dumps(
            {
                "output": str(args.output_dir.resolve()),
                "case_count": payload.get("case_count"),
                "all_topics_rank_eligible": aggregate.get(
                    "all_topics_rank_eligible"
                ),
                "formal_macro_spearman": aggregate.get(
                    "formal_macro_spearman_base_vs_human"
                ),
                "diagnostic_macro_spearman": _mapping(
                    aggregate.get("exploratory_unqualified"), "suite exploratory"
                ).get("macro_spearman_base_vs_human"),
                "model_response_legality": _mapping(
                    aggregate.get("model_response_legality"),
                    "model response legality",
                ).get("legal_response_rate"),
                "validation_gate_passed": _mapping(
                    aggregate.get("validation_gate"), "validation gate"
                ).get("passed"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    validation_gate = _mapping(aggregate.get("validation_gate"), "validation gate")
    return 0 if validation_gate.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
