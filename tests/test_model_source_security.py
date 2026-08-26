from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from ppt_eval.adapters import ModelAuditRequest, PptxAdapter, PromptSpec
from ppt_eval.application.oracle import EvaluationContext
from ppt_eval.domain import EvalCase, EvalProfile, MetricStatus, SceneType
from ppt_eval.infrastructure import (
    QWEN_FLASH_MODEL,
    QwenModelAuditProviderError,
    QwenOpenAICompatibleProvider,
    qwen_model_audits,
)
from ppt_eval.oracles import ModelSourceAccessPolicy
from ppt_eval.oracles.model_audits import (
    MODEL_AUDIT_COMPOSITE_ID,
    LlmScenarioComplianceAuditOracle,
)
from ppt_eval.runtime import LocalEvaluationRuntime, build_runtime_from_environment
from tests.fixtures.pptx_factory import build_pptx


class RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[ModelAuditRequest] = []

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        return {
            "score": 0.8,
            "confidence": 0.9,
            "model": {"provider": "fake", "model_id": "test", "version": "1"},
            "prompt": dict(request.prompt.reference()),
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0},
            "evidence": [
                {
                    "evidence_id": "source-security-page-1",
                    "kind": "model_audit_finding",
                    "message": "Grounded on page one.",
                    "page_number": 1,
                    "payload": {},
                }
            ],
        }


def _context(deck: Path, source_materials: tuple[str, ...]) -> EvaluationContext:
    scene = SceneType.PROJECT_SUMMARY
    return EvaluationContext(
        case=EvalCase(
            case_id="source-security",
            scene=scene,
            pptx_path=str(deck),
            source_materials=source_materials,
        ),
        profile=EvalProfile.default(scene),
    )


def test_inline_source_text_remains_available_without_file_roots(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    provider = RecordingProvider()
    result = LlmScenarioComplianceAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_context(deck, ("Revenue increased to 120 in Q2.",)))

    assert result.metric_status == MetricStatus.SCORED
    assert len(provider.requests) == 1
    context = provider.requests[0].context
    assert "Revenue increased to 120 in Q2." in context["source_material"]
    assert context["source_uris"] == ["source:inline:1"]
    assert context["source_access"] == {
        "inline_count": 1,
        "file_count": 0,
        "blocked_count": 0,
    }


