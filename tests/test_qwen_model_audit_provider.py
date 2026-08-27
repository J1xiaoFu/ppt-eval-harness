from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any, Callable, Mapping
from unittest.mock import patch

from ppt_eval.adapters import (
    ModelAuditModality,
    ModelAuditRequest,
    ModelAuditResponse,
    ModelImageInput,
    PromptSpec,
)
from ppt_eval.infrastructure import (
    QWEN_ADVANCED_MODEL,
    QWEN_FLASH_MODEL,
    QWEN_LEGACY_PLUS_MODEL,
    QwenModelAuditProvider,
    QwenModelAuditProviderError,
    QwenOpenAICompatibleProvider,
    qwen_model_audits,
)
from tests.fixtures.pptx_factory import PNG_1X1


def _request(
    *,
    modality: ModelAuditModality = ModelAuditModality.LLM,
    image: ModelImageInput | None = None,
) -> ModelAuditRequest:
    return ModelAuditRequest(
        audit_id="audit-content",
        metric_id="llm_content_quality_audit",
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
    model: str = "qwen3.7-flash-2026-08-01",
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
    assert response.model.model_id == "qwen3.7-flash-2026-08-01"
    assert response.model.version == "fp-qwen-20260801"
    assert response.usage.input_tokens == 321
    assert response.usage.output_tokens == 45
    assert response.usage.cost == 0.0
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private chain of thought" not in serialized
    assert "fake-api-key" not in repr(provider)


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
        QWEN_ADVANCED_MODEL,
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


def test_provider_rejects_actual_model_from_another_configured_tier() -> None:
    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(
            _vendor_response(model="qwen3.8-flash-2026-08-01")
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


def test_provider_can_explicitly_replay_legacy_qwen37_plus() -> None:
    def fake_urlopen(http_request, *, timeout):
        del http_request, timeout
        return _FakeHttpResponse(
            _vendor_response(model="qwen3.7-plus-2026-08-01")
        )

    provider = QwenOpenAICompatibleProvider(
        "fake-api-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_LEGACY_PLUS_MODEL,
    )
    request = _request()
    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)

    response = ModelAuditResponse.from_mapping(payload, request=request)
    assert response.model.model_id == "qwen3.7-plus-2026-08-01"


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
        QWEN_ADVANCED_MODEL,
    )

    with patch.object(qwen_model_audits.urllib.request, "urlopen", fake_urlopen):
        payload = provider.audit(request)
    ModelAuditResponse.from_mapping(payload, request=request)

    body = captured["body"]
    content = body["messages"][1]["content"]
    assert isinstance(content, list)
    data_uri = content[1]["image_url"]["url"]
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
        QWEN_ADVANCED_MODEL,
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
