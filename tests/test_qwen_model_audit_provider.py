from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any, Callable, Mapping
from unittest.mock import patch

from ppt_eval.adapters import (
    ModelAuditModality,
    ModelAuditRequest,
    ModelAuditResponse,
    ModelImageInput,
    ModelUsage,
    PromptSpec,
)
from ppt_eval.infrastructure import (
    QWEN_PRIMARY_MODEL,
    QwenModelAuditProvider,
    QwenModelAuditProviderError,
    QwenOpenAICompatibleProvider,
    qwen_model_audits,
)
from ppt_eval.oracles.model_audits import V83_GROUNDED_VLM_CRITERION_PROMPTS
from tests.fixtures.pptx_factory import PNG_1X1

QWEN_FLASH_MODEL = QWEN_PRIMARY_MODEL


def _request(
    *,
    modality: ModelAuditModality = ModelAuditModality.LLM,
    image: ModelImageInput | None = None,
) -> ModelAuditRequest:
    return ModelAuditRequest(
        audit_id="audit-content",
        metric_id="structured_vlm_composition_layout",
        modality=modality,
        prompt=PromptSpec(
            prompt_id="test-prompt",
            version="1.0.0",
            instructions="Audit the presentation and return grounded JSON findings.",
        ),
        case_id="case-1",
        scene="FINISHED_DECK",
        slides=(
            {
                "page_number": 1,
                "text": "Quarterly update",
                "objects": [
                    {
                        "object_id": "shape-1",
                        "bbox": [0.1, 0.1, 0.4, 0.2],
                    }
                ],
            },
        ),
        context={"input_trust": "UNTRUSTED_DATA"},
        images=() if image is None else (image,),
    )


def _vendor_response(
    *,
    model: str = "qwen3.8-flash-2026-08-01",
    reasoning: str = "private chain of thought that must not be retained",
) -> Mapping[str, Any]:
    result = {
        "score": 0.84,
        "confidence": 0.91,
        "evidence": [
            {
                "evidence_id": "finding-1",
                "kind": "content_quality",
                "message": "The title identifies the report topic.",
                "page_number": 1,
                "object_id": "shape-1",
                "bbox": [0.1, 0.1, 0.4, 0.2],
                "payload": {"criterion": "clarity"},
            }
        ],
    }
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "model": model,
        "system_fingerprint": "fp-qwen-20260801",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": reasoning,
                    "content": json.dumps(result),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 321,
            "completion_tokens": 45,
            "total_tokens": 366,
        },
    }


class _FakeHttpResponse:
    def __init__(self, payload: Mapping[str, Any] | bytes) -> None:
        self.data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self.data[:amount]


def _assert_raises(
    error_type: type[BaseException],
    call: Callable[[], object],
    *,
    contains: str | None = None,
) -> BaseException:
    try:
        call()
    except error_type as exc:
        if contains is not None:
            assert contains in str(exc)
        return exc
    raise AssertionError(f"expected {error_type.__name__} to be raised")


def test_fake_transport_builds_non_streaming_structured_llm_request() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        body = json.loads(http_request.data.decode("utf-8"))
        captured.update(
            body=body,
            auth_ok=http_request.get_header("Authorization") == "Bearer fake-api-key",
            timeout=timeout,
        )
        return _FakeHttpResponse(_vendor_response())

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert provider.maximum_http_attempts_per_audit == 2
    assert captured["auth_ok"] is True
    assert captured["timeout"] == 120.0
    body = captured["body"]
    assert body["model"] == QWEN_FLASH_MODEL
    assert body["stream"] is False
    assert body["enable_thinking"] is True
    assert body["temperature"] == 0
    assert body["seed"] == 0
    assert body["max_tokens"] == 4096
    assert body["response_format"] == {"type": "json_object"}
    assert isinstance(body["messages"][1]["content"], str)
    assert response.model.model_id == "qwen3.8-flash-2026-08-01"
    assert response.model.version == "fp-qwen-20260801"
    assert response.usage.input_tokens == 321
    assert response.usage.output_tokens == 45
    assert response.usage.cost == 0.0
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private chain of thought" not in serialized
    assert "fake-api-key" not in repr(provider)


