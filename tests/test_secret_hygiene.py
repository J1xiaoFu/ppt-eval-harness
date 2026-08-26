from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _patterns(name: str) -> set[str]:
    return {
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_git_and_docker_context_exclude_local_model_credentials() -> None:
    git_patterns = _patterns(".gitignore")
    docker_patterns = _patterns(".dockerignore")

    assert "api/" in git_patterns
    assert ".env" in git_patterns
    assert ".env.*" in git_patterns
    assert "!.env.example" in git_patterns

    assert "api" in docker_patterns
    assert ".env" in docker_patterns
    assert ".env.*" in docker_patterns
    assert "!.env.example" in docker_patterns
