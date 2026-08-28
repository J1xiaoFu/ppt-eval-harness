"""Current v8 semantic helpers and atomic scene observations."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

from ppt_eval.adapters.facts import FactVerdict, FactVerificationBundle
from ppt_eval.adapters.pptx import ParsedPresentation
from ppt_eval.domain.enums import SceneType, ScoreRole
from ppt_eval.domain.models import Evidence, OracleResult

from .base import (
    AtomicOracle,
    case_metadata,
    clamp,
    evidence,
    locate_text,
    normalize_text,
    text_tokens,
    token_recall,
)


def _case(context: object) -> object:
    return getattr(context, "case", context)


def _scene(context: object) -> SceneType | None:
    value = getattr(_case(context), "scene", None)
    try:
        return SceneType(value)
    except (TypeError, ValueError):
        return None


def _request(context: object) -> str:
    return str(getattr(_case(context), "request", "") or "").strip()






def _requirements(request: str) -> list[str]:
    result = []
    for part in re.split(r"[\n\r;；。]+", request):
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*", "", part).strip()
        if len(normalize_text(cleaned)) >= 2:
            result.append(cleaned)
    return result or ([request.strip()] if request.strip() else [])






def _requirement_evidence(
    metric_id: str,
    presentation: ParsedPresentation,
    requirement: str,
    index: int,
    score: float,
) -> Evidence:
    tokens = sorted(text_tokens(requirement), key=len, reverse=True)
    page = object_id = bbox = None
    for token in tokens:
        page, object_id, bbox = locate_text(presentation, token)
        if page is not None:
            break
    return evidence(
        metric_id,
        f"requirement-{index}",
        "requirement_match" if score >= 0.45 else "requirement_gap",
        f"Requirement coverage heuristic: {score:.0%}.",
        page_number=page,
        object_id=object_id,
        bbox=bbox,
        payload={"requirement": requirement, "coverage": round(score, 4)},
    )






class AudienceFitOracle(AtomicOracle):
    oracle_id = "audience_fit_oracle"
    metric_id = "audience_fit"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.TEXT_TO_PPT

    def _evaluate(self, context: object) -> OracleResult:
        audience = str(getattr(_case(context), "audience", "") or "").strip()
        if not audience:
            return self.not_applicable("No target audience was supplied.")
        presentation = self.presentation(context)
        text = presentation.all_visible_text
        direct = token_recall(audience, text)
        lowered = audience.lower()
        cue_groups = []
        if any(cue in lowered for cue in ("领导", "高管", "管理", "executive", "board")):
            cue_groups.append(("结论", "价值", "风险", "决策", "roi", "recommendation"))
        if any(cue in lowered for cue in ("技术", "研发", "工程", "technical", "engineer")):
            cue_groups.append(("架构", "实现", "接口", "性能", "architecture", "api"))
        if any(cue in lowered for cue in ("客户", "用户", "customer", "client")):
            cue_groups.append(("价值", "收益", "案例", "方案", "benefit", "solution"))
        cue_hits = 0
        cue_count = 0
        normalized_deck = normalize_text(text)
        for group in cue_groups:
            cue_hits += sum(normalize_text(cue) in normalized_deck for cue in group)
            cue_count += len(group)
        cue_score = cue_hits / cue_count if cue_count else 0.5
        score = clamp(0.35 + 0.35 * direct + 0.30 * cue_score)
        return self.scored(
            score,
            (
                evidence(
                    self.metric_id,
                    "audience-cues",
                    "audience_heuristic",
                    f"Audience-specific lexical cues covered {cue_hits}/{cue_count or 'N/A'} checks.",
                    payload={"audience": audience, "direct_recall": direct, "cue_hits": cue_hits, "cue_count": cue_count},
                ),
            ),
            confidence=0.62,
        )


class FactQualityOracle(AtomicOracle):
    oracle_id = "fact_quality_oracle"
    metric_id = "fact_quality"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.TEXT_TO_PPT

    def _evaluate(self, context: object) -> OracleResult:
        metadata = case_metadata(context)
        bundle_value = metadata.get("fact_verification") or metadata.get("fact_verification_path")
        if bundle_value:
            bundle = FactVerificationBundle.load(bundle_value)
            if not bundle.claims:
                return self.not_applicable("Fact verification bundle contains no material claims.")
            supported = sum(item.verdict == FactVerdict.SUPPORTED for item in bundle.claims)
            contradicted = sum(item.verdict == FactVerdict.CONTRADICTED for item in bundle.claims)
            score = (supported + 0.25 * (len(bundle.claims) - supported - contradicted)) / len(bundle.claims)
            details = []
            for item in bundle.claims:
                source = item.sources[0] if item.sources else None
                details.append(
                    evidence(
                        self.metric_id,
                        item.claim_id,
                        "fact_verification",
                        f"{item.verdict.value}: {item.claim}",
                        source_uri=source.url if source else None,
                        payload={
                            "claim_id": item.claim_id,
                            "verdict": item.verdict.value,
                            "confidence": item.confidence,
                            "quote": source.quote if source else None,
                            "content_sha256": source.content_sha256 if source else None,
                            "captured_at": source.captured_at if source else None,
                            "bundle_id": bundle.bundle_id,
                        },
                    )
                )
            confidence = sum(item.confidence for item in bundle.claims) / len(bundle.claims)
            return self.scored(
                score,
                details,
                confidence=confidence,
                metadata={
                    "bundle_id": bundle.bundle_id,
                    "supported": supported,
                    "contradicted": contradicted,
                    "verifier_version": bundle.verifier_version,
                    "query_policy_version": bundle.query_policy_version,
                },
            )

        facts = metadata.get("verified_facts")
        expected = _fact_strings(facts)
        if not expected:
            return self.not_applicable(
                "No offline verified facts were supplied; this Oracle never performs network retrieval.",
                code="NO_TRUSTED_FACTS",
            )
        presentation = self.presentation(context)
        values = [token_recall(item, presentation.all_visible_text) for item in expected]
        fact_details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(expected, values), start=1)
        )
        return self.scored(sum(values) / len(values), fact_details, confidence=0.92)












def compression_quality_score(ratio: float) -> float:
    """Continuously reward useful compression, with an optimum near 20 percent."""

    value = max(0.0, float(ratio))
    if value <= 0.03:
        return value / 0.03 * 0.50
    if value <= 0.20:
        return 0.50 + (value - 0.03) / 0.17 * 0.50
    if value <= 0.45:
        return 1.00 - (value - 0.20) / 0.25 * 0.15
    return max(0.0, 0.85 * (1.0 - (value - 0.45) / 0.75))
























def _fact_strings(value: object) -> list[str]:
    if isinstance(value, Mapping):
        return [f"{key}: {item}" for key, item in value.items()]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


_LABEL_VALUE = re.compile(
    r"(?P<label>[A-Za-z\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff ]{1,24})\s*[:：]\s*"
    r"(?P<value>[+\-]?\d[\d,.]*(?:%|万|亿|k|m)?)",
    re.IGNORECASE,
)






def _asset_hash(asset: str) -> str | None:
    try:
        path = Path(asset)
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
