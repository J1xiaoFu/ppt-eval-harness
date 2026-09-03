from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Mapping
from unittest.mock import patch

from ppt_eval.adapters import (
    ModelAuditModality,
    ModelAuditRequest,
    ModelAuditResponse,
    ModelImageInput,
    PromptSpec,
)
from ppt_eval.infrastructure import (
    ZHIPU_GLM_FLASH_MODEL,
    ZHIPU_PROVIDER_NAME,
    QwenModelAuditProviderError,
    QwenOpenAICompatibleProvider,
    ZhipuModelAuditProviderError,
    ZhipuOpenAICompatibleProvider,
    qwen_model_audits,
)
from tests.fixtures.pptx_factory import PNG_1X1


class _FakeHttpResponse:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self.data[:amount]


def _request(image: ModelImageInput) -> ModelAuditRequest:
    return ModelAuditRequest(
        audit_id="v8.visual.composition_layout",
        metric_id="structured_vlm_composition_layout",
        modality=ModelAuditModality.VLM,
        prompt=PromptSpec(
            prompt_id="ppt-vlm-grounded-composition-layout-audit",
            version="2.0.0",
            instructions="Return grounded JSON evidence for this visual criterion.",
        ),
        case_id="real-deck-smoke",
        scene="FINISHED_DECK",
        slides=({"page_number": 1, "objects": []},),
        context={"input_trust": "UNTRUSTED_DATA"},
        images=(image,),
    )


