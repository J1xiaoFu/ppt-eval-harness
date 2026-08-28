"""Safe, local multipart ingestion for the single-node API runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Mapping, Protocol, Sequence

from ppt_eval.adapters.pptx import PptxAdapter, SecurityLimits

PRESENTATION_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
MAX_PRESENTATION_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENT_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 100 * 1024 * 1024
MAX_SOURCE_MATERIALS = 16
MAX_ASSETS = 32

_CHUNK_BYTES = 1024 * 1024
_WORKSPACE_MARKER = ".ppt-eval-upload-workspace"
_WORKSPACE_NAME = re.compile(r"^upload-[0-9a-f]{32}$")
_INVALID_WINDOWS_FILENAME = re.compile(r"[<>:\"|?*]")
_PRESENTATION_MEDIA_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "application/zip",
        PRESENTATION_MEDIA_TYPE,
    }
)
_SAFE_SOURCE_SUFFIXES = frozenset(
    {
        ".csv",
        ".json",
        ".md",
        ".tsv",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SAFE_ASSET_SUFFIXES = frozenset(
    {
        ".csv",
        ".gif",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp4",
        ".pdf",
        ".png",
        ".svg",
        ".tsv",
        ".webm",
        ".webp",
        ".xls",
        ".xlsx",
    }
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class UploadValidationError(ValueError):
    """An upload is unsafe or does not satisfy the API contract."""

    error_code = "UPLOAD_INVALID"


class UploadTooLargeError(UploadValidationError):
    """A streamed file exceeded a configured byte limit."""

    error_code = "UPLOAD_TOO_LARGE"


class UploadStorageError(RuntimeError):
    """The controlled upload store could not persist an input safely."""

    error_code = "UPLOAD_STORAGE_UNAVAILABLE"


class UploadLike(Protocol):
    filename: str | None
    file: BinaryIO
    content_type: str | None


@dataclass(frozen=True, slots=True)
class StoredUpload:
    original_name: str
    suffix: str
    sha256: str
    size_bytes: int
    staged_path: Path

    def fingerprint_payload(self) -> Mapping[str, Any]:
        return {
            "name": self.original_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(slots=True)
class UploadWorkspace:
    presentation_path: Path
    source_material_paths: tuple[Path, ...]
    asset_paths: tuple[Path, ...]
    presentation_sha256: str
    presentation_original_name: str
    source_material_hashes: tuple[str, ...]
    asset_hashes: tuple[str, ...]
    total_size_bytes: int
    fingerprint: str
    directory: Path
    work_root: Path

    def cleanup(self) -> None:
        """Remove only this marked, opaque staging workspace."""

        root = self.work_root.resolve()
        target = self.directory.resolve()
        if (
            target.parent != root
            or not _WORKSPACE_NAME.fullmatch(target.name)
            or not (target / _WORKSPACE_MARKER).is_file()
        ):
            raise RuntimeError("refusing to clean an unexpected upload workspace")
        if target.exists():
            shutil.rmtree(target)


class LocalUploadStore:
    """Atomically stage multipart inputs until the runtime binds them to CAS."""

    def __init__(
        self,
        root: str | Path,
        *,
        pptx_limits: SecurityLimits | None = None,
        max_presentation_bytes: int = MAX_PRESENTATION_UPLOAD_BYTES,
        max_attachment_bytes: int = MAX_ATTACHMENT_UPLOAD_BYTES,
        max_attachment_total_bytes: int = MAX_ATTACHMENT_TOTAL_BYTES,
    ) -> None:
        self.root = Path(root)
        self.work_root = self.root / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.pptx_limits = pptx_limits or SecurityLimits(
            max_archive_bytes=max_presentation_bytes
        )
        for name, value in (
            ("max_presentation_bytes", max_presentation_bytes),
            ("max_attachment_bytes", max_attachment_bytes),
            ("max_attachment_total_bytes", max_attachment_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_presentation_bytes = max_presentation_bytes
        self.max_attachment_bytes = max_attachment_bytes
        self.max_attachment_total_bytes = max_attachment_total_bytes

    def prepare(
        self,
        presentation: UploadLike,
        *,
        source_materials: Sequence[UploadLike] = (),
        assets: Sequence[UploadLike] = (),
        submission: Mapping[str, Any],
    ) -> UploadWorkspace:
        sources = tuple(source_materials)
        asset_files = tuple(assets)
        if len(sources) > MAX_SOURCE_MATERIALS:
            raise UploadValidationError("too many source material files")
        if len(asset_files) > MAX_ASSETS:
            raise UploadValidationError("too many asset files")

        _validate_presentation_media_type(presentation.content_type)
        presentation_name = _validated_filename(
            presentation.filename,
            allowed_suffixes=frozenset({".pptx"}),
            label="presentation",
        )
        source_names = _validated_distinct_filenames(
            sources,
            allowed_suffixes=_SAFE_SOURCE_SUFFIXES,
            label="source material",
        )
        asset_names = _validated_distinct_filenames(
            asset_files,
            allowed_suffixes=_SAFE_ASSET_SUFFIXES,
            label="asset",
        )

        workspace_dir = self.work_root / f"upload-{uuid.uuid4().hex}"
        draft_dir = self.work_root / f".{workspace_dir.name}.tmp"
        try:
            (draft_dir / "sources").mkdir(parents=True)
            (draft_dir / "assets").mkdir()
            stored_presentation = self._receive(
                presentation,
                destination=draft_dir / "presentation.pptx",
                original_name=presentation_name,
                maximum_bytes=self.max_presentation_bytes,
                validate_pptx=True,
            )
            stored_sources: list[StoredUpload] = []
            stored_assets: list[StoredUpload] = []
            attachment_bytes = 0
            for upload, name in zip(sources, source_names):
                remaining = self.max_attachment_total_bytes - attachment_bytes
                if remaining <= 0:
                    raise UploadTooLargeError("attachment total exceeds the byte limit")
                stored = self._receive(
                    upload,
                    destination=draft_dir / "sources" / name,
                    original_name=name,
                    maximum_bytes=min(self.max_attachment_bytes, remaining),
                )
                stored_sources.append(stored)
                attachment_bytes += stored.size_bytes
            for upload, name in zip(asset_files, asset_names):
                remaining = self.max_attachment_total_bytes - attachment_bytes
                if remaining <= 0:
                    raise UploadTooLargeError("attachment total exceeds the byte limit")
                stored = self._receive(
                    upload,
                    destination=draft_dir / "assets" / name,
                    original_name=name,
                    maximum_bytes=min(self.max_attachment_bytes, remaining),
                )
                stored_assets.append(stored)
                attachment_bytes += stored.size_bytes
            (draft_dir / _WORKSPACE_MARKER).write_text("1\n", encoding="ascii")
            os.replace(draft_dir, workspace_dir)
        except (UploadValidationError, UploadTooLargeError):
            if draft_dir.exists():
                shutil.rmtree(draft_dir, ignore_errors=True)
            raise
        except OSError as exc:
            if draft_dir.exists():
                shutil.rmtree(draft_dir, ignore_errors=True)
            raise UploadStorageError("upload workspace is unavailable") from exc

        presentation_path = workspace_dir / "presentation.pptx"
        source_paths = tuple(workspace_dir / "sources" / name for name in source_names)
        asset_paths = tuple(workspace_dir / "assets" / name for name in asset_names)

        fingerprint_payload = {
            "submission": dict(submission),
            "presentation": stored_presentation.fingerprint_payload(),
            "source_materials": [item.fingerprint_payload() for item in stored_sources],
            "assets": [item.fingerprint_payload() for item in stored_assets],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return UploadWorkspace(
            presentation_path=presentation_path,
            source_material_paths=source_paths,
            asset_paths=asset_paths,
            presentation_sha256=stored_presentation.sha256,
            presentation_original_name=stored_presentation.original_name,
            source_material_hashes=tuple(item.sha256 for item in stored_sources),
            asset_hashes=tuple(item.sha256 for item in stored_assets),
            total_size_bytes=stored_presentation.size_bytes + attachment_bytes,
            fingerprint=fingerprint,
            directory=workspace_dir,
            work_root=self.work_root,
        )

    def _receive(
        self,
        upload: UploadLike,
        *,
        destination: Path,
        original_name: str,
        maximum_bytes: int,
        validate_pptx: bool = False,
    ) -> StoredUpload:
        incoming = destination.parent / f".{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            try:
                upload.file.seek(0)
            except (AttributeError, OSError):
                pass
            with incoming.open("xb") as output_stream:
                while True:
                    block = upload.file.read(_CHUNK_BYTES)
                    if not block:
                        break
                    if not isinstance(block, bytes):
                        raise UploadValidationError("uploaded file stream did not return bytes")
                    size += len(block)
                    if size > maximum_bytes:
                        raise UploadTooLargeError("uploaded file exceeds the byte limit")
                    digest.update(block)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            if size == 0:
                raise UploadValidationError("uploaded files must not be empty")
            if validate_pptx:
                preflight = PptxAdapter(limits=self.pptx_limits).preflight(incoming)
                if not preflight.is_safe:
                    codes = ",".join(
                        sorted({item.code for item in preflight.blocking_findings})
                    )
                    raise UploadValidationError(
                        f"presentation failed OOXML security preflight ({codes})"
                    )
            sha256 = digest.hexdigest()
            os.replace(incoming, destination)
            return StoredUpload(
                original_name=original_name,
                suffix=Path(original_name).suffix.casefold(),
                sha256=sha256,
                size_bytes=size,
                staged_path=destination,
            )
        except (UploadValidationError, UploadTooLargeError):
            raise
        except OSError as exc:
            raise UploadStorageError("upload storage is unavailable") from exc
        finally:
            try:
                incoming.unlink(missing_ok=True)
            except OSError:
                pass


def _validated_distinct_filenames(
    uploads: Sequence[UploadLike],
    *,
    allowed_suffixes: frozenset[str],
    label: str,
) -> tuple[str, ...]:
    names = tuple(
        _validated_filename(
            upload.filename,
            allowed_suffixes=allowed_suffixes,
            label=label,
        )
        for upload in uploads
    )
    if len(names) != len({name.casefold() for name in names}):
        raise UploadValidationError(f"duplicate {label} filenames are not allowed")
    return names


def _validated_filename(
    value: object,
    *,
    allowed_suffixes: frozenset[str],
    label: str,
) -> str:
    if not isinstance(value, str):
        raise UploadValidationError(f"{label} filename is required")
    name = unicodedata.normalize("NFC", value).strip()
    if (
        not name
        or len(name) > 120
        or len(name.encode("utf-8")) > 240
        or _CONTROL_CHARACTER.search(name)
        or _INVALID_WINDOWS_FILENAME.search(name)
        or name in {".", ".."}
        or name.endswith((" ", "."))
        or Path(name).name != name
        or PureWindowsPath(name).name != name
        or "/" in name
        or "\\" in name
    ):
        raise UploadValidationError(f"{label} filename is unsafe")
    if Path(name).stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise UploadValidationError(f"{label} filename is unsafe")
    suffix = Path(name).suffix.casefold()
    if suffix not in allowed_suffixes:
        raise UploadValidationError(f"{label} has an unsupported file extension")
    return name


def _validate_presentation_media_type(value: object) -> None:
    media_type = str(value or "").split(";", 1)[0].strip().casefold()
    if media_type not in _PRESENTATION_MEDIA_TYPES:
        raise UploadValidationError("presentation has an unsupported media type")


__all__ = [
    "LocalUploadStore",
    "MAX_ASSETS",
    "MAX_ATTACHMENT_TOTAL_BYTES",
    "MAX_ATTACHMENT_UPLOAD_BYTES",
    "MAX_PRESENTATION_UPLOAD_BYTES",
    "MAX_SOURCE_MATERIALS",
    "PRESENTATION_MEDIA_TYPE",
    "UploadStorageError",
    "UploadTooLargeError",
    "UploadValidationError",
    "UploadWorkspace",
]
