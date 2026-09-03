"""Versioned contracts for adaptive, page-scoped visual auditing.

These contracts deliberately stop before scoring.  A page index, Scout result,
selection plan, audit round or coverage certificate may route evidence, but none
of them is an :class:`OracleResult` and none may contribute a PASS/FAIL or score.

The dataclasses contain only JSON-compatible primitives, tuples and mappings.
``to_dict`` is provided for callers that do not use the repository's shared
``to_primitive`` serializer.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

VISUAL_PAGE_INDEX_VERSION = "1.0.0"
ATLAS_SCOUT_VERSION = "1.0.0"
VISUAL_SELECTION_POLICY_VERSION = "3.0.0"
VISUAL_AUDIT_ROUND_VERSION = "1.0.0"
VISUAL_COVERAGE_CERTIFICATE_VERSION = "1.0.0"
RENDERED_PAGE_SET_VERSION = "1.0.0"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
_CLUSTER_KINDS = frozenset({"layout_style", "asset_content"})
_SEVERITIES = frozenset({"INFO", "MINOR", "MAJOR", "CRITICAL"})
_PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})


class JsonFriendlyContract:
    """Small serialization helper shared by persisted visual contracts."""

    def to_dict(self) -> dict[str, Any]:
        payload = _json_value(self)
        if not isinstance(payload, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("visual contract did not serialize to a JSON object")
        return payload


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _nonblank(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value.strip()


def _positive_page(value: int, label: str = "page_number") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_number(value: float, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return normalized


def _unique_pages(values: tuple[int, ...], label: str) -> tuple[int, ...]:
    pages = tuple(_positive_page(item, label) for item in values)
    if len(pages) != len(set(pages)):
        raise ValueError(f"{label} must not contain duplicate pages")
    return pages


def _strings(values: tuple[str, ...], label: str, *, unique: bool = True) -> tuple[str, ...]:
    normalized = tuple(_nonblank(item, label) for item in values)
    if unique and len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must not contain duplicates")
    return normalized


def _sha256(value: str, label: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _phashes(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    normalized = tuple(str(value).lower() for value in values)
    if any(not _PHASH_RE.fullmatch(value) for value in normalized):
        raise ValueError(f"{label} must contain 64-bit lowercase hexadecimal hashes")
    return normalized


def rendered_page_set_sha256(
    deck_sha256: str,
    page_sha256: Mapping[int, str],
) -> str:
    """Bind a contiguous page-number/digest set to one presentation digest."""

    deck_digest = _sha256(deck_sha256, "deck_sha256")
    if not isinstance(page_sha256, Mapping) or not page_sha256:
        raise ValueError("rendered page set must not be empty")
    normalized: list[tuple[int, str]] = []
    for raw_page, raw_digest in page_sha256.items():
        page_number = _positive_page(raw_page, "rendered page number")
        normalized.append(
            (page_number, _sha256(str(raw_digest).lower(), "rendered page sha256"))
        )
    normalized.sort()
    pages = tuple(page for page, _digest in normalized)
    if pages != tuple(range(1, len(normalized) + 1)):
        raise ValueError("rendered page set must be contiguous and one-based")
    payload = {
        "version": RENDERED_PAGE_SET_VERSION,
        "deck_sha256": deck_digest,
        "pages": [
            {"page_number": page, "sha256": digest}
            for page, digest in normalized
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class VisualPageFeatures(JsonFriendlyContract):
    """Non-scoring structural and low-resolution visual features for one page."""

    page_number: int
    slide_id: str
    role: str
    text_character_count: int
    text_token_count: int
    visible_object_count: int
    object_density: float
    object_area_ratio: float
    image_count: int
    image_area_ratio: float
    page_phash: str | None = None
    image_phashes: tuple[str, ...] = ()
    asset_hashes: tuple[str, ...] = ()
    duplicate_asset_hashes: tuple[str, ...] = ()
    layout_silhouette: tuple[int, ...] = ()
    layout_silhouette_hash: str = ""
    content_fingerprint: str = ""
    color_histogram: tuple[float, ...] = ()
    visual_entropy: float | None = None
    edge_density: float | None = None
    object_pixel_parity_anomaly: bool = False
    raster_only: bool = False
    image_dominant: bool = False
    missing_alt_text_count: int = 0
    missing_caption_count: int = 0
    ocr_text_character_count: int | None = None
    image_text_dense: bool | None = None
    rule_severity: str = "INFO"
    rule_risk_metric_ids: tuple[str, ...] = ()
    unobservable_metric_ids: tuple[str, ...] = ()
    layout_cluster_id: str | None = None
    asset_cluster_id: str | None = None
    layout_outlier: bool = False
    asset_outlier: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _positive_page(self.page_number)
        _nonblank(self.slide_id, "slide_id")
        _nonblank(self.role, "role")
        for label in (
            "text_character_count",
            "text_token_count",
            "visible_object_count",
            "image_count",
            "missing_alt_text_count",
            "missing_caption_count",
        ):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.ocr_text_character_count is not None and (
            isinstance(self.ocr_text_character_count, bool)
            or not isinstance(self.ocr_text_character_count, int)
            or self.ocr_text_character_count < 0
        ):
            raise ValueError("ocr_text_character_count must be a non-negative integer or None")
        for label in ("object_density", "object_area_ratio", "image_area_ratio"):
            object.__setattr__(
                self,
                label,
                _finite_number(getattr(self, label), label, minimum=0.0, maximum=1.0),
            )
        for label in ("visual_entropy", "edge_density"):
            value = getattr(self, label)
            if value is not None:
                object.__setattr__(
                    self,
                    label,
                    _finite_number(value, label, minimum=0.0, maximum=1.0),
                )
        if not isinstance(self.object_pixel_parity_anomaly, bool):
            raise ValueError("object_pixel_parity_anomaly must be a boolean")
        if self.page_phash is not None:
            normalized = str(self.page_phash).lower()
            if not _PHASH_RE.fullmatch(normalized):
                raise ValueError("page_phash must be a 64-bit lowercase hexadecimal hash")
            object.__setattr__(self, "page_phash", normalized)
        object.__setattr__(self, "image_phashes", _phashes(self.image_phashes, "image_phashes"))
        for label in ("asset_hashes", "duplicate_asset_hashes"):
            normalized_hashes = tuple(
                sorted({_sha256(item, label) for item in getattr(self, label)})
            )
            object.__setattr__(self, label, normalized_hashes)
        silhouette = tuple(self.layout_silhouette)
        if not silhouette or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 15
            for item in silhouette
        ):
            raise ValueError("layout_silhouette must contain integer bitmasks in [0, 15]")
        object.__setattr__(self, "layout_silhouette", silhouette)
        _sha256(self.layout_silhouette_hash, "layout_silhouette_hash")
        _sha256(self.content_fingerprint, "content_fingerprint")
        histogram = tuple(float(item) for item in self.color_histogram)
        if histogram:
            if len(histogram) != 24:
                raise ValueError("color_histogram must contain 24 normalized RGB bins")
            if any(not math.isfinite(item) or not 0.0 <= item <= 1.0 for item in histogram):
                raise ValueError("color_histogram entries must be finite values in [0, 1]")
            for offset in (0, 8, 16):
                if not math.isclose(sum(histogram[offset : offset + 8]), 1.0, abs_tol=2e-5):
                    raise ValueError("each color_histogram channel must sum to one")
        object.__setattr__(self, "color_histogram", histogram)
        if self.image_text_dense is not None and self.ocr_text_character_count is None:
            raise ValueError("image_text_dense requires an OCR observation")
        severity = str(self.rule_severity).upper()
        if severity not in _SEVERITIES:
            raise ValueError("rule_severity is invalid")
        object.__setattr__(self, "rule_severity", severity)
        for label in ("rule_risk_metric_ids", "unobservable_metric_ids"):
            object.__setattr__(self, label, _strings(tuple(getattr(self, label)), label))
        for label in ("layout_cluster_id", "asset_cluster_id"):
            value = getattr(self, label)
            if value is not None:
                object.__setattr__(self, label, _nonblank(value, label))


@dataclass(frozen=True, slots=True)
class VisualCluster(JsonFriendlyContract):
    """A deterministic routing cluster; representative scores are never propagated."""

    cluster_id: str
    kind: str
    member_page_numbers: tuple[int, ...]
    medoid_page_number: int
    fingerprint: str
    distance_threshold: float
    is_outlier: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _nonblank(self.cluster_id, "cluster_id"))
        kind = _nonblank(self.kind, "cluster kind")
        if kind not in _CLUSTER_KINDS:
            raise ValueError(f"cluster kind must be one of {sorted(_CLUSTER_KINDS)}")
        object.__setattr__(self, "kind", kind)
        members = tuple(sorted(_unique_pages(self.member_page_numbers, "member_page_numbers")))
        if not members:
            raise ValueError("a visual cluster must contain at least one page")
        object.__setattr__(self, "member_page_numbers", members)
        _positive_page(self.medoid_page_number, "medoid_page_number")
        if self.medoid_page_number not in members:
            raise ValueError("medoid_page_number must be a cluster member")
        _sha256(self.fingerprint, "cluster fingerprint")
        object.__setattr__(
            self,
            "distance_threshold",
            _finite_number(
                self.distance_threshold,
                "distance_threshold",
                minimum=0.0,
                maximum=1.0,
            ),
        )


@dataclass(frozen=True, slots=True)
class VisualPageIndex(JsonFriendlyContract):
    """A complete, deterministic routing index over every slide in a deck."""

    deck_sha256: str
    pages: tuple[VisualPageFeatures, ...]
    layout_clusters: tuple[VisualCluster, ...]
    asset_clusters: tuple[VisualCluster, ...]
    rendered_page_set_sha256: str | None = None
    rendered_page_numbers: tuple[int, ...] = ()
    ocr_available: bool = False
    warnings: tuple[str, ...] = ()
    version: str = VISUAL_PAGE_INDEX_VERSION

    def __post_init__(self) -> None:
        _sha256(self.deck_sha256, "deck_sha256")
        if self.rendered_page_set_sha256 is not None:
            object.__setattr__(
                self,
                "rendered_page_set_sha256",
                _sha256(
                    self.rendered_page_set_sha256,
                    "rendered_page_set_sha256",
                ),
            )
        object.__setattr__(self, "version", _nonblank(self.version, "VisualPageIndex version"))
        pages = tuple(sorted(self.pages, key=lambda item: item.page_number))
        if not pages:
            raise ValueError("VisualPageIndex requires at least one page")
        page_numbers = tuple(item.page_number for item in pages)
        if page_numbers != tuple(range(1, len(pages) + 1)):
            raise ValueError("VisualPageIndex pages must cover every one-based page exactly once")
        object.__setattr__(self, "pages", pages)
        rendered = tuple(sorted(_unique_pages(self.rendered_page_numbers, "rendered_page_numbers")))
        if not set(rendered) <= set(page_numbers):
            raise ValueError("rendered_page_numbers contains a page outside the deck")
        object.__setattr__(self, "rendered_page_numbers", rendered)
        object.__setattr__(self, "warnings", _strings(tuple(self.warnings), "warnings"))
        self._validate_cluster_partition("layout_style", self.layout_clusters, page_numbers)
        self._validate_cluster_partition("asset_content", self.asset_clusters, page_numbers)
        layout_ids = {cluster.cluster_id for cluster in self.layout_clusters}
        asset_ids = {cluster.cluster_id for cluster in self.asset_clusters}
        for page in pages:
            if page.layout_cluster_id not in layout_ids:
                raise ValueError("each page must reference its layout cluster")
            if page.asset_cluster_id not in asset_ids:
                raise ValueError("each page must reference its asset cluster")

    @staticmethod
    def _validate_cluster_partition(
        kind: str,
        clusters: tuple[VisualCluster, ...],
        page_numbers: tuple[int, ...],
    ) -> None:
        if not clusters:
            raise ValueError(f"{kind} clusters must not be empty")
        ids = [item.cluster_id for item in clusters]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{kind} cluster ids must be unique")
        if any(item.kind != kind for item in clusters):
            raise ValueError(f"{kind} cluster set contains a mismatched cluster kind")
        members = [page for cluster in clusters for page in cluster.member_page_numbers]
        if tuple(sorted(members)) != page_numbers:
            raise ValueError(f"{kind} clusters must partition all deck pages exactly once")


@dataclass(frozen=True, slots=True)
class ScoutFinding(JsonFriendlyContract):
    """Non-scoring semantic risk emitted by a low-resolution Atlas Scout."""

    page_number: int
    risk_code: str
    confidence: float
    suggested_criteria: tuple[str, ...]
    atlas_id: str | None = None

    def __post_init__(self) -> None:
        _positive_page(self.page_number)
        object.__setattr__(self, "risk_code", _nonblank(self.risk_code, "risk_code"))
        object.__setattr__(
            self,
            "confidence",
            _finite_number(self.confidence, "confidence", minimum=0.0, maximum=1.0),
        )
        criteria = _strings(tuple(self.suggested_criteria), "suggested_criteria")
        if not criteria:
            raise ValueError("Scout findings require at least one suggested criterion")
        object.__setattr__(self, "suggested_criteria", criteria)
        if self.atlas_id is not None:
            object.__setattr__(self, "atlas_id", _nonblank(self.atlas_id, "atlas_id"))


@dataclass(frozen=True, slots=True)
class ScoutResult(JsonFriendlyContract):
    """Validated output of one or more Atlas calls, never a scoring result."""

    scout_id: str
    findings: tuple[ScoutFinding, ...]
    covered_page_numbers: tuple[int, ...]
    deck_sha256: str | None = None
    rendered_page_set_sha256: str | None = None
    atlas_ids: tuple[str, ...] = ()
    coverage_complete: bool = True
    provider_id: str | None = None
    model_id: str | None = None
    error_code: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    audit_metadata: Mapping[str, Any] = field(default_factory=dict)
    version: str = ATLAS_SCOUT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "scout_id", _nonblank(self.scout_id, "scout_id"))
        if self.deck_sha256 is not None:
            object.__setattr__(
                self,
                "deck_sha256",
                _sha256(self.deck_sha256, "deck_sha256"),
            )
        if self.rendered_page_set_sha256 is not None:
            object.__setattr__(
                self,
                "rendered_page_set_sha256",
                _sha256(
                    self.rendered_page_set_sha256,
                    "rendered_page_set_sha256",
                ),
            )
        if self.rendered_page_set_sha256 is not None and self.deck_sha256 is None:
            raise ValueError("rendered_page_set_sha256 requires deck_sha256")
        covered = tuple(sorted(_unique_pages(self.covered_page_numbers, "covered_page_numbers")))
        object.__setattr__(self, "covered_page_numbers", covered)
        findings = tuple(
            sorted(
                self.findings,
                key=lambda item: (item.page_number, item.risk_code, item.suggested_criteria),
            )
        )
        finding_keys = [
            (item.page_number, item.risk_code, item.suggested_criteria) for item in findings
        ]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("ScoutResult must not contain duplicate findings")
        if any(item.page_number not in covered for item in findings):
            raise ValueError("Scout findings must reference Atlas-covered pages")
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "atlas_ids", _strings(tuple(self.atlas_ids), "atlas_ids"))
        for label in ("provider_id", "model_id", "error_code"):
            value = getattr(self, label)
            if value is not None:
                object.__setattr__(self, label, _nonblank(value, label))
        if self.coverage_complete and self.error_code is not None:
            raise ValueError("a complete Scout result cannot carry error_code")
        object.__setattr__(self, "version", _nonblank(self.version, "ScoutResult version"))


# Descriptive alias used by the Atlas adapter while the persisted schema keeps
# the shorter public contract requested by Profile 8.4.
AtlasScoutResult = ScoutResult


@dataclass(frozen=True, slots=True)
class VisualSelectionItem(JsonFriendlyContract):
    page_number: int
    priority: str
    reasons: tuple[str, ...]
    criteria: tuple[str, ...]
    mandatory: bool = False
    consumes_exploration_budget: bool = True
    cluster_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _positive_page(self.page_number)
        priority = str(self.priority).upper()
        if priority not in _PRIORITIES:
            raise ValueError("selection priority must be one of P0, P1, P2 or P3")
        object.__setattr__(self, "priority", priority)
        reasons = _strings(tuple(self.reasons), "selection reasons")
        if not reasons:
            raise ValueError("selection items require at least one reason")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "criteria", _strings(tuple(self.criteria), "criteria"))
        object.__setattr__(self, "cluster_ids", _strings(tuple(self.cluster_ids), "cluster_ids"))
        if priority == "P0" and not self.mandatory:
            raise ValueError("P0 pages must be mandatory")
        if self.mandatory and self.consumes_exploration_budget:
            raise ValueError("mandatory pages cannot consume the ordinary exploration budget")


@dataclass(frozen=True, slots=True)
class VisualSelectionPlan(JsonFriendlyContract):
    plan_id: str
    deck_sha256: str
    items: tuple[VisualSelectionItem, ...]
    common_page_local: tuple[int, ...]
    common_cross_slide: tuple[int, ...]
    high_resolution_budget: int
    rendered_page_set_sha256: str | None = None
    forced_page_numbers: tuple[int, ...] = ()
    unresolved_risk_page_numbers: tuple[int, ...] = ()
    round_size: int = 2
    index_version: str = VISUAL_PAGE_INDEX_VERSION
    scout_version: str = ATLAS_SCOUT_VERSION
    version: str = VISUAL_SELECTION_POLICY_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _nonblank(self.plan_id, "plan_id"))
        _sha256(self.deck_sha256, "deck_sha256")
        if self.rendered_page_set_sha256 is not None:
            object.__setattr__(
                self,
                "rendered_page_set_sha256",
                _sha256(
                    self.rendered_page_set_sha256,
                    "rendered_page_set_sha256",
                ),
            )
        if (
            isinstance(self.high_resolution_budget, bool)
            or not isinstance(self.high_resolution_budget, int)
            or self.high_resolution_budget < 1
            or self.high_resolution_budget > 16
        ):
            raise ValueError("high_resolution_budget must be an integer in [1, 16]")
        if isinstance(self.round_size, bool) or not isinstance(self.round_size, int) or self.round_size < 1:
            raise ValueError("round_size must be a positive integer")
        items = tuple(sorted(self.items, key=lambda item: (item.page_number, item.priority)))
        item_pages = [item.page_number for item in items]
        if len(item_pages) != len(set(item_pages)):
            raise ValueError("VisualSelectionPlan must contain at most one item per page")
        object.__setattr__(self, "items", items)
        local = _unique_pages(self.common_page_local, "common_page_local")
        cross = _unique_pages(self.common_cross_slide, "common_cross_slide")
        if len(local) > 4 or len(cross) > 8:
            raise ValueError("cache cohorts are bounded to four local and eight cross-slide pages")
        if not set(local) <= set(item_pages) or not set(cross) <= set(item_pages):
            raise ValueError("cache cohort pages must be selected items")
        object.__setattr__(self, "common_page_local", local)
        object.__setattr__(self, "common_cross_slide", cross)
        forced = tuple(sorted(_unique_pages(self.forced_page_numbers, "forced_page_numbers")))
        unresolved = tuple(
            sorted(_unique_pages(self.unresolved_risk_page_numbers, "unresolved_risk_page_numbers"))
        )
        if not set(forced) <= set(item_pages):
            raise ValueError("forced pages must be selected items")
        if any(
            not item.mandatory for item in items if item.page_number in set(forced)
        ):
            raise ValueError("forced pages must refer to mandatory selection items")
        object.__setattr__(self, "forced_page_numbers", forced)
        object.__setattr__(self, "unresolved_risk_page_numbers", unresolved)
        for label in ("index_version", "scout_version", "version"):
            object.__setattr__(self, label, _nonblank(getattr(self, label), label))


@dataclass(frozen=True, slots=True)
class VisualAuditRound(JsonFriendlyContract):
    round_number: int
    page_numbers: tuple[int, ...]
    criterion_pages: Mapping[str, tuple[int, ...]]
    new_major_count: int = 0
    new_critical_count: int = 0
    low_confidence_criteria: tuple[str, ...] = ()
    conflict_codes: tuple[str, ...] = ()
    uncovered_cluster_ids: tuple[str, ...] = ()
    composite_lower_bound: float | None = None
    composite_upper_bound: float | None = None
    continue_audit: bool = False
    stopping_reason: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    version: str = VISUAL_AUDIT_ROUND_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int) or self.round_number < 1:
            raise ValueError("round_number must be a positive integer")
        pages = tuple(sorted(_unique_pages(self.page_numbers, "page_numbers")))
        object.__setattr__(self, "page_numbers", pages)
        normalized_criteria: dict[str, tuple[int, ...]] = {}
        for criterion, criterion_pages in self.criterion_pages.items():
            criterion_id = _nonblank(str(criterion), "criterion id")
            normalized = tuple(sorted(_unique_pages(tuple(criterion_pages), "criterion pages")))
            if not set(normalized) <= set(pages):
                raise ValueError("criterion pages must be part of the audit round")
            normalized_criteria[criterion_id] = normalized
        object.__setattr__(self, "criterion_pages", normalized_criteria)
        for label in ("new_major_count", "new_critical_count"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        for label in (
            "low_confidence_criteria",
            "conflict_codes",
            "uncovered_cluster_ids",
        ):
            object.__setattr__(self, label, _strings(tuple(getattr(self, label)), label))
        bounds = (self.composite_lower_bound, self.composite_upper_bound)
        if any(value is None for value in bounds) and not all(value is None for value in bounds):
            raise ValueError("composite bounds must either both be present or both be absent")
        if self.composite_lower_bound is not None and self.composite_upper_bound is not None:
            lower = _finite_number(
                self.composite_lower_bound,
                "composite_lower_bound",
                minimum=0.0,
                maximum=100.0,
            )
            upper = _finite_number(
                self.composite_upper_bound,
                "composite_upper_bound",
                minimum=0.0,
                maximum=100.0,
            )
            if lower > upper:
                raise ValueError("composite_lower_bound cannot exceed composite_upper_bound")
            object.__setattr__(self, "composite_lower_bound", lower)
            object.__setattr__(self, "composite_upper_bound", upper)
        if self.continue_audit and self.stopping_reason is not None:
            raise ValueError("a continuing audit round cannot carry a stopping reason")
        if not self.continue_audit:
            if self.stopping_reason is None:
                raise ValueError("a final audit round requires a stopping reason")
            object.__setattr__(
                self,
                "stopping_reason",
                _nonblank(self.stopping_reason, "stopping_reason"),
            )
        object.__setattr__(self, "version", _nonblank(self.version, "VisualAuditRound version"))


@dataclass(frozen=True, slots=True)
class VisualCoverageCertificate(JsonFriendlyContract):
    deck_sha256: str
    total_pages: int
    atlas_covered_page_numbers: tuple[int, ...]
    high_resolution_page_numbers: tuple[int, ...]
    criterion_pages: Mapping[str, tuple[int, ...]]
    covered_cluster_ids: tuple[str, ...]
    uncovered_cluster_ids: tuple[str, ...]
    hard_gate_candidate_pages: tuple[int, ...]
    resolved_hard_gate_pages: tuple[int, ...]
    unresolved_risk_codes: tuple[str, ...] = ()
    forced_pages_not_audited: tuple[int, ...] = ()
    round_count: int = 0
    atlas_coverage_complete: bool = False
    semantic_coverage_complete: bool = False
    coverage_complete: bool = False
    stopping_reason: str = ""
    version: str = VISUAL_COVERAGE_CERTIFICATE_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _sha256(self.deck_sha256, "deck_sha256")
        if isinstance(self.total_pages, bool) or not isinstance(self.total_pages, int) or self.total_pages < 1:
            raise ValueError("total_pages must be a positive integer")
        if isinstance(self.round_count, bool) or not isinstance(self.round_count, int) or self.round_count < 0:
            raise ValueError("round_count must be a non-negative integer")
        valid_pages = set(range(1, self.total_pages + 1))
        for label in (
            "atlas_covered_page_numbers",
            "high_resolution_page_numbers",
            "hard_gate_candidate_pages",
            "resolved_hard_gate_pages",
            "forced_pages_not_audited",
        ):
            normalized = tuple(sorted(_unique_pages(tuple(getattr(self, label)), label)))
            if not set(normalized) <= valid_pages:
                raise ValueError(f"{label} contains a page outside the deck")
            object.__setattr__(self, label, normalized)
        if not set(self.resolved_hard_gate_pages) <= set(self.hard_gate_candidate_pages):
            raise ValueError("resolved hard-gate pages must be hard-gate candidates")
        normalized_criteria: dict[str, tuple[int, ...]] = {}
        for criterion, pages in self.criterion_pages.items():
            criterion_id = _nonblank(str(criterion), "criterion id")
            normalized = tuple(sorted(_unique_pages(tuple(pages), "criterion pages")))
            if not set(normalized) <= valid_pages:
                raise ValueError("criterion pages contain a page outside the deck")
            if not set(normalized) <= set(self.high_resolution_page_numbers):
                raise ValueError("criterion pages must have high-resolution evidence")
            normalized_criteria[criterion_id] = normalized
        object.__setattr__(self, "criterion_pages", normalized_criteria)
        for label in (
            "covered_cluster_ids",
            "uncovered_cluster_ids",
            "unresolved_risk_codes",
        ):
            object.__setattr__(self, label, _strings(tuple(getattr(self, label)), label))
        if set(self.covered_cluster_ids) & set(self.uncovered_cluster_ids):
            raise ValueError("covered and uncovered cluster ids cannot overlap")
        if self.atlas_coverage_complete and len(self.atlas_covered_page_numbers) != self.total_pages:
            raise ValueError("complete Atlas coverage must include every page")
        if self.coverage_complete and (
            not self.atlas_coverage_complete
            or not self.semantic_coverage_complete
            or self.uncovered_cluster_ids
            or self.unresolved_risk_codes
            or self.forced_pages_not_audited
            or set(self.hard_gate_candidate_pages) != set(self.resolved_hard_gate_pages)
        ):
            raise ValueError("coverage_complete conflicts with unresolved visual evidence")
        object.__setattr__(
            self,
            "stopping_reason",
            _nonblank(self.stopping_reason, "stopping_reason"),
        )
        object.__setattr__(
            self,
            "version",
            _nonblank(self.version, "VisualCoverageCertificate version"),
        )


__all__ = [
    "ATLAS_SCOUT_VERSION",
    "AtlasScoutResult",
    "ScoutFinding",
    "ScoutResult",
    "RENDERED_PAGE_SET_VERSION",
    "VISUAL_AUDIT_ROUND_VERSION",
    "VISUAL_COVERAGE_CERTIFICATE_VERSION",
    "VISUAL_PAGE_INDEX_VERSION",
    "VISUAL_SELECTION_POLICY_VERSION",
    "VisualAuditRound",
    "VisualCluster",
    "VisualCoverageCertificate",
    "VisualPageFeatures",
    "VisualPageIndex",
    "VisualSelectionItem",
    "VisualSelectionPlan",
    "rendered_page_set_sha256",
]
