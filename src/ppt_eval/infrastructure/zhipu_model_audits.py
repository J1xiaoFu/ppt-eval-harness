"""OpenAI-compatible Zhipu/BigModel adapter for advanced model audits.

The transport and provider-neutral response validation are shared with the
Qwen adapter.  Only the vendor dialect, endpoint configuration, model
identity, and documented GLM image limit differ.  PromptSpec content is never
rewritten here.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ppt_eval.adapters.model_audits import ModelAuditProviderError, ModelAuditRequest

from .qwen_model_audits import (
    QwenModelAuditProviderError,
    QwenOpenAICompatibleProvider,
)

ZHIPU_GLM_FLASH_MODEL = "glm-5.3-flash"
DEFAULT_ZHIPU_HTTP_TIMEOUT_SECONDS = 300.0
ZHIPU_PROVIDER_NAME = "zhipu-bigmodel-openai-compatible"
_ZHIPU_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ZHIPU_IMAGE_MEDIA_TYPES = frozenset(("image/jpeg", "image/jpg", "image/png"))


class ZhipuModelAuditProviderError(ModelAuditProviderError):
    """A safe, redacted failure while calling the BigModel endpoint."""


class ZhipuOpenAICompatibleProvider(QwenOpenAICompatibleProvider):
    """Call GLM-5.3-Flash through BigModel's OpenAI-compatible endpoint."""

    __slots__ = ()

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = ZHIPU_GLM_FLASH_MODEL,
        *,
        timeout_seconds: float = DEFAULT_ZHIPU_HTTP_TIMEOUT_SECONDS,
        protected_secrets: Sequence[str] = (),
    ) -> None:
        super().__init__(
            api_key,
            base_url,
            model,
            timeout_seconds=timeout_seconds,
            dialect="zhipu",
            provider_name=ZHIPU_PROVIDER_NAME,
            max_image_bytes=_ZHIPU_MAX_IMAGE_BYTES,
            protected_secrets=protected_secrets,
        )

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        if any(
            image.media_type.strip().lower() not in _ZHIPU_IMAGE_MEDIA_TYPES
            for image in request.images
        ):
            raise ZhipuModelAuditProviderError(
                "Zhipu rendered inputs must use PNG or JPEG media types"
            )
        try:
            return super().audit(request)
        except QwenModelAuditProviderError as exc:
            # The shared validator retains historical Qwen-safe error strings
            # for backward compatibility.  Translate only the vendor label;
            # never include a response body, prompt, local path, or secret.
            message = str(exc).replace("Qwen", "Zhipu")
            raise ZhipuModelAuditProviderError(
                message,
                audit_metadata=exc.audit_metadata,
                cost=exc.cost,
            ) from exc


ZhipuModelAuditProvider = ZhipuOpenAICompatibleProvider


__all__ = [
    "DEFAULT_ZHIPU_HTTP_TIMEOUT_SECONDS",
    "ZHIPU_GLM_FLASH_MODEL",
    "ZHIPU_PROVIDER_NAME",
    "ZhipuModelAuditProvider",
    "ZhipuModelAuditProviderError",
    "ZhipuOpenAICompatibleProvider",
]
