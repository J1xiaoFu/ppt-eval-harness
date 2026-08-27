"""Local model-audit settings with secret-safe environment/file loading."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .qwen_model_audits import (
    DEFAULT_QWEN_HTTP_TIMEOUT_SECONDS,
    QWEN_ADVANCED_MODEL,
    QWEN_PRIMARY_MODEL,
    QwenModelAuditProvider,
)
from .zhipu_model_audits import (
    DEFAULT_ZHIPU_HTTP_TIMEOUT_SECONDS,
    ZHIPU_GLM_FLASH_MODEL,
    ZhipuModelAuditProvider,
)

DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_KEY_FILE = "api/qwen3.7_flash_api.txt"
DEFAULT_ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_ZHIPU_KEY_FILE = "api/glm5.3_flash_api.txt"
DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS = 240.0
# Backward-compatible symbol for callers that have not migrated their naming.
DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS = DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS


class ModelAuditConfigurationError(RuntimeError):
    """Model audits were enabled without a valid local secret configuration."""


@dataclass(frozen=True, slots=True)
class QwenAuditSettings:
    enabled: bool
    base_url: str
    flash_model: str
    advanced_model: str
    http_timeout_seconds: float
    advanced_http_timeout_seconds: float
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
            None
            if configured is None or not str(configured).strip()
            else _parse_boolean(configured)
        )
        base_url = str(env.get("PPT_EVAL_QWEN_BASE_URL") or DEFAULT_QWEN_BASE_URL)
        flash_model = str(env.get("PPT_EVAL_QWEN_FLASH_MODEL") or QWEN_PRIMARY_MODEL)
        advanced_model = _advanced_text_setting(
            env,
            "PPT_EVAL_QWEN_ADVANCED_MODEL",
            "PPT_EVAL_QWEN_PLUS_MODEL",
            QWEN_ADVANCED_MODEL,
        )
        http_timeout_seconds = _positive_timeout(
            env,
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS",
            DEFAULT_QWEN_HTTP_TIMEOUT_SECONDS,
        )
        advanced_http_timeout_seconds = _advanced_timeout_setting(
            env,
            max(DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS, http_timeout_seconds),
        )
        if explicitly_enabled is False:
            # An operational kill switch must not even open the local secret
            # file.  This also keeps maintenance-only CLI commands usable when
            # a stale or permission-restricted key file is present.
            return cls(
                enabled=False,
                base_url=base_url,
                flash_model=flash_model,
                advanced_model=advanced_model,
                http_timeout_seconds=http_timeout_seconds,
                advanced_http_timeout_seconds=advanced_http_timeout_seconds,
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
            advanced_model=advanced_model,
            http_timeout_seconds=http_timeout_seconds,
            advanced_http_timeout_seconds=advanced_http_timeout_seconds,
            api_key_source=source,
            api_key=key or None,
        )

    def providers(
        self,
        *,
        protected_secrets: Sequence[str] = (),
    ) -> tuple[QwenModelAuditProvider | None, QwenModelAuditProvider | None]:
        if not self.enabled or not self.api_key:
            return None, None
        return (
            QwenModelAuditProvider(
                self.api_key,
                self.base_url,
                self.flash_model,
                timeout_seconds=self.http_timeout_seconds,
                protected_secrets=protected_secrets,
            ),
            QwenModelAuditProvider(
                self.api_key,
                self.base_url,
                self.advanced_model,
                timeout_seconds=self.advanced_http_timeout_seconds,
                protected_secrets=protected_secrets,
            ),
        )

    @property
    def plus_model(self) -> str:
        """Deprecated compatibility alias; use ``advanced_model``."""

        return self.advanced_model

    @property
    def plus_http_timeout_seconds(self) -> float:
        """Deprecated compatibility alias; use ``advanced_http_timeout_seconds``."""

        return self.advanced_http_timeout_seconds


@dataclass(frozen=True, slots=True)
class ZhipuAuditSettings:
    """Independent BigModel settings for the criterion-isomorphic fallback."""

    enabled: bool
    base_url: str
    model: str
    http_timeout_seconds: float
    api_key_source: str
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> "ZhipuAuditSettings":
        env = dict(os.environ if environment is None else environment)
        root = Path(workspace_root or Path.cwd()).resolve()
        configured = env.get("PPT_EVAL_ZHIPU_AUDIT_ENABLED")
        explicitly_enabled = (
            None
            if configured is None or not str(configured).strip()
            else _parse_boolean(configured, "PPT_EVAL_ZHIPU_AUDIT_ENABLED")
        )
        base_url = str(
            env.get("PPT_EVAL_ZHIPU_BASE_URL") or DEFAULT_ZHIPU_BASE_URL
        )
        model = str(env.get("PPT_EVAL_ZHIPU_MODEL") or ZHIPU_GLM_FLASH_MODEL)
        http_timeout_seconds = _positive_timeout(
            env,
            "PPT_EVAL_ZHIPU_HTTP_TIMEOUT_SECONDS",
            DEFAULT_ZHIPU_HTTP_TIMEOUT_SECONDS,
        )
        if explicitly_enabled is False:
            return cls(
                enabled=False,
                base_url=base_url,
                model=model,
                http_timeout_seconds=http_timeout_seconds,
                api_key_source="disabled",
                api_key=None,
            )

        key = _zhipu_environment_key(env)
        source = "environment" if key else "none"
        key_file_value = str(
            env.get("PPT_EVAL_ZHIPU_API_KEY_FILE") or DEFAULT_ZHIPU_KEY_FILE
        ).strip()
        key_file = Path(key_file_value)
        if not key_file.is_absolute():
            key_file = root / key_file
        if not key and key_file.is_file():
            try:
                if key_file.stat().st_size > 4096:
                    raise ModelAuditConfigurationError(
                        "Zhipu API key file is unexpectedly large"
                    )
                key = key_file.read_text(encoding="utf-8").strip()
            except ModelAuditConfigurationError:
                raise
            except OSError as exc:
                raise ModelAuditConfigurationError(
                    "Zhipu API key file is unreadable"
                ) from exc
            source = "ignored_local_file"

        enabled = bool(key) if explicitly_enabled is None else explicitly_enabled
        if enabled and not key:
            raise ModelAuditConfigurationError(
                "Zhipu model audits are enabled but no BigModel API key is configured"
            )
        if key and (any(character.isspace() for character in key) or len(key) < 16):
            raise ModelAuditConfigurationError("BigModel API key has an invalid shape")
        return cls(
            enabled=enabled,
            base_url=base_url,
            model=model,
            http_timeout_seconds=http_timeout_seconds,
            api_key_source=source,
            api_key=key or None,
        )

    def provider(
        self,
        *,
        protected_secrets: Sequence[str] = (),
    ) -> ZhipuModelAuditProvider | None:
        if not self.enabled or not self.api_key:
            return None
        return ZhipuModelAuditProvider(
            self.api_key,
            self.base_url,
            self.model,
            timeout_seconds=self.http_timeout_seconds,
            protected_secrets=protected_secrets,
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


def _advanced_text_setting(
    environment: Mapping[str, str],
    preferred_name: str,
    legacy_name: str,
    default: str,
) -> str:
    preferred = str(environment.get(preferred_name) or "").strip()
    legacy = str(environment.get(legacy_name) or "").strip()
    if preferred and legacy and preferred != legacy:
        raise ModelAuditConfigurationError(
            f"{preferred_name} conflicts with legacy {legacy_name}"
        )
    return preferred or legacy or default


def _advanced_timeout_setting(
    environment: Mapping[str, str],
    default: float,
) -> float:
    preferred_name = "PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS"
    legacy_name = "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS"
    preferred = str(environment.get(preferred_name) or "").strip()
    legacy = str(environment.get(legacy_name) or "").strip()
    if preferred and legacy:
        preferred_value = _positive_timeout(
            {preferred_name: preferred}, preferred_name, default
        )
        legacy_value = _positive_timeout({legacy_name: legacy}, legacy_name, default)
        if not math.isclose(preferred_value, legacy_value):
            raise ModelAuditConfigurationError(
                f"{preferred_name} conflicts with legacy {legacy_name}"
            )
        return preferred_value
    if preferred:
        return _positive_timeout(
            {preferred_name: preferred}, preferred_name, default
        )
    if legacy:
        return _positive_timeout({legacy_name: legacy}, legacy_name, default)
    return default


def _zhipu_environment_key(environment: Mapping[str, str]) -> str:
    names = (
        "PPT_EVAL_ZHIPU_API_KEY",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "BIGMODEL_API_KEY",
    )
    configured = {
        name: str(environment.get(name) or "").strip()
        for name in names
        if str(environment.get(name) or "").strip()
    }
    values = set(configured.values())
    if len(values) > 1:
        raise ModelAuditConfigurationError(
            "Zhipu API key environment aliases conflict"
        )
    return next(iter(values), "")


def _parse_boolean(
    value: object,
    name: str = "PPT_EVAL_QWEN_AUDIT_ENABLED",
) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ModelAuditConfigurationError(
        f"{name} must be a boolean value"
    )


__all__ = [
    "DEFAULT_QWEN_BASE_URL",
    "DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_QWEN_KEY_FILE",
    "DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_ZHIPU_BASE_URL",
    "DEFAULT_ZHIPU_KEY_FILE",
    "ModelAuditConfigurationError",
    "QwenAuditSettings",
    "ZhipuAuditSettings",
]
