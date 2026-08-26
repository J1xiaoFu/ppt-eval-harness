"""Local Qwen audit settings with secret-safe environment/file loading."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .qwen_model_audits import (
    DEFAULT_QWEN_HTTP_TIMEOUT_SECONDS,
    QWEN_FLASH_MODEL,
    QWEN_PLUS_MODEL,
    QwenModelAuditProvider,
)

DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_KEY_FILE = "api/qwen3.7_flash_api.txt"
DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS = 240.0


class ModelAuditConfigurationError(RuntimeError):
    """Model audits were enabled without a valid local secret configuration."""


@dataclass(frozen=True, slots=True)
class QwenAuditSettings:
    enabled: bool
    base_url: str
    flash_model: str
    plus_model: str
    http_timeout_seconds: float
    plus_http_timeout_seconds: float
    api_key_source: str
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> "QwenAuditSettings":
        env = dict(os.environ if environment is None else environment)
        root = Path(workspace_root or Path.cwd()).resolve()
        configured = env.get("PPT_EVAL_QWEN_AUDIT_ENABLED")
        explicitly_enabled = (
            None if configured is None else _parse_boolean(configured)
        )
        base_url = str(env.get("PPT_EVAL_QWEN_BASE_URL") or DEFAULT_QWEN_BASE_URL)
        flash_model = str(env.get("PPT_EVAL_QWEN_FLASH_MODEL") or QWEN_FLASH_MODEL)
        plus_model = str(env.get("PPT_EVAL_QWEN_PLUS_MODEL") or QWEN_PLUS_MODEL)
        http_timeout_seconds = _positive_timeout(
            env,
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS",
            DEFAULT_QWEN_HTTP_TIMEOUT_SECONDS,
        )
        plus_http_timeout_seconds = _positive_timeout(
            env,
            "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS",
            max(DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS, http_timeout_seconds),
        )
        if explicitly_enabled is False:
            # An operational kill switch must not even open the local secret
            # file.  This also keeps maintenance-only CLI commands usable when
            # a stale or permission-restricted key file is present.
            return cls(
                enabled=False,
                base_url=base_url,
                flash_model=flash_model,
                plus_model=plus_model,
                http_timeout_seconds=http_timeout_seconds,
                plus_http_timeout_seconds=plus_http_timeout_seconds,
                api_key_source="disabled",
                api_key=None,
            )

        key = str(env.get("DASHSCOPE_API_KEY") or "").strip()
        source = "environment" if key else "none"
        key_file_value = str(
            env.get("PPT_EVAL_DASHSCOPE_API_KEY_FILE") or DEFAULT_QWEN_KEY_FILE
        ).strip()
        key_file = Path(key_file_value)
        if not key_file.is_absolute():
            key_file = root / key_file
        if not key and key_file.is_file():
            try:
                if key_file.stat().st_size > 4096:
                    raise ModelAuditConfigurationError(
                        "DashScope API key file is unexpectedly large"
                    )
                key = key_file.read_text(encoding="utf-8").strip()
            except ModelAuditConfigurationError:
                raise
            except OSError as exc:
                raise ModelAuditConfigurationError("DashScope API key file is unreadable") from exc
            source = "ignored_local_file"

        enabled = bool(key) if explicitly_enabled is None else explicitly_enabled
        if enabled and not key:
            raise ModelAuditConfigurationError(
                "Qwen model audits are enabled but no DashScope API key is configured"
            )
        if key and (any(character.isspace() for character in key) or len(key) < 16):
            raise ModelAuditConfigurationError("DashScope API key has an invalid shape")
        return cls(
            enabled=enabled,
            base_url=base_url,
            flash_model=flash_model,
            plus_model=plus_model,
            http_timeout_seconds=http_timeout_seconds,
            plus_http_timeout_seconds=plus_http_timeout_seconds,
            api_key_source=source,
            api_key=key or None,
        )

    def providers(self) -> tuple[QwenModelAuditProvider | None, QwenModelAuditProvider | None]:
        if not self.enabled or not self.api_key:
            return None, None
        return (
            QwenModelAuditProvider(
                self.api_key,
                self.base_url,
                self.flash_model,
                timeout_seconds=self.http_timeout_seconds,
            ),
            QwenModelAuditProvider(
                self.api_key,
                self.base_url,
                self.plus_model,
                timeout_seconds=self.plus_http_timeout_seconds,
            ),
        )


def _positive_timeout(
    environment: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = environment.get(name, str(default))
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelAuditConfigurationError(
            f"{name} must be a positive finite number"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ModelAuditConfigurationError(
            f"{name} must be a positive finite number"
        )
    return timeout


def _parse_boolean(value: object) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ModelAuditConfigurationError(
        "PPT_EVAL_QWEN_AUDIT_ENABLED must be a boolean value"
    )


__all__ = [
    "DEFAULT_QWEN_BASE_URL",
    "DEFAULT_QWEN_KEY_FILE",
    "DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS",
    "ModelAuditConfigurationError",
    "QwenAuditSettings",
]