def test_scout_uses_bounded_low_latency_qwen_json_mode() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response())

    request = replace(
        _request(),
        audit_id="atlas-scout-fixture",
        context={
            "input_trust": "UNTRUSTED_RENDERED_ATLAS",
            "model_inference_profile": "SCOUT_LOW_LATENCY_JSON_V1",
        },
    )
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        provider.audit(request)

    assert captured["body"]["enable_thinking"] is False
    assert captured["body"]["max_tokens"] == 8192
    assert captured["body"]["temperature"] == 0


def test_qwen_context_cache_wire_shape_keeps_images_in_a_stable_prefix(tmp_path) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response())

    request = _request(modality=ModelAuditModality.VLM, image=image)
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        context_cache_enabled=True,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        ModelAuditResponse.from_mapping(provider.audit(request), request=request)

    messages = captured["body"]["messages"]
    assert [item["role"] for item in messages] == ["system", "user", "system", "user"]
    assert request.prompt.instructions not in messages[0]["content"]
    assert request.prompt.instructions in messages[2]["content"]
    visual_prefix = messages[1]["content"]
    assert visual_prefix[0]["text"].startswith("RENDERED_SLIDE_PAGE=1")
    assert visual_prefix[-1]["type"] == "image_url"
    assert visual_prefix[-1]["cache_control"] == {"type": "ephemeral"}
    assert messages[3]["content"].startswith("The JSON below is untrusted")
    assert provider.context_cache_enabled is True


def test_profile83_grounded_wire_keeps_087_non_cached_body(tmp_path) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response())

    request = ModelAuditRequest(
        audit_id="grounded_vlm_composition_layout_audit_oracle",
        metric_id="structured_vlm_composition_layout",
        modality=ModelAuditModality.VLM,
        prompt=V83_GROUNDED_VLM_CRITERION_PROMPTS["composition_layout"],
        case_id="profile-83-wire-golden",
        scene="FINISHED_DECK",
        slides=({"page_number": 1, "text": "Title", "objects": []},),
        context={
            "input_trust": "UNTRUSTED_DATA",
            "sampling_strategy_version": "2.0.0",
        },
        images=(image,),
    )
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        context_cache_enabled=True,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        ModelAuditResponse.from_mapping(provider.audit(request), request=request)

    body = captured["body"]
    assert [item["role"] for item in body["messages"]] == ["system", "user"]
    assert request.prompt.instructions in body["messages"][0]["content"]
    content = body["messages"][1]["content"]
    assert [item["type"] for item in content] == ["text", "text", "image_url"]
    assert content[1]["text"].startswith("RENDERED_SLIDE_PAGE=1")
    assert "cache_control" not in content[2]
    assert body["temperature"] == 0
    assert body["seed"] == 0
    assert body["enable_thinking"] is True


