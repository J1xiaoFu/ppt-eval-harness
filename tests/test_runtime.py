from __future__ import annotations

import hmac
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ppt_eval.adapters import RenderResult
from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure import LocalArtifactStore, sha256_file
from ppt_eval.infrastructure.local import (
    _font_directory_fingerprint,
    _fontconfig_fingerprint,
)
from ppt_eval.runtime import (
    LocalEvaluationRuntime,
    RenderSourceChangedError,
    _load_render_cache,
)
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx


class CountingRenderer:
    renderer_id = "counting-renderer"
    version = "1.2.3"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        del pptx_path
        self.calls += 1
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        image = output / "Slide1.PNG"
        image.write_bytes(PNG_1X1)
        return RenderResult(self.renderer_id, self.version, (image,))


class MutatingRenderer(CountingRenderer):
    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        result = super().render(pptx_path, output_dir)
        with Path(pptx_path).open("ab") as handle:
            handle.write(b"source-changed-during-render")
        return result


def test_local_artifact_store_uses_short_atomic_temp_name(tmp_path) -> None:
    written_names: list[str] = []
    original_write_bytes = Path.write_bytes

    def tracked_write_bytes(path: Path, data: bytes) -> int:
        written_names.append(path.name)
        return original_write_bytes(path, data)

    with patch.object(Path, "write_bytes", tracked_write_bytes):
        artifact = LocalArtifactStore(tmp_path / ("nested-" * 12)).put_bytes(b"observation")

    assert Path(artifact["uri"]).read_bytes() == b"observation"
    assert len(written_names) == 1
    assert written_names[0].startswith(".")
    assert written_names[0].endswith(".tmp")
    assert len(written_names[0]) <= 40


def test_local_runtime_persists_report_manifest_and_valid_audit_chain(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck)),
        default_profile(SceneType.READY_MADE),
    )

    assert report["coverage"] == "DEGRADED"
    assert report["decision"] == "REVIEW"
    assert report["base_score"] is not None
    assert report["manifest"]["input_hash"]
    assert runtime.get(report["run_id"])["case_id"] == "ready"
    assert runtime.audit_log.verify() == (True, None)