def _vendor_response() -> Mapping[str, Any]:
    return {
        "id": "glm-completion-1",
        "model": ZHIPU_GLM_FLASH_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": "private reasoning must not be retained",
                    "content": json.dumps(
                        {
                            "score": 0.82,
                            "confidence": 0.88,
                            "evidence": [
                                {
                                    "evidence_id": "finding-1",
                                    "kind": "composition_layout",
                                    "message": "The title and body form a clear hierarchy.",
                                    "page_number": 1,
                                    "payload": {},
                                }
                            ],
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "total_cost": 0.008,
            "input_tokens_details": {
                "image_tokens": 72,
                "cached_tokens": 16,
                "cache_creation_input_tokens": 4,
            },
        },
    }


def test_zhipu_provider_uses_documented_multimodal_request(tmp_path) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput(
        page_number=1,
        uri=str(image_path),
        media_type="image/png",
        sha256=hashlib.sha256(PNG_1X1).hexdigest(),
    )
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        captured["authorization"] = http_request.get_header("Authorization")
        captured["url"] = http_request.full_url
        captured["timeout"] = timeout
        return _FakeHttpResponse(_vendor_response())

    provider = ZhipuOpenAICompatibleProvider(
        "bigmodel-test-secret",
        "https://open.bigmodel.cn/api/paas/v4/",
    )
    request = _request(image)
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    body = captured["body"]
    assert captured["url"].endswith("/api/paas/v4/chat/completions")
    assert captured["authorization"] == "Bearer bigmodel-test-secret"
    assert captured["timeout"] == 300.0
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert body["reasoning_effort"] == "max"
    assert body["temperature"] == 1.0
    assert body["top_p"] == 0.95
    assert "enable_thinking" not in body
    assert "seed" not in body
    content = body["messages"][1]["content"]
    assert [item["type"] for item in content] == ["text", "text", "image_url"]
    assert content[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert response.model.provider == ZHIPU_PROVIDER_NAME
    assert response.model.model_id == ZHIPU_GLM_FLASH_MODEL
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.image_tokens == 72
    assert response.usage.cached_tokens == 16
    assert response.usage.cache_creation_input_tokens == 4
    assert response.usage.request_bytes == len(
        json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert response.usage.cost_known is True
    assert response.evidence[0].payload["adapter_cost_known"] is True
    assert "private reasoning" not in json.dumps(payload)
    assert "bigmodel-test-secret" not in repr(provider)


def test_zhipu_scout_keeps_thinking_but_uses_low_reasoning_effort(
    tmp_path,
) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput(
        page_number=1,
        uri=str(image_path),
        media_type="image/png",
        sha256=hashlib.sha256(PNG_1X1).hexdigest(),
    )
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response())

    request = replace(
        _request(image),
        audit_id="atlas-scout-fixture",
        context={
            "input_trust": "UNTRUSTED_RENDERED_ATLAS",
            "model_inference_profile": "SCOUT_LOW_LATENCY_JSON_V1",
        },
    )
    provider = ZhipuOpenAICompatibleProvider(
        "bigmodel-test-secret",
        "https://open.bigmodel.cn/api/paas/v4/",
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        provider.audit(request)

    assert captured["body"]["thinking"] == {
        "type": "enabled",
        "clear_thinking": False,
    }
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["max_tokens"] == 8192


def test_zhipu_provider_translates_safe_vendor_error(tmp_path) -> None:
    provider = ZhipuOpenAICompatibleProvider(
        "bigmodel-test-secret",
        "https://open.bigmodel.cn/api/paas/v4",
    )

    def failed_urlopen(_request, *, timeout):
        del timeout
        raise OSError("sensitive transport detail")

    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput(
        page_number=1,
        uri=str(image_path),
        media_type="image/png",
        sha256=hashlib.sha256(PNG_1X1).hexdigest(),
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", failed_urlopen):
        try:
            provider.audit(_request(image))
        except ZhipuModelAuditProviderError as exc:
            assert "sensitive transport detail" not in str(exc)
            assert "bigmodel-test-secret" not in str(exc)
        else:
            raise AssertionError("Zhipu provider failure should be normalized")


def test_zhipu_provider_rejects_unsupported_image_media_type(tmp_path) -> None:
    image_path = tmp_path / "slide.gif"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput(
        page_number=1,
        uri=str(image_path),
        media_type="image/gif",
        sha256=hashlib.sha256(PNG_1X1).hexdigest(),
    )
    provider = ZhipuOpenAICompatibleProvider(
        "bigmodel-test-secret",
        "https://open.bigmodel.cn/api/paas/v4",
    )

    try:
        provider.audit(_request(image))
    except ZhipuModelAuditProviderError as exc:
        assert "PNG or JPEG" in str(exc)
    else:
        raise AssertionError("unsupported GLM image media type should fail locally")


def test_cross_provider_credentials_are_blocked_before_network(tmp_path) -> None:
    qwen_secret = "qwen-cross-provider-test-secret"
    zhipu_secret = "zhipu-cross-provider-test-secret"
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput(
        page_number=1,
        uri=str(image_path),
        media_type="image/png",
        sha256=hashlib.sha256(PNG_1X1).hexdigest(),
    )
    base_request = _request(image)
    qwen_request = replace(
        base_request,
        context={"input_trust": "UNTRUSTED_DATA", "leaked_secret": zhipu_secret},
    )
    zhipu_request = replace(
        base_request,
        context={"input_trust": "UNTRUSTED_DATA", "leaked_secret": qwen_secret},
    )
    qwen = QwenOpenAICompatibleProvider(
        qwen_secret,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.8-flash",
        protected_secrets=(qwen_secret, zhipu_secret),
    )
    zhipu = ZhipuOpenAICompatibleProvider(
        zhipu_secret,
        "https://open.bigmodel.cn/api/paas/v4",
        protected_secrets=(qwen_secret, zhipu_secret),
    )

    with patch.object(
        qwen_model_audits.urllib.request,
        "urlopen",
        side_effect=AssertionError("network must not be called"),
    ):
        try:
            qwen.audit(qwen_request)
        except QwenModelAuditProviderError as exc:
            assert "protected runtime credential" in str(exc)
        else:
            raise AssertionError("Qwen must reject the BigModel key")
        try:
            zhipu.audit(zhipu_request)
        except ZhipuModelAuditProviderError as exc:
            assert "protected runtime credential" in str(exc)
        else:
            raise AssertionError("GLM must reject the DashScope key")