def test_profile84_cache_prefix_is_stable_before_criterion_risk_pages(
    tmp_path,
) -> None:
    images = []
    for page_number in range(1, 5):
        path = tmp_path / f"slide-{page_number}.png"
        path.write_bytes(PNG_1X1)
        images.append(ModelImageInput.from_path(path, page_number=page_number))
    prompt = PromptSpec(
        prompt_id="profile-84-cache-order",
        version="3.0.0",
        instructions="Evaluate exactly one visual criterion.",
    )

    def request(risk_page: int) -> ModelAuditRequest:
        return ModelAuditRequest(
            audit_id="grounded_vlm_composition_layout_audit_oracle",
            metric_id="structured_vlm_composition_layout",
            modality=ModelAuditModality.VLM,
            prompt=prompt,
            case_id="cache-cohort",
            scene="ready_made",
            slides=tuple(
                {"page_number": page_number, "text": "", "objects": []}
                for page_number in range(1, 5)
            ),
            context={
                "qwen_context_cache_profile_enabled": True,
                "cache_prefix_pages": [1, 2],
                "criterion_risk_pages": [risk_page],
            },
            images=(images[0], images[1], images[risk_page - 1]),
        )

    bodies: list[dict[str, Any]] = []

    def fake_urlopen(http_request, *, timeout):
        del timeout
        bodies.append(json.loads(http_request.data.decode("utf-8")))
        return _FakeHttpResponse(_vendor_response())

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        context_cache_enabled=True,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        provider.audit(request(3))
        provider.audit(request(4))

    common_prefix_hashes = [
        hashlib.sha256(
            json.dumps(
                body["messages"][:2],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for body in bodies
    ]
    assert common_prefix_hashes[0] == common_prefix_hashes[1]
    for body, risk_page in zip(bodies, (3, 4), strict=True):
        common_text = json.dumps(body["messages"][1], ensure_ascii=False)
        risk_text = json.dumps(body["messages"][3], ensure_ascii=False)
        assert "RENDERED_SLIDE_PAGE=1" in common_text
        assert "RENDERED_SLIDE_PAGE=2" in common_text
        assert f"CRITERION_RISK_SLIDE_PAGE={risk_page}" not in common_text
        assert f"CRITERION_RISK_SLIDE_PAGE={risk_page}" in risk_text
        assert body["messages"][1]["content"][-1]["cache_control"] == {
            "type": "ephemeral"
        }


def test_qwen_provider_can_reference_a_verified_signed_image_url(tmp_path) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    captured: dict[str, Any] = {}
    resolved: list[str] = []

    def resolver(item: ModelImageInput) -> str:
        resolved.append(item.sha256)
        return f"https://assets.example.com/v1/model-assets/slide/{item.sha256}?token=safe"

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response())

    request = _request(modality=ModelAuditModality.VLM, image=image)
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        image_url_resolver=resolver,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        ModelAuditResponse.from_mapping(provider.audit(request), request=request)

    content = captured["body"]["messages"][1]["content"]
    image_url = next(item["image_url"]["url"] for item in content if item["type"] == "image_url")
    assert image_url.startswith("https://assets.example.com/")
    assert not image_url.startswith("data:")
    assert resolved == [image.sha256]
    assert provider.image_transport_mode == "signed-url"


def test_signed_image_url_transport_preserves_provider_image_size_limit(tmp_path) -> None:
    image_path = tmp_path / "large-slide.png"
    image_path.write_bytes(PNG_1X1 + b"padding")
    image = ModelImageInput.from_path(image_path, page_number=1)
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        max_image_bytes=len(PNG_1X1),
        image_url_resolver=lambda item: f"https://assets.example.com/{item.sha256}",
    )

    _assert_raises(
        QwenModelAuditProviderError,
        lambda: provider.audit(_request(modality=ModelAuditModality.VLM, image=image)),
        contains="exceeds the size limit",
    )


def test_provider_boundary_rejects_disguised_and_trailing_non_image_bytes(
    tmp_path,
) -> None:
    disguised = tmp_path / "disguised-presentation.png"
    disguised.write_bytes(b"PK\x03\x04raw-pptx-must-never-leave-host")
    trailing = tmp_path / "image-with-pptx-trailer.png"
    trailing.write_bytes(PNG_1X1 + b"PK\x03\x04hidden-pptx-trailer")
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )

    for path in (disguised, trailing):
        image = ModelImageInput.from_path(path, page_number=1)
        _assert_raises(
            QwenModelAuditProviderError,
            lambda image=image: provider.audit(
                _request(modality=ModelAuditModality.VLM, image=image)
            ),
            contains="safe raster validation",
        )


def test_model_usage_keeps_legacy_payload_compatible() -> None:
    usage = ModelUsage.from_mapping(
        {"input_tokens": 12, "output_tokens": 3, "cost": 0.0}
    )

    assert usage.image_tokens is None
    assert usage.cached_tokens is None
    assert usage.cache_creation_input_tokens is None
    assert usage.request_bytes is None
    assert usage.cost_known is None
    assert usage.to_mapping() == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cost": 0.0,
    }


