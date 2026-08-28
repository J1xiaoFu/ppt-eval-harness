from __future__ import annotations

import io
import json
import re
import tomllib
from contextlib import redirect_stdout
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

from ppt_eval import __version__
from ppt_eval.cli import build_parser
from ppt_eval.config import default_profile
from ppt_eval.domain import SceneType
from ppt_eval.domain.models import SCHEMA_VERSION
from ppt_eval.oracles.v8_composites import V8_QUALITY_VERSION
from ppt_eval.runtime import LocalEvaluationRuntime

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_VERSION = "0.8.4"


def test_product_release_metadata_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ui_package = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    openapi = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    info = openapi.split("paths:", 1)[0]

    assert __version__ == PRODUCT_VERSION
    assert distribution_version("ppt-eval-harness") == PRODUCT_VERSION
    assert project["project"]["version"] == PRODUCT_VERSION
    assert ui_package["version"] == PRODUCT_VERSION
    assert re.search(r"(?m)^  version: 0\.8\.4$", info)


def test_cli_reports_product_release_without_constructing_runtime() -> None:
    output = io.StringIO()
    with redirect_stdout(output), pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])

    assert raised.value.code == 0
    assert output.getvalue().strip() == f"ppt-eval {PRODUCT_VERSION}"


def test_product_bump_does_not_change_evaluation_contract_versions() -> None:
    assert SCHEMA_VERSION == "1.0"
    assert V8_QUALITY_VERSION == "8.3.0"
    for scene in SceneType:
        profile = default_profile(scene)
        assert profile.version == "8.3"
        assert profile.metadata["lifecycle"] == "PRE_RESEARCH"


def test_api_release_surface_and_legacy_run_read_compatibility(tmp_path: Path) -> None:
    try:
        import python_multipart
        from fastapi.testclient import TestClient
    except ImportError:
        return
    del python_multipart
    from ppt_eval.api import create_app

    runtime = LocalEvaluationRuntime(tmp_path / "var")
    legacy = {
        "run_id": "run-legacy-release-version",
        "case_id": "legacy-profile-83",
        "profile_id": "finished-deck-v8",
        "profile_version": "8.3",
        "scenario": "ready_made",
        "coverage": "DEGRADED",
        "decision": "REVIEW",
        "schema_version": "1.0",
        "manifest": {
            "run_id": "run-legacy-release-version",
            "profile_id": "finished-deck-v8",
            "profile_version": "8.3",
            "schema_version": "1.0",
        },
        "results": [],
    }
    runtime.repository.save(legacy)
    client = TestClient(create_app(runtime))

    assert client.app.version == PRODUCT_VERSION
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["service_version"] == PRODUCT_VERSION
    response = client.get("/v1/evaluations/run-legacy-release-version")
    assert response.status_code == 200
    assert response.json()["service_version"] == "0.8.3"
    assert response.json()["profile_version"] == "8.3"
    assert response.json()["schema_version"] == "1.0"
