"""Fail-closed local-file boundary for outbound model-audit evidence.

``EvalCase.source_materials`` is intentionally flexible: an item may be inline
text or a local filename.  That flexibility is useful to deterministic local
Oracles, but an API caller must not be able to turn the model audit into an
arbitrary-file exfiltration primitive.  This module is used only by outbound
LLM/VLM audits and therefore does not change local deterministic evaluation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

_FILE_LIKE_SUFFIXES = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".json",
        ".md",
        ".pdf",
        ".ppt",
        ".pptx",
        ".text",
        ".tsv",
        ".txt",
        ".xls",
        ".xlsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_SENSITIVE_PARTS = frozenset(
    {
        ".aws",
        ".azure",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
        "api",
        "secrets",
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        "authorized_keys",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "qwen3.7_flash_api.txt",
        "secrets.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"}
)
_SENSITIVE_NAME_MARKERS = (
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "secret",
)


@dataclass(frozen=True, slots=True)
class PreparedModelSources:
    """Bounded source text plus opaque references safe for a remote model."""

    text: str
    source_uris: tuple[str, ...]
    inline_count: int
    file_count: int
    blocked_count: int


@dataclass(frozen=True, slots=True)
class ModelSourceAccessPolicy:
    """Authorize local source files beneath explicitly configured roots.

    With no roots (the default), ordinary inline text remains usable while
    every file-like value is rejected.  Authorized filenames are replaced by
    opaque source URIs before a request is handed to a provider, so a remote
    endpoint never receives a local absolute path.
    """

    allowed_roots: tuple[Path, ...] = ()
    denied_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_roots",
            _normalized_paths(self.allowed_roots, label="model source root"),
        )
        object.__setattr__(
            self,
            "denied_paths",
            _normalized_paths(self.denied_paths, label="denied model source path"),
        )

    def prepare(
        self,
        materials: Sequence[str],
        *,
        maximum_bytes: int,
    ) -> PreparedModelSources:
        """Resolve permitted files and preserve inline text without path leaks."""

        if isinstance(maximum_bytes, bool) or maximum_bytes < 1:
            raise ValueError("maximum_bytes must be a positive integer")

        chunks: list[str] = []
        source_uris: list[str] = []
        remaining = maximum_bytes
        inline_count = 0
        file_count = 0
        blocked_count = 0

        for source_index, raw_value in enumerate(materials, start=1):
            value = str(raw_value)
            if not value.strip():
                continue

            resolution = self._resolve_file(value)
            if resolution.blocked:
                blocked_count += 1
                continue

            if resolution.path is not None and resolution.root_index is not None:
                try:
                    data = _read_regular_file(resolution.path, remaining)
                except OSError:
                    blocked_count += 1
                    continue
                uri = _opaque_file_uri(
                    resolution.path,
                    self.allowed_roots[resolution.root_index],
                    source_index=source_index,
                )
                file_count += 1
                text = data.decode("utf-8", errors="replace")
            else:
                if remaining <= 0:
                    continue
                uri = f"source:inline:{source_index}"
                inline_count += 1
                text = value

            encoded = text.encode("utf-8")[:remaining]
            bounded = encoded.decode("utf-8", errors="ignore")
            chunks.append(f"[{uri}]\n{bounded}")
            source_uris.append(uri)
            remaining -= len(encoded)

        return PreparedModelSources(
            text="\n".join(chunks).strip(),
            source_uris=tuple(source_uris),
            inline_count=inline_count,
            file_count=file_count,
            blocked_count=blocked_count,
        )

    def _resolve_file(self, value: str) -> "_FileResolution":
        if _is_file_uri(value) or value.lstrip().startswith("~"):
            return _FileResolution(blocked=True)

        try:
            path = Path(value)
        except (OSError, ValueError):
            return _FileResolution(blocked=_looks_like_local_file(value))

        candidates: list[Path] = []
        if path.is_absolute():
            candidates.append(path)
        else:
            # A bare ``facts.txt`` is naturally relative to an allowed source
            # root.  A workspace-relative ``datasets/facts.txt`` continues to
            # work when the current working directory is the project root.
            candidates.extend(root / path for root in self.allowed_roots)
            candidates.append(Path.cwd() / path)

        existing_local_path = False
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            marker = os.path.normcase(str(resolved))
            if marker in seen:
                continue
            seen.add(marker)
            existing_local_path = True
            if not resolved.is_file() or self._is_denied(resolved):
                continue
            for root_index, root in enumerate(self.allowed_roots):
                if resolved.is_relative_to(root):
                    return _FileResolution(path=resolved, root_index=root_index)

        if existing_local_path or _looks_like_local_file(value):
            return _FileResolution(blocked=True)
        return _FileResolution()

    def _is_denied(self, path: Path) -> bool:
        folded_parts = tuple(part.casefold() for part in path.parts)
        name = path.name.casefold()
        if any(part in _SENSITIVE_PARTS for part in folded_parts):
            return True
        if name == ".env" or name.startswith(".env."):
            return True
        if name in _SENSITIVE_FILENAMES or path.suffix.casefold() in _SENSITIVE_SUFFIXES:
            return True
        normalized_name = name.replace("-", "_").replace(".", "_")
        if any(marker in normalized_name for marker in _SENSITIVE_NAME_MARKERS):
            return True
        if _is_protected_system_path(path):
            return True
        return any(path == denied or path.is_relative_to(denied) for denied in self.denied_paths)


@dataclass(frozen=True, slots=True)
class _FileResolution:
    path: Path | None = None
    root_index: int | None = None
    blocked: bool = False


def sanitize_declared_uris(
    values: Sequence[str],
    *,
    label: str,
) -> tuple[str, ...]:
    """Remove local absolute/traversal paths from non-file scenario metadata."""

    sanitized: list[str] = []
    for index, raw_value in enumerate(values, start=1):
        value = str(raw_value).strip()
        if not value:
            continue
        if _unsafe_declared_uri(value):
            sanitized.append(f"{label}:local:{index}")
        else:
            sanitized.append(value)
    return tuple(sanitized)


def _normalized_paths(values: Sequence[Path], *, label: str) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            path = Path(value).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"{label} is invalid") from exc
        marker = os.path.normcase(str(path))
        if marker not in seen:
            normalized.append(path)
            seen.add(marker)
    return tuple(normalized)


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    if maximum_bytes <= 0:
        return b""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("model source is not a regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _opaque_file_uri(path: Path, root: Path, *, source_index: int) -> str:
    relative = path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return f"source:file:{source_index}:{digest}"


def _looks_like_local_file(value: str) -> bool:
    stripped = value.strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return False
    parsed = urlsplit(stripped)
    if parsed.scheme in {"http", "https", "urn"}:
        return False
    if parsed.scheme == "file" or stripped.startswith(("~", "\\\\")):
        return True
    try:
        path = Path(stripped)
        if path.is_absolute():
            return True
        if ".." in path.parts:
            return True
        if ("/" in stripped or "\\" in stripped) and not any(
            character.isspace() for character in stripped
        ):
            return True
        return (
            not any(character.isspace() for character in stripped)
            and path.suffix.casefold() in _FILE_LIKE_SUFFIXES
        )
    except (OSError, ValueError):
        return True


def _is_file_uri(value: str) -> bool:
    try:
        return urlsplit(value.strip()).scheme.casefold() == "file"
    except ValueError:
        return True


def _unsafe_declared_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.username or parsed.password)
    if parsed.scheme == "file" or value.startswith(("~", "\\\\")):
        return True
    try:
        path = Path(value)
        return path.is_absolute() or ".." in path.parts
    except (OSError, ValueError):
        return True


def _is_protected_system_path(path: Path) -> bool:
    if os.name == "nt":
        return False
    protected = (
        Path("/boot"),
        Path("/dev"),
        Path("/etc"),
        Path("/proc"),
        Path("/root"),
        Path("/sys"),
        Path("/run/secrets"),
        Path("/var/run/secrets"),
    )
    return any(path == root or path.is_relative_to(root) for root in protected)


__all__ = [
    "ModelSourceAccessPolicy",
    "PreparedModelSources",
    "sanitize_declared_uris",
]
