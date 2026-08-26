from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from ppt_eval.adapters.renderers import LibreOfficeRenderer, PowerPointRenderer

_SECRET_ENVIRONMENT = {
    "DASHSCOPE_API_KEY": "sk-dashscope-must-not-leak",
    "OPENAI_API_KEY": "sk-openai-must-not-leak",
    "DATABASE_PASSWORD": "database-password-must-not-leak",
    "SERVICE_TOKEN": "service-token-must-not-leak",
    "CLIENT_SECRET": "client-secret-must-not-leak",
}


def _assert_secret_free(environment: dict[str, str]) -> None:
    names = {name.upper() for name in environment}
    assert not any(
        marker in name
        for name in names
        for marker in ("API_KEY", "PASSWORD", "SECRET", "TOKEN")
    )
    serialized = "\n".join(environment.values())
    assert not any(value in serialized for value in _SECRET_ENVIRONMENT.values())


def test_powerpoint_renderer_uses_safe_environment_and_disables_macros(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "POWERPNT.EXE"
    executable.write_bytes(b"fake executable")
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"fake deck")
    output = tmp_path / "rendered"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        environment = dict(kwargs["env"])
        calls.append((list(command), environment))
        if "-EncodedCommand" in command:
            Path(environment["PPT_EVAL_RENDER_OUTPUT"], "Slide1.PNG").write_bytes(
                b"png"
            )
        return subprocess.CompletedProcess(command, 0, stdout="16.0", stderr="")

    renderer = PowerPointRenderer(executable=executable)
    renderer.powershell = "powershell"
    safe_environment = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(tmp_path / "temp"),
        **_SECRET_ENVIRONMENT,
    }
    with patch.dict(os.environ, safe_environment, clear=True), patch(
        "ppt_eval.adapters.renderers.subprocess.run",
        side_effect=fake_run,
    ):
        result = renderer.render(source, output)

    render_command, render_environment = next(
        item for item in calls if "-EncodedCommand" in item[0]
    )
    assert render_environment["PATH"] == safe_environment["PATH"]
    assert render_environment["TEMP"] == safe_environment["TEMP"]
    assert render_environment["PPT_EVAL_RENDER_INPUT"] == str(source.resolve())
    assert render_environment["PPT_EVAL_RENDER_OUTPUT"] == str(output.resolve())
    _assert_secret_free(render_environment)
    encoded = render_command[render_command.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "$app.AutomationSecurity = 3" in script
    assert result.slide_images == (output.resolve() / "Slide1.PNG",)
    for _, environment in calls:
        _assert_secret_free(environment)


def test_libreoffice_renderer_rasterizes_pdf_with_safe_environment(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "soffice"
    executable.write_bytes(b"fake executable")
    rasterizer = tmp_path / "pdftoppm"
    rasterizer.write_bytes(b"fake executable")
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"fake deck")
    output = tmp_path / "rendered"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        environment = dict(kwargs["env"])
        command = list(command)
        calls.append((command, environment))
        if "--convert-to" in command:
            Path(command[command.index("--outdir") + 1], "deck.pdf").write_bytes(
                b"pdf"
            )
            return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")
        if command[0] == str(rasterizer) and "-png" in command:
            prefix = Path(command[-1])
            prefix.with_name(f"{prefix.name}-2.png").write_bytes(b"png-2")
            prefix.with_name(f"{prefix.name}-1.png").write_bytes(b"png-1")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == str(rasterizer) and "-v" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="pdftoppm version 24.01",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="LibreOffice 24.2",
            stderr="",
        )

    renderer = LibreOfficeRenderer(
        executable=executable,
        rasterizer=rasterizer,
    )
    safe_environment = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(tmp_path / "temp"),
        "HOME": str(tmp_path / "profile"),
        **_SECRET_ENVIRONMENT,
    }
    with patch.dict(os.environ, safe_environment, clear=True), patch(
        "ppt_eval.adapters.renderers.subprocess.run",
        side_effect=fake_run,
    ):
        result = renderer.render(source, output)

    assert [path.name for path in result.slide_images] == [
        "deck-slide-1.png",
        "deck-slide-2.png",
    ]
    assert result.document_path == output.resolve() / "deck.pdf"
    assert result.warnings == ()
    assert "LibreOffice 24.2" in result.renderer_version
    assert "pdftoppm version 24.01" in result.renderer_version
    assert any("--convert-to" in command for command, _ in calls)
    assert any("-png" in command for command, _ in calls)
    for _, environment in calls:
        assert environment["PATH"] == safe_environment["PATH"]
        assert environment["TEMP"] == safe_environment["TEMP"]
        assert environment["HOME"] == safe_environment["HOME"]
        _assert_secret_free(environment)


def test_libreoffice_without_pdftoppm_retains_pdf_degradation(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "soffice"
    executable.write_bytes(b"fake executable")
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"fake deck")
    output = tmp_path / "rendered"

    def fake_run(command, **kwargs):
        environment = dict(kwargs["env"])
        _assert_secret_free(environment)
        command = list(command)
        if "--convert-to" in command:
            Path(command[command.index("--outdir") + 1], "deck.pdf").write_bytes(
                b"pdf"
            )
            return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="LibreOffice 24.2",
            stderr="",
        )

    with patch(
        "ppt_eval.adapters.renderers.shutil.which",
        return_value=None,
    ):
        renderer = LibreOfficeRenderer(executable=executable)
    with patch.dict(os.environ, _SECRET_ENVIRONMENT, clear=False), patch(
        "ppt_eval.adapters.renderers.subprocess.run",
        side_effect=fake_run,
    ):
        result = renderer.render(source, output)

    assert result.slide_images == ()
    assert result.document_path == output.resolve() / "deck.pdf"
    assert result.warnings == (
        "LibreOffice exported PDF, but pdftoppm is unavailable for per-slide pixels.",
    )
