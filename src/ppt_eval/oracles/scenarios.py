"""Scenario-specific deterministic Oracles with explicit N/A semantics."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping, Sequence

from ppt_eval.adapters.facts import FactVerdict, FactVerificationBundle
from ppt_eval.adapters.pptx import ParsedPresentation, SlideObject
from ppt_eval.domain.enums import SceneType, ScoreRole
from ppt_eval.domain.models import OracleResult

from .base import (
    AtomicOracle,
    CompositeOracle,
    case_metadata,
    clamp,
    evidence,
    locate_text,
    normalize_text,
    read_materials,
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


def _source_materials(context: object) -> tuple[str, ...]:
    return tuple(str(value) for value in getattr(_case(context), "source_materials", ()) if str(value))


def _assets(context: object) -> tuple[str, ...]:
    return tuple(str(value) for value in getattr(_case(context), "assets", ()) if str(value))


def _requirements(request: str) -> list[str]:
    result = []
    for part in re.split(r"[\n\r;；。]+", request):
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*", "", part).strip()
        if len(normalize_text(cleaned)) >= 2:
            result.append(cleaned)
    return result or ([request.strip()] if request.strip() else [])


def _critical_requirements(request: str) -> list[str]:
    markers = ("必须", "务必", "不得", "禁止", "不可", "must", "required", "never", "do not")
    return [item for item in _requirements(request) if any(marker in item.lower() for marker in markers)]


def _requirement_score(requirement: str, deck_text: str) -> float:
    subject = re.sub(
        r"\b(?:must|required|never|do\s+not|include|show|use)\b",
        " ",
        requirement,
        flags=re.IGNORECASE,
    )
    subject = re.sub(r"必须|务必|不得|禁止|不可|应当|需要|请|包含|展示|使用|加入|提供", "", subject)
    return token_recall(subject.strip() or requirement, deck_text)


def _requirement_evidence(
    metric_id: str,
    presentation: ParsedPresentation,
    requirement: str,
    index: int,
    score: float,
) -> object:
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


class CriticalInstructionComplianceOracle(AtomicOracle):
    oracle_id = "critical_instruction_compliance_oracle"
    metric_id = "critical_instruction_compliance"
    score_role = ScoreRole.SCENE_MULTIPLIER

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.TEXT_TO_PPT

    def _evaluate(self, context: object) -> OracleResult:
        request = _request(context)
        if not request:
            return self.not_applicable("No generation request was supplied for instruction evaluation.")
        critical = _critical_requirements(request)
        if not critical:
            return self.multiplied(
                1.0,
                (evidence(self.metric_id, "none-declared", "scope", "No hard instruction was declared."),),
                raw_value="no_hard_instructions",
            )
        presentation = self.presentation(context)
        deck_text = presentation.all_visible_text
        misses = []
        details = []
        for index, requirement in enumerate(critical, start=1):
            lowered = requirement.lower()
            negative = any(marker in lowered for marker in ("不得", "禁止", "不可", "never", "do not"))
            coverage = _requirement_score(requirement, deck_text)
            failed = coverage >= 0.55 if negative else coverage < 0.45
            if failed:
                misses.append(requirement)
            details.append(_requirement_evidence(self.metric_id, presentation, requirement, index, coverage))
        ratio = len(misses) / len(critical)
        multiplier = 1.0 if not misses else 0.5 if ratio < 0.5 else 0.0
        return self.multiplied(
            multiplier,
            details,
            raw_value=len(misses),
            confidence=0.90,
            metadata={"critical_requirements": len(critical), "violations": len(misses)},
        )


class InstructionCoverageOracle(AtomicOracle):
    oracle_id = "instruction_coverage_oracle"
    metric_id = "instruction_coverage"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.TEXT_TO_PPT

    def _evaluate(self, context: object) -> OracleResult:
        request = _request(context)
        if not request:
            return self.not_applicable("No generation request was supplied for coverage evaluation.")
        presentation = self.presentation(context)
        requirements = _requirements(request)
        values = [_requirement_score(item, presentation.all_visible_text) for item in requirements]
        score = sum(values) / len(values) if values else 0.0
        details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(requirements, values), start=1)
        )
        return self.scored(
            score,
            details,
            confidence=0.78,
            raw_value=score,
            metadata={"requirements": len(requirements), "matched": sum(value >= 0.45 for value in values)},
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
        details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(expected, values), start=1)
        )
        return self.scored(sum(values) / len(values), details, confidence=0.92)


class CriticalSourceConsistencyOracle(AtomicOracle):
    oracle_id = "critical_source_consistency_oracle"
    metric_id = "critical_source_consistency"
    score_role = ScoreRole.SCENE_MULTIPLIER

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        sources = _source_materials(context)
        source_text = read_materials(sources)
        if not source_text:
            return self.not_applicable("No readable project source material was supplied.")
        presentation = self.presentation(context)
        critical_facts = _fact_strings(case_metadata(context).get("critical_facts"))
        if critical_facts:
            missed = [item for item in critical_facts if token_recall(item, presentation.all_visible_text) < 0.65]
            details = tuple(
                _requirement_evidence(
                    self.metric_id,
                    presentation,
                    item,
                    index,
                    token_recall(item, presentation.all_visible_text),
                )
                for index, item in enumerate(critical_facts, start=1)
            )
            multiplier = 1.0 if not missed else 0.5 if len(missed) < len(critical_facts) / 2 else 0.0
            return self.multiplied(multiplier, details, raw_value=len(missed), confidence=0.96)

        conflicts = _label_value_conflicts(source_text, presentation)
        details = tuple(
            evidence(
                self.metric_id,
                f"conflict-{index}",
                "source_conflict",
                f"Deck value '{deck_value}' conflicts with source value '{source_value}' for '{label}'.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={"label": label, "source_value": source_value, "deck_value": deck_value},
            )
            for index, (label, source_value, deck_value, page, item) in enumerate(conflicts, start=1)
        )
        return self.multiplied(
            0.5 if conflicts else 1.0,
            details
            or (
                evidence(
                    self.metric_id,
                    "no-conflict",
                    "source_summary",
                    "No high-precision label/value conflict was found.",
                ),
            ),
            raw_value=len(conflicts),
            confidence=0.95,
        )


class SourceFaithfulnessOracle(AtomicOracle):
    oracle_id = "source_faithfulness_oracle"
    metric_id = "source_faithfulness"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        source_text = read_materials(_source_materials(context))
        if not source_text:
            return self.not_applicable("No readable project source material was supplied.")
        presentation = self.presentation(context)
        deck_tokens = text_tokens(presentation.all_visible_text)
        source_tokens = text_tokens(source_text)
        if not deck_tokens:
            return self.scored(0.0, raw_value=0)
        supported = deck_tokens & source_tokens
        precision = len(supported) / len(deck_tokens)
        return self.scored(
            precision,
            (
                evidence(
                    self.metric_id,
                    "lexical-support",
                    "source_alignment",
                    f"{len(supported)}/{len(deck_tokens)} deck content tokens are supported by source tokens.",
                    payload={"supported_tokens": len(supported), "deck_tokens": len(deck_tokens)},
                ),
            ),
            confidence=0.68,
        )


class KeyPointRecallOracle(AtomicOracle):
    oracle_id = "key_point_recall_oracle"
    metric_id = "key_point_recall"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        source_text = read_materials(_source_materials(context))
        if not source_text:
            return self.not_applicable("No readable project source material was supplied.")
        configured = case_metadata(context).get("key_points")
        points = [str(item) for item in configured] if isinstance(configured, (list, tuple)) else []
        if not points:
            points = [
                item.strip()
                for item in re.split(r"[\n。！？!?]+", source_text)
                if 8 <= len(item.strip()) <= 180
            ][:20]
        if not points:
            return self.not_applicable("The supplied source contains no extractable key points.")
        presentation = self.presentation(context)
        values = [token_recall(item, presentation.all_visible_text) for item in points]
        details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(points, values), start=1)
        )
        return self.scored(sum(values) / len(values), details, confidence=0.74)


class NumericAccuracyOracle(AtomicOracle):
    oracle_id = "numeric_accuracy_oracle"
    metric_id = "numeric_accuracy"
    score_role = ScoreRole.SCENE_ADDITIVE

    _NUMBER = re.compile(r"(?<![\w])[-+]?\d[\d,.]*(?:%|万|亿|k|m)?", re.IGNORECASE)

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        source_text = read_materials(_source_materials(context))
        if not source_text:
            return self.not_applicable("No readable project source material was supplied.")
        presentation = self.presentation(context)
        source_numbers = {_canonical_number(value) for value in self._NUMBER.findall(source_text)}
        deck_items = []
        for slide in presentation.slides:
            for item in slide.visible_objects:
                deck_items.extend((value, slide.page_number, item) for value in self._NUMBER.findall(item.text))
        if not deck_items:
            return self.scored(
                1.0,
                (evidence(self.metric_id, "no-numbers", "scope", "Deck contains no explicit numeric claims."),),
                raw_value=0,
            )
        unsupported = [row for row in deck_items if _canonical_number(row[0]) not in source_numbers]
        score = 1.0 - len(unsupported) / len(deck_items)
        details = tuple(
            evidence(
                self.metric_id,
                f"unsupported-{page}-{item.object_id}-{index}",
                "unsupported_number",
                f"Numeric claim '{value}' was not found in supplied source material.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={"value": value},
            )
            for index, (value, page, item) in enumerate(unsupported[:20], start=1)
        )
        return self.scored(
            score,
            details,
            raw_value=len(unsupported),
            confidence=0.91,
            metadata={"numeric_claims": len(deck_items), "unsupported": len(unsupported)},
        )


class CompressionQualityOracle(AtomicOracle):
    oracle_id = "compression_quality_oracle"
    metric_id = "compression_quality"
    score_role = ScoreRole.SCENE_ADDITIVE
    version = "1.1.0"

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        source_text = read_materials(_source_materials(context))
        if not source_text:
            return self.not_applicable("No readable project source material was supplied.")
        presentation = self.presentation(context)
        ratio = len(normalize_text(presentation.all_visible_text)) / max(1, len(normalize_text(source_text)))
        score = compression_quality_score(ratio)
        return self.scored(
            score,
            (
                evidence(
                    self.metric_id,
                    "compression-ratio",
                    "compression",
                    f"Deck/source normalized character ratio is {ratio:.1%}.",
                    payload={"ratio": ratio},
                ),
            ),
            confidence=0.70,
            raw_value=ratio,
            metadata={
                "minimum_useful_ratio": 0.03,
                "target_ratio": 0.20,
                "maximum_concise_ratio": 0.45,
                "zero_score_ratio": 1.20,
            },
        )


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


class TraceabilityOracle(AtomicOracle):
    oracle_id = "traceability_oracle"
    metric_id = "traceability"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.PROJECT_SUMMARY

    def _evaluate(self, context: object) -> OracleResult:
        sources = _source_materials(context)
        if not sources:
            return self.not_applicable("No project source material was supplied.")
        presentation = self.presentation(context)
        deck = (presentation.all_visible_text + "\n" + "\n".join(slide.notes_text for slide in presentation.slides)).lower()
        cited = 0
        details = []
        for index, source in enumerate(sources, start=1):
            label = Path(source).name if _looks_like_path(source) else ""
            candidates = [label, source] if label else []
            hit = any(candidate and candidate.lower() in deck for candidate in candidates)
            cited += hit
            details.append(
                evidence(
                    self.metric_id,
                    f"source-{index}",
                    "source_reference" if hit else "missing_source_reference",
                    "Source is explicitly referenced in deck/notes." if hit else "Source has no explicit deck/notes reference.",
                    source_uri=source if _looks_like_path(source) else None,
                    payload={"source_index": index},
                )
            )
        return self.scored(cited / len(sources), details, raw_value=cited, confidence=0.88)


class RequiredAssetComplianceOracle(AtomicOracle):
    oracle_id = "required_asset_compliance_oracle"
    metric_id = "required_asset_compliance"
    score_role = ScoreRole.SCENE_MULTIPLIER

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        configured = case_metadata(context).get("required_assets")
        required = tuple(str(item) for item in configured) if isinstance(configured, (list, tuple)) else ()
        if not required:
            return self.multiplied(
                1.0,
                (evidence(self.metric_id, "none-declared", "scope", "No asset was declared hard-required."),),
                raw_value="no_hard_required_assets",
            )
        presentation = self.presentation(context)
        matches = _match_assets(required, presentation)
        missing = [asset for asset, match in matches.items() if match is None]
        details = tuple(_asset_evidence(self.metric_id, asset, match, index) for index, (asset, match) in enumerate(matches.items(), 1))
        ratio = len(missing) / len(required)
        multiplier = 1.0 if not missing else 0.5 if ratio < 0.5 else 0.0
        return self.multiplied(multiplier, details, raw_value=len(missing), confidence=0.99)


class CriticalChartDataAccuracyOracle(AtomicOracle):
    oracle_id = "critical_chart_data_accuracy_oracle"
    metric_id = "critical_chart_data_accuracy"
    score_role = ScoreRole.SCENE_MULTIPLIER

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        facts = _fact_strings(case_metadata(context).get("critical_chart_values"))
        if not facts:
            return self.multiplied(
                1.0,
                (evidence(self.metric_id, "none-declared", "scope", "No critical chart value was declared."),),
                raw_value="no_critical_chart_values",
            )
        presentation = self.presentation(context)
        values = [token_recall(item, presentation.all_visible_text) for item in facts]
        misses = sum(value < 0.70 for value in values)
        multiplier = 1.0 if not misses else 0.5 if misses < len(facts) / 2 else 0.0
        details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(facts, values), 1)
        )
        return self.multiplied(multiplier, details, raw_value=misses, confidence=0.97)


class AssetComplianceOracle(AtomicOracle):
    oracle_id = "asset_compliance_oracle"
    metric_id = "asset_compliance"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        assets = _assets(context)
        if not assets:
            return self.not_applicable("No candidate asset manifest was supplied.")
        presentation = self.presentation(context)
        matches = _match_assets(assets, presentation)
        score = sum(match is not None for match in matches.values()) / len(matches)
        details = tuple(_asset_evidence(self.metric_id, asset, match, index) for index, (asset, match) in enumerate(matches.items(), 1))
        return self.scored(score, details, raw_value=sum(match is not None for match in matches.values()), confidence=0.99)


class AssetPresentationOracle(AtomicOracle):
    oracle_id = "asset_presentation_oracle"
    metric_id = "asset_presentation"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        assets = _assets(context)
        if not assets:
            return self.not_applicable("No candidate asset manifest was supplied.")
        presentation = self.presentation(context)
        matches = _match_assets(assets, presentation)
        found = [match for match in matches.values() if match is not None]
        if not found:
            return self.scored(0.0, tuple(_asset_evidence(self.metric_id, asset, match, index) for index, (asset, match) in enumerate(matches.items(), 1)))
        local = [clamp((item.bbox.area - 0.01) / 0.08) for _, item in found]
        details = tuple(
            evidence(
                self.metric_id,
                f"asset-presentation-{index}",
                "asset_layout",
                f"Matched asset occupies {item.bbox.area:.1%} of the slide.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
                payload={"slide_area_ratio": item.bbox.area},
            )
            for index, (page, item) in enumerate(found, 1)
        )
        return self.scored(sum(local) / len(local), details, confidence=0.82)


class CropClarityOracle(AtomicOracle):
    oracle_id = "crop_clarity_oracle"
    metric_id = "crop_clarity"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        images = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind == "picture"
        ]
        if not images:
            return self.not_applicable("No embedded picture object is available for crop evaluation.")
        values = []
        details = []
        for page, item in images:
            crop = item.metadata.get("crop")
            crop_amount = sum(max(0.0, float(value)) for value in crop) if isinstance(crop, (tuple, list)) else 0.0
            aspect = item.bbox.width / max(1e-9, item.bbox.height)
            score = 1.0 - min(0.55, crop_amount * 0.8)
            if item.bbox.area < 0.0025 or aspect > 8 or aspect < 0.125:
                score -= 0.30
            values.append(clamp(score))
            if score < 0.70:
                details.append(
                    evidence(
                        self.metric_id,
                        f"crop-{page}-{item.object_id}",
                        "crop_risk",
                        "Picture crop/geometry may make the visual difficult to inspect.",
                        page_number=page,
                        object_id=item.object_id,
                        bbox=item.bbox.as_tuple(),
                        payload={"crop_amount": crop_amount, "aspect_ratio": aspect},
                    )
                )
        return self.scored(sum(values) / len(values), details, confidence=0.76)


class ChartDataAccuracyOracle(AtomicOracle):
    oracle_id = "chart_data_accuracy_oracle"
    metric_id = "chart_data_accuracy"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        expectations = _fact_strings(case_metadata(context).get("chart_expectations"))
        if not expectations:
            return self.not_applicable("No chart ground-truth values were supplied.")
        presentation = self.presentation(context)
        chart_count = sum(item.kind == "chart" for slide in presentation.slides for item in slide.visible_objects)
        if chart_count == 0:
            return self.scored(
                0.0,
                (evidence(self.metric_id, "no-chart", "chart", "No editable chart object was found."),),
                raw_value=0,
                confidence=0.99,
            )
        values = [token_recall(item, presentation.all_visible_text) for item in expectations]
        details = tuple(
            _requirement_evidence(self.metric_id, presentation, item, index, value)
            for index, (item, value) in enumerate(zip(expectations, values), 1)
        )
        return self.scored(sum(values) / len(values), details, confidence=0.92, metadata={"chart_objects": chart_count})


class MediaAvailabilityOracle(AtomicOracle):
    oracle_id = "media_availability_oracle"
    metric_id = "media_availability"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        return super().supports(context) and _scene(context) == SceneType.MULTIMODAL

    def _evaluate(self, context: object) -> OracleResult:
        presentation = self.presentation(context)
        media = [
            (slide.page_number, item)
            for slide in presentation.slides
            for item in slide.visible_objects
            if item.kind in {"picture", "media", "linked_picture"}
        ]
        if not media:
            return self.not_applicable("No media object is present in the presentation.")
        available = [(page, item) for page, item in media if item.media_sha256]
        missing = [(page, item) for page, item in media if not item.media_sha256]
        details = tuple(
            evidence(
                self.metric_id,
                f"missing-{page}-{item.object_id}",
                "media_unavailable",
                "Media relationship has no readable embedded payload.",
                page_number=page,
                object_id=item.object_id,
                bbox=item.bbox.as_tuple(),
            )
            for page, item in missing
        )
        return self.scored(len(available) / len(media), details, raw_value=len(available), confidence=0.99)


class TextGenerationQualityOracle(CompositeOracle):
    ORACLE_ID = "scenario.instruction_alignment"
    oracle_id = ORACLE_ID
    metric_id = "text_generation_quality"

    def __init__(self, adapter=None) -> None:
        super().__init__(
            (
                CriticalInstructionComplianceOracle(adapter),
                InstructionCoverageOracle(adapter),
                AudienceFitOracle(adapter),
                FactQualityOracle(adapter),
            )
        )


class ProjectSummaryQualityOracle(CompositeOracle):
    ORACLE_ID = "scenario.source_faithfulness"
    oracle_id = ORACLE_ID
    metric_id = "project_summary_quality"

    def __init__(self, adapter=None) -> None:
        super().__init__(
            (
                CriticalSourceConsistencyOracle(adapter),
                SourceFaithfulnessOracle(adapter),
                KeyPointRecallOracle(adapter),
                NumericAccuracyOracle(adapter),
                CompressionQualityOracle(adapter),
                TraceabilityOracle(adapter),
            )
        )


class MultimodalQualityOracle(CompositeOracle):
    ORACLE_ID = "scenario.asset_compliance"
    oracle_id = ORACLE_ID
    metric_id = "multimodal_quality"

    def __init__(self, adapter=None) -> None:
        super().__init__(
            (
                RequiredAssetComplianceOracle(adapter),
                CriticalChartDataAccuracyOracle(adapter),
                AssetComplianceOracle(adapter),
                AssetPresentationOracle(adapter),
                CropClarityOracle(adapter),
                ChartDataAccuracyOracle(adapter),
                MediaAvailabilityOracle(adapter),
            )
        )


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


def _label_value_conflicts(
    source_text: str, presentation: ParsedPresentation
) -> list[tuple[str, str, str, int, SlideObject]]:
    source_pairs = {
        re.sub(r"\s+", "", match.group("label")).lower(): _canonical_number(match.group("value"))
        for match in _LABEL_VALUE.finditer(source_text)
    }
    conflicts = []
    for slide in presentation.slides:
        for item in slide.visible_objects:
            for match in _LABEL_VALUE.finditer(item.text):
                label = re.sub(r"\s+", "", match.group("label")).lower()
                source_value = source_pairs.get(label)
                deck_value = _canonical_number(match.group("value"))
                if source_value is not None and source_value != deck_value:
                    conflicts.append((label, source_value, deck_value, slide.page_number, item))
    return conflicts


def _canonical_number(value: str) -> str:
    return value.lower().replace(",", "").replace(" ", "")


def _asset_hash(asset: str) -> str | None:
    try:
        path = Path(asset)
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _match_assets(
    assets: Sequence[str], presentation: ParsedPresentation
) -> dict[str, tuple[int, SlideObject] | None]:
    objects = [
        (slide.page_number, item)
        for slide in presentation.slides
        for item in slide.visible_objects
        if item.kind in {"picture", "media", "linked_picture"}
    ]
    matches: dict[str, tuple[int, SlideObject] | None] = {}
    for asset in assets:
        digest = _asset_hash(asset)
        basename = Path(asset).name.lower()
        found = None
        for page, item in objects:
            target_name = Path(item.relationship_target or "").name.lower()
            if digest and item.media_sha256 == digest:
                found = (page, item)
                break
            if basename and basename == target_name:
                found = (page, item)
                break
        matches[asset] = found
    return matches


def _asset_evidence(
    metric_id: str,
    asset: str,
    match: tuple[int, SlideObject] | None,
    index: int,
) -> object:
    if match is None:
        return evidence(
            metric_id,
            f"asset-{index}",
            "missing_asset",
            f"Asset '{Path(asset).name}' was not matched to embedded media.",
            source_uri=asset,
            payload={"match_method": "sha256_or_part_name"},
        )
    page, item = match
    return evidence(
        metric_id,
        f"asset-{index}",
        "matched_asset",
        f"Asset '{Path(asset).name}' matched an embedded media object.",
        page_number=page,
        object_id=item.object_id,
        bbox=item.bbox.as_tuple(),
        source_uri=asset,
        payload={"match_method": "sha256_or_part_name", "media_sha256": item.media_sha256},
    )


def _looks_like_path(value: str) -> bool:
    return bool(re.search(r"[/\\]", value) or re.search(r"\.[A-Za-z0-9]{1,8}$", value))
