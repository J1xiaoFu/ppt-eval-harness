from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from examples import generate_demo as demo_generator
from ppt_eval.config import load_case
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.pptx_factory import PNG_1X1

ROOT = Path(__file__).resolve().parents[1]
TRACKED_DEMO = ROOT / "examples" / "demo"


def _tracked_demo_hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(TRACKED_DEMO.iterdir())
        if path.is_file()
    }


def _unique_var_output(label: str) -> Path:
    return demo_generator.VAR_ROOT / f"{label}-{uuid.uuid4().hex}"


def _remove_test_output(output: Path) -> None:
    resolved = output.resolve()
    allowed_root = demo_generator.VAR_ROOT.resolve()
    if resolved.parent != allowed_root or "-test-" not in resolved.name:
        raise AssertionError("refusing to remove an unexpected demo test directory")
    if resolved.exists():
        shutil.rmtree(resolved)


def test_tracked_demo_cases_load_and_run_from_manifest_relative_paths(
    tmp_path: Path,
) -> None:
    runtime = LocalEvaluationRuntime(tmp_path / "runtime")
    manifests = sorted(TRACKED_DEMO.glob("case_*.json"))
    assert len(manifests) == 4
    for manifest in manifests:
        case = load_case(manifest)
        assert Path(case.pptx_path).is_file()
        assert Path(case.pptx_path).parent == TRACKED_DEMO.resolve()
        report = runtime.evaluate(case)
        assert report["case_id"] == case.case_id
        assert report["run_id"]
    project = load_case(TRACKED_DEMO / "case_project_summary.json")
    assert len(project.source_materials) == 1
    assert Path(project.source_materials[0]).is_file()


def test_load_case_resolves_only_explicit_attachment_paths(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "portable"
    manifest_dir.mkdir()
    (manifest_dir / "source.txt").write_text("file-backed source", encoding="utf-8")
    (manifest_dir / "asset.png").write_bytes(PNG_1X1)
    manifest = manifest_dir / "case.json"
    manifest.write_text(
        json.dumps(
            {
                "case_id": "relative-inputs",
                "scene": "project_summary",
                "pptx_path": "./deck.pptx",
                "source_materials": [
                    "This sentence is inline source evidence.",
                    "./source.txt",
                    "https://example.com/source.txt",
                ],
                "assets": ["./asset.png"],
            }
        ),
        encoding="utf-8",
    )

    case = load_case(manifest)

    assert case.pptx_path == str((manifest_dir / "deck.pptx").resolve())
    assert case.source_materials[0] == "This sentence is inline source evidence."
    assert case.source_materials[1] == str((manifest_dir / "source.txt").resolve())
    assert case.source_materials[2] == "https://example.com/source.txt"
    assert case.assets == (str((manifest_dir / "asset.png").resolve()),)


def test_generator_output_dir_writes_portable_manifests(tmp_path: Path) -> None:
    del tmp_path
    output = _unique_var_output("demo-cli-test")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "examples" / "generate_demo.py"),
                "--output-dir",
                str(output.relative_to(ROOT)),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        assert Path(completed.stdout.strip()) == output.resolve() / "aurora_demo.pptx"
        manifests = sorted(output.glob("case_*.json"))
        assert len(manifests) == 4
        for manifest in manifests:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            assert payload["pptx_path"] == "./aurora_demo.pptx"
            assert not Path(payload["pptx_path"]).is_absolute()
            assert Path(load_case(manifest).pptx_path).is_file()
        project = json.loads(
            (output / "case_project_summary.json").read_text(encoding="utf-8")
        )
        assert project["source_materials"] == ["./source.txt"]
    finally:
        _remove_test_output(output)
    assert not output.exists()


def test_generator_default_never_modifies_tracked_demo() -> None:
    before = _tracked_demo_hashes()
    output = _unique_var_output("demo-default-test")
    original_default = demo_generator.DEFAULT_OUTPUT_DIR
    demo_generator.DEFAULT_OUTPUT_DIR = output
    try:
        deck_path = demo_generator.generate()
        assert deck_path == output.resolve() / "aurora_demo.pptx"
        assert deck_path.is_file()
        assert _tracked_demo_hashes() == before
    finally:
        demo_generator.DEFAULT_OUTPUT_DIR = original_default
        _remove_test_output(output)
    assert not output.exists()


def test_generator_rejects_output_outside_repository_var(tmp_path: Path) -> None:
    rejected = (
        tmp_path / "absolute-outside",
        Path("../demo-escape"),
        Path("examples/demo-generated"),
        demo_generator.VAR_ROOT,
    )
    for value in rejected:
        try:
            demo_generator.generate(value)
        except ValueError as exc:
            assert "repository var directory" in str(exc)
        else:
            raise AssertionError(f"outside output {value!s} should be rejected")
    assert not (tmp_path / "absolute-outside").exists()

    allowed = _unique_var_output("demo-absolute-test")
    try:
        assert demo_generator.generate(allowed).parent == allowed.resolve()
    finally:
        _remove_test_output(allowed)
    assert not allowed.exists()
