from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
PINNED_FROM = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}(?:\s+AS\s+\S+)?$", re.I)


def test_docker_build_inputs_are_immutably_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert len(from_lines) == 2
    assert all(PINNED_FROM.fullmatch(line) for line in from_lines)
    assert "--constraint constraints/docker-py311-linux.txt" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert "python -m pip check" in dockerfile


def test_node_manifest_and_lockfile_use_exact_versions() -> None:
    package = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    lock = yaml.safe_load((ROOT / "ui" / "pnpm-lock.yaml").read_text(encoding="utf-8"))

    assert package["packageManager"] == "pnpm@11.24.0"
    declared = {**package["dependencies"], **package["devDependencies"]}
    assert declared
    assert all(EXACT_VERSION.fullmatch(version) for version in declared.values())
    importer = lock["importers"]["."]
    locked_specifiers = {
        name: value["specifier"]
        for section in ("dependencies", "devDependencies")
        for name, value in importer[section].items()
    }
    assert locked_specifiers == declared


def test_docker_python_constraints_are_complete_exact_pins() -> None:
    path = ROOT / "constraints" / "docker-py311-linux.txt"
    requirements = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", requirement)
        for requirement in requirements
    )
    pins = {
        name.casefold().replace("_", "-"): version
        for name, version in (
            requirement.split("==", 1) for requirement in requirements
        )
    }
    assert pins == {
        "annotated-doc": "0.0.5",
        "annotated-types": "0.8.0",
        "anyio": "4.14.2",
        "click": "8.5.0",
        "fastapi": "0.141.1",
        "h11": "0.16.0",
        "httptools": "0.8.0",
        "idna": "3.19",
        "lxml": "6.1.2",
        "packaging": "26.3",
        "pillow": "12.3.0",
        "pydantic": "2.13.5",
        "pydantic-core": "2.46.5",
        "python-dotenv": "1.2.3",
        "python-multipart": "0.0.32",
        "python-pptx": "1.0.2",
        "pyyaml": "6.0.3",
        "starlette": "1.6.0",
        "typing-extensions": "4.16.0",
        "typing-inspection": "0.4.4",
        "uvicorn": "0.52.4",
        "uvloop": "0.22.1",
        "watchfiles": "1.2.0",
        "websockets": "17.1",
        "xlsxwriter": "3.2.9",
    }