def test_qwen_provider_parses_nested_usage_and_measures_request_bytes(
    tmp_path,
) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    vendor = json.loads(json.dumps(_vendor_response()))
    vendor["usage"].update(
        {
            "cost": 0.012,
            "prompt_tokens_details": {
                "image_tokens": 211,
                "cached_tokens": 128,
                "cache_creation_input_tokens": 64,
            },
        }
    )
    captured: dict[str, int] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["request_bytes"] = len(http_request.data)
        return _FakeHttpResponse(vendor)

    request = _request(modality=ModelAuditModality.VLM, image=image)
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.usage.image_tokens == 211
    assert response.usage.cached_tokens == 128
    assert response.usage.cache_creation_input_tokens == 64
    assert response.usage.request_bytes == captured["request_bytes"]
    assert response.usage.cost_known is True
    assert response.evidence[0].payload["adapter_cost_known"] is True


def test_optional_vendor_usage_extensions_fail_open_without_losing_core_result() -> None:
    malformed = json.loads(json.dumps(_vendor_response()))
    malformed["usage"]["prompt_tokens_details"] = "vendor-extension"
    conflicting = json.loads(json.dumps(_vendor_response()))
    conflicting["usage"].update(
        {
            "cached_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 13},
        }
    )
    responses = iter((malformed, conflicting))

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(next(responses))

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    request = _request()
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        first = ModelAuditResponse.from_mapping(provider.audit(request), request=request)
        second = ModelAuditResponse.from_mapping(provider.audit(request), request=request)

    assert first.usage.input_tokens == 321
    assert first.usage.cached_tokens is None
    assert second.usage.input_tokens == 321
    assert second.usage.cached_tokens is None


def test_provider_uses_explicit_transport_timeout() -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del http_request
        captured["timeout"] = timeout
        return _FakeHttpResponse(_vendor_response())

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
        timeout_seconds=37.5,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        provider.audit(_request())

    assert captured["timeout"] == 37.5
    assert provider.timeout_seconds == 37.5
    assert "timeout_seconds=37.5" in repr(provider)


def test_qwen_adapter_drops_invalid_optional_bbox_with_audit_marker() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["bbox"] = [120, 80, 400, 200]
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].bbox is None
    assert response.evidence[0].payload["adapter_sanitized_fields"] == ["bbox"]


def test_qwen_adapter_drops_null_or_blank_optional_ids_with_audit_marker() -> None:
    vendor = json.loads(json.dumps(_vendor_response(model="qwen3.8-flash")))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["object_id"] = ""
    content["evidence"][0]["source_uri"] = None
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_PRIMARY_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].object_id is None
    assert response.evidence[0].source_uri is None
    assert response.evidence[0].payload["adapter_sanitized_fields"] == [
        "object_id",
        "source_uri",
    ]


def test_qwen_adapter_drops_unverifiable_optional_object_id() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["object_id"] = "14"
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].object_id is None
    assert response.evidence[0].payload["adapter_sanitized_fields"] == [
        "object_id"
    ]


def test_qwen_adapter_drops_unverifiable_optional_source_when_page_is_valid() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["source_uri"] = "sha256:not-a-request-source"
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].source_uri is None
    assert response.evidence[0].payload["adapter_sanitized_fields"] == [
        "source_uri"
    ]


def test_qwen_adapter_moves_related_pages_into_evidence_payload() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["related_page_numbers"] = [1]
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].payload["related_page_numbers"] == [1]
    assert response.evidence[0].payload["adapter_sanitized_fields"] == [
        "related_page_numbers->payload"
    ]


def test_qwen_adapter_grounds_summary_from_valid_related_pages() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    finding = content["evidence"][0]
    finding.pop("page_number")
    finding.pop("object_id")
    finding["payload"]["related_page_numbers"] = [1]
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.evidence[0].page_number == 1
    assert response.evidence[0].payload["adapter_sanitized_fields"] == [
        "page_number<-related_page_numbers"
    ]


