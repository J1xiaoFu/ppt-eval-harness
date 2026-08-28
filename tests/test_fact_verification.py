from __future__ import annotations

import hashlib

from ppt_eval.adapters import FactVerdict, FactVerificationBundle, NetworkFactVerifier, SearchHit
from ppt_eval.application import EvaluationContext
from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.oracles.scenarios import FactQualityOracle
from tests.fixtures.pptx_factory import build_pptx


class Provider:
    def search(self, query: str, *, limit: int):
        quote = "官方公告确认该数字。"
        return (
            SearchHit(
                "https://gov.example.cn/notice/1",
                "公告",
                quote,
                "2026-08-26T00:00:00+00:00",
                hashlib.sha256(quote.encode()).hexdigest(),
            ),
            SearchHit(
                "https://untrusted.invalid/post",
                "转载",
                quote,
                "2026-08-26T00:00:00+00:00",
                hashlib.sha256(quote.encode()).hexdigest(),
            ),
        )


def test_network_fact_verifier_filters_hosts_and_builds_snapshot() -> None:
    verifier = NetworkFactVerifier(
        Provider(),
        lambda claim, hits: (FactVerdict.SUPPORTED, 0.95, "official source"),
        allowed_hosts=("gov.example.cn",),
        verifier_version="test-1",
    )
    bundle = verifier.verify(("试点周期缩短35%",), created_at="2026-08-26T00:00:00+00:00")

    assert bundle.claims[0].verdict == FactVerdict.SUPPORTED
    assert len(bundle.claims[0].sources) == 1
    assert bundle.claims[0].sources[0].url.startswith("https://gov.example.cn/")


def test_fact_oracle_consumes_auditable_bundle(tmp_path) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    quote = "官方来源"
    bundle = {
        "bundle_id": "facts-1",
        "created_at": "2026-08-26T00:00:00+00:00",
        "verifier_version": "test-1",
        "claims": [
            {
                "claim_id": "claim-1",
                "claim": "销售额100万元",
                "verdict": "SUPPORTED",
                "confidence": 0.96,
                "sources": [
                    {
                        "url": "https://gov.example.cn/notice/1",
                        "quote": quote,
                        "captured_at": "2026-08-26T00:00:00+00:00",
                        "content_sha256": hashlib.sha256(quote.encode()).hexdigest()
                    }
                ]
            }
        ]
    }
    case = EvalCase(
        case_id="text-facts",
        scene=SceneType.TEXT_TO_PPT,
        pptx_path=str(deck),
        request="制作项目汇报",
        metadata={"fact_verification": bundle},
    )
    context = EvaluationContext(case, default_profile(SceneType.TEXT_TO_PPT))
    result = FactQualityOracle().evaluate(context)

    assert result.normalized_score == 1.0
    assert result.evidence[0].source_uri == "https://gov.example.cn/notice/1"
    assert result.metadata["bundle_id"] == "facts-1"


def test_fact_bundle_rejects_non_https_evidence() -> None:
    payload = {
        "bundle_id": "facts-bad",
        "created_at": "2026-08-26T00:00:00+00:00",
        "verifier_version": "test",
        "claims": [{
            "claim_id": "c1",
            "claim": "claim",
            "verdict": "SUPPORTED",
            "confidence": 1.0,
            "sources": [{
                "url": "http://example.com",
                "quote": "quote",
                "captured_at": "2026-08-26T00:00:00+00:00",
                "content_sha256": "0" * 64
            }]
        }]
    }
    try:
        FactVerificationBundle.from_mapping(payload)
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("expected non-HTTPS evidence to be rejected")
