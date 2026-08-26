from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse


class FactVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class FactSourceSnapshot:
    url: str
    quote: str
    captured_at: str
    content_sha256: str
    title: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("fact evidence URL must use HTTPS and contain a host")
        if not self.quote.strip():
            raise ValueError("fact evidence quote must not be blank")
        if len(self.content_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.content_sha256.lower()):
            raise ValueError("content_sha256 must be a 64-character hexadecimal digest")
        try:
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("captured_at must be an ISO-8601 timestamp") from exc


@dataclass(frozen=True, slots=True)
class FactVerification:
    claim_id: str
    claim: str
    verdict: FactVerdict
    confidence: float
    sources: tuple[FactSourceSnapshot, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.claim_id or not self.claim.strip():
            raise ValueError("fact claim id and text must not be blank")
        if not isinstance(self.verdict, FactVerdict):
            object.__setattr__(self, "verdict", FactVerdict(self.verdict))
        if not 0 <= self.confidence <= 1:
            raise ValueError("fact confidence must be between zero and one")
        if self.verdict != FactVerdict.INSUFFICIENT_EVIDENCE and not self.sources:
            raise ValueError("supported/contradicted facts require source snapshots")


@dataclass(frozen=True, slots=True)
class FactVerificationBundle:
    bundle_id: str
    created_at: str
    verifier_version: str
    claims: tuple[FactVerification, ...]
    query_policy_version: str = "1.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FactVerificationBundle":
        claims = []
        for item in payload.get("claims", ()):
            sources = tuple(FactSourceSnapshot(**source) for source in item.get("sources", ()))
            claims.append(
                FactVerification(
                    claim_id=str(item["claim_id"]),
                    claim=str(item["claim"]),
                    verdict=FactVerdict(item["verdict"]),
                    confidence=float(item["confidence"]),
                    sources=sources,
                    rationale=str(item.get("rationale", "")),
                )
            )
        return cls(
            bundle_id=str(payload["bundle_id"]),
            created_at=str(payload["created_at"]),
            verifier_version=str(payload["verifier_version"]),
            claims=tuple(claims),
            query_policy_version=str(payload.get("query_policy_version", "1.0")),
            metadata=dict(payload.get("metadata", {})),
        )

    @classmethod
    def load(cls, value: str | Path | Mapping[str, Any]) -> "FactVerificationBundle":
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
        return cls.from_mapping(payload)


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    quote: str
    captured_at: str
    content_sha256: str


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> Sequence[SearchHit]: ...


class NetworkFactVerifier:
    """Provider-neutral online verifier that produces an immutable evidence bundle.

    The provider owns retrieval; the adjudicator owns entailment. The Harness only
    accepts the resulting snapshot, so model/network failures remain auditable and
    cannot silently change historical scores.
    """

    def __init__(
        self,
        provider: SearchProvider,
        adjudicator: Callable[[str, Sequence[SearchHit]], tuple[FactVerdict, float, str]],
        *,
        allowed_hosts: Sequence[str],
        verifier_version: str,
        query_policy_version: str = "1.0",
    ) -> None:
        self.provider = provider
        self.adjudicator = adjudicator
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.verifier_version = verifier_version
        self.query_policy_version = query_policy_version

    def verify(self, claims: Sequence[str], *, created_at: str) -> FactVerificationBundle:
        results = []
        for index, claim in enumerate(claims, start=1):
            hits = tuple(hit for hit in self.provider.search(claim, limit=8) if self._allowed(hit.url))
            if hits:
                verdict, confidence, rationale = self.adjudicator(claim, hits)
            else:
                verdict, confidence, rationale = FactVerdict.INSUFFICIENT_EVIDENCE, 0.0, "no allowlisted evidence"
            sources = tuple(
                FactSourceSnapshot(hit.url, hit.quote, hit.captured_at, hit.content_sha256, hit.title)
                for hit in hits
            )
            results.append(FactVerification(f"claim-{index}", claim, verdict, confidence, sources, rationale))
        digest = hashlib.sha256(
            json.dumps(
                [(item.claim, item.verdict.value, item.confidence, [source.content_sha256 for source in item.sources]) for item in results],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:20]
        return FactVerificationBundle(
            bundle_id=f"facts-{digest}",
            created_at=created_at,
            verifier_version=self.verifier_version,
            claims=tuple(results),
            query_policy_version=self.query_policy_version,
            metadata={"allowed_hosts": sorted(self.allowed_hosts)},
        )

    def _allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts)