def test_qwen_adapter_replaces_model_supplied_reserved_adapter_telemetry() -> None:
    vendor = json.loads(json.dumps(_vendor_response()))
    content = json.loads(vendor["choices"][0]["message"]["content"])
    content["evidence"][0]["payload"].update(
        {
            "adapter_retry_count": 99,
            "adapter_retry_reasons": ["FAKE"],
            "adapter_usage_complete": True,
            "adapter_sanitized_fields": ["page_number"],
        }
    )
    vendor["choices"][0]["message"]["content"] = json.dumps(content)

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(vendor)

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)
    detail = response.evidence[0].payload

    assert "adapter_retry_count" not in detail
    assert "adapter_retry_reasons" not in detail
    assert "adapter_usage_complete" not in detail
    assert detail["adapter_sanitized_fields"] == [
        "payload.adapter_retry_count",
        "payload.adapter_retry_reasons",
        "payload.adapter_sanitized_fields",
        "payload.adapter_usage_complete",
    ]


def test_qwen_adapter_retries_invalid_json_and_accumulates_usage() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    invalid["usage"] = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {
            "image_tokens": 10,
            "cached_tokens": 4,
            "cache_creation_input_tokens": 2,
        },
    }
    valid = json.loads(json.dumps(_vendor_response()))
    valid["usage"]["input_tokens_details"] = {
        "image_tokens": 20,
        "cached_tokens": 8,
        "cache_creation_input_tokens": 3,
    }
    responses = iter((invalid, valid))
    calls = 0
    request_bodies: list[dict[str, Any]] = []
    request_sizes: list[int] = []

    def fake_urlopen(http_request, *, timeout):
        del timeout
        nonlocal calls
        calls += 1
        request_bodies.append(json.loads(http_request.data.decode("utf-8")))
        request_sizes.append(len(http_request.data))
        return _FakeHttpResponse(next(responses))

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert calls == 2
    assert response.usage.input_tokens == 421
    assert response.usage.output_tokens == 95
    assert response.usage.total_tokens == 516
    assert response.usage.image_tokens == 30
    assert response.usage.cached_tokens == 12
    assert response.usage.cache_creation_input_tokens == 5
    assert response.usage.request_bytes == sum(request_sizes)
    assert response.usage.cost_known is False
    assert response.evidence[0].payload["adapter_retry_count"] == 1
    assert response.evidence[0].payload["adapter_retry_reasons"] == [
        "JSON_INVALID"
    ]
    assert response.evidence[0].payload["adapter_attempts_with_usage"] == 2
    assert response.evidence[0].payload["adapter_usage_complete"] is True
    assert len(request_bodies[0]["messages"]) == 2
    assert len(request_bodies[1]["messages"]) == 3
    assert "Machine-readable error category: JSON_INVALID" in (
        request_bodies[1]["messages"][2]["content"]
    )
    assert "not-json" not in json.dumps(request_bodies[1], ensure_ascii=False)


def test_qwen_retry_omits_optional_tokens_missing_from_any_outbound_attempt() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    invalid["usage"]["cost"] = 0.01
    valid = json.loads(json.dumps(_vendor_response()))
    valid["usage"].update(
        {
            "cost": 0.02,
            "prompt_tokens_details": {
                "image_tokens": 20,
                "cached_tokens": 8,
                "cache_creation_input_tokens": 3,
            },
        }
    )
    responses = iter((invalid, valid))
    request_sizes: list[int] = []

    def fake_urlopen(http_request, *, timeout):
        del timeout
        request_sizes.append(len(http_request.data))
        return _FakeHttpResponse(next(responses))

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.usage.input_tokens == 642
    assert response.usage.output_tokens == 90
    assert response.usage.cost == 0.03
    assert response.usage.image_tokens is None
    assert response.usage.cached_tokens is None
    assert response.usage.cache_creation_input_tokens is None
    assert response.usage.request_bytes == sum(request_sizes)
    assert response.usage.cost_known is True
    assert response.evidence[0].payload["adapter_usage_complete"] is True


