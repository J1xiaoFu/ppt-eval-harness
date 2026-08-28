from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from types import ModuleType
from unittest import SkipTest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _tracked_paths() -> tuple[Path, ...]:
    if not (ROOT / ".git").exists():
        raise SkipTest("repository metadata is unavailable in the runtime image")
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return tuple(
        ROOT / value.decode("utf-8")
        for value in completed.stdout.split(b"\0")
        if value
    )


def _tracked_text() -> tuple[tuple[Path, str], ...]:
    items: list[tuple[Path, str]] = []
    for path in _tracked_paths():
        if not path.is_file():
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        items.append((path.relative_to(ROOT), content.decode("utf-8", errors="ignore")))
    return tuple(items)


def test_tracked_text_contains_no_host_specific_repository_paths() -> None:
    # Build sensitive markers from fragments so this regression test does not
    # trigger itself once it becomes tracked. Generic protocol examples such
    # as C:\path, Windows system defaults, attack fixtures, vendored /141nfs,
    # and upstream /tmp remain intentionally allowed.
    forbidden_literals = (
        "Diego" + "Wang",
        "DIego" + " Wang",
        "Campus" + " study",
        "Campus" + "%20study",
        "AppData" + "\\Local\\Temp",
        "AppData" + "/Local/Temp",
    )
    forbidden_patterns = (
        re.compile(r"C:[\\/]Users[\\/]", re.IGNORECASE),
        re.compile(r"(?<![A-Za-z])D:[\\/]", re.IGNORECASE),
        re.compile(r"file:///(?:[A-Za-z]:|Users/|home/)", re.IGNORECASE),
        re.compile(r"/(?:Users|home)/[^/\s]+/"),
    )
    violations: list[str] = []
    for path, text in _tracked_text():
        for marker in forbidden_literals:
            if marker in text:
                violations.append(f"{path.as_posix()}: contains {marker!r}")
        for pattern in forbidden_patterns:
            if pattern.search(text):
                violations.append(
                    f"{path.as_posix()}: matches host-path pattern {pattern.pattern!r}"
                )
    assert not violations, "\n".join(violations)


def test_repository_tracks_no_cache_build_or_runtime_output() -> None:
    forbidden_components = {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "temp",
        "tmp",
        "var",
    }
    forbidden_suffixes = {".bak", ".pyc", ".pyo", ".tmp", ".tsbuildinfo"}
    violations = [
        path.relative_to(ROOT).as_posix()
        for path in _tracked_paths()
        if forbidden_components.intersection(path.relative_to(ROOT).parts)
        or path.suffix.casefold() in forbidden_suffixes
    ]
    assert not violations, "tracked generated/runtime paths:\n" + "\n".join(violations)


def _load_ppteval_runner() -> ModuleType:
    path = ROOT / "third_party" / "ppteval" / "run_ppteval.py"
    spec = importlib.util.spec_from_file_location("path_hygiene_ppteval_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load PPTEval runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ppteval_evidence_generator_emits_portable_paths(tmp_path: Path) -> None:
    runner = _load_ppteval_runner()
    inside = ROOT / "third_party" / "ppteval" / "run_ppteval.py"
    outside = tmp_path / "external-deck.pptx"

    assert runner.portable_path(inside) == "third_party/ppteval/run_ppteval.py"
    assert runner.portable_path(outside) == "<external>/external-deck.pptx"

    license_audit = (
        ROOT / "third_party" / "ppteval" / "audit_zenodo_licenses.py"
    ).read_text(encoding="utf-8")
    assert '"manifest": portable_path(args.manifest)' in license_audit

    smoke = (ROOT / "third_party" / "slidesbench" / "run_smoke.ps1").read_text(
        encoding="utf-8"
    )
    assert '.Replace($repoRoot, "<repo>").Replace(' in smoke
    assert '"<python-env>"' in smoke
    assert ').Replace("\\", "/")' in smoke
    assert "$exitCode = $LASTEXITCODE" in smoke


def test_ppteval_runner_rejects_non_text_model_content() -> None:
    runner = _load_ppteval_runner()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":{"score":5}}}]}'

    with patch.object(runner.urllib.request, "urlopen", return_value=Response()):
        try:
            runner.chat_completion(
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="test-model",
                prompt="test prompt",
            )
        except TypeError as exc:
            assert "content must be a string" in str(exc)
        else:
            raise AssertionError("non-text model content must be rejected")