def test_render_cache_is_stable_across_case_ids_and_source_paths(tmp_path: Path) -> None:
    (tmp_path / "first").mkdir()
    first_deck = build_pptx(tmp_path / "first" / "deck.pptx")
    second_deck = tmp_path / "second" / "renamed.pptx"
    second_deck.parent.mkdir(parents=True)
    second_deck.write_bytes(first_deck.read_bytes())
    renderer = CountingRenderer()
    font_digest = "f" * 64
    with patch("ppt_eval.runtime.font_fingerprint", return_value=font_digest):
        runtime = LocalEvaluationRuntime(
            tmp_path / "var",
            slide_renderer=renderer,
            review_rendering=True,
        )

    first = runtime.evaluate(
        EvalCase(
            case_id="first-case",
            scene=SceneType.READY_MADE,
            pptx_path=str(first_deck),
        )
    )
    second = runtime.evaluate(
        EvalCase(
            case_id="different-case",
            scene=SceneType.READY_MADE,
            pptx_path=str(second_deck),
        )
    )

    assert renderer.calls == 1
    assert first["manifest"]["input_hash"] != second["manifest"]["input_hash"]
    cache_key = first["render_artifact"]["cache_key"]
    assert cache_key == second["render_artifact"]["cache_key"]
    assert cache_key not in {
        first["manifest"]["input_hash"],
        second["manifest"]["input_hash"],
    }
    assert runtime.review_slide_paths(first["run_id"])[0].read_bytes() == PNG_1X1
    assert runtime.review_slide_paths(second["run_id"])[0].read_bytes() == PNG_1X1

    manifest = json.loads(
        (runtime.paths.render_cache / cache_key / "render-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == "2.0"
    assert manifest["cache_key"] == cache_key
    assert manifest["source_sha256"] == sha256_file(first_deck)
    assert manifest["renderer_id"] == renderer.renderer_id
    assert manifest["renderer_version"] == renderer.version
    assert manifest["font_fingerprint"] == font_digest
    assert manifest["render_policy"] == {
        "image_format": "png",
        "output_size": "renderer-default",
        "page_selection": "all",
        "policy_id": "native-full-slide",
        "version": "1.0",
    }


def test_render_cache_rejects_source_changed_during_render(tmp_path: Path) -> None:
    deck = build_pptx(tmp_path / "changing.pptx")
    renderer = MutatingRenderer()
    with patch("ppt_eval.runtime.font_fingerprint", return_value="f" * 64):
        runtime = LocalEvaluationRuntime(
            tmp_path / "var",
            slide_renderer=renderer,
            review_rendering=True,
        )

    with patch(
        "ppt_eval.runtime.hmac.compare_digest",
        wraps=hmac.compare_digest,
    ) as constant_time_compare:
        with pytest.raises(
            RenderSourceChangedError,
            match="source presentation changed.*refusing to persist render cache",
        ):
            runtime.evaluate(
                EvalCase(
                    case_id="changing",
                    scene=SceneType.READY_MADE,
                    pptx_path=str(deck),
                )
            )

    assert renderer.calls == 1
    assert constant_time_compare.call_count == 1
    assert runtime.paths.render_cache.is_dir()
    assert tuple(runtime.paths.render_cache.iterdir()) == ()
    assert runtime.repository.list() == []


def test_unobservable_fonts_disable_cross_evaluation_render_cache(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    renderer = CountingRenderer()
    with patch("ppt_eval.runtime.font_fingerprint", return_value="unavailable"):
        runtime = LocalEvaluationRuntime(
            tmp_path / "var",
            slide_renderer=renderer,
            review_rendering=True,
        )

    first = runtime.evaluate(
        EvalCase(case_id="first", scene=SceneType.READY_MADE, pptx_path=str(deck))
    )
    second = runtime.evaluate(
        EvalCase(case_id="second", scene=SceneType.READY_MADE, pptx_path=str(deck))
    )

    assert renderer.calls == 2
    assert first["render_artifact"]["cache_key"] != second["render_artifact"]["cache_key"]
    manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in runtime.paths.render_cache.glob("*/render-manifest.json")
    ]
    assert len(manifests) == 2
    assert all(
        item["font_fingerprint"].startswith("unavailable-run:")
        for item in manifests
    )


def test_fontconfig_fingerprint_is_stable_across_inventory_order() -> None:
    first = SimpleNamespace(
        returncode=0,
        stdout=(
            "/usr/share/fonts/b.ttf|Family B|Regular|2\n"
            "/usr/share/fonts/a.ttf|Family A|Bold|1\n"
        ),
    )
    second = SimpleNamespace(
        returncode=0,
        stdout=(
            "/usr/share/fonts/a.ttf|Family A|Bold|1\n"
            "/usr/share/fonts/b.ttf|Family B|Regular|2\n"
        ),
    )

    with patch(
        "ppt_eval.infrastructure.local.subprocess.run",
        side_effect=(first, second),
    ) as run:
        first_digest = _fontconfig_fingerprint("/usr/bin/fc-list")
        second_digest = _fontconfig_fingerprint("/usr/bin/fc-list")

    assert first_digest == second_digest
    assert first_digest is not None
    assert len(first_digest) == 64
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == [
        "/usr/bin/fc-list",
        "--format=%{file}|%{family}|%{style}|%{fontversion}\n",
    ]


def test_font_fingerprint_falls_back_to_font_file_contents(tmp_path: Path) -> None:
    font_root = tmp_path / "fonts"
    font_root.mkdir()
    font = font_root / "example.ttf"
    font.write_bytes(b"font-version-one")

    first = _font_directory_fingerprint((font_root,), hash_contents=True)
    font.write_bytes(b"font-version-two")
    second = _font_directory_fingerprint((font_root,), hash_contents=True)

    assert first is not None
    assert second is not None
    assert len(first) == 64
    assert len(second) == 64
    assert first != second


def test_legacy_render_cache_is_readable_only_with_its_original_run_identity(
    tmp_path: Path,
) -> None:
    legacy_key = "a" * 64
    cache_dir = tmp_path / legacy_key
    cache_dir.mkdir()
    image = cache_dir / "Slide1.PNG"
    image.write_bytes(PNG_1X1)
    (cache_dir / "render-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "input_hash": legacy_key,
                "renderer_id": "legacy-renderer",
                "renderer_version": "1.0",
                "slide_count": 1,
                "slide_images": [
                    {
                        "name": image.name,
                        "sha256": sha256_file(image),
                        "size_bytes": image.stat().st_size,
                    }
                ],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )

    assert (
        _load_render_cache(
            cache_dir,
            expected_cache_key=legacy_key,
            expected_slide_count=1,
            expected_legacy_input_hash=legacy_key,
        )
        is not None
    )
    assert (
        _load_render_cache(
            cache_dir,
            expected_cache_key=legacy_key,
            expected_slide_count=1,
        )
        is None
    )


def test_release_runtime_rejects_legacy_profile_before_writing(tmp_path) -> None:
    deck = build_pptx(tmp_path / "legacy.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    legacy = replace(default_profile(SceneType.READY_MADE), version="7.0")

    with pytest.raises(ValueError, match="only Profile version 8.3"):
        runtime.evaluate(
            EvalCase(
                case_id="legacy-profile",
                scene=SceneType.READY_MADE,
                pptx_path=str(deck),
            ),
            legacy,
        )

    assert runtime.repository.list() == []


def test_runtime_scene_degradation_keeps_intrinsic_score_and_routes_review(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(
            case_id="text",
            scene=SceneType.TEXT_TO_PPT,
            pptx_path=str(deck),
            request="制作一份中文项目汇报",
        )
    )

    assert report["coverage"] == "DEGRADED"
    assert report["decision"] == "REVIEW"
    assert report["base_score"] is not None
    assert report["full_score"] is None
    assert "unresolved_metric:fact_claim" in report["degradation_reasons"]


def test_review_and_run_export_are_audited(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(case_id="ready", scene=SceneType.READY_MADE, pptx_path=str(deck)),
        default_profile(SceneType.READY_MADE),
    )
    task = runtime.review_task(report["run_id"])
    issue_resolutions = [
        {"issue_id": item["issue_id"], "resolution": "CONFIRMED"}
        for item in task["issues"]
        if item["priority"] in {"P0", "P1"}
    ]
    review = runtime.review(
        {
            "run_id": report["run_id"],
            "verdict": "CONFIRM_SYSTEM_DECISION",
            "reviewer_id": "tester",
            "note": "ok",
            "issue_resolutions": issue_resolutions,
            "track_resolutions": {},
        }
    )
    markdown, html = runtime.export(report["run_id"], tmp_path / "exports")

    assert review["review_id"].startswith("review-")
    assert markdown.is_file() and html.is_file()
    assert report["run_id"] in html.read_text(encoding="utf-8")
    assert runtime.audit_log.verify() == (True, None)
