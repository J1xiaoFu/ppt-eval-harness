"""Offline acceptance benchmark for Profile 8.4 visual page routing.

This benchmark deliberately does not call a model provider.  It materializes
deterministic 20/50/100-page rendered decks, runs the production
``VisualPageIndex`` builder, creates the production 4x4 Atlas artifacts, and
feeds a versioned deterministic Scout fixture into the production selection
policy.  A perfect, criterion-scoped local audit fixture then separates page
*selection* quality from provider/model quality.

The resulting recall and cost figures are therefore a release preflight for
the routing and cache design, not evidence that a live VLM has the same
precision or recall.  Live-provider validation remains a separate release
gate.

Run from the repository root::

    python scripts/benchmarks/profile84_long_deck_acceptance.py \
        --output var/benchmarks/profile84-long-deck.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ppt_eval.adapters.model_audits import ModelImageInput  # noqa: E402
from ppt_eval.adapters.pptx import (  # noqa: E402
    BoundingBox,
    ParsedPresentation,
    ParsedSlide,
    SlideObject,
    ZipPreflightReport,
)
from ppt_eval.application.visual_index import VisualPageIndexBuilder  # noqa: E402
from ppt_eval.application.visual_selection import (  # noqa: E402
    CROSS_SLIDE_CRITERIA,
    PAGE_LOCAL_CRITERIA,
    assess_visual_criterion_progress,
    build_visual_selection_plan,
    criterion_page_order,
)
from ppt_eval.domain import (  # noqa: E402
    AtomicObservation,
    EvaluationScope,
    Evidence,
    MetricStatus,
    Severity,
)
from ppt_eval.domain.visual import (  # noqa: E402
    ScoutFinding,
    ScoutResult,
    VisualPageIndex,
    VisualSelectionPlan,
)
from ppt_eval.infrastructure.atlas_scout import (  # noqa: E402
    ATLAS_PAGE_CAPACITY,
    MAX_ATLASES_PER_REQUEST,
    AtlasBuilder,
)

BENCHMARK_ID = "profile84-long-deck-routing"
BENCHMARK_VERSION = "1.0.0"
CHALLENGE_SET_VERSION = "1.0.0"
LOCAL_AUDIT_FIXTURE_VERSION = "1.0.0"
COST_MODEL_VERSION = "1.0.0"
BASELINE_SELECTION_VERSION = "profile-8.3-canonical-4-8@1.0.0"

PROFILE_83 = "8.3"
PROFILE_84 = "8.4"
PAGE_INDEX_VERSION = "1.0.0"
ATLAS_SCOUT_VERSION = "1.0.0"
SELECTION_POLICY_VERSION = "3.0.0"

PAGE_LOCAL_SAMPLE_COUNT_83 = 4
CROSS_SLIDE_SAMPLE_COUNT_83 = 8
HIGH_RES_TOKENS_PER_PAGE = 1_024
ATLAS_TOKENS_PER_PAGE_CELL = 64
UNCACHED_INPUT_USD_PER_MILLION = 0.30
CACHED_INPUT_USD_PER_MILLION = 0.03
PROGRESSIVE_ROUND_PAGE_COUNT = 2

MIN_RECALL_IMPROVEMENT_PP = 10.0
MAX_PRECISION_DECLINE_PP = 3.0
MIN_TOKEN_REDUCTION_PERCENT = 25.0
MIN_COST_REDUCTION_PERCENT = 25.0


@dataclass(frozen=True, slots=True)
class ChallengeDefect:
    """One page-level MAJOR/CRITICAL ground-truth defect."""

    defect_id: str
    page_number: int
    defect_type: str
    severity: str
    owner_criterion: str
    scout_risk_code: str | None
    scout_criteria: tuple[str, ...]
    rule_blind_spot: bool
    rule_metric_id: str | None = None

    def to_mapping(self) -> Mapping[str, Any]:
        return {
            "defect_id": self.defect_id,
            "page_number": self.page_number,
            "defect_type": self.defect_type,
            "severity": self.severity,
            "owner_criterion": self.owner_criterion,
            "scout_risk_code": self.scout_risk_code,
            "scout_criteria": list(self.scout_criteria),
            "rule_blind_spot": self.rule_blind_spot,
            "rule_metric_id": self.rule_metric_id,
        }


@dataclass(frozen=True, slots=True)
class ChallengeDeck:
    """A deterministic deck manifest; no model-produced labels are used."""

    deck_id: str
    page_count: int
    defects: tuple[ChallengeDefect, ...]
    benign_scout_page: int


def _defect(
    deck_id: str,
    page_number: int,
    defect_type: str,
    severity: str,
    owner_criterion: str,
    scout_risk_code: str | None,
    scout_criteria: tuple[str, ...],
    *,
    rule_blind_spot: bool,
    rule_metric_id: str | None = None,
) -> ChallengeDefect:
    return ChallengeDefect(
        defect_id=f"{deck_id}:page-{page_number}:{defect_type}",
        page_number=page_number,
        defect_type=defect_type,
        severity=severity,
        owner_criterion=owner_criterion,
        scout_risk_code=scout_risk_code,
        scout_criteria=scout_criteria,
        rule_blind_spot=rule_blind_spot,
        rule_metric_id=rule_metric_id,
    )


def challenge_decks() -> tuple[ChallengeDeck, ...]:
    """Return the frozen long-deck challenge manifest.

    Every semantic/style target is outside the relevant Profile 8.3 canonical
    criterion sample.  One rule-visible typography defect per deck is an
    in-sample positive control, which keeps baseline precision meaningful.
    Page 57 in the 100-page deck is intentionally a high-resolution
    placeholder case.  It happens to be in the *cross-slide* 8-page sample,
    but its primary owner is the page-local imagery criterion; criterion
    isolation therefore correctly prevents an unrelated audit from claiming
    it as detected.
    """

    specifications = (
        ("long-020", 20, (4, 8, 12, 16, 19), 10, 13),
        ("long-050", 50, (6, 18, 27, 38, 47), 25, 33),
        ("long-100", 100, (57, 23, 49, 78, 96), 75, 67),
    )
    result: list[ChallengeDeck] = []
    for deck_id, page_count, pages, benign_page, rule_control_page in specifications:
        placeholder, watermark, image_text, semantic_mismatch, style_anomaly = pages
        result.append(
            ChallengeDeck(
                deck_id=deck_id,
                page_count=page_count,
                benign_scout_page=benign_page,
                defects=(
                    _defect(
                        deck_id,
                        placeholder,
                        "placeholder_visual",
                        "CRITICAL",
                        "imagery_data_visualization",
                        "placeholder_visual_suspected",
                        ("imagery_data_visualization",),
                        rule_blind_spot=True,
                    ),
                    _defect(
                        deck_id,
                        watermark,
                        "visible_stock_watermark",
                        "MAJOR",
                        "imagery_data_visualization",
                        "stock_watermark_suspected",
                        ("imagery_data_visualization",),
                        rule_blind_spot=True,
                    ),
                    _defect(
                        deck_id,
                        image_text,
                        "embedded_image_text_unreadable",
                        "MAJOR",
                        "raster_content_structure",
                        "image_text_dense",
                        ("raster_content_structure", "typography_legibility"),
                        rule_blind_spot=True,
                    ),
                    _defect(
                        deck_id,
                        semantic_mismatch,
                        "image_semantics_mismatch",
                        "CRITICAL",
                        "imagery_data_visualization",
                        "semantic_mismatch_suspected",
                        ("imagery_data_visualization",),
                        rule_blind_spot=True,
                    ),
                    _defect(
                        deck_id,
                        style_anomaly,
                        "single_page_style_anomaly",
                        "MAJOR",
                        "cross_slide_consistency",
                        None,
                        (),
                        rule_blind_spot=False,
                    ),
                    # A canonical-sample positive control keeps baseline
                    # precision mathematically defined and proves that the
                    # benchmark is not making 8.3 fail every observable case.
                    _defect(
                        deck_id,
                        rule_control_page,
                        "known_rule_typography_failure",
                        "CRITICAL",
                        "typography_legibility",
                        None,
                        (),
                        rule_blind_spot=False,
                        rule_metric_id="slide_typography_functional",
                    ),
                ),
            )
        )
    return tuple(result)


def canonical_sample_pages(total_pages: int, maximum: int) -> tuple[int, ...]:
    """Reproduce the frozen Profile 8.3 evenly spaced sample contract."""

    if total_pages < 1 or maximum < 1:
        raise ValueError("total_pages and maximum must be positive")
    if total_pages <= maximum:
        return tuple(range(1, total_pages + 1))
    if maximum == 1:
        return (1,)
    last_index = total_pages - 1
    return tuple(
        (position * last_index) // (maximum - 1) + 1
        for position in range(maximum)
    )


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deck_sha256(deck: ChallengeDeck) -> str:
    payload = {
        "challenge_set_version": CHALLENGE_SET_VERSION,
        "deck_id": deck.deck_id,
        "page_count": deck.page_count,
        "defects": [item.to_mapping() for item in deck.defects],
        "benign_scout_page": deck.benign_scout_page,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _text_object(
    object_id: str,
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
) -> SlideObject:
    return SlideObject(
        object_id=object_id,
        name=object_id,
        kind="text",
        bbox=BoundingBox(x=x, y=y, width=width, height=height),
        text=text,
        font_sizes_pt=(24.0,),
        font_names=("Fixture Sans",),
    )


def _picture_object(deck: ChallengeDeck, defect: ChallengeDefect) -> SlideObject:
    return SlideObject(
        object_id=f"visual-{defect.page_number}",
        name=f"visual-{defect.defect_type}",
        kind="picture",
        bbox=BoundingBox(x=0.12, y=0.30, width=0.76, height=0.56),
        media_sha256=_stable_hash(f"{deck.deck_id}:{defect.defect_type}"),
        metadata={"alt_text": ""},
    )


def _parsed_presentation(deck: ChallengeDeck) -> ParsedPresentation:
    defects = {item.page_number: item for item in deck.defects}
    slides: list[ParsedSlide] = []
    media_hashes: set[str] = set()
    for page_number in range(1, deck.page_count + 1):
        objects: list[SlideObject] = [
            _text_object(
                f"title-{page_number}",
                f"Market research finding {page_number}",
                x=0.08,
                y=0.08,
                width=0.84,
                height=0.12,
            ),
            _text_object(
                f"body-{page_number}",
                "Evidence, implication, and recommended next action.",
                x=0.08,
                y=0.25,
                width=0.84,
                height=0.54,
            ),
        ]
        defect = defects.get(page_number)
        if defect is not None and defect.defect_type != "single_page_style_anomaly":
            picture = _picture_object(deck, defect)
            objects.append(picture)
            if picture.media_sha256 is not None:
                media_hashes.add(picture.media_sha256)
        if defect is not None and defect.defect_type == "single_page_style_anomaly":
            # A one-page layout/style singleton that the deterministic Index,
            # rather than the Scout fixture, must route.
            objects = [
                _text_object(
                    f"style-node-{page_number}-{index}",
                    f"Node {index}",
                    x=0.04 + (index % 4) * 0.23,
                    y=0.05 + (index // 4) * 0.28,
                    width=0.13,
                    height=0.12,
                )
                for index in range(8)
            ]
        slides.append(
            ParsedSlide(
                page_number=page_number,
                slide_id=f"slide-{page_number}",
                objects=tuple(objects),
            )
        )
    return ParsedPresentation(
        source_name=f"{deck.deck_id}.pptx",
        source_sha256=_deck_sha256(deck),
        width_emu=12_192_000,
        height_emu=6_858_000,
        slides=tuple(slides),
        media_hashes=tuple(sorted(media_hashes)),
        preflight=ZipPreflightReport(
            archive_bytes=1,
            entry_count=1,
            total_uncompressed_bytes=1,
            max_observed_compression_ratio=1.0,
        ),
        parser_backend="profile84-local-challenge-fixture",
    )


def _render_page(path: Path, page_number: int, defect_type: str | None) -> Path:
    image = Image.new("RGB", (640, 360), (246, 248, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 28, 604, 75), fill=(24, 48, 82))
    draw.rectangle((54, 112, 586, 306), outline=(70, 112, 165), width=4)
    for row in range(5):
        y = 132 + row * 28
        draw.line((76, y, 474 - row * 18, y), fill=(112, 132, 154), width=5)

    if defect_type == "placeholder_visual":
        draw.rectangle((110, 105, 530, 315), fill=(220, 224, 230), outline=(90, 94, 101), width=6)
        draw.line((120, 115, 520, 305), fill=(90, 94, 101), width=8)
        draw.line((520, 115, 120, 305), fill=(90, 94, 101), width=8)
    elif defect_type == "visible_stock_watermark":
        for offset in range(-240, 680, 70):
            draw.line((offset, 350, offset + 300, 80), fill=(170, 170, 170), width=8)
    elif defect_type == "embedded_image_text_unreadable":
        draw.rectangle((80, 96, 560, 326), fill=(252, 252, 252))
        for row in range(22):
            y = 104 + row * 9
            draw.line((92, y, 548 - (row % 5) * 24, y), fill=(38, 42, 48), width=2)
    elif defect_type == "image_semantics_mismatch":
        draw.ellipse((190, 98, 450, 326), fill=(232, 40, 126), outline=(90, 0, 45), width=8)
        draw.polygon(((320, 112), (418, 286), (222, 286)), fill=(255, 214, 10))
    elif defect_type == "single_page_style_anomaly":
        draw.rectangle((0, 0, 639, 359), fill=(18, 18, 22))
        for index in range(8):
            left = 30 + (index % 4) * 152
            top = 32 + (index // 4) * 170
            draw.rectangle((left, top, left + 88, top + 88), fill=(245, 113, 24))
            draw.ellipse((left + 68, top + 60, left + 118, top + 110), fill=(20, 184, 166))
    # A page-specific one-pixel marker ensures deterministic page hashes while
    # remaining visually irrelevant to the benchmark labels.
    draw.point((page_number % 640, page_number % 360), fill=(page_number % 251, 0, 0))
    image.save(path, format="PNG", optimize=False, compress_level=6)
    image.close()
    return path


def _materialize_renders(
    deck: ChallengeDeck,
    output_directory: Path,
) -> tuple[Mapping[int, Path], tuple[ModelImageInput, ...]]:
    output_directory.mkdir(parents=True, exist_ok=True)
    defects = {item.page_number: item.defect_type for item in deck.defects}
    rendered: dict[int, Path] = {}
    model_images: list[ModelImageInput] = []
    for page_number in range(1, deck.page_count + 1):
        path = _render_page(
            output_directory / f"page-{page_number:03d}.png",
            page_number,
            defects.get(page_number),
        )
        rendered[page_number] = path
        model_images.append(ModelImageInput.from_path(path, page_number=page_number))
    return rendered, tuple(model_images)


def _ocr_fixture(deck: ChallengeDeck) -> Mapping[int, str | None]:
    image_text_pages = {
        item.page_number
        for item in deck.defects
        if item.defect_type == "embedded_image_text_unreadable"
    }
    return {
        page_number: (
            "dense embedded label " * 30
            if page_number in image_text_pages
            else f"Market research finding {page_number}"
        )
        for page_number in range(1, deck.page_count + 1)
    }


def _rule_observations(deck: ChallengeDeck) -> tuple[AtomicObservation, ...]:
    observations: list[AtomicObservation] = []
    for defect in deck.defects:
        if defect.rule_metric_id is None:
            continue
        severity = Severity(defect.severity)
        observations.append(
            AtomicObservation(
                observation_id=f"offline-rule-{defect.defect_id}",
                oracle_id="offline.challenge.rule",
                metric_id=defect.rule_metric_id,
                scope=EvaluationScope.PAGE,
                unit_key=f"page:{defect.page_number}",
                raw_value="FROZEN_CHALLENGE_DEFECT",
                local_score=0.20,
                confidence=1.0,
                severity=severity,
                critical=severity == Severity.CRITICAL,
                metric_status=MetricStatus.SCORED,
                evidence=(
                    Evidence(
                        evidence_id=f"offline-rule-evidence-{defect.defect_id}",
                        kind="frozen_challenge_rule",
                        message="Versioned positive-control rule candidate.",
                        page_number=defect.page_number,
                    ),
                ),
            )
        )
    return tuple(observations)


def _scout_fixture(
    deck: ChallengeDeck,
    page_index: VisualPageIndex,
    *,
    atlas_ids_by_page: Mapping[int, str],
) -> ScoutResult:
    findings = [
        ScoutFinding(
            page_number=item.page_number,
            risk_code=item.scout_risk_code,
            confidence=0.92,
            suggested_criteria=item.scout_criteria,
            atlas_id=atlas_ids_by_page[item.page_number],
        )
        for item in deck.defects
        if item.scout_risk_code is not None
    ]
    # This intentional false-positive *route* is resolved by the independent
    # page-level audit fixture and therefore must not become a scoring defect.
    findings.append(
        ScoutFinding(
            page_number=deck.benign_scout_page,
            risk_code="semantic_mismatch_suspected",
            confidence=0.72,
            suggested_criteria=("imagery_data_visualization",),
            atlas_id=atlas_ids_by_page[deck.benign_scout_page],
        )
    )
    return ScoutResult(
        scout_id=f"offline-scout-{_deck_sha256(deck)[:24]}",
        findings=tuple(findings),
        covered_page_numbers=tuple(range(1, deck.page_count + 1)),
        deck_sha256=_deck_sha256(deck),
        rendered_page_set_sha256=page_index.rendered_page_set_sha256,
        atlas_ids=tuple(dict.fromkeys(atlas_ids_by_page.values())),
        coverage_complete=True,
        provider_id="deterministic-local-fixture",
        model_id=LOCAL_AUDIT_FIXTURE_VERSION,
        usage={"model_calls": 0, "image_tokens": 0},
        audit_metadata={
            "non_scoring": True,
            "fixture_version": LOCAL_AUDIT_FIXTURE_VERSION,
        },
    )


def _baseline_criterion_pages(page_count: int) -> Mapping[str, tuple[int, ...]]:
    page_local = canonical_sample_pages(page_count, PAGE_LOCAL_SAMPLE_COUNT_83)
    cross_slide = canonical_sample_pages(page_count, CROSS_SLIDE_SAMPLE_COUNT_83)
    return {
        **{criterion: page_local for criterion in PAGE_LOCAL_CRITERIA},
        **{criterion: cross_slide for criterion in CROSS_SLIDE_CRITERIA},
    }


def _candidate_criterion_pages(
    deck: ChallengeDeck,
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
) -> tuple[Mapping[str, tuple[int, ...]], Mapping[str, int]]:
    # Profile 8.4 calls render_integrity only when a render-risk trigger exists.
    # This challenge has valid renders, so its diagnostic N/A has zero model
    # input and zero coverage penalty.
    active = tuple(
        criterion for criterion in PAGE_LOCAL_CRITERIA if criterion != "render_integrity"
    ) + CROSS_SLIDE_CRITERIA
    defects_by_criterion = {
        criterion: {
            item.page_number: item
            for item in deck.defects
            if item.owner_criterion == criterion
        }
        for criterion in active
    }
    executed: dict[str, tuple[int, ...]] = {}
    rounds: dict[str, int] = {}
    for criterion in active:
        ordered = criterion_page_order(plan, criterion)
        common = (
            plan.common_cross_slide
            if criterion in CROSS_SLIDE_CRITERIA
            else plan.common_page_local
        )
        audited = [page_number for page_number in common if page_number in ordered]
        if not audited:
            audited = list(ordered[:1])
        last_chunk = tuple(audited)
        round_count = 1 if audited else 0
        while audited:
            newly_found = [
                defects_by_criterion[criterion][page_number]
                for page_number in last_chunk
                if page_number in defects_by_criterion[criterion]
            ]
            progress = assess_visual_criterion_progress(
                page_index,
                plan,
                criterion,
                audited_page_numbers=audited,
                metric_scored=True,
                confidence=0.95,
                new_major_count=sum(item.severity == "MAJOR" for item in newly_found),
                new_critical_count=sum(
                    item.severity == "CRITICAL" for item in newly_found
                ),
            )
            if not progress.continue_audit:
                break
            last_chunk = progress.next_page_numbers
            audited.extend(last_chunk)
            round_count += 1
            if round_count > len(ordered) + 1:
                raise AssertionError("adaptive audit fixture failed to converge")
        executed[criterion] = tuple(audited)
        rounds[criterion] = round_count
    return executed, rounds


def _classification(
    deck: ChallengeDeck,
    criterion_pages: Mapping[str, Sequence[int]],
) -> Mapping[str, Any]:
    detected = tuple(
        item
        for item in deck.defects
        if item.page_number in criterion_pages.get(item.owner_criterion, ())
    )
    detected_ids = {item.defect_id for item in detected}
    missed = tuple(item for item in deck.defects if item.defect_id not in detected_ids)
    true_positive = len(detected)
    false_positive = 0
    false_negative = len(missed)
    recall = true_positive / max(1, true_positive + false_negative)
    precision = true_positive / max(1, true_positive + false_positive)
    blind_spot_false_passes = tuple(
        item.defect_id for item in missed if item.rule_blind_spot
    )
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "recall": round(recall, 6),
        "precision": round(precision, 6),
        "detected_defect_ids": sorted(detected_ids),
        "missed_defect_ids": sorted(item.defect_id for item in missed),
        "rule_blind_spot_false_pass_ids": sorted(blind_spot_false_passes),
        "audit_fixture_semantics": (
            "A selected page is classified exactly from frozen GT only for its primary owner "
            "criterion; Scout suspicions never count as predictions."
        ),
    }


def _money(tokens: int, rate: float) -> float:
    return tokens * rate / 1_000_000


def _baseline_cost(
    criterion_pages: Mapping[str, Sequence[int]],
) -> Mapping[str, Any]:
    image_instances = sum(len(pages) for pages in criterion_pages.values())
    uncached_tokens = image_instances * HIGH_RES_TOKENS_PER_PAGE
    estimated_cost = _money(uncached_tokens, UNCACHED_INPUT_USD_PER_MILLION)
    return {
        "request_count": len(criterion_pages),
        "atlas_count": 0,
        "atlas_page_cells": 0,
        "uncached_high_resolution_page_instances": image_instances,
        "cached_high_resolution_page_instances": 0,
        "uncached_visual_input_tokens": uncached_tokens,
        "cached_visual_input_tokens": 0,
        "estimated_visual_input_cost_usd": round(estimated_cost, 8),
    }


def _candidate_cost(
    *,
    page_count: int,
    atlas_count: int,
    common_page_local: Sequence[int],
    common_cross_slide: Sequence[int],
    criterion_pages: Mapping[str, Sequence[int]],
) -> Mapping[str, Any]:
    local_active = tuple(
        criterion for criterion in PAGE_LOCAL_CRITERIA if criterion != "render_integrity"
    )
    cross_active = CROSS_SLIDE_CRITERIA
    uncached_high_res = len(common_page_local) + len(common_cross_slide)
    cached_high_res = (
        max(0, len(local_active) - 1) * len(common_page_local)
        + max(0, len(cross_active) - 1) * len(common_cross_slide)
    )
    atlas_request_count = math.ceil(atlas_count / MAX_ATLASES_PER_REQUEST)
    request_count = len(local_active) + len(cross_active) + atlas_request_count

    for criterion in (*local_active, *cross_active):
        common = (
            tuple(common_cross_slide)
            if criterion in cross_active
            else tuple(common_page_local)
        )
        common_set = set(common)
        extra_pages = [
            page_number
            for page_number in criterion_pages[criterion]
            if page_number not in common_set
        ]
        uncached_high_res += len(extra_pages)
        extra_rounds = math.ceil(len(extra_pages) / PROGRESSIVE_ROUND_PAGE_COUNT)
        cached_high_res += extra_rounds * len(common)
        request_count += extra_rounds

    uncached_tokens = (
        uncached_high_res * HIGH_RES_TOKENS_PER_PAGE
        + page_count * ATLAS_TOKENS_PER_PAGE_CELL
    )
    cached_tokens = cached_high_res * HIGH_RES_TOKENS_PER_PAGE
    estimated_cost = _money(
        uncached_tokens,
        UNCACHED_INPUT_USD_PER_MILLION,
    ) + _money(cached_tokens, CACHED_INPUT_USD_PER_MILLION)
    return {
        "request_count": request_count,
        "atlas_count": atlas_count,
        "atlas_request_count": atlas_request_count,
        "atlas_page_cells": page_count,
        "uncached_high_resolution_page_instances": uncached_high_res,
        "cached_high_resolution_page_instances": cached_high_res,
        "uncached_visual_input_tokens": uncached_tokens,
        "cached_visual_input_tokens": cached_tokens,
        "estimated_visual_input_cost_usd": round(estimated_cost, 8),
    }


def _percentage_reduction(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0
    return round(100.0 * (baseline - candidate) / baseline, 6)


def _deck_result(deck: ChallengeDeck, work_directory: Path) -> Mapping[str, Any]:
    deck_directory = work_directory / deck.deck_id
    rendered, model_images = _materialize_renders(deck, deck_directory / "renders")
    presentation = _parsed_presentation(deck)
    observations = _rule_observations(deck)
    page_index = VisualPageIndexBuilder().build(
        presentation,
        rendered_images=rendered,
        observations=observations,
        ocr_text_by_page=_ocr_fixture(deck),
    )
    atlases = AtlasBuilder(deck_directory / "atlases").build(model_images)
    atlas_ids_by_page = {
        page_number: atlas.atlas_id
        for atlas in atlases
        for page_number in atlas.page_numbers
    }
    scout = _scout_fixture(
        deck,
        page_index,
        atlas_ids_by_page=atlas_ids_by_page,
    )
    plan = build_visual_selection_plan(page_index, scout, observations)

    style_page = next(
        item.page_number
        for item in deck.defects
        if item.defect_type == "single_page_style_anomaly"
    )
    style_indexed_as_outlier = page_index.pages[style_page - 1].layout_outlier

    baseline_pages = _baseline_criterion_pages(deck.page_count)
    candidate_pages, candidate_rounds = _candidate_criterion_pages(
        deck,
        page_index,
        plan,
    )
    baseline_classification = _classification(deck, baseline_pages)
    candidate_classification = _classification(deck, candidate_pages)
    baseline_cost = _baseline_cost(baseline_pages)
    candidate_cost = _candidate_cost(
        page_count=deck.page_count,
        atlas_count=len(atlases),
        common_page_local=plan.common_page_local,
        common_cross_slide=plan.common_cross_slide,
        criterion_pages=candidate_pages,
    )

    expected_atlas_count = math.ceil(deck.page_count / ATLAS_PAGE_CAPACITY)
    if len(atlases) != expected_atlas_count:
        raise AssertionError("Atlas builder did not cover the deck in 4x4 pages")
    if set(atlas_ids_by_page) != set(range(1, deck.page_count + 1)):
        raise AssertionError("Atlas artifacts do not cover every page")
    if not style_indexed_as_outlier:
        raise AssertionError("single-page style anomaly was not indexed as an outlier")
    if candidate_classification["false_negative"]:
        raise AssertionError("Profile 8.4 selection omitted a frozen challenge defect")

    return {
        "deck_id": deck.deck_id,
        "page_count": deck.page_count,
        "defects": [item.to_mapping() for item in deck.defects],
        "benign_scout_page": deck.benign_scout_page,
        "index": {
            "page_count": len(page_index.pages),
            "layout_cluster_count": len(page_index.layout_clusters),
            "asset_cluster_count": len(page_index.asset_clusters),
            "style_anomaly_page": style_page,
            "style_anomaly_is_layout_outlier": style_indexed_as_outlier,
            "rendered_page_count": len(page_index.rendered_page_numbers),
        },
        "atlas": {
            "grid": "4x4",
            "page_capacity": ATLAS_PAGE_CAPACITY,
            "atlas_count": len(atlases),
            "covered_page_count": len(atlas_ids_by_page),
        },
        "profile_8_3": {
            "page_local_sample_pages": list(
                canonical_sample_pages(deck.page_count, PAGE_LOCAL_SAMPLE_COUNT_83)
            ),
            "cross_slide_sample_pages": list(
                canonical_sample_pages(deck.page_count, CROSS_SLIDE_SAMPLE_COUNT_83)
            ),
            "classification": baseline_classification,
            "cost": baseline_cost,
        },
        "profile_8_4": {
            "scout_finding_pages": sorted(item.page_number for item in scout.findings),
            "selection_plan_id": plan.plan_id,
            "high_resolution_budget": plan.high_resolution_budget,
            "selected_pages": list(plan.metadata["selection_order"]),
            "common_page_local": list(plan.common_page_local),
            "common_cross_slide": list(plan.common_cross_slide),
            "criterion_pages": {
                criterion: list(pages)
                for criterion, pages in sorted(candidate_pages.items())
            },
            "criterion_round_count": dict(sorted(candidate_rounds.items())),
            "classification": candidate_classification,
            "cost": candidate_cost,
        },
        "deltas": {
            "recall_improvement_pp": round(
                100.0
                * (
                    candidate_classification["recall"]
                    - baseline_classification["recall"]
                ),
                6,
            ),
            "precision_change_pp": round(
                100.0
                * (
                    candidate_classification["precision"]
                    - baseline_classification["precision"]
                ),
                6,
            ),
            "uncached_visual_input_token_reduction_percent": _percentage_reduction(
                baseline_cost["uncached_visual_input_tokens"],
                candidate_cost["uncached_visual_input_tokens"],
            ),
            "estimated_visual_input_cost_reduction_percent": _percentage_reduction(
                baseline_cost["estimated_visual_input_cost_usd"],
                candidate_cost["estimated_visual_input_cost_usd"],
            ),
        },
    }


def _aggregate(deck_results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    baseline_classifications = [
        result["profile_8_3"]["classification"] for result in deck_results
    ]
    candidate_classifications = [
        result["profile_8_4"]["classification"] for result in deck_results
    ]

    def combine_classification(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        true_positive = sum(int(item["true_positive"]) for item in items)
        false_positive = sum(int(item["false_positive"]) for item in items)
        false_negative = sum(int(item["false_negative"]) for item in items)
        return {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "recall": round(
                true_positive / max(1, true_positive + false_negative),
                6,
            ),
            "precision": round(
                true_positive / max(1, true_positive + false_positive),
                6,
            ),
            "rule_blind_spot_false_pass_count": sum(
                len(item["rule_blind_spot_false_pass_ids"]) for item in items
            ),
        }

    baseline = combine_classification(baseline_classifications)
    candidate = combine_classification(candidate_classifications)
    baseline_uncached = sum(
        int(result["profile_8_3"]["cost"]["uncached_visual_input_tokens"])
        for result in deck_results
    )
    candidate_uncached = sum(
        int(result["profile_8_4"]["cost"]["uncached_visual_input_tokens"])
        for result in deck_results
    )
    baseline_cost = sum(
        float(result["profile_8_3"]["cost"]["estimated_visual_input_cost_usd"])
        for result in deck_results
    )
    candidate_cost = sum(
        float(result["profile_8_4"]["cost"]["estimated_visual_input_cost_usd"])
        for result in deck_results
    )
    return {
        "deck_count": len(deck_results),
        "page_count": sum(int(result["page_count"]) for result in deck_results),
        "ground_truth_defect_count": sum(
            len(result["defects"]) for result in deck_results
        ),
        "profile_8_3": baseline,
        "profile_8_4": candidate,
        "mean_uncached_visual_input_tokens": {
            "profile_8_3": round(baseline_uncached / len(deck_results), 6),
            "profile_8_4": round(candidate_uncached / len(deck_results), 6),
        },
        "estimated_visual_input_cost_usd": {
            "profile_8_3": round(baseline_cost, 8),
            "profile_8_4": round(candidate_cost, 8),
        },
        "deltas": {
            "recall_improvement_pp": round(
                100.0 * (candidate["recall"] - baseline["recall"]),
                6,
            ),
            "precision_decline_pp": round(
                max(0.0, 100.0 * (baseline["precision"] - candidate["precision"])),
                6,
            ),
            "uncached_visual_input_token_reduction_percent": _percentage_reduction(
                baseline_uncached,
                candidate_uncached,
            ),
            "estimated_visual_input_cost_reduction_percent": _percentage_reduction(
                baseline_cost,
                candidate_cost,
            ),
        },
    }


def _acceptance(aggregate: Mapping[str, Any]) -> Mapping[str, Any]:
    deltas = aggregate["deltas"]
    candidate = aggregate["profile_8_4"]
    checks = {
        "recall_improvement_at_least_10pp": (
            deltas["recall_improvement_pp"] >= MIN_RECALL_IMPROVEMENT_PP
        ),
        "precision_decline_at_most_3pp": (
            deltas["precision_decline_pp"] <= MAX_PRECISION_DECLINE_PP
        ),
        "rule_blind_spot_false_pass_is_zero": (
            candidate["rule_blind_spot_false_pass_count"] == 0
        ),
        "uncached_visual_input_token_reduction_at_least_25_percent": (
            deltas["uncached_visual_input_token_reduction_percent"]
            >= MIN_TOKEN_REDUCTION_PERCENT
        ),
        "estimated_visual_input_cost_reduction_at_least_25_percent": (
            deltas["estimated_visual_input_cost_reduction_percent"]
            >= MIN_COST_REDUCTION_PERCENT
        ),
    }
    return {
        "thresholds": {
            "minimum_recall_improvement_pp": MIN_RECALL_IMPROVEMENT_PP,
            "maximum_precision_decline_pp": MAX_PRECISION_DECLINE_PP,
            "maximum_rule_blind_spot_false_pass_count": 0,
            "minimum_uncached_visual_input_token_reduction_percent": (
                MIN_TOKEN_REDUCTION_PERCENT
            ),
            "minimum_estimated_visual_input_cost_reduction_percent": (
                MIN_COST_REDUCTION_PERCENT
            ),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _assumptions() -> Mapping[str, Any]:
    return {
        "scope": (
            "Offline selection/cache preflight. It validates routing recall under frozen "
            "Scout and exact page-audit fixtures; it does not estimate live-model accuracy."
        ),
        "no_weight_fitting": True,
        "ground_truth_source": "hand-authored synthetic page defects frozen in challenge set 1.0.0",
        "baseline_8_3": {
            "selection_version": BASELINE_SELECTION_VERSION,
            "page_local_images_per_criterion": PAGE_LOCAL_SAMPLE_COUNT_83,
            "cross_slide_images_per_criterion": CROSS_SLIDE_SAMPLE_COUNT_83,
            "sample_formula": "floor(position*(N-1)/(maximum-1))+1",
            "criterion_isolation": True,
            "context_cache_enabled": False,
            "page_local_criterion_count": len(PAGE_LOCAL_CRITERIA),
            "cross_slide_criterion_count": len(CROSS_SLIDE_CRITERIA),
        },
        "candidate_8_4": {
            "page_index_version": PAGE_INDEX_VERSION,
            "atlas_scout_version": ATLAS_SCOUT_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "local_audit_fixture_version": LOCAL_AUDIT_FIXTURE_VERSION,
            "page_local_common_cache_prefix": 4,
            "cross_slide_common_cache_prefix": 8,
            "progressive_round_page_count": PROGRESSIVE_ROUND_PAGE_COUNT,
            "maximum_atlases_per_scout_request": MAX_ATLASES_PER_REQUEST,
            "conditional_render_integrity": "SKIPPED_NO_TRIGGER",
            "cluster_score_propagation": "FORBIDDEN",
            "scout_findings_are_non_scoring": True,
        },
        "visual_input_cost_model": {
            "version": COST_MODEL_VERSION,
            "high_resolution_tokens_per_page_instance": HIGH_RES_TOKENS_PER_PAGE,
            "atlas_tokens_per_page_cell": ATLAS_TOKENS_PER_PAGE_CELL,
            "atlas_ratio_to_high_resolution": round(
                ATLAS_TOKENS_PER_PAGE_CELL / HIGH_RES_TOKENS_PER_PAGE,
                6,
            ),
            "uncached_input_usd_per_million_tokens": UNCACHED_INPUT_USD_PER_MILLION,
            "cached_input_usd_per_million_tokens": CACHED_INPUT_USD_PER_MILLION,
            "cache_hit_assumption": (
                "Stable byte-identical 4-page and 8-page public prefixes hit after the first "
                "request in each cohort; criterion-specific pages remain uncached."
            ),
            "included": "visual input tokens only",
            "excluded": [
                "text input",
                "model output",
                "provider retries",
                "fallback calls",
                "network storage",
            ],
            "billing_status": "versioned comparative estimate, not a provider invoice",
        },
    }


def run_benchmark(work_directory: str | Path) -> Mapping[str, Any]:
    """Run the deterministic benchmark and return a JSON-compatible report."""

    work_path = Path(work_directory)
    work_path.mkdir(parents=True, exist_ok=True)
    results = tuple(_deck_result(deck, work_path) for deck in challenge_decks())
    aggregate = _aggregate(results)
    acceptance = _acceptance(aggregate)
    report = {
        "schema_version": "1.0",
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "challenge_set_version": CHALLENGE_SET_VERSION,
        "profiles": {"baseline": PROFILE_83, "candidate": PROFILE_84},
        "assumptions": _assumptions(),
        "decks": list(results),
        "aggregate": aggregate,
        "acceptance": acceptance,
    }
    # Prove the public result is JSON compatible before returning it.
    json.dumps(report, ensure_ascii=True, sort_keys=True, allow_nan=False)
    return report


def write_report(report: Mapping[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the machine-readable JSON report.",
    )
    parser.add_argument(
        "--work-directory",
        type=Path,
        help="Keep deterministic render/Atlas fixtures in this directory.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="ppt-eval-profile84-benchmark-") as temporary:
            report = run_benchmark(temporary)
    else:
        report = run_benchmark(arguments.work_directory)
    if arguments.output is not None:
        write_report(report, arguments.output)
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
