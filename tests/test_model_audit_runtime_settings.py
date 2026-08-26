from __future__ import annotations

from ppt_eval.infrastructure.model_audit_runtime import (
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
    flash, plus = settings.providers()

    assert settings.enabled is True
    assert settings.api_key_source == "ignored_local_file"
    assert settings.base_url == DEFAULT_QWEN_BASE_URL
    assert settings.http_timeout_seconds == 120.0
    assert settings.plus_http_timeout_seconds == DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS
    assert flash is not None and flash.model == "qwen3.7-flash"
    assert plus is not None and plus.model == "qwen3.7-plus"
    assert flash.timeout_seconds == 120.0
    assert plus.timeout_seconds == DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS
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
    flash, plus = settings.providers()

    assert settings.http_timeout_seconds == 42.5
    assert settings.plus_http_timeout_seconds == DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS
    assert flash is not None and flash.timeout_seconds == 42.5
    assert plus is not None and plus.timeout_seconds == DEFAULT_QWEN_PLUS_HTTP_TIMEOUT_SECONDS

    explicit_plus = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS": "42.5",
            "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS": "55",
        },
        workspace_root=tmp_path,
    )
    _, plus = explicit_plus.providers()
    assert explicit_plus.plus_http_timeout_seconds == 55.0
    assert plus is not None and plus.timeout_seconds == 55.0

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
                    "PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS": invalid,
                },
                workspace_root=tmp_path,
            )
        except ModelAuditConfigurationError as exc:
            assert "positive finite" in str(exc)
        else:
            raise AssertionError(f"invalid Plus HTTP timeout {invalid!r} should fail")
