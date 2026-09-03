from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tomllib
from contextlib import redirect_stdout
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

from ppt_eval import __version__
from ppt_eval.application import TRIAGE_POLICY_VERSION
from ppt_eval.cli import build_parser
from ppt_eval.config import default_profile
from ppt_eval.domain import SceneType
from ppt_eval.domain.models import SCHEMA_VERSION
from ppt_eval.oracles.model_audits import (
    V8_AUTHORSHIP_VLM_ORACLE_VERSION,
    V8_GROUNDED_VLM_ORACLE_VERSION,
    V8_RASTER_TEXT_VLM_ORACLE_VERSION,
)
from ppt_eval.oracles.v8_atomic import V8_ATOMIC_VERSION
from ppt_eval.oracles.v8_composites import V8_QUALITY_VERSION
from ppt_eval.runtime import LocalEvaluationRuntime
from scripts.release_version import check_repository, sync_product_surfaces
from tests.fixtures.api_client import make_test_client

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_VERSION = "0.8.7"


def test_product_release_metadata_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ui_package = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    openapi = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    info = openapi.split("paths:", 1)[0]

    assert __version__ == PRODUCT_VERSION
    assert distribution_version("ppt-eval-harness") == PRODUCT_VERSION
    assert project["project"]["version"] == PRODUCT_VERSION
    assert ui_package["version"] == PRODUCT_VERSION
    assert re.search(rf"(?m)^  version: {re.escape(PRODUCT_VERSION)}$", info)


def test_machine_readable_version_matrix_matches_release_contracts() -> None:
    matrix = json.loads(
        (ROOT / "release" / "version-matrix.json").read_text(encoding="utf-8")
    )

    assert matrix["schema_version"] == "1.0"
    assert matrix["product"]["version"] == PRODUCT_VERSION
    assert matrix["evaluation"] == {
        "default_profile_version": "8.3",
        "profile_lifecycle": "PRE_RESEARCH",
        "composite_version": "8.3.0",
        "atomic_observation_version": "2.1.0",
        "grounded_vlm_versions": {
            "visual": "2.0.0",
            "authorship": "2.1.0",
            "raster_text": "1.0.0",
        },
        "selection_policy_version": "2.0.0",
    }
    assert matrix["contracts"] == {
        "eval_report_schema_version": "1.0",
        "audit_schema_version": "1.0",
        "model_audit_schema_version": "1.0",
        "http_api_namespace": "/v1",
        "attention_policy_version": "audit-attention@0.8.6",
    }
    assert matrix["infrastructure"] == {
        "render_manifest_schema_version": "2.0",
        "visual_asset_transport_version": "1.0.0",
        "qwen_context_cache_wire_version": "1.0.0",
    }


def test_release_version_check_reports_no_drift() -> None:
    assert check_repository(ROOT) == []
    completed = subprocess.run(
        [sys.executable, "scripts/release_version.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "product 0.8.7" in completed.stdout


def test_release_writer_changes_only_product_surfaces(tmp_path: Path) -> None:
    relative_paths = (
        "pyproject.toml",
        "src/ppt_eval/version.py",
        "tests/test_release_version.py",
        "ui/package.json",
        "docs/openapi.yaml",
        "README.md",
        "docs/review_platform.md",
        "audit/README.md",
        "ui/src/demo.ts",
    )
    for relative in relative_paths:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    sync_product_surfaces(tmp_path, "9.8.7")

    project = tomllib.loads(
        (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["project"]["version"] == "9.8.7"
    python_version = (tmp_path / "src/ppt_eval/version.py").read_text(
        encoding="utf-8"
    )
    assert '__version__ = "9.8.7"' in python_version
    release_test = (tmp_path / "tests/test_release_version.py").read_text(
        encoding="utf-8"
    )
    assert 'PRODUCT_VERSION = "9.8.7"' in release_test
    assert _read_ui_version(tmp_path) == "9.8.7"
    demo = (tmp_path / "ui/src/demo.ts").read_text(encoding="utf-8")
    assert 'service_version: "9.8.7"' in demo
    assert 'triage_policy_version: "audit-attention@0.8.6"' in demo


def _read_ui_version(root: Path) -> str:
    return json.loads((root / "ui/package.json").read_text(encoding="utf-8"))["version"]


def test_cli_reports_product_release_without_constructing_runtime() -> None:
    output = io.StringIO()
    with redirect_stdout(output), pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])

    assert raised.value.code == 0
    assert output.getvalue().strip() == f"ppt-eval {PRODUCT_VERSION}"


def test_product_bump_does_not_change_evaluation_contract_versions() -> None:
    assert TRIAGE_POLICY_VERSION == "audit-attention@0.8.6"
    assert SCHEMA_VERSION == "1.0"
    assert V8_QUALITY_VERSION == "8.3.0"
    assert V8_ATOMIC_VERSION == "2.1.0"
    assert V8_GROUNDED_VLM_ORACLE_VERSION == "2.0.0"
    assert V8_AUTHORSHIP_VLM_ORACLE_VERSION == "2.1.0"
    assert V8_RASTER_TEXT_VLM_ORACLE_VERSION == "1.0.0"
    for scene in SceneType:
        profile = default_profile(scene)
        assert profile.version == "8.3"
        assert profile.metadata["lifecycle"] == "PRE_RESEARCH"


def test_api_release_surface_and_legacy_run_read_compatibility(tmp_path: Path) -> None:
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
    client = make_test_client(lambda: create_app(runtime))

    assert client.app.version == PRODUCT_VERSION
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["service_version"] == PRODUCT_VERSION
    response = client.get("/v1/evaluations/run-legacy-release-version")
    assert response.status_code == 200
    assert response.json()["service_version"] == "0.8.3"
    assert response.json()["profile_version"] == "8.3"
    assert response.json()["schema_version"] == "1.0"
