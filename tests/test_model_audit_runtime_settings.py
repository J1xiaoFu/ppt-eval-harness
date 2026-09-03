from __future__ import annotations

import pytest

from ppt_eval.infrastructure.model_audit_runtime import (
    DEFAULT_QWEN_BASE_URL,
    DEFAULT_ZHIPU_BASE_URL,
    ModelAuditConfigurationError,
    QwenAuditSettings,
    ZhipuAuditSettings,
)


def test_settings_load_ignored_local_key_without_exposing_it(tmp_path) -> None:
    secret = "sk-local-test-secret-value"
    key_file = tmp_path / "api" / "qwen_api.txt"
    key_file.parent.mkdir()
    key_file.write_text(secret, encoding="utf-8")

    settings = QwenAuditSettings.from_environment({}, workspace_root=tmp_path)
    provider = settings.provider()

    assert settings.enabled is True
    assert settings.api_key_source == "ignored_local_file"
    assert settings.base_url == DEFAULT_QWEN_BASE_URL
    assert settings.http_timeout_seconds == 120.0
    assert settings.model == "qwen3.8-flash"
    assert settings.context_cache_enabled is True
    assert provider is not None and provider.model == "qwen3.8-flash"
    assert provider.context_cache_enabled is True
    assert provider.timeout_seconds == 120.0
    assert secret not in repr(settings)
    assert secret not in repr(provider)


def test_zhipu_settings_load_independent_ignored_key(tmp_path) -> None:
    secret = "independent-bigmodel-test-secret"
    key_file = tmp_path / "api" / "glm5.3_flash_api.txt"
    key_file.parent.mkdir()
    key_file.write_text(secret, encoding="utf-8")

    settings = ZhipuAuditSettings.from_environment({}, workspace_root=tmp_path)
    provider = settings.provider()

    assert settings.enabled is True
    assert settings.api_key_source == "ignored_local_file"
    assert settings.base_url == DEFAULT_ZHIPU_BASE_URL
    assert settings.model == "glm-5.3-flash"
    assert settings.http_timeout_seconds == 300.0
    assert provider is not None and provider.model == "glm-5.3-flash"
    assert provider.timeout_seconds == 300.0
    assert secret not in repr(settings)
    assert secret not in repr(provider)


def test_zhipu_settings_support_official_env_and_fail_closed(tmp_path) -> None:
    settings = ZhipuAuditSettings.from_environment(
        {
            "ZAI_API_KEY": "official-zai-test-secret",
            "PPT_EVAL_ZHIPU_HTTP_TIMEOUT_SECONDS": "45",
        },
        workspace_root=tmp_path,
    )
    assert settings.api_key_source == "environment"
    assert settings.http_timeout_seconds == 45.0
    assert settings.provider() is not None

    disabled = ZhipuAuditSettings.from_environment(
        {
            "PPT_EVAL_ZHIPU_AUDIT_ENABLED": "false",
            "ZAI_API_KEY": "official-zai-test-secret",
        },
        workspace_root=tmp_path,
    )
    assert disabled.enabled is False
    assert disabled.provider() is None

    with pytest.raises(ModelAuditConfigurationError, match="aliases conflict"):
        ZhipuAuditSettings.from_environment(
            {
                "ZAI_API_KEY": "first-bigmodel-test-secret",
                "BIGMODEL_API_KEY": "different-bigmodel-test-secret",
            },
            workspace_root=tmp_path,
        )

    with pytest.raises(ModelAuditConfigurationError, match="no BigModel API key"):
        ZhipuAuditSettings.from_environment(
            {"PPT_EVAL_ZHIPU_AUDIT_ENABLED": "true"},
            workspace_root=tmp_path,
        )


def test_explicit_disable_keeps_providers_off_even_when_key_exists(tmp_path) -> None:
    key_file = tmp_path / "api" / "qwen_api.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-local-test-secret-value", encoding="utf-8")

    settings = QwenAuditSettings.from_environment(
        {"PPT_EVAL_QWEN_AUDIT_ENABLED": "false"},
        workspace_root=tmp_path,
    )

    assert settings.enabled is False
    assert settings.api_key_source == "disabled"
    assert settings.provider() is None


def test_explicit_disable_does_not_open_an_invalid_local_key_file(tmp_path) -> None:
    key_file = tmp_path / "api" / "qwen_api.txt"
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
    provider = settings.provider()

    assert settings.http_timeout_seconds == 42.5
    assert provider is not None and provider.timeout_seconds == 42.5

    explicit_model = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS": "42.5",
            "PPT_EVAL_QWEN_FLASH_MODEL": "qwen3.8-flash-custom",
        },
        workspace_root=tmp_path,
    )
    assert explicit_model.model == "qwen3.8-flash-custom"
    assert explicit_model.provider() is not None

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


def test_qwen_context_cache_defaults_on_and_can_be_disabled(tmp_path) -> None:
    settings = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_CONTEXT_CACHE_ENABLED": "true",
        },
        workspace_root=tmp_path,
    )

    provider = settings.provider()
    assert settings.context_cache_enabled is True
    assert provider is not None and provider.context_cache_enabled is True

    disabled = QwenAuditSettings.from_environment(
        {
            "DASHSCOPE_API_KEY": "sk-local-test-secret-value",
            "PPT_EVAL_QWEN_CONTEXT_CACHE_ENABLED": "false",
        },
        workspace_root=tmp_path,
    )
    assert disabled.context_cache_enabled is False
    assert disabled.provider() is not None
    assert disabled.provider().context_cache_enabled is False

    with pytest.raises(ModelAuditConfigurationError, match="CONTEXT_CACHE"):
        QwenAuditSettings.from_environment(
            {"PPT_EVAL_QWEN_CONTEXT_CACHE_ENABLED": "sometimes"},
            workspace_root=tmp_path,
        )