def test_qwen_adapter_retries_ungrounded_structured_evidence() -> None:
    ungrounded = json.loads(json.dumps(_vendor_response()))
    content = json.loads(ungrounded["choices"][0]["message"]["content"])
    content["evidence"][0].pop("page_number")
    content["evidence"][0].pop("object_id")
    ungrounded["choices"][0]["message"]["content"] = json.dumps(content)
    responses = iter((ungrounded, _vendor_response()))
    calls = 0

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        nonlocal calls
        calls += 1
        return _FakeHttpResponse(next(responses))

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert calls == 2
    assert response.evidence[0].payload["adapter_retry_reasons"] == [
        "EVIDENCE_UNGROUNDED"
    ]


def test_qwen_adapter_marks_retry_usage_partial_when_first_usage_is_missing() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    invalid.pop("usage")
    responses = iter((invalid, _vendor_response()))

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(next(responses))

    request = _request()
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    response = ModelAuditResponse.from_mapping(payload, request=request)

    assert response.usage.total_tokens == 366
    assert response.evidence[0].payload["adapter_attempts_with_usage"] == 1
    assert response.evidence[0].payload["adapter_usage_complete"] is False


def test_qwen_adapter_preserves_first_usage_when_retry_transport_fails() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    calls = 0

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeHttpResponse(invalid)
        raise urllib.error.URLError("synthetic retry transport failure")

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        try:
            provider.audit(_request())
        except QwenModelAuditProviderError as exc:
            assert "after structured response retry" in str(exc)
            assert exc.audit_metadata["usage"]["total_tokens"] == 366
            assert exc.audit_metadata["provider_attempts"] == 2
            assert exc.audit_metadata["provider_attempts_with_usage"] == 1
            assert exc.audit_metadata["provider_usage_complete"] is False
        else:
            raise AssertionError("retry transport failure must be reported")


def test_qwen_adapter_preserves_usage_when_retry_model_mismatches() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    mismatched = _vendor_response(model="qwen-max")
    responses = iter((invalid, mismatched))

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(next(responses))

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        try:
            provider.audit(_request())
        except QwenModelAuditProviderError as exc:
            assert "outside the configured audit tier" in str(exc)
            assert exc.audit_metadata["usage"]["total_tokens"] == 732
            assert exc.audit_metadata["provider_attempts_with_usage"] == 2
            assert exc.audit_metadata["provider_usage_complete"] is True
        else:
            raise AssertionError("mismatched retry model must fail")


def test_qwen_adapter_reports_usage_when_bounded_json_retry_fails() -> None:
    invalid = json.loads(json.dumps(_vendor_response()))
    invalid["choices"][0]["message"]["content"] = "not-json"
    calls = 0

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        nonlocal calls
        calls += 1
        return _FakeHttpResponse(invalid)

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        try:
            provider.audit(_request())
        except QwenModelAuditProviderError as exc:
            assert "after retry" in str(exc)
            assert exc.audit_metadata["provider_attempts"] == 2
            assert exc.audit_metadata["provider_retry_reasons"] == [
                "JSON_INVALID",
                "JSON_INVALID",
            ]
            assert exc.audit_metadata["usage"]["total_tokens"] == 732
            assert exc.audit_metadata["provider_attempts_with_usage"] == 2
            assert exc.audit_metadata["provider_usage_complete"] is True
        else:
            raise AssertionError("invalid structured responses must fail")

    assert calls == 2


def test_provider_rejects_actual_model_from_another_configured_tier() -> None:
    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(
            _vendor_response(model="qwen-max-2026-08-01")
        )

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        _assert_raises(
            QwenModelAuditProviderError,
            lambda: provider.audit(_request()),
            contains="outside the configured audit tier",
        )


