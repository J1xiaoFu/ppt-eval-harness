from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SAFE_SUBPROCESS_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "USERNAME",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


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
        # Desktop Office COM automation is substantially more reliable under
        # Windows PowerShell than under PowerShell Core.  Codex ships ``pwsh``
        # on PATH, so preferring it here can make a healthy Office install fail
        # with an opaque HRESULT while opening the deck.
        if os.name == "nt":
            self.powershell = (
                shutil.which("powershell")
                or shutil.which("pwsh")
                or "powershell"
            )
        else:
            self.powershell = (
                shutil.which("pwsh")
                or shutil.which("powershell")
                or "pwsh"
            )
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
            env=_safe_subprocess_environment(),
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
  $app.AutomationSecurity = 3
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
        environment = _safe_subprocess_environment(
            render_input=source,
            render_output=destination,
        )
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

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        rasterizer: str | Path | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.executable = (
            Path(executable)
            if executable
            else Path(shutil.which("soffice") or "soffice")
        )
        located_rasterizer = (
            str(rasterizer) if rasterizer is not None else shutil.which("pdftoppm")
        )
        self.rasterizer = located_rasterizer or None
        self.timeout_seconds = timeout_seconds

    @property
    def version(self) -> str:
        try:
            completed = subprocess.run(
                [str(self.executable), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                env=_safe_subprocess_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        office_version = completed.stdout.strip() or "unknown"
        if self.rasterizer is None:
            return office_version
        try:
            rasterizer = subprocess.run(
                [self.rasterizer, "-v"],
                capture_output=True,
                text=True,
                timeout=10,
                env=_safe_subprocess_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            rasterizer_version = "unavailable"
        else:
            output = (rasterizer.stderr or rasterizer.stdout).strip()
            rasterizer_version = output.splitlines()[0] if output else "unknown"
        return f"{office_version}; {rasterizer_version}"

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
                env=_safe_subprocess_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderingError(f"LibreOffice is unavailable: {exc}") from exc
        pdf = destination / f"{source.stem}.pdf"
        if completed.returncode != 0 or not pdf.is_file():
            raise RenderingError((completed.stderr or completed.stdout or "LibreOffice export failed").strip())
        images, warnings = self._rasterize(pdf, destination)
        return RenderResult(self.renderer_id, self.version, images, pdf, warnings)

    def _rasterize(
        self,
        pdf: Path,
        destination: Path,
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        if self.rasterizer is None:
            return (), (
                "LibreOffice exported PDF, but pdftoppm is unavailable for per-slide pixels.",
            )
        prefix = destination / f"{pdf.stem}-slide"
        try:
            completed = subprocess.run(
                [self.rasterizer, "-png", str(pdf), str(prefix)],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_safe_subprocess_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return (), ("pdftoppm could not be started; only the PDF was retained.",)
        if completed.returncode != 0:
            return (), ("pdftoppm failed; only the PDF was retained.",)
        candidates = tuple(destination.glob(f"{prefix.name}-*.png"))
        if not candidates:
            candidates = tuple(destination.glob(f"{prefix.name}-*.PNG"))
        images = tuple(sorted(candidates, key=_natural_slide_key))
        if not images:
            return (), ("pdftoppm produced no slide images; only the PDF was retained.",)
        return images, ()


def _safe_subprocess_environment(
    *,
    render_input: Path | None = None,
    render_output: Path | None = None,
) -> dict[str, str]:
    """Build a renderer environment without inheriting credentials.

    Office and PDF conversion only need process discovery, user-profile,
    locale, font, and temporary-directory settings.  An explicit allowlist
    keeps API keys, tokens, passwords, database URLs, and other application
    secrets out of subprocesses that open untrusted presentation content.
    """

    environment = {
        name.upper(): value
        for name, value in os.environ.items()
        if name.upper() in _SAFE_SUBPROCESS_ENVIRONMENT_NAMES
    }
    if render_input is not None:
        environment["PPT_EVAL_RENDER_INPUT"] = str(render_input)
    if render_output is not None:
        environment["PPT_EVAL_RENDER_OUTPUT"] = str(render_output)
    return environment


def _natural_slide_key(path: Path) -> tuple[int, str]:
    digits = "".join(character for character in path.stem if character.isdigit())
    return (int(digits) if digits else 0, path.name.lower())