def test_default_policy_blocks_arbitrary_existing_file_without_network(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    secret = "sk-arbitrary-local-file-must-not-leave-host"
    local_file = tmp_path / "private.txt"
    local_file.write_text(secret, encoding="utf-8")
    provider = RecordingProvider()

    result = LlmScenarioComplianceAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(_context(deck, (str(local_file),)))

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["reason_code"] == "MODEL_SOURCE_ACCESS_DENIED"
    assert result.metadata["source_access"]["blocked_count"] == 1
    assert provider.requests == []
    serialized_result = repr(result)
    assert secret not in serialized_result
    assert str(local_file) not in serialized_result


def test_allowed_root_file_is_read_with_only_an_opaque_remote_uri(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    source_root = tmp_path / "approved-sources"
    source_root.mkdir()
    source_file = source_root / "facts.txt"
    source_file.write_text("Approved source fact: 42.", encoding="utf-8")
    provider = RecordingProvider()
    policy = ModelSourceAccessPolicy(allowed_roots=(source_root,))

    result = LlmScenarioComplianceAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
        source_access_policy=policy,
    ).evaluate(_context(deck, (str(source_file),)))

    assert result.metric_status == MetricStatus.SCORED
    request_context = provider.requests[0].context
    assert "Approved source fact: 42." in request_context["source_material"]
    assert request_context["source_uris"][0].startswith("source:file:1:")
    assert str(source_file) not in repr(request_context)
    assert str(source_root) not in repr(request_context)


def test_allowed_root_rejects_path_traversal_outside_root(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    source_root = tmp_path / "approved"
    source_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = "traversal-secret-must-not-leave-host"
    outside_file = outside / "facts.txt"
    outside_file.write_text(secret, encoding="utf-8")
    traversal = source_root / ".." / "outside" / "facts.txt"
    provider = RecordingProvider()

    result = LlmScenarioComplianceAuditOracle(
        provider,
        PptxAdapter(backend="ooxml"),
        source_access_policy=ModelSourceAccessPolicy(allowed_roots=(source_root,)),
    ).evaluate(_context(deck, (str(traversal),)))

    assert result.metric_status == MetricStatus.NA
    assert result.metadata["reason_code"] == "MODEL_SOURCE_ACCESS_DENIED"
    assert provider.requests == []
    assert secret not in repr(result)
    assert str(traversal) not in repr(result)


def test_sensitive_api_and_env_files_stay_blocked_inside_broad_root(tmp_path) -> None:
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    api_key = api_dir / "qwen3.7_flash_api.txt"
    api_key.write_text("sk-sensitive-api-key-value", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("DASHSCOPE_API_KEY=sk-sensitive", encoding="utf-8")
    policy = ModelSourceAccessPolicy(allowed_roots=(tmp_path,))

    prepared = policy.prepare(
        (str(api_key), str(env_file)),
        maximum_bytes=100_000,
    )

    assert prepared.text == ""
    assert prepared.source_uris == ()
    assert prepared.blocked_count == 2


def test_proc_self_environ_is_denied_even_with_filesystem_root_allowed() -> None:
    environ = Path("/proc/self/environ")
    if os.name == "nt" or not environ.is_file():
        return

    prepared = ModelSourceAccessPolicy(allowed_roots=(Path("/"),)).prepare(
        (str(environ),),
        maximum_bytes=100_000,
    )

    assert prepared.text == ""
    assert prepared.source_uris == ()
    assert prepared.blocked_count == 1


def test_runtime_composition_shares_explicit_policy_across_flash_and_plus(tmp_path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    provider = RecordingProvider()
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        llm_provider=provider,
        advanced_llm_provider=provider,
        model_source_roots=(source_root,),
    )

    baseline = runtime.registry.get(MODEL_AUDIT_COMPOSITE_ID)
    baseline_scenario = baseline.children[-1]
    advanced = runtime.advanced_model_review

    assert advanced is not None
    assert runtime.model_source_access_policy.allowed_roots == (source_root.resolve(),)
    assert baseline_scenario.source_access_policy is runtime.model_source_access_policy
    assert advanced.children[-1].source_access_policy is runtime.model_source_access_policy


def test_environment_factory_parses_roots_and_protects_custom_key_file(tmp_path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    key_file = tmp_path / "vault" / "runtime-value.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-custom-runtime-secret-value", encoding="utf-8")
    runtime = build_runtime_from_environment(
        tmp_path / "var",
        environment={
            "PPT_EVAL_DASHSCOPE_API_KEY_FILE": str(key_file),
            "PPT_EVAL_MODEL_SOURCE_ROOTS": "sources",
        },
        workspace_root=tmp_path,
    )

    policy = runtime.model_source_access_policy
    assert policy.allowed_roots == (source_root.resolve(),)
    prepared = policy.prepare((str(key_file),), maximum_bytes=100_000)
    assert prepared.text == ""
    assert prepared.blocked_count == 1


def test_qwen_provider_rejects_runtime_key_and_absolute_path_before_network(tmp_path) -> None:
    secret = "sk-runtime-credential-never-in-body"
    provider = QwenOpenAICompatibleProvider(
        secret,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        QWEN_FLASH_MODEL,
    )
    base_request = ModelAuditRequest(
        audit_id="source-security",
        metric_id="llm_scenario_compliance_audit",
        modality="LLM",
        prompt=PromptSpec(
            prompt_id="source-security",
            version="1",
            instructions="Return a grounded JSON audit.",
        ),
        case_id="case-1",
        scene="PROJECT_SUMMARY",
        slides=({"page_number": 1, "text": "Summary", "objects": ()},),
        context={"source_material": secret},
    )
    called = False

    def unexpected_network(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    with patch.object(qwen_model_audits.urllib.request, "urlopen", unexpected_network):
        try:
            provider.audit(base_request)
        except QwenModelAuditProviderError as exc:
            assert "protected runtime credential" in str(exc)
            assert secret not in str(exc)
        else:
            raise AssertionError("runtime key in evidence should be rejected")
    assert called is False

    path_request = ModelAuditRequest(
        audit_id=base_request.audit_id,
        metric_id=base_request.metric_id,
        modality=base_request.modality,
        prompt=base_request.prompt,
        case_id=base_request.case_id,
        scene=base_request.scene,
        slides=base_request.slides,
        context={"source_uris": [str(tmp_path / "private.txt")]},
    )
    with patch.object(qwen_model_audits.urllib.request, "urlopen", unexpected_network):
        try:
            provider.audit(path_request)
        except QwenModelAuditProviderError as exc:
            assert "unsanitized local path" in str(exc)
            assert str(tmp_path) not in str(exc)
        else:
            raise AssertionError("absolute local path should be rejected")
    assert called is False
