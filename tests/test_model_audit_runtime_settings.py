from __future__ import annotations

import pytest

from ppt_eval.infrastructure.model_audit_runtime import (
    DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS,
    DEFAULT_QWEN_BASE_URL,
    DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS,
    ModelAuditConfigurationError,
    QwenAuditSettings,
)


def test_settings_load_ignored_local_key_without_exposing_it(tmp_path) -> None:
    secret = "sk-local-test-secret-value"
    key_file = tmp_path / "api" / "qwen3.7_flash_api.txt"
    key_file.parent.mkdir()
    key_file.write_text(secret, encoding="utf-8")

    settings = QwenAuditSettings.from_environment({}, workspace_root=tmp_path)
    flash, advanced = settings.providers()

    assert settings.enabled is True
    assert settings.api_key_source == "ignored_local_file"
    assert settings.base_url == DEFAULT_QWEN_BASE_URL
    assert settings.http_timeout_seconds == 120.0
    assert settings.advanced_http_timeout_seconds == (
        DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS
    )
    assert settings.plus_http_timeout_seconds == DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS
    assert flash is not None and flash.model == "qwen3.7-flash"
    assert advanced is not None and advanced.model == "qwen3.8-flash"
    assert settings.plus_model == settings.advanced_model
    assert flash.timeout_seconds == 120.0
    assert advanced.timeout_seconds == DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS
    assert secret not in repr(settings)
    assert secret not in repr(flash)


def test_explicit_disable_keeps_providers_off_even_when_key_exists(tmp_path) -> None:
    key_file = tmp_path / "api" / "qwen3.7_flash_api.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-local-test-secret-value", encoding="utf-8")

    settings = QwenAuditSettings.from_environment(
        {"PPT_EVAL_QWEN_AUDIT_ENABLED": "false"},
        workspace_root=tmp_path,
    )

    assert settings.enabled is False
    assert settings.api_key_source == "disabled"
    assert settings.providers() == (None, None)


def test_explicit_disable_does_not_open_an_invalid_local_key_file(tmp_path) -> None:
    key_file = tmp_path / "api" / "qwen3.7_flash_api.txt"
    key_file.parent.mkdir()
    key_file.write_bytes(b"x" * 5000)

    settings = QwenAuditSettings.from_environment(
        {"PPT_EVAL_QWEN_AUDIT_ENABLED": "false"},
        workspace_root=tmp_path,
    )

    assert settings.enabled is False
    assert settings.api_key is None


def test_enabled_without_key_and_invalid_boolean_are_safe_errors(tmp_path) -> None:
    try:
        QwenAuditSettings.from_environment(
            {"PPT_EVAL_QWEN_AUDIT_ENABLED": "true"},
            workspace_root=tmp_path,
        )
    except ModelAuditConfigurationError as exc:
        assert "no DashScope API key" in str(exc)
    else:
        raise AssertionError("missing key should fail")

    try:
        QwenAuditSettings.from_environment(
            {"PPT_EVAL_QWEN_AUDIT_ENABLED": "sometimes"},
            workspace_root=tmp_path,
        )
    except ModelAuditConfigurationError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("invalid boolean should fail")


def test_http_timeout_is_environment_configurable_and_validated(tmp_path) -> None:
    settings = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS": "42.5",
        },
        workspace_root=tmp_path,
    )
    flash, advanced = settings.providers()

    assert settings.http_timeout_seconds == 42.5
    assert settings.advanced_http_timeout_seconds == (
        DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS
    )
    assert flash is not None and flash.timeout_seconds == 42.5
    assert advanced is not None and advanced.timeout_seconds == (
        DEFAULT_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS
    )

    explicit_advanced = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS": "42.5",
            "PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS": "55",
            "PPT_EVAL_QWEN_ADVANCED_MODEL": "qwen3.8-flash-custom",
        },
        workspace_root=tmp_path,
    )
    _, advanced = explicit_advanced.providers()
    assert explicit_advanced.advanced_http_timeout_seconds == 55.0
    assert explicit_advanced.advanced_model == "qwen3.8-flash-custom"
    assert advanced is not None and advanced.timeout_seconds == 55.0

    legacy_alias = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_PLUS_MODEL": "legacy-advanced-model",
            "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS": "56",
        },
        workspace_root=tmp_path,
    )
    assert legacy_alias.advanced_model == "legacy-advanced-model"
    assert legacy_alias.advanced_http_timeout_seconds == 56.0

    empty_preferred_uses_legacy = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_ADVANCED_MODEL": "",
            "PPT_EVAL_QWEN_PLUS_MODEL": "legacy-advanced-model",
            "PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS": "",
            "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS": "57",
        },
        workspace_root=tmp_path,
    )
    assert empty_preferred_uses_legacy.advanced_model == "legacy-advanced-model"
    assert empty_preferred_uses_legacy.advanced_http_timeout_seconds == 57.0

    for conflicting in (
        {
            "PPT_EVAL_QWEN_ADVANCED_MODEL": "qwen3.8-flash",
            "PPT_EVAL_QWEN_PLUS_MODEL": "qwen3.7-plus",
        },
        {
            "PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS": "55",
            "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS": "56",
        },
    ):
        with pytest.raises(ModelAuditConfigurationError, match="conflicts"):
            QwenAuditSettings.from_environment(
                {
                    "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
                    **conflicting,
                },
                workspace_root=tmp_path,
            )

    for invalid in ("0", "-1", "nan", "not-a-number"):
        try:
            QwenAuditSettings.from_environment(
                {
                    "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
                    "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS": invalid,
                },
                workspace_root=tmp_path,
            )
        except ModelAuditConfigurationError as exc:
            assert "positive finite" in str(exc)
        else:
            raise AssertionError(f"invalid HTTP timeout {invalid!r} should fail")

        try:
            QwenAuditSettings.from_environment(
                {
                    "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
                    "PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS": invalid,
                },
                workspace_root=tmp_path,
            )
        except ModelAuditConfigurationError as exc:
            assert "positive finite" in str(exc)
        else:
            raise AssertionError(f"invalid advanced HTTP timeout {invalid!r} should fail")
