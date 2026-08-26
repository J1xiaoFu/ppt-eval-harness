from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class RenderingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RenderResult:
    renderer_id: str
    renderer_version: str
    slide_images: tuple[Path, ...]
    document_path: Path | None = None
    warnings: tuple[str, ...] = ()


class PowerPointRenderer:
    renderer_id = "powerpoint"

    def __init__(self, executable: str | Path | None = None, *, timeout_seconds: int = 120) -> None:
        self.executable = Path(executable or "C:/Program Files/Microsoft Office/root/Office16/POWERPNT.EXE")
        self.powershell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        self.timeout_seconds = timeout_seconds

    @property
    def version(self) -> str:
        if not self.executable.is_file():
            return "unavailable"
        escaped_executable = str(self.executable).replace("'", "''")
        command = f"(Get-Item -LiteralPath '{escaped_executable}').VersionInfo.ProductVersion"
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return completed.stdout.strip() or "unknown"

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        source = Path(pptx_path).resolve()
        destination = Path(output_dir).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if not self.executable.is_file():
            raise RenderingError("Microsoft PowerPoint is not installed at the configured path")
        destination.mkdir(parents=True, exist_ok=True)
        script = r"""
$ErrorActionPreference = 'Stop'
$inputPath = $env:PPT_EVAL_RENDER_INPUT
$outputPath = $env:PPT_EVAL_RENDER_OUTPUT
$app = $null
$deck = $null
$failure = $null
try {
  $app = New-Object -ComObject PowerPoint.Application
  $deck = $app.Presentations.Open($inputPath, -1, 0, 0)
  $deck.Export($outputPath, 'PNG', 0, 0)
} catch {
  $failure = $_
} finally {
  if ($null -ne $deck) { try { $deck.Close() } catch {} }
  if ($null -ne $app) { try { $app.Quit() } catch {} }
  if ($null -ne $deck) { try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($deck) } catch {} }
  if ($null -ne $app) { try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch {} }
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
}
if ($null -ne $failure) { throw $failure }
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        environment = {
            **os.environ,
            "PPT_EVAL_RENDER_INPUT": str(source),
            "PPT_EVAL_RENDER_OUTPUT": str(destination),
        }
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RenderingError((completed.stderr or completed.stdout or "PowerPoint export failed").strip())
        images = tuple(sorted(destination.glob("*.PNG"), key=_natural_slide_key))
        if not images:
            images = tuple(sorted(destination.glob("*.png"), key=_natural_slide_key))
        if not images:
            raise RenderingError("PowerPoint completed without exporting slide images")
        return RenderResult(self.renderer_id, self.version, images)


class LibreOfficeRenderer:
    renderer_id = "libreoffice"

    def __init__(self, executable: str | Path | None = None, *, timeout_seconds: int = 120) -> None:
        self.executable = Path(executable) if executable else Path(shutil.which("soffice") or "soffice")
        self.timeout_seconds = timeout_seconds

    @property
    def version(self) -> str:
        try:
            completed = subprocess.run(
                [str(self.executable), "--version"], capture_output=True, text=True, timeout=10, check=False
            )
        except OSError:
            return "unavailable"
        return completed.stdout.strip() or "unknown"

    def render(self, pptx_path: str | Path, output_dir: str | Path) -> RenderResult:
        source = Path(pptx_path).resolve()
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(destination),
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except OSError as exc:
            raise RenderingError(f"LibreOffice is unavailable: {exc}") from exc
        pdf = destination / f"{source.stem}.pdf"
        if completed.returncode != 0 or not pdf.is_file():
            raise RenderingError((completed.stderr or completed.stdout or "LibreOffice export failed").strip())
        warnings = ("LibreOffice adapter exported PDF; install a PDF raster adapter for per-slide pixels.",)
        return RenderResult(self.renderer_id, self.version, (), pdf, warnings)


def _natural_slide_key(path: Path) -> tuple[int, str]:
    digits = "".join(character for character in path.stem if character.isdigit())
    return (int(digits) if digits else 0, path.name.lower())