def test_vlm_images_are_integrity_checked_and_sent_as_data_uris(
    tmp_path,
) -> None:
    image_path = tmp_path / "slide-1.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, *, timeout):
        del timeout
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeHttpResponse(_vendor_response(model="qwen3.8-flash-2026-08-01"))

    request = _request(modality=ModelAuditModality.VLM, image=image)
    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_PRIMARY_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    ModelAuditResponse.from_mapping(payload, request=request)

    body = captured["body"]
    content = body["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1] == {
        "type": "text",
        "text": (
            "RENDERED_SLIDE_PAGE=1. The image immediately following this label is "
            "the rendered pixel evidence for slide 1 only."
        ),
    }
    data_uri = content[2]["image_url"]["url"]
    assert data_uri.startswith("data:image/png;base64,")
    assert str(image_path) not in json.dumps(body, ensure_ascii=False)
    audit_text = content[0]["text"]
    assert image.sha256 in audit_text
    assert '"uri"' not in audit_text


def test_vlm_rejects_changed_image_before_network_call(tmp_path) -> None:
    image_path = tmp_path / "slide-1.png"
    image_path.write_bytes(PNG_1X1)
    image = ModelImageInput.from_path(image_path, page_number=1)
    image_path.write_bytes(PNG_1X1 + b"changed")
    called = False

    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        nonlocal called
        called = True
        return _FakeHttpResponse(_vendor_response())

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_PRIMARY_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        _assert_raises(
            QwenModelAuditProviderError,
            lambda: provider.audit(_request(modality=ModelAuditModality.VLM, image=image)),
            contains="integrity validation",
        )

    assert called is False


def test_http_and_json_failures_do_not_expose_credentials_or_response_content() -> None:
    secret_key = "api-key-that-must-remain-secret"
    secret_body = b'{"message":"private vendor response"}'

    def failing_urlopen(http_request, *, timeout):
        del http_request, timeout
        raise urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(secret_body),
        )

    provider = QwenOpenAICompatibleProvider(
        secret_key,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", failing_urlopen):
        error = _assert_raises(QwenModelAuditProviderError, lambda: provider.audit(_request()))

    message = str(error)
    assert "HTTP status 401" in message
    assert secret_key not in message
    assert "private vendor response" not in message

    invalid_response = lambda *_args, **_kwargs: _FakeHttpResponse(  # noqa: E731
        b'{"secret":"not an envelope"}'
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", invalid_response):
        error = _assert_raises(QwenModelAuditProviderError, lambda: provider.audit(_request()))
    assert "not an envelope" not in str(error)


def test_local_http_mock_exercises_real_wire_request() -> None:
    received: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers["Content-Length"])
            received["path"] = self.path
            received["authorized"] = self.headers.get("Authorization") == "Bearer local-key"
            received["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            response = json.dumps(_vendor_response()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/compatible-mode/v1"
        provider = QwenOpenAICompatibleProvider("local-key", base_url, QWEN_FLASH_MODEL)
        request = _request()
        payload = provider.audit(request)
        ModelAuditResponse.from_mapping(payload, request=request)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert received["path"] == "/compatible-mode/v1/chat/completions"
    assert received["authorized"] is True
    assert received["body"]["stream"] is False


def test_provider_url_and_secret_validation() -> None:
    assert QwenModelAuditProvider is QwenOpenAICompatibleProvider
    _assert_raises(
        ValueError,
        lambda: QwenOpenAICompatibleProvider("", "https://example.com/v1", QWEN_FLASH_MODEL),
        contains="non-blank",
    )
    _assert_raises(
        ValueError,
        lambda: QwenOpenAICompatibleProvider(
            "key",
            "https://user:password@example.com/v1",
            QWEN_FLASH_MODEL,
        ),
        contains="credentials",
    )
    _assert_raises(
        ValueError,
        lambda: QwenOpenAICompatibleProvider(
            "key",
            "http://example.com/v1",
            QWEN_FLASH_MODEL,
        ),
        contains="local development",
    )
    _assert_raises(
        ValueError,
        lambda: QwenOpenAICompatibleProvider(
            "key",
            "https://example.com/v1",
            QWEN_FLASH_MODEL,
            timeout_seconds=0,
        ),
        contains="positive finite",
    )
