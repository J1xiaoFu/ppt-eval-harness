"""Pure Profile 8.4 policy for adaptive high-resolution visual auditing.

The policy deliberately performs routing only.  It never creates a score,
copies a cluster representative's score to another page, or decides whether a
suspected defect is real.  Deterministic observations and the low-resolution
Atlas Scout merely decide which original rendered pages deserve independent,
criterion-scoped inspection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ppt_eval.domain.enums import EvaluationScope, MetricStatus, Severity
from ppt_eval.domain.models import AtomicObservation
from ppt_eval.domain.visual import (
    VISUAL_SELECTION_POLICY_VERSION as DOMAIN_VISUAL_SELECTION_POLICY_VERSION,
)
from ppt_eval.domain.visual import (
    AtlasScoutResult,
    VisualAuditRound,
    VisualCluster,
    VisualCoverageCertificate,
    VisualPageFeatures,
    VisualPageIndex,
    VisualSelectionItem,
    VisualSelectionPlan,
)

VISUAL_SELECTION_POLICY_VERSION = "3.0.0"
if VISUAL_SELECTION_POLICY_VERSION != DOMAIN_VISUAL_SELECTION_POLICY_VERSION:
    raise RuntimeError("visual selection policy version drift")

VISUAL_SELECTION_ROUND_SIZE = 2
PAGE_LOCAL_CRITERIA: tuple[str, ...] = (
    "composition_layout",
    "typography_legibility",
    "color_contrast",
    "imagery_data_visualization",
    "render_integrity",
    "raster_content_structure",
    "raster_language_consistency",
)
CROSS_SLIDE_CRITERIA: tuple[str, ...] = (
    "cross_slide_consistency",
    "authorship_specificity",
)
VISUAL_CRITERIA = frozenset((*PAGE_LOCAL_CRITERIA, *CROSS_SLIDE_CRITERIA))

# A cluster representative is a routing/coverage unit, not a reason to make
# three different visual constructs inspect the same page.  Keep exactly one
# primary owner for each cluster kind.  Cross-slide and authorship criteria may
# still receive medoids through the shared cohort or a concrete risk route, but
# their completion no longer depends on re-auditing every cluster medoid.
CLUSTER_PRIMARY_OWNER: Mapping[str, str] = {
    "layout_style": "composition_layout",
    "asset_content": "imagery_data_visualization",
}

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
}
_SCOUT_P1_RISK_CODES = frozenset(
    {
        "placeholder_visual_suspected",
        "stock_watermark_suspected",
        "semantic_mismatch_suspected",
    }
)
_SEMANTIC_ASSET_RISK_CODES = frozenset(
    {
        *_SCOUT_P1_RISK_CODES,
        "duplicated_stock_visual",
    }
)
_KEY_PAGE_ROLES = frozenset(
    {
        "cover",
        "data",
        "closing",
        "conclusion",
        "summary",
        "section",
        "title",
        "key",
    }
)
_CONTROL_PAGE_ROLES = frozenset({"cover", "data", "closing", "conclusion"})

# This mapping only routes an existing fact to an isomorphic visual criterion.
# It does not make the fact score-affecting and is intentionally narrower than
# the complete atomic metric catalogue.
RULE_METRIC_VISUAL_CRITERIA: Mapping[str, tuple[str, ...]] = {
    "slide_content_presence": ("raster_content_structure",),
    "slide_reading_load": ("typography_legibility", "raster_content_structure"),
    "slide_geometry_integrity": ("composition_layout",),
    "slide_typography_functional": ("typography_legibility",),
    "slide_editability": ("composition_layout",),
    "media_integrity": ("imagery_data_visualization", "render_integrity"),
    "crop_geometry_risk": ("imagery_data_visualization",),
    "title_body_alignment": ("raster_content_structure",),
    "duplicate_slide": ("cross_slide_consistency", "authorship_specificity"),
    "transition_coherence_proxy": ("cross_slide_consistency",),
    "language_consistency": ("raster_language_consistency",),
    "authorship_specificity_signals": ("authorship_specificity",),
    "reading_order_proxy": ("composition_layout", "typography_legibility"),
    "slide_pixel_contrast": ("color_contrast",),
    "effective_image_resolution": ("imagery_data_visualization",),
    "render_availability_parity": ("render_integrity",),
    "visual_asset_semantic_risk": ("imagery_data_visualization",),
}


@dataclass(slots=True)
class _Candidate:
    page_number: int
    priority_rank: int = 4
    reasons: set[str] = field(default_factory=set)
    criteria: set[str] = field(default_factory=set)
    reasons_by_priority: dict[int, set[str]] = field(default_factory=dict)
    criteria_by_priority: dict[int, set[str]] = field(default_factory=dict)
    cluster_ids: set[str] = field(default_factory=set)
    maximum_scout_confidence: float = 0.0
    severity_rank: int = 0
    key_page: bool = False
    control_page: bool = False
    outlier: bool = False

    def add(
        self,
        priority: str,
        reason: str,
        *,
        criteria: Iterable[str] = (),
        cluster_ids: Iterable[str] = (),
        scout_confidence: float = 0.0,
        severity_rank: int = 0,
        key_page: bool = False,
        control_page: bool = False,
        outlier: bool = False,
    ) -> None:
        priority_rank = _PRIORITY_RANK[priority]
        self.priority_rank = min(self.priority_rank, priority_rank)
        self.reasons.add(reason)
        normalized_criteria = {
            item for item in criteria if item in VISUAL_CRITERIA
        }
        self.criteria.update(normalized_criteria)
        self.reasons_by_priority.setdefault(priority_rank, set()).add(reason)
        self.criteria_by_priority.setdefault(priority_rank, set()).update(
            normalized_criteria
        )
        self.cluster_ids.update(item for item in cluster_ids if item)
        self.maximum_scout_confidence = max(
            self.maximum_scout_confidence, scout_confidence
        )
        self.severity_rank = max(self.severity_rank, severity_rank)
        self.key_page = self.key_page or key_page
        self.control_page = self.control_page or control_page
        self.outlier = self.outlier or outlier

    @property
    def priority(self) -> str:
        if self.priority_rank not in _PRIORITY_RANK.values():
            raise ValueError("visual selection candidate has no priority")
        return f"P{self.priority_rank}"

    @property
    def effective_criteria(self) -> set[str]:
        risk_criteria = set().union(
            *(self.criteria_by_priority.get(rank, set()) for rank in (0, 1, 2))
        )
        return risk_criteria or set(self.criteria_by_priority.get(3, set()))


@dataclass(frozen=True, slots=True)
class VisualCriterionProgress:
    """One criterion's decision from the shared adaptive stop policy."""

    criterion_id: str
    audited_page_numbers: tuple[int, ...]
    required_page_numbers: tuple[int, ...]
    next_page_numbers: tuple[int, ...]
    remaining_page_numbers: tuple[int, ...]
    uncovered_cluster_ids: tuple[str, ...]
    unresolved_codes: tuple[str, ...]
    continue_audit: bool
    coverage_complete_for_criterion: bool
    requires_review: bool
    stopping_reason: str | None
    composite_lower_bound: float | None = None
    composite_upper_bound: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.criterion_id not in VISUAL_CRITERIA:
            raise ValueError("VisualCriterionProgress has an unknown criterion")
        for label in (
            "audited_page_numbers",
            "required_page_numbers",
            "next_page_numbers",
            "remaining_page_numbers",
        ):
            values = tuple(getattr(self, label))
            if (
                len(values) != len(set(values))
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 1
                    for value in values
                )
            ):
                raise ValueError(f"{label} must contain unique positive pages")
            object.__setattr__(self, label, values)
        if len(self.next_page_numbers) > VISUAL_SELECTION_ROUND_SIZE:
            raise ValueError("next_page_numbers cannot contain more than two pages")
        if not set(self.required_page_numbers) <= (
            set(self.audited_page_numbers) | set(self.remaining_page_numbers)
        ):
            # Required pages omitted by Bmax remain explicit in metadata and
            # force REVIEW; they need not be in the runnable remainder.
            unselectable = self.metadata.get("unselectable_required_page_numbers", ())
            if not set(self.required_page_numbers) <= (
                set(self.audited_page_numbers)
                | set(self.remaining_page_numbers)
                | set(unselectable)
            ):
                raise ValueError("required pages are missing from progress lineage")
        if self.continue_audit:
            if self.stopping_reason is not None or not self.next_page_numbers:
                raise ValueError("continuing progress requires next pages and no stop reason")
        elif not self.stopping_reason:
            raise ValueError("stopped progress requires a stopping reason")
        if self.coverage_complete_for_criterion and (
            self.continue_audit or self.requires_review or self.unresolved_codes
        ):
            raise ValueError("complete criterion coverage conflicts with unresolved state")


def assess_visual_criterion_progress(
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
    criterion_id: str,
    *,
    audited_page_numbers: Iterable[int],
    metric_scored: bool,
    confidence: float | None,
    new_major_count: int = 0,
    new_critical_count: int = 0,
    rule_conflict: bool = False,
    pending_asset_context_pages: Iterable[int] = (),
    composite_lower_bound: float | None = None,
    composite_upper_bound: float | None = None,
    minimum_confidence: float = 0.60,
    cost_exhausted: bool = False,
    timeout_exhausted: bool = False,
) -> VisualCriterionProgress:
    """Choose the next two pages or stop one independent visual criterion.

    P0/P1 pages and criterion-owned cluster medoids are required. P2 pages are
    candidates, not automatic REVIEW blockers: they are consumed only while a
    real continuation signal (new major defect, low confidence, rule conflict,
    pending asset context, cluster gap, or threshold-crossing interval) exists.
    """

    _validate_plan_index(page_index, plan)
    if criterion_id not in VISUAL_CRITERIA:
        raise ValueError(f"unknown visual criterion {criterion_id!r}")
    if not isinstance(metric_scored, bool):
        raise ValueError("metric_scored must be boolean")
    for label, value in (
        ("new_major_count", new_major_count),
        ("new_critical_count", new_critical_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if (
        isinstance(minimum_confidence, bool)
        or not isinstance(minimum_confidence, (int, float))
        or not 0.0 <= float(minimum_confidence) <= 1.0
    ):
        raise ValueError("minimum_confidence must be in [0,1]")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValueError("confidence must be in [0,1] or None")
    interval_crosses = _interval_crosses_decision_threshold(
        composite_lower_bound,
        composite_upper_bound,
    )
    ordered_pages = criterion_page_order(plan, criterion_id)
    ordered_set = set(ordered_pages)
    audited = _valid_page_set(
        audited_page_numbers,
        valid_pages=ordered_set,
        label="audited_page_numbers",
    )
    required_pages = _required_pages_for_criterion(plan, criterion_id)
    missing_required = set(required_pages) - audited
    pending_context = _valid_page_set(
        pending_asset_context_pages,
        valid_pages=set(range(1, len(page_index.pages) + 1)),
        label="pending_asset_context_pages",
    ) - audited
    target_clusters = _criterion_owned_clusters(page_index, criterion_id)
    uncovered_clusters = tuple(
        sorted(
            cluster.cluster_id
            for cluster in target_clusters
            if cluster.medoid_page_number not in audited
        )
    )
    cluster_medoid_pages = {
        cluster.medoid_page_number
        for cluster in target_clusters
        if cluster.medoid_page_number not in audited
    }
    remaining = tuple(
        page_number for page_number in ordered_pages if page_number not in audited
    )
    unselectable_required = missing_required - ordered_set
    unselectable_cluster_medoids = cluster_medoid_pages - ordered_set
    unselectable_context = pending_context - ordered_set
    low_confidence = bool(
        metric_scored
        and (confidence is None or float(confidence) < float(minimum_confidence))
    )

    unresolved: list[str] = []
    if not metric_scored:
        unresolved.append("MODEL_UNRESOLVED")
    if missing_required:
        unresolved.append("REQUIRED_P0_P1_NOT_AUDITED")
    if uncovered_clusters:
        unresolved.append("CRITERION_CLUSTER_MEDOID_NOT_AUDITED")
    if new_major_count or new_critical_count:
        unresolved.append("NEW_MAJOR_OR_CRITICAL")
    if low_confidence:
        unresolved.append("LOW_CONFIDENCE")
    if rule_conflict:
        unresolved.append("RULE_VLM_CONFLICT")
    if pending_context:
        unresolved.append("ASSET_CONTEXT_NOT_AUDITED")
    if interval_crosses:
        unresolved.append("DECISION_INTERVAL_CROSSES_THRESHOLD")

    hard_block_reason: str | None = None
    if not metric_scored:
        hard_block_reason = "MODEL_UNRESOLVED_REVIEW"
    elif cost_exhausted:
        hard_block_reason = "COST_BUDGET_EXHAUSTED_REVIEW"
    elif timeout_exhausted:
        hard_block_reason = "TIMEOUT_EXHAUSTED_REVIEW"
    elif unselectable_required:
        hard_block_reason = "SELECTION_BUDGET_EXHAUSTED_REVIEW"
    elif unselectable_cluster_medoids:
        hard_block_reason = "CLUSTER_COVERAGE_BUDGET_EXHAUSTED_REVIEW"
    elif unselectable_context:
        hard_block_reason = "ASSET_CONTEXT_BUDGET_EXHAUSTED_REVIEW"

    should_continue = bool(unresolved and remaining and hard_block_reason is None)
    next_pages = remaining[: plan.round_size] if should_continue else ()
    if should_continue:
        stopping_reason = None
        coverage_complete = False
        requires_review = False
    elif unresolved:
        stopping_reason = hard_block_reason or "PLAN_EXHAUSTED_REVIEW"
        coverage_complete = False
        requires_review = True
    else:
        stopping_reason = "ADAPTIVE_STOP_CONDITIONS_MET"
        coverage_complete = True
        requires_review = False

    p2_candidates = {
        page_number
        for page_number in _risk_pages_for_priority(plan, "P2")
        if criterion_id in _risk_criteria_for_page(plan, page_number, "P2")
    }
    metadata: dict[str, Any] = {
        "selection_plan_id": plan.plan_id,
        "selection_policy_version": plan.version,
        "unselectable_required_page_numbers": sorted(unselectable_required),
        "unselectable_cluster_medoid_page_numbers": sorted(
            unselectable_cluster_medoids
        ),
        "unselectable_asset_context_page_numbers": sorted(unselectable_context),
        "priority_two_candidate_pages_not_audited": sorted(p2_candidates - audited),
        "decision_interval_crosses_threshold": interval_crosses,
        "cluster_coverage_uses_medoid_pages": True,
        "cluster_score_propagation": "FORBIDDEN",
    }
    return VisualCriterionProgress(
        criterion_id=criterion_id,
        audited_page_numbers=tuple(sorted(audited)),
        required_page_numbers=required_pages,
        next_page_numbers=next_pages,
        remaining_page_numbers=remaining,
        uncovered_cluster_ids=uncovered_clusters,
        unresolved_codes=tuple(unresolved),
        continue_audit=should_continue,
        coverage_complete_for_criterion=coverage_complete,
        requires_review=requires_review,
        stopping_reason=stopping_reason,
        composite_lower_bound=composite_lower_bound,
        composite_upper_bound=composite_upper_bound,
        metadata=metadata,
    )


def high_resolution_page_budget(total_pages: int) -> int:
    """Return Profile 8.4's ordinary high-resolution page budget."""

    if isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < 1:
        raise ValueError("total_pages must be a positive integer")
    return min(total_pages, 16, 4 + math.ceil(math.sqrt(total_pages)))


def build_visual_selection_plan(
    page_index: VisualPageIndex,
    scout_result: AtlasScoutResult,
    observations: Iterable[AtomicObservation],
) -> VisualSelectionPlan:
    """Build one deterministic P0-P3 plan without evaluating any defect.

    P0 pages are mandatory and do not consume the ordinary exploration budget.
    P1-P3 pages are admitted strictly by priority until ``Bmax`` is exhausted.
    The returned metadata preserves the execution order because the persisted
    item contract itself is canonically sorted by page for stable hashing.
    """

    if not _scout_matches_index(page_index, scout_result):
        raise ValueError("Atlas Scout pages must belong to the supplied VisualPageIndex")
    normalized_observations = tuple(
        sorted(observations, key=_observation_sort_key)
    )
    pages = {page.page_number: page for page in page_index.pages}
    candidates = {page_number: _Candidate(page_number) for page_number in pages}
    key_pages = {
        page.page_number
        for page in page_index.pages
        if _normalized_role(page.role) in _KEY_PAGE_ROLES
    }
    for observation in normalized_observations:
        if observation.key_unit:
            key_pages.update(_observation_page_numbers(observation, pages))

    observed_metrics_by_page: dict[int, set[str]] = {}
    for observation in normalized_observations:
        for page_number in _observation_page_numbers(observation, pages):
            observed_metrics_by_page.setdefault(page_number, set()).add(
                observation.metric_id
            )
    _add_page_index_rule_risk(
        candidates,
        page_index,
        key_pages=key_pages,
        observed_metrics_by_page=observed_metrics_by_page,
    )
    _add_atomic_observation_risk(
        candidates,
        pages,
        normalized_observations,
        key_pages=key_pages,
        duplicate_asset_route_pages=_bounded_duplicate_asset_route_pages(
            page_index
        ),
    )
    semantic_risk_pages = _add_scout_risk(candidates, scout_result)
    _add_structural_risk(candidates, page_index)
    _add_semantic_asset_expansion(
        candidates,
        page_index,
        seed_page_numbers=semantic_risk_pages,
    )
    _add_control_pages(candidates, page_index)

    ranked = tuple(
        sorted(
            (item for item in candidates.values() if item.priority_rank < 4),
            key=_candidate_order_key,
        )
    )
    forced = tuple(item for item in ranked if item.priority == "P0")
    ordinary_candidates = tuple(item for item in ranked if item.priority != "P0")
    ordinary_budget = high_resolution_page_budget(len(page_index.pages))
    admitted_ordinary = ordinary_candidates[:ordinary_budget]
    selected = (*forced, *admitted_ordinary)
    overflow = tuple(
        item
        for item in ordinary_candidates[ordinary_budget:]
        if item.priority == "P1"
    )
    unrouted_mandatory = tuple(
        item.page_number
        for item in selected
        if item.priority in {"P0", "P1"}
        and not item.criteria_by_priority.get(_PRIORITY_RANK[item.priority])
    )
    unresolved = tuple(
        sorted(
            {
                *(item.page_number for item in overflow),
                *unrouted_mandatory,
            }
        )
    )

    selection_items = tuple(_to_selection_item(item) for item in selected)
    common_order = _common_cohort_order(selected)
    common_page_local = tuple(sorted(common_order[: min(4, len(common_order))]))
    cross_candidates = [*common_page_local]
    cross_candidates.extend(
        page_number
        for page_number in common_order
        if page_number not in set(cross_candidates)
    )
    common_cross_slide = tuple(
        sorted(cross_candidates[: min(8, len(cross_candidates))])
    )
    selection_order = tuple(item.page_number for item in selected)
    criterion_orders = {
        criterion: list(
            _criterion_page_order_from_candidates(
                selected,
                criterion,
                common_page_local=common_page_local,
                common_cross_slide=common_cross_slide,
            )
        )
        for criterion in (*PAGE_LOCAL_CRITERIA, *CROSS_SLIDE_CRITERIA)
    }
    metadata: dict[str, Any] = {
        "selection_policy_version": VISUAL_SELECTION_POLICY_VERSION,
        "total_pages": len(page_index.pages),
        "priority_order": ["P0", "P1", "P2", "P3"],
        "selection_order": list(selection_order),
        "ordinary_selected_page_numbers": [
            item.page_number for item in admitted_ordinary
        ],
        "ordinary_budget_semantics": "P1_P3_UNIQUE_PAGES_ONLY",
        "mandatory_pages_excluded_from_ordinary_budget": True,
        "candidate_count_by_priority": {
            priority: sum(item.priority == priority for item in ranked)
            for priority in _PRIORITY_RANK
        },
        "selected_count_by_priority": {
            priority: sum(item.priority == priority for item in selected)
            for priority in _PRIORITY_RANK
        },
        "overflow_page_numbers_by_priority": {
            priority: [
                item.page_number
                for item in ordinary_candidates[ordinary_budget:]
                if item.priority == priority
            ]
            for priority in ("P1", "P2", "P3")
        },
        "unrouted_mandatory_page_numbers": list(unrouted_mandatory),
        "risk_criteria_by_page": {
            str(item.page_number): {
                f"P{priority_rank}": sorted(
                    item.criteria_by_priority.get(priority_rank, set())
                )
                for priority_rank in sorted(item.reasons_by_priority)
                if priority_rank <= _PRIORITY_RANK["P2"]
            }
            for item in ranked
            if any(
                priority_rank <= _PRIORITY_RANK["P2"]
                for priority_rank in item.reasons_by_priority
            )
        },
        "criterion_page_order": criterion_orders,
        "criterion_risk_pages_follow_common_prefix": True,
        "cluster_score_propagation": "FORBIDDEN",
        "selected_pages_have_independent_evidence": True,
        "scout_coverage_complete": scout_result.coverage_complete,
        "scout_error_code": scout_result.error_code,
    }
    plan_id = _plan_id(
        page_index=page_index,
        scout_result=scout_result,
        items=selection_items,
        common_page_local=common_page_local,
        common_cross_slide=common_cross_slide,
        ordinary_budget=ordinary_budget,
        forced_pages=tuple(item.page_number for item in forced),
        unresolved_pages=unresolved,
        metadata=metadata,
    )
    return VisualSelectionPlan(
        plan_id=plan_id,
        deck_sha256=page_index.deck_sha256,
        rendered_page_set_sha256=page_index.rendered_page_set_sha256,
        items=selection_items,
        common_page_local=common_page_local,
        common_cross_slide=common_cross_slide,
        high_resolution_budget=ordinary_budget,
        forced_page_numbers=tuple(item.page_number for item in forced),
        unresolved_risk_page_numbers=unresolved,
        round_size=VISUAL_SELECTION_ROUND_SIZE,
        index_version=page_index.version,
        scout_version=scout_result.version,
        version=VISUAL_SELECTION_POLICY_VERSION,
        metadata=metadata,
    )


def expand_visual_selection_plan_for_asset_context(
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
    *,
    seed_page_numbers: Iterable[int],
    audited_page_numbers: Iterable[int] = (),
    protected_page_numbers: Iterable[int] = (),
) -> tuple[VisualSelectionPlan, tuple[int, ...]]:
    """Activate bounded imagery context after a high-resolution discovery.

    The expansion remains bound to the frozen deck/render set and never grows
    the ordinary Bmax allocation.  It may replace only an unaudited P3 control
    page; P0/P1, cache-prefix and already-audited pages are immutable.  Returned
    pending pages include context that could not be admitted so the caller can
    require REVIEW instead of silently exceeding the budget.
    """

    _validate_plan_index(page_index, plan)
    all_pages = set(range(1, len(page_index.pages) + 1))
    seeds = _valid_page_set(
        seed_page_numbers,
        valid_pages=all_pages,
        label="seed_page_numbers",
    )
    audited = _valid_page_set(
        audited_page_numbers,
        valid_pages=all_pages,
        label="audited_page_numbers",
    )
    globally_protected = _valid_page_set(
        protected_page_numbers,
        valid_pages=all_pages,
        label="protected_page_numbers",
    )
    if not seeds:
        return plan, ()

    pages = {page.page_number: page for page in page_index.pages}
    asset_clusters = {
        cluster.cluster_id: cluster for cluster in page_index.asset_clusters
    }
    desired: list[int] = []

    def add(page_number: int) -> None:
        if page_number in all_pages and page_number not in seeds and page_number not in desired:
            desired.append(page_number)

    for seed_page_number in sorted(seeds):
        seed = pages[seed_page_number]
        seed_hashes = set(seed.asset_hashes) | set(seed.duplicate_asset_hashes)
        matches = tuple(
            page
            for page in page_index.pages
            if page.page_number != seed_page_number
            and seed_hashes
            and seed_hashes
            & (set(page.asset_hashes) | set(page.duplicate_asset_hashes))
        )
        for match in _bounded_role_diverse_pages(
            matches,
            seed_role=_normalized_role(seed.role),
            maximum=2,
        ):
            add(match.page_number)
        if seed.asset_cluster_id in asset_clusters:
            add(asset_clusters[seed.asset_cluster_id].medoid_page_number)
        add(seed_page_number - 1)
        add(seed_page_number + 1)

    existing = {item.page_number: item for item in plan.items}
    protected = {
        *plan.forced_page_numbers,
        *plan.common_page_local,
        *plan.common_cross_slide,
        *audited,
        *globally_protected,
        *(cluster.medoid_page_number for cluster in page_index.layout_clusters),
        *(cluster.medoid_page_number for cluster in page_index.asset_clusters),
    }
    selection_order = list(_selection_order(plan))
    activated: list[int] = []
    removed: list[int] = []
    unresolved: list[int] = []
    ordinary_count = sum(item.consumes_exploration_budget for item in existing.values())
    for page_number in desired:
        if page_number in audited:
            continue
        if page_number in existing:
            activated.append(page_number)
            continue
        if ordinary_count >= plan.high_resolution_budget:
            removable = next(
                (
                    candidate_page
                    for candidate_page in reversed(selection_order)
                    if candidate_page not in protected
                    and existing[candidate_page].priority == "P3"
                    and candidate_page not in desired
                ),
                None,
            )
            if removable is None:
                unresolved.append(page_number)
                continue
            del existing[removable]
            selection_order.remove(removable)
            removed.append(removable)
            ordinary_count -= 1
        page = pages[page_number]
        existing[page_number] = VisualSelectionItem(
            page_number=page_number,
            priority="P2",
            reasons=("high_resolution_asset_context",),
            criteria=("imagery_data_visualization",),
            mandatory=False,
            consumes_exploration_budget=True,
            cluster_ids=_page_cluster_ids(page),
        )
        selection_order.append(page_number)
        ordinary_count += 1
        activated.append(page_number)

    if not activated and not removed and not unresolved:
        return plan, ()
    selected_candidates = tuple(
        _candidate_from_selection_item(existing[page_number])
        for page_number in selection_order
    )
    criterion_orders = {
        criterion: list(
            _criterion_page_order_from_candidates(
                selected_candidates,
                criterion,
                common_page_local=plan.common_page_local,
                common_cross_slide=plan.common_cross_slide,
            )
        )
        for criterion in (*PAGE_LOCAL_CRITERIA, *CROSS_SLIDE_CRITERIA)
    }
    metadata = dict(plan.metadata)
    risk_by_page = {
        str(page): dict(priorities)
        for page, priorities in metadata.get("risk_criteria_by_page", {}).items()
        if isinstance(priorities, Mapping)
    }
    for page_number in activated:
        priorities = dict(risk_by_page.get(str(page_number), {}))
        priorities["P2"] = ["imagery_data_visualization"]
        risk_by_page[str(page_number)] = priorities
    metadata.update(
        {
            "selection_order": selection_order,
            "criterion_page_order": criterion_orders,
            "risk_criteria_by_page": risk_by_page,
            "parent_selection_plan_id": plan.plan_id,
            "lazy_asset_context_seed_pages": sorted(seeds),
            "lazy_asset_context_activated_pages": sorted(set(activated)),
            "lazy_asset_context_removed_p3_pages": sorted(set(removed)),
            "unresolved_asset_context_page_numbers": sorted(set(unresolved)),
            "ordinary_selected_page_numbers": [
                page
                for page in selection_order
                if existing[page].consumes_exploration_budget
            ],
        }
    )
    items = tuple(existing.values())
    plan_id = _expanded_plan_id(plan, items=items, metadata=metadata)
    expanded = VisualSelectionPlan(
        plan_id=plan_id,
        deck_sha256=plan.deck_sha256,
        rendered_page_set_sha256=plan.rendered_page_set_sha256,
        items=items,
        common_page_local=plan.common_page_local,
        common_cross_slide=plan.common_cross_slide,
        high_resolution_budget=plan.high_resolution_budget,
        forced_page_numbers=plan.forced_page_numbers,
        unresolved_risk_page_numbers=plan.unresolved_risk_page_numbers,
        round_size=plan.round_size,
        index_version=plan.index_version,
        scout_version=plan.scout_version,
        version=plan.version,
        metadata=metadata,
    )
    pending = tuple(sorted((set(activated) | set(unresolved)) - audited))
    return expanded, pending


def criterion_page_order(
    plan: VisualSelectionPlan,
    criterion_id: str,
) -> tuple[int, ...]:
    """Return cache prefix first, followed by only criterion-owned risk pages."""

    if criterion_id not in VISUAL_CRITERIA:
        raise ValueError(f"unknown visual criterion {criterion_id!r}")
    raw_orders = plan.metadata.get("criterion_page_order")
    if isinstance(raw_orders, Mapping):
        raw_order = raw_orders.get(criterion_id)
        if isinstance(raw_order, Sequence) and not isinstance(raw_order, (str, bytes)):
            result = tuple(int(page_number) for page_number in raw_order)
            if len(result) == len(set(result)) and set(result) <= {
                item.page_number for item in plan.items
            }:
                return result
    item_by_page = {item.page_number: item for item in plan.items}
    candidates = tuple(
        _candidate_from_selection_item(item) for item in item_by_page.values()
    )
    return _criterion_page_order_from_candidates(
        tuple(sorted(candidates, key=_candidate_order_key)),
        criterion_id,
        common_page_local=plan.common_page_local,
        common_cross_slide=plan.common_cross_slide,
    )


def next_visual_audit_pages(
    plan: VisualSelectionPlan,
    audited_page_numbers: Iterable[int] = (),
) -> tuple[int, ...]:
    """Return at most two not-yet-audited pages in persisted policy order."""

    valid_pages = {item.page_number for item in plan.items}
    audited = _valid_page_set(
        audited_page_numbers,
        valid_pages=valid_pages,
        label="audited_page_numbers",
    )
    ordered = _selection_order(plan)
    return tuple(
        page_number
        for page_number in ordered
        if page_number not in audited
    )[: plan.round_size]


def assess_visual_audit_round(
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
    *,
    round_number: int,
    page_numbers: Iterable[int],
    criterion_pages: Mapping[str, Iterable[int]],
    audited_before: Iterable[int] = (),
    audited_criterion_pages_before: Mapping[str, Iterable[int]] | None = None,
    resolved_hard_gate_pages: Iterable[int] | None = None,
    new_major_count: int = 0,
    new_critical_count: int = 0,
    low_confidence_criteria: Iterable[str] = (),
    conflict_codes: Iterable[str] = (),
    pending_asset_context_pages: Iterable[int] = (),
    composite_lower_bound: float | None = None,
    composite_upper_bound: float | None = None,
    cost_exhausted: bool = False,
    timeout_exhausted: bool = False,
) -> VisualAuditRound:
    """Record one two-page round and decide whether another round is justified."""

    _validate_plan_index(page_index, plan)
    plan_pages = {item.page_number for item in plan.items}
    raw_current = tuple(page_numbers)
    if len(raw_current) != len(set(raw_current)):
        raise ValueError("an audit round cannot contain duplicate pages")
    current = tuple(
        sorted(
            _valid_page_set(
                raw_current,
                valid_pages=plan_pages,
                label="page_numbers",
            )
        )
    )
    if not current:
        raise ValueError("an audit round must add at least one page")
    if len(current) > plan.round_size:
        raise ValueError("an audit round cannot add more than two unique pages")
    prior = _valid_page_set(
        audited_before,
        valid_pages=plan_pages,
        label="audited_before",
    )
    normalized_criterion_pages = _normalize_criterion_pages(
        criterion_pages,
        valid_pages=set(current),
    )
    historical_criterion_pages = _normalize_criterion_pages(
        audited_criterion_pages_before or {},
        valid_pages=prior,
    )
    repeated_pages = prior & set(current)
    for page_number in repeated_pages:
        current_criteria = {
            criterion_id
            for criterion_id, criterion_page_numbers in normalized_criterion_pages.items()
            if page_number in criterion_page_numbers
        }
        if not current_criteria:
            raise ValueError(
                "a repeated page must add evidence for a new visual criterion"
            )
        repeated_criteria = {
            criterion_id
            for criterion_id, criterion_page_numbers in historical_criterion_pages.items()
            if page_number in criterion_page_numbers
        }
        if current_criteria & repeated_criteria:
            raise ValueError(
                "a criterion cannot audit the same page in multiple rounds"
            )
    audited = prior | set(current)
    merged_criterion_pages = _merge_criterion_pages(
        historical_criterion_pages,
        normalized_criterion_pages,
    )
    low_confidence = tuple(sorted(set(low_confidence_criteria)))
    conflicts = tuple(sorted(set(conflict_codes)))
    pending_context = _valid_page_set(
        pending_asset_context_pages,
        valid_pages=set(range(1, len(page_index.pages) + 1)),
        label="pending_asset_context_pages",
    ) - audited
    requested_resolved = (
        set()
        if resolved_hard_gate_pages is None
        else _valid_page_set(
            resolved_hard_gate_pages,
            valid_pages=set(plan.forced_page_numbers),
            label="resolved_hard_gate_pages",
        )
    )
    resolved = {
        page_number
        for page_number in requested_resolved
        if _has_required_criterion_evidence(
            page_number,
            _risk_criteria_for_page(plan, page_number, "P0"),
            merged_criterion_pages,
        )
    }
    invalid_resolution_evidence = requested_resolved - resolved
    missing_forced = set(plan.forced_page_numbers) - resolved
    missing_priority_one = _missing_priority_criterion_pages(
        plan,
        "P1",
        merged_criterion_pages,
    )
    missing_priority_two = _missing_priority_criterion_pages(
        plan,
        "P2",
        merged_criterion_pages,
    )
    unrouted_mandatory = _metadata_page_set(
        plan.metadata,
        "unrouted_mandatory_page_numbers",
        valid_pages=plan_pages,
    )
    selection_overflow = set(plan.unresolved_risk_page_numbers) - unrouted_mandatory
    uncovered_clusters = _uncovered_cluster_ids(
        page_index,
        merged_criterion_pages,
    )
    interval_crosses = _interval_crosses_decision_threshold(
        composite_lower_bound,
        composite_upper_bound,
    )
    triggers: list[str] = []
    if missing_forced:
        triggers.append("UNRESOLVED_HARD_GATE")
    if invalid_resolution_evidence:
        triggers.append("HARD_GATE_RESOLUTION_LACKS_ISOMORPHIC_EVIDENCE")
    if missing_priority_one:
        triggers.append("P1_NOT_AUDITED")
    if selection_overflow:
        triggers.append("SELECTION_BUDGET_OVERFLOW")
    if unrouted_mandatory:
        triggers.append("MANDATORY_CRITERION_UNROUTED")
    if plan.metadata.get("scout_coverage_complete") is not True:
        triggers.append("SCOUT_INCOMPLETE")
    if new_major_count or new_critical_count:
        triggers.append("NEW_MAJOR_OR_CRITICAL")
    if low_confidence:
        triggers.append("LOW_CONFIDENCE")
    if conflicts:
        triggers.append("RULE_VLM_CONFLICT")
    if uncovered_clusters:
        triggers.append("UNCOVERED_CLUSTER")
    if pending_context:
        triggers.append("ASSET_CONTEXT_NOT_AUDITED")
    if interval_crosses:
        triggers.append("DECISION_INTERVAL_CROSSES_THRESHOLD")
    available_next = next_visual_audit_pages(plan, audited)
    pending_criterion_work = _pending_isomorphic_criterion_pages(
        plan,
        merged_criterion_pages,
    )
    blocked_reason = _audit_blocked_reason(
        cost_exhausted=cost_exhausted,
        timeout_exhausted=timeout_exhausted,
        has_available_pages=bool(available_next or pending_criterion_work),
    )
    continue_audit = bool(
        triggers
        and (available_next or pending_criterion_work)
        and blocked_reason is None
    )
    if continue_audit:
        stopping_reason = None
    elif triggers:
        stopping_reason = blocked_reason or "PLAN_EXHAUSTED_REVIEW"
    else:
        stopping_reason = "COVERAGE_COMPLETE"
    usage: dict[str, Any] = {
        "continuation_triggers": triggers,
        "next_page_numbers": list(available_next),
        "pending_asset_context_page_numbers": sorted(pending_context),
        "resolved_hard_gate_page_numbers": sorted(resolved),
        "invalid_hard_gate_resolution_page_numbers": sorted(
            invalid_resolution_evidence
        ),
        "forced_pages_not_resolved": sorted(missing_forced),
        "priority_one_pages_not_audited": sorted(missing_priority_one),
        "priority_two_pages_not_audited": sorted(missing_priority_two),
        "decision_interval_crosses_threshold": interval_crosses,
        "pending_isomorphic_criterion_pages": {
            criterion_id: list(page_numbers)
            for criterion_id, page_numbers in pending_criterion_work.items()
        },
        "required_decision": "REVIEW" if triggers and not continue_audit else None,
    }
    return VisualAuditRound(
        round_number=round_number,
        page_numbers=current,
        criterion_pages=normalized_criterion_pages,
        new_major_count=new_major_count,
        new_critical_count=new_critical_count,
        low_confidence_criteria=low_confidence,
        conflict_codes=conflicts,
        uncovered_cluster_ids=uncovered_clusters,
        composite_lower_bound=composite_lower_bound,
        composite_upper_bound=composite_upper_bound,
        continue_audit=continue_audit,
        stopping_reason=stopping_reason,
        usage=usage,
    )


def build_visual_coverage_certificate(
    page_index: VisualPageIndex,
    scout_result: AtlasScoutResult,
    plan: VisualSelectionPlan,
    rounds: Iterable[VisualAuditRound],
    *,
    criterion_pages: Mapping[str, Iterable[int]],
    resolved_hard_gate_pages: Iterable[int] | None = None,
    unresolved_risk_codes: Iterable[str] = (),
    pending_asset_context_pages: Iterable[int] = (),
    stopping_reason: str | None = None,
) -> VisualCoverageCertificate:
    """Build auditable coverage proof and force REVIEW when evidence is incomplete."""

    _validate_plan_index(page_index, plan)
    if not _scout_matches_index(page_index, scout_result):
        raise ValueError("Atlas Scout pages must belong to the supplied VisualPageIndex")
    normalized_rounds = tuple(sorted(rounds, key=lambda item: item.round_number))
    if len({item.round_number for item in normalized_rounds}) != len(normalized_rounds):
        raise ValueError("visual audit round numbers must be unique")
    if tuple(item.round_number for item in normalized_rounds) != tuple(
        range(1, len(normalized_rounds) + 1)
    ):
        raise ValueError("visual audit round numbers must be contiguous from one")
    all_pages = set(range(1, len(page_index.pages) + 1))
    selected_pages = {item.page_number for item in plan.items}
    seen_criterion_pages: set[tuple[str, int]] = set()
    for audit_round in normalized_rounds:
        if len(audit_round.page_numbers) > plan.round_size:
            raise ValueError("a visual audit round exceeds the two-page expansion size")
        if not set(audit_round.page_numbers) <= selected_pages:
            raise ValueError("a visual audit round contains a page outside the selection plan")
        for criterion_id, page_numbers in audit_round.criterion_pages.items():
            for page_number in page_numbers:
                key = (criterion_id, page_number)
                if key in seen_criterion_pages:
                    raise ValueError(
                        "visual audit rounds cannot repeat criterion/page evidence"
                    )
                seen_criterion_pages.add(key)
    supplied_criterion_pages = _normalize_criterion_pages(
        criterion_pages,
        valid_pages=all_pages,
    )
    merged_criterion_page_sets: dict[str, set[int]] = {
        criterion_id: set(page_numbers)
        for criterion_id, page_numbers in supplied_criterion_pages.items()
    }
    for audit_round in normalized_rounds:
        round_criterion_pages = _normalize_criterion_pages(
            audit_round.criterion_pages,
            valid_pages=all_pages,
        )
        for criterion_id, page_numbers in round_criterion_pages.items():
            merged_criterion_page_sets.setdefault(criterion_id, set()).update(
                page_numbers
            )
    normalized_criterion_pages = {
        criterion_id: tuple(sorted(page_numbers))
        for criterion_id, page_numbers in sorted(merged_criterion_page_sets.items())
    }
    high_resolution_pages = {
        page_number
        for pages in normalized_criterion_pages.values()
        for page_number in pages
    }
    forced = set(plan.forced_page_numbers)
    requested_resolved = (
        set()
        if resolved_hard_gate_pages is None
        else _valid_page_set(
            resolved_hard_gate_pages,
            valid_pages=forced,
            label="resolved_hard_gate_pages",
        )
    )
    resolved = {
        page_number
        for page_number in requested_resolved
        if _has_required_criterion_evidence(
            page_number,
            _risk_criteria_for_page(plan, page_number, "P0"),
            normalized_criterion_pages,
        )
    }
    invalid_resolution_evidence = requested_resolved - resolved
    forced_not_audited = forced - high_resolution_pages
    priority_one_not_audited = _missing_priority_criterion_pages(
        plan,
        "P1",
        normalized_criterion_pages,
    )
    priority_two_not_audited = _missing_priority_criterion_pages(
        plan,
        "P2",
        normalized_criterion_pages,
    )
    unrouted_mandatory = _metadata_page_set(
        plan.metadata,
        "unrouted_mandatory_page_numbers",
        valid_pages=all_pages,
    )
    selection_overflow = set(plan.unresolved_risk_page_numbers) - unrouted_mandatory
    covered_clusters, uncovered_clusters = _cluster_coverage(
        page_index,
        normalized_criterion_pages,
    )
    pending_context = _valid_page_set(
        pending_asset_context_pages,
        valid_pages=all_pages,
        label="pending_asset_context_pages",
    ) - high_resolution_pages
    risks = set(str(item).strip() for item in unresolved_risk_codes if str(item).strip())
    if selection_overflow:
        risks.add("selection_budget_overflow")
    if unrouted_mandatory:
        risks.add("mandatory_visual_criterion_unrouted")
    if forced - resolved:
        risks.add("hard_gate_not_resolved")
    if invalid_resolution_evidence:
        risks.add("hard_gate_resolution_lacks_isomorphic_evidence")
    if forced_not_audited:
        risks.add("forced_page_not_audited")
    if priority_one_not_audited:
        risks.add("priority_one_page_not_audited")
    if pending_context:
        risks.add("asset_context_not_audited")
    if normalized_rounds:
        latest = normalized_rounds[-1]
        if latest.continue_audit:
            risks.add("audit_round_requested_continuation")
        risks.update(f"low_confidence:{item}" for item in latest.low_confidence_criteria)
        risks.update(f"rule_vlm_conflict:{item}" for item in latest.conflict_codes)
        if latest.new_major_count or latest.new_critical_count:
            risks.add("recent_major_or_critical_requires_expansion")
        if _interval_crosses_decision_threshold(
            latest.composite_lower_bound,
            latest.composite_upper_bound,
        ):
            risks.add("decision_interval_crosses_threshold")
    atlas_pages = tuple(sorted(set(scout_result.covered_page_numbers)))
    atlas_complete = (
        scout_result.coverage_complete
        and not scout_result.error_code
        and set(atlas_pages) == all_pages
    )
    semantic_complete = bool(
        atlas_complete
        and not priority_one_not_audited
        and not pending_context
        and not plan.unresolved_risk_page_numbers
    )
    coverage_complete = bool(
        atlas_complete
        and semantic_complete
        and not uncovered_clusters
        and not risks
        and forced == resolved
        and not forced_not_audited
    )
    final_reason = stopping_reason or _coverage_stopping_reason(
        coverage_complete=coverage_complete,
        atlas_complete=atlas_complete,
        forced_not_audited=bool(forced_not_audited),
        plan_overflow=bool(selection_overflow),
        unrouted_mandatory=bool(unrouted_mandatory),
    )
    metadata: dict[str, Any] = {
        "selection_plan_id": plan.plan_id,
        "index_version": page_index.version,
        "scout_version": scout_result.version,
        "selection_policy_version": plan.version,
        "required_decision": None if coverage_complete else "REVIEW",
        "hard_gate_evidence_status": (
            "RESOLVED" if forced == resolved else "N/A"
        ),
        "invalid_hard_gate_resolution_page_numbers": sorted(
            invalid_resolution_evidence
        ),
        "priority_one_pages_not_audited": sorted(priority_one_not_audited),
        "priority_two_pages_not_audited": sorted(priority_two_not_audited),
        "pending_asset_context_page_numbers": sorted(pending_context),
        "selection_overflow_page_numbers": list(plan.unresolved_risk_page_numbers),
        "actual_budget_overflow_page_numbers": sorted(selection_overflow),
        "unrouted_mandatory_page_numbers": sorted(unrouted_mandatory),
        "cluster_coverage_uses_medoid_pages": True,
        "cluster_score_propagation": "FORBIDDEN",
        "unobserved_pages_received_no_inferred_score": True,
    }
    return VisualCoverageCertificate(
        deck_sha256=page_index.deck_sha256,
        total_pages=len(page_index.pages),
        atlas_covered_page_numbers=atlas_pages,
        high_resolution_page_numbers=tuple(sorted(high_resolution_pages)),
        criterion_pages=normalized_criterion_pages,
        covered_cluster_ids=covered_clusters,
        uncovered_cluster_ids=uncovered_clusters,
        hard_gate_candidate_pages=tuple(sorted(forced)),
        resolved_hard_gate_pages=tuple(sorted(resolved)),
        unresolved_risk_codes=tuple(sorted(risks)),
        forced_pages_not_audited=tuple(sorted(forced_not_audited)),
        round_count=len(normalized_rounds),
        atlas_coverage_complete=atlas_complete,
        semantic_coverage_complete=semantic_complete,
        coverage_complete=coverage_complete,
        stopping_reason=final_reason,
        metadata=metadata,
    )


def _add_page_index_rule_risk(
    candidates: Mapping[int, _Candidate],
    page_index: VisualPageIndex,
    *,
    key_pages: set[int],
    observed_metrics_by_page: Mapping[int, set[str]],
) -> None:
    for page in page_index.pages:
        candidate = candidates[page.page_number]
        fallback_metric_ids = tuple(
            metric_id
            for metric_id in page.rule_risk_metric_ids
            if metric_id not in observed_metrics_by_page.get(page.page_number, set())
        )
        criteria = _criteria_for_metrics(fallback_metric_ids)
        cluster_ids = _page_cluster_ids(page)
        if page.rule_severity == "CRITICAL" and (
            fallback_metric_ids or not page.rule_risk_metric_ids
        ):
            candidate.add(
                "P0",
                "rule_critical",
                criteria=criteria,
                cluster_ids=cluster_ids,
                severity_rank=_SEVERITY_RANK[Severity.CRITICAL],
                key_page=page.page_number in key_pages,
            )
        elif page.rule_severity == "MAJOR" and (
            fallback_metric_ids or not page.rule_risk_metric_ids
        ):
            candidate.add(
                "P2",
                "rule_major",
                criteria=criteria,
                cluster_ids=cluster_ids,
                severity_rank=_SEVERITY_RANK[Severity.MAJOR],
                key_page=page.page_number in key_pages,
            )
        if page.unobservable_metric_ids and page.page_number in key_pages:
            unobservable_criteria = _criteria_for_metrics(
                page.unobservable_metric_ids
            )
            if not page.raster_only:
                unobservable_criteria = tuple(
                    criterion
                    for criterion in unobservable_criteria
                    if not criterion.startswith("raster_")
                )
            if unobservable_criteria:
                candidate.add(
                    "P1",
                    "unobservable_key_page",
                    criteria=unobservable_criteria,
                    cluster_ids=cluster_ids,
                    key_page=True,
                )


def _add_atomic_observation_risk(
    candidates: Mapping[int, _Candidate],
    pages: Mapping[int, VisualPageFeatures],
    observations: Sequence[AtomicObservation],
    *,
    key_pages: set[int],
    duplicate_asset_route_pages: set[int],
) -> None:
    for observation in observations:
        page_numbers = _observation_page_numbers(observation, pages)
        if not page_numbers:
            continue
        criteria = _observation_criteria(observation)
        raw_routing_codes = observation.metadata.get("routing_codes", ())
        routing_codes = (
            tuple(
                item
                for item in raw_routing_codes
                if isinstance(item, str) and item.strip()
            )
            if isinstance(raw_routing_codes, Sequence)
            and not isinstance(raw_routing_codes, (str, bytes))
            else ()
        )
        non_scoring_diagnostic = observation.metadata.get("score_affecting") is False
        resolved_gate = _hard_gate_explicitly_resolved(observation)
        unresolved_gate = _is_unresolved_hard_gate(observation) and not resolved_gate
        for page_number in page_numbers:
            page = pages[page_number]
            candidate = candidates[page_number]
            page_criteria = (
                criteria
                if page.raster_only
                else tuple(
                    criterion
                    for criterion in criteria
                    if not criterion.startswith("raster_")
                )
            )
            actionable_routing_codes = _actionable_asset_routing_codes(
                page,
                routing_codes,
                duplicate_asset_route_pages=duplicate_asset_route_pages,
            )
            if observation.severity == Severity.CRITICAL:
                candidate.add(
                    "P0",
                    "rule_critical",
                    criteria=page_criteria,
                    cluster_ids=_page_cluster_ids(page),
                    severity_rank=_SEVERITY_RANK[Severity.CRITICAL],
                    key_page=page_number in key_pages,
                )
            if unresolved_gate:
                candidate.add(
                    "P0",
                    "unresolved_hard_gate",
                    criteria=page_criteria,
                    cluster_ids=_page_cluster_ids(page),
                    severity_rank=_SEVERITY_RANK[observation.severity],
                    key_page=page_number in key_pages,
                )
            elif observation.severity == Severity.MAJOR:
                candidate.add(
                    "P2",
                    "rule_major",
                    criteria=page_criteria,
                    cluster_ids=_page_cluster_ids(page),
                    severity_rank=_SEVERITY_RANK[Severity.MAJOR],
                    key_page=page_number in key_pages,
                )
            elif (
                observation.metric_id == "visual_asset_semantic_risk"
                and actionable_routing_codes
            ):
                candidate.add(
                    "P2",
                    "deterministic_visual_asset_semantic_risk",
                    criteria=page_criteria or ("imagery_data_visualization",),
                    cluster_ids=_page_cluster_ids(page),
                    key_page=page_number in key_pages,
                )
            if (
                observation.metric_status in {MetricStatus.NA, MetricStatus.ERROR}
                and not non_scoring_diagnostic
                and (observation.key_unit or page_number in key_pages)
                and page_criteria
            ):
                candidate.add(
                    "P1",
                    "unobservable_key_page",
                    criteria=page_criteria,
                    cluster_ids=_page_cluster_ids(page),
                    key_page=True,
                )


def _add_scout_risk(
    candidates: Mapping[int, _Candidate],
    scout_result: AtlasScoutResult,
) -> set[int]:
    semantic_risk_pages: set[int] = set()
    for finding in scout_result.findings:
        priority = (
            "P1"
            if finding.confidence >= 0.70
            and finding.risk_code in _SCOUT_P1_RISK_CODES
            else "P2"
        )
        candidates[finding.page_number].add(
            priority,
            f"scout:{finding.risk_code}",
            criteria=finding.suggested_criteria,
            scout_confidence=finding.confidence,
        )
        if (
            finding.confidence >= 0.70
            and finding.risk_code in _SEMANTIC_ASSET_RISK_CODES
        ):
            semantic_risk_pages.add(finding.page_number)
    return semantic_risk_pages


def _bounded_duplicate_asset_route_pages(
    page_index: VisualPageIndex,
) -> set[int]:
    pages_by_hash: dict[str, list[VisualPageFeatures]] = {}
    for page in page_index.pages:
        if page.image_area_ratio < 0.25 and not page.image_dominant:
            continue
        for asset_hash in page.duplicate_asset_hashes:
            pages_by_hash.setdefault(asset_hash, []).append(page)
    selected: set[int] = set()
    for pages in pages_by_hash.values():
        seed_role = _normalized_role(pages[0].role)
        selected.update(
            page.page_number
            for page in _bounded_role_diverse_pages(
                pages,
                seed_role=seed_role,
                maximum=2,
            )
        )
    return selected


def _actionable_asset_routing_codes(
    page: VisualPageFeatures,
    routing_codes: tuple[str, ...],
    *,
    duplicate_asset_route_pages: set[int],
) -> tuple[str, ...]:
    result: list[str] = []
    for routing_code in routing_codes:
        if routing_code == "duplicated_asset_requires_semantic_check":
            if page.page_number in duplicate_asset_route_pages:
                result.append(routing_code)
            continue
        if routing_code == "embedded_text_unobservable_without_ocr":
            embedded_text_proxy = bool(
                page.raster_only
                or page.image_dominant
                or (
                    page.image_area_ratio >= 0.25
                    and page.edge_density is not None
                    and page.edge_density >= 0.14
                    and page.visual_entropy is not None
                    and page.visual_entropy >= 0.25
                )
            )
            if embedded_text_proxy:
                result.append(routing_code)
            continue
        result.append(routing_code)
    return tuple(result)


def _add_structural_risk(
    candidates: Mapping[int, _Candidate],
    page_index: VisualPageIndex,
) -> None:
    layout_clusters = {item.cluster_id: item for item in page_index.layout_clusters}
    asset_clusters = {item.cluster_id: item for item in page_index.asset_clusters}
    for page in page_index.pages:
        candidate = candidates[page.page_number]
        clusters = _page_cluster_ids(page)
        if page.object_pixel_parity_anomaly:
            candidate.add(
                "P1",
                "object_pixel_parity_anomaly",
                criteria=("render_integrity",),
                cluster_ids=clusters,
            )
        if page.layout_outlier:
            candidate.add(
                "P2",
                "layout_cluster_outlier",
                criteria=(
                    "composition_layout",
                    "cross_slide_consistency",
                    "authorship_specificity",
                ),
                cluster_ids=clusters,
                outlier=True,
            )
        if page.asset_outlier:
            candidate.add(
                "P2",
                "asset_cluster_outlier",
                criteria=(
                    "imagery_data_visualization",
                    "cross_slide_consistency",
                    "authorship_specificity",
                ),
                cluster_ids=clusters,
                outlier=True,
            )
        if page.image_dominant:
            candidate.add(
                "P2",
                "image_dominant",
                criteria=("imagery_data_visualization",),
                cluster_ids=clusters,
            )
        has_image_pixels = page.image_count > 0 or page.raster_only
        embedded_text_proxy = bool(
            page.image_area_ratio >= 0.25
            and page.edge_density is not None
            and page.edge_density >= 0.14
            and page.visual_entropy is not None
            and page.visual_entropy >= 0.25
        )
        ocr_gap = bool(
            page.image_text_dense is True
            or (
                (not page_index.ocr_available or page.ocr_text_character_count is None)
                and (page.raster_only or page.image_dominant or embedded_text_proxy)
            )
        )
        if has_image_pixels and ocr_gap:
            candidate.add(
                "P2",
                "ocr_gap",
                criteria=(
                    "imagery_data_visualization",
                    "raster_content_structure",
                ),
                cluster_ids=clusters,
            )
        if page.image_area_ratio >= 0.25 and (
            page.missing_alt_text_count > 0 or page.missing_caption_count > 0
        ):
            candidate.add(
                "P2",
                "image_semantic_context_gap",
                criteria=("imagery_data_visualization",),
                cluster_ids=clusters,
            )
    for cluster in (*layout_clusters.values(), *asset_clusters.values()):
        if not cluster.is_outlier:
            continue
        criterion = (
            ("composition_layout", "cross_slide_consistency", "authorship_specificity")
            if cluster.kind == "layout_style"
            else (
                "imagery_data_visualization",
                "cross_slide_consistency",
                "authorship_specificity",
            )
        )
        candidates[cluster.medoid_page_number].add(
            "P2",
            f"{cluster.kind}_cluster_outlier",
            criteria=criterion,
            cluster_ids=(cluster.cluster_id,),
            outlier=True,
        )


def _add_semantic_asset_expansion(
    candidates: Mapping[int, _Candidate],
    page_index: VisualPageIndex,
    *,
    seed_page_numbers: set[int],
) -> None:
    if not seed_page_numbers:
        return
    pages = {page.page_number: page for page in page_index.pages}
    asset_clusters = {
        cluster.cluster_id: cluster for cluster in page_index.asset_clusters
    }
    for seed_page_number in sorted(seed_page_numbers):
        seed = pages[seed_page_number]
        seed_hashes = set(seed.asset_hashes) | set(seed.duplicate_asset_hashes)
        exact_hash_matches = [
            page
            for page in page_index.pages
            if page.page_number != seed_page_number
            and seed_hashes
            and seed_hashes
            & (set(page.asset_hashes) | set(page.duplicate_asset_hashes))
        ]
        for page in _bounded_role_diverse_pages(
            exact_hash_matches,
            seed_role=_normalized_role(seed.role),
            maximum=2,
        ):
            candidates[page.page_number].add(
                "P2",
                "same_asset_hash_as_semantic_risk",
                criteria=("imagery_data_visualization",),
                cluster_ids=_page_cluster_ids(page),
            )
        # A cluster can contain many slides. Inspect its medoid as context but
        # do not explode one Scout suspicion into a full-cluster upload.
        if seed.asset_cluster_id is not None:
            cluster = asset_clusters[seed.asset_cluster_id]
            medoid = pages[cluster.medoid_page_number]
            if medoid.page_number != seed_page_number:
                candidates[medoid.page_number].add(
                    "P2",
                    "asset_cluster_medoid_for_semantic_risk",
                    criteria=("imagery_data_visualization",),
                    cluster_ids=_page_cluster_ids(medoid),
                    control_page=True,
                )
        for adjacent in (seed_page_number - 1, seed_page_number + 1):
            if adjacent in pages:
                candidates[adjacent].add(
                    "P2",
                    "adjacent_to_semantic_asset_risk",
                    criteria=("imagery_data_visualization",),
                    cluster_ids=_page_cluster_ids(pages[adjacent]),
                )


def _bounded_role_diverse_pages(
    pages: Sequence[VisualPageFeatures],
    *,
    seed_role: str,
    maximum: int,
) -> tuple[VisualPageFeatures, ...]:
    if maximum < 1:
        return ()
    ordered = tuple(sorted(pages, key=lambda item: item.page_number))
    selected: list[VisualPageFeatures] = []
    selected_pages: set[int] = set()
    observed_roles = {seed_role}
    for page in ordered:
        role = _normalized_role(page.role)
        if role in observed_roles:
            continue
        selected.append(page)
        selected_pages.add(page.page_number)
        observed_roles.add(role)
        if len(selected) >= maximum:
            return tuple(selected)
    for page in ordered:
        if page.page_number in selected_pages:
            continue
        selected.append(page)
        if len(selected) >= maximum:
            break
    return tuple(selected)


def _add_control_pages(
    candidates: Mapping[int, _Candidate],
    page_index: VisualPageIndex,
) -> None:
    for cluster in page_index.layout_clusters:
        candidates[cluster.medoid_page_number].add(
            "P3",
            "layout_cluster_medoid",
            criteria=(
                "composition_layout",
                "cross_slide_consistency",
                "authorship_specificity",
            ),
            cluster_ids=(cluster.cluster_id,),
            control_page=True,
        )
    for cluster in page_index.asset_clusters:
        candidates[cluster.medoid_page_number].add(
            "P3",
            "asset_cluster_medoid",
            criteria=(
                "imagery_data_visualization",
                "cross_slide_consistency",
                "authorship_specificity",
            ),
            cluster_ids=(cluster.cluster_id,),
            control_page=True,
        )
    last_page = len(page_index.pages)
    for page in page_index.pages:
        role = _normalized_role(page.role)
        if role in _CONTROL_PAGE_ROLES:
            candidates[page.page_number].add(
                "P3",
                f"role:{role}",
                criteria=VISUAL_CRITERIA,
                cluster_ids=_page_cluster_ids(page),
                key_page=role in _KEY_PAGE_ROLES,
                control_page=True,
            )
    for page_number, reason in ((1, "opening_position"), (last_page, "closing_position")):
        page = page_index.pages[page_number - 1]
        candidates[page_number].add(
            "P3",
            reason,
            criteria=VISUAL_CRITERIA,
            cluster_ids=_page_cluster_ids(page),
            key_page=True,
            control_page=True,
        )
    exploration_page = int(page_index.deck_sha256[:16], 16) % last_page + 1
    exploration = page_index.pages[exploration_page - 1]
    candidates[exploration_page].add(
        "P3",
        "deck_hash_exploration",
        criteria=VISUAL_CRITERIA,
        cluster_ids=_page_cluster_ids(exploration),
        control_page=True,
    )


def _candidate_order_key(item: _Candidate) -> tuple[Any, ...]:
    return (
        item.priority_rank,
        -item.severity_rank,
        -int(item.key_page),
        -item.maximum_scout_confidence,
        -int(item.outlier),
        -int(item.control_page),
        -len(item.effective_criteria),
        item.page_number,
    )


def _common_cohort_order(candidates: Sequence[_Candidate]) -> tuple[int, ...]:
    # Prefer pages deliberately selected as representatives or role controls,
    # then pages useful to multiple criteria. Criterion-specific risk pages are
    # therefore appended after the common cache boundary whenever possible.
    ordered = sorted(
        candidates,
        key=lambda item: (
            -int(item.control_page),
            -len(item.effective_criteria),
            item.priority_rank,
            -item.severity_rank,
            item.page_number,
        ),
    )
    return tuple(item.page_number for item in ordered)


def _to_selection_item(candidate: _Candidate) -> VisualSelectionItem:
    mandatory = candidate.priority == "P0"
    return VisualSelectionItem(
        page_number=candidate.page_number,
        priority=candidate.priority,
        reasons=tuple(sorted(candidate.reasons)),
        criteria=tuple(sorted(candidate.effective_criteria)),
        mandatory=mandatory,
        consumes_exploration_budget=not mandatory,
        cluster_ids=tuple(sorted(candidate.cluster_ids)),
    )


def _candidate_from_selection_item(item: VisualSelectionItem) -> _Candidate:
    candidate = _Candidate(
        page_number=item.page_number,
        priority_rank=_PRIORITY_RANK[item.priority],
        reasons=set(item.reasons),
        criteria=set(item.criteria),
        cluster_ids=set(item.cluster_ids),
    )
    candidate.criteria_by_priority[_PRIORITY_RANK[item.priority]] = set(
        item.criteria
    )
    candidate.reasons_by_priority[_PRIORITY_RANK[item.priority]] = set(
        item.reasons
    )
    candidate.control_page = any(
        reason.endswith("_medoid")
        or reason.startswith("role:")
        or reason in {"opening_position", "closing_position", "deck_hash_exploration"}
        for reason in item.reasons
    )
    candidate.key_page = any(
        reason.startswith("role:")
        or reason in {"opening_position", "closing_position", "unobservable_key_page"}
        for reason in item.reasons
    )
    candidate.outlier = any("outlier" in reason for reason in item.reasons)
    return candidate


def _criterion_page_order_from_candidates(
    selected: Sequence[_Candidate],
    criterion_id: str,
    *,
    common_page_local: tuple[int, ...],
    common_cross_slide: tuple[int, ...],
) -> tuple[int, ...]:
    common = (
        common_cross_slide
        if criterion_id in CROSS_SLIDE_CRITERIA
        else common_page_local
    )
    result = list(common)
    common_set = set(common)
    result.extend(
        item.page_number
        for item in selected
        if criterion_id in item.effective_criteria
        and item.page_number not in common_set
    )
    return tuple(result)


def _criteria_for_metrics(metric_ids: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for metric_id in metric_ids:
        result.update(RULE_METRIC_VISUAL_CRITERIA.get(metric_id, ()))
    return tuple(sorted(result))


def _observation_criteria(observation: AtomicObservation) -> tuple[str, ...]:
    criteria = set(RULE_METRIC_VISUAL_CRITERIA.get(observation.metric_id, ()))
    for key in ("criterion_id", "visual_criterion"):
        value = observation.metadata.get(key)
        if isinstance(value, str) and value in VISUAL_CRITERIA:
            criteria.add(value)
    for key in (
        "criterion_ids",
        "visual_criteria",
        "suggested_criteria",
        "routes_to",
    ):
        value = observation.metadata.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            criteria.update(
                item for item in value if isinstance(item, str) and item in VISUAL_CRITERIA
            )
    return tuple(sorted(criteria))


def _observation_page_numbers(
    observation: AtomicObservation,
    pages: Mapping[int, VisualPageFeatures],
) -> tuple[int, ...]:
    evidence_pages = {
        item.page_number
        for item in observation.evidence
        if item.page_number is not None and item.page_number in pages
    }
    if evidence_pages:
        return tuple(sorted(evidence_pages))
    if observation.scope == EvaluationScope.DECK:
        return ()
    metadata_page = observation.metadata.get("page_number")
    if (
        isinstance(metadata_page, int)
        and not isinstance(metadata_page, bool)
        and metadata_page in pages
    ):
        return (metadata_page,)
    explicit = re.search(
        r"(?:^|[/\s])page(?:_number)?[:=\-](\d+)(?:$|[/\s])",
        observation.unit_key,
        flags=re.IGNORECASE,
    )
    if explicit is not None:
        page_number = int(explicit.group(1))
        return (page_number,) if page_number in pages else ()
    if observation.scope != EvaluationScope.PAGE:
        # An OBJECT/ASSET/CLAIM id can be numeric without being a page.  Never
        # invent visual locality when neither evidence nor an explicit page
        # field establishes it.
        return ()
    parsed = {
        int(value)
        for value in re.findall(r"(?<![A-Za-z])\d+", observation.unit_key)
        if int(value) in pages
    }
    return tuple(sorted(parsed))


def _observation_sort_key(observation: AtomicObservation) -> tuple[Any, ...]:
    return (
        observation.metric_id,
        observation.unit_key,
        observation.observation_id,
    )


def _hard_gate_explicitly_resolved(observation: AtomicObservation) -> bool:
    if observation.metadata.get("hard_gate_resolved") is True:
        return True
    status = str(observation.metadata.get("hard_gate_status", "")).strip().upper()
    return status in {"RESOLVED", "DISMISSED", "CONFIRMED", "PASS", "FAIL"}


def _is_unresolved_hard_gate(observation: AtomicObservation) -> bool:
    if observation.critical:
        return True
    return any(
        observation.metadata.get(key) is True
        for key in (
            "hard_gate_candidate",
            "hard_gate_unresolved",
            "unresolved_hard_gate",
            "contestable_hard_gate",
        )
    )


def _page_cluster_ids(page: VisualPageFeatures) -> tuple[str, ...]:
    return tuple(
        item
        for item in (page.layout_cluster_id, page.asset_cluster_id)
        if item is not None
    )


def _normalized_role(role: str) -> str:
    return role.strip().lower().replace("-", "_").replace(" ", "_")


def _selection_order(plan: VisualSelectionPlan) -> tuple[int, ...]:
    valid_pages = {item.page_number for item in plan.items}
    raw = plan.metadata.get("selection_order")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        try:
            order = tuple(int(item) for item in raw)
        except (TypeError, ValueError):
            order = ()
        if len(order) == len(set(order)) and set(order) == valid_pages:
            return order
    item_by_page = {item.page_number: item for item in plan.items}
    return tuple(
        item.page_number
        for item in sorted(
            item_by_page.values(),
            key=lambda item: (_PRIORITY_RANK[item.priority], item.page_number),
        )
    )


def _cluster_coverage(
    page_index: VisualPageIndex,
    criterion_pages: Mapping[str, tuple[int, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    clusters = (*page_index.layout_clusters, *page_index.asset_clusters)
    page_sets = {
        criterion_id: set(page_numbers)
        for criterion_id, page_numbers in criterion_pages.items()
    }

    def covered_by_owned_criterion(cluster: VisualCluster) -> bool:
        owner = CLUSTER_PRIMARY_OWNER.get(cluster.kind)
        if owner is None:
            raise ValueError(f"visual cluster has no primary owner: {cluster.kind!r}")
        return cluster.medoid_page_number in page_sets.get(owner, set())

    covered = tuple(
        sorted(
            cluster.cluster_id
            for cluster in clusters
            if covered_by_owned_criterion(cluster)
        )
    )
    uncovered = tuple(
        sorted(
            cluster.cluster_id
            for cluster in clusters
            if not covered_by_owned_criterion(cluster)
        )
    )
    return covered, uncovered


def _uncovered_cluster_ids(
    page_index: VisualPageIndex,
    criterion_pages: Mapping[str, tuple[int, ...]],
) -> tuple[str, ...]:
    return _cluster_coverage(page_index, criterion_pages)[1]


def _normalize_criterion_pages(
    criterion_pages: Mapping[str, Iterable[int]],
    *,
    valid_pages: set[int],
) -> Mapping[str, tuple[int, ...]]:
    result: dict[str, tuple[int, ...]] = {}
    for criterion_id, values in sorted(criterion_pages.items()):
        if criterion_id not in VISUAL_CRITERIA:
            raise ValueError(f"unknown visual criterion {criterion_id!r}")
        pages = _valid_page_set(
            values,
            valid_pages=valid_pages,
            label=f"criterion_pages[{criterion_id}]",
        )
        result[criterion_id] = tuple(sorted(pages))
    return result


def _has_required_criterion_evidence(
    page_number: int,
    required_criteria: tuple[str, ...],
    criterion_pages: Mapping[str, tuple[int, ...]],
) -> bool:
    if not required_criteria:
        return False
    return all(
        page_number in set(criterion_pages.get(criterion_id, ()))
        for criterion_id in required_criteria
    )


def _risk_criteria_for_page(
    plan: VisualSelectionPlan,
    page_number: int,
    priority: str,
) -> tuple[str, ...]:
    raw_by_page = plan.metadata.get("risk_criteria_by_page")
    if isinstance(raw_by_page, Mapping):
        raw_page = raw_by_page.get(str(page_number))
        if isinstance(raw_page, Mapping) and priority in raw_page:
            raw_criteria = raw_page.get(priority)
            if isinstance(raw_criteria, Sequence) and not isinstance(
                raw_criteria, (str, bytes)
            ):
                return tuple(
                    sorted(
                        {
                            criterion_id
                            for criterion_id in raw_criteria
                            if isinstance(criterion_id, str)
                            and criterion_id in VISUAL_CRITERIA
                        }
                    )
                )
            return ()
    item = next(
        (item for item in plan.items if item.page_number == page_number),
        None,
    )
    if item is None or item.priority != priority:
        return ()
    return tuple(item.criteria)


def _risk_pages_for_priority(
    plan: VisualSelectionPlan,
    priority: str,
) -> tuple[int, ...]:
    raw_by_page = plan.metadata.get("risk_criteria_by_page")
    result: set[int] = set()
    if isinstance(raw_by_page, Mapping):
        raw_total_pages = plan.metadata.get("total_pages")
        valid_pages = (
            set(range(1, int(raw_total_pages) + 1))
            if isinstance(raw_total_pages, int)
            and not isinstance(raw_total_pages, bool)
            and raw_total_pages >= 1
            else {item.page_number for item in plan.items}
        )
        for raw_page_number, raw_priorities in raw_by_page.items():
            if not isinstance(raw_priorities, Mapping) or priority not in raw_priorities:
                continue
            try:
                page_number = int(raw_page_number)
            except (TypeError, ValueError):
                continue
            if page_number in valid_pages:
                result.add(page_number)
    if result:
        return tuple(sorted(result))
    return tuple(
        sorted(
            item.page_number for item in plan.items if item.priority == priority
        )
    )


def _missing_priority_criterion_pages(
    plan: VisualSelectionPlan,
    priority: str,
    criterion_pages: Mapping[str, tuple[int, ...]],
) -> set[int]:
    return {
        page_number
        for page_number in _risk_pages_for_priority(plan, priority)
        if not _has_required_criterion_evidence(
            page_number,
            _risk_criteria_for_page(plan, page_number, priority),
            criterion_pages,
        )
    }


def _required_pages_for_criterion(
    plan: VisualSelectionPlan,
    criterion_id: str,
) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                page_number
                for priority in ("P0", "P1")
                for page_number in _risk_pages_for_priority(plan, priority)
                if criterion_id
                in _risk_criteria_for_page(plan, page_number, priority)
            }
        )
    )


def _criterion_owned_clusters(
    page_index: VisualPageIndex,
    criterion_id: str,
) -> tuple[VisualCluster, ...]:
    if criterion_id == CLUSTER_PRIMARY_OWNER["layout_style"]:
        return tuple(page_index.layout_clusters)
    if criterion_id == CLUSTER_PRIMARY_OWNER["asset_content"]:
        return tuple(page_index.asset_clusters)
    return ()


def _merge_criterion_pages(
    *values: Mapping[str, tuple[int, ...]],
) -> Mapping[str, tuple[int, ...]]:
    result: dict[str, set[int]] = {}
    for value in values:
        for criterion_id, page_numbers in value.items():
            result.setdefault(criterion_id, set()).update(page_numbers)
    return {
        criterion_id: tuple(sorted(page_numbers))
        for criterion_id, page_numbers in sorted(result.items())
    }


def _pending_isomorphic_criterion_pages(
    plan: VisualSelectionPlan,
    criterion_pages: Mapping[str, tuple[int, ...]],
) -> Mapping[str, tuple[int, ...]]:
    pending: dict[str, set[int]] = {}
    for priority in ("P0", "P1"):
        for page_number in _risk_pages_for_priority(plan, priority):
            required = _risk_criteria_for_page(plan, page_number, priority)
            for criterion_id in required:
                if page_number not in set(criterion_pages.get(criterion_id, ())):
                    pending.setdefault(criterion_id, set()).add(page_number)
    return {
        criterion_id: tuple(sorted(page_numbers))
        for criterion_id, page_numbers in sorted(pending.items())
    }


def _valid_page_set(
    values: Iterable[int],
    *,
    valid_pages: set[int],
    label: str,
) -> set[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value not in valid_pages:
            raise ValueError(f"{label} contains an invalid page")
        result.add(value)
    return result


def _interval_crosses_decision_threshold(
    lower: float | None,
    upper: float | None,
) -> bool:
    if lower is None and upper is None:
        return False
    if lower is None or upper is None:
        raise ValueError("composite interval bounds must both be present or absent")
    if not 0.0 <= lower <= upper <= 100.0:
        raise ValueError("composite interval must satisfy 0 <= lower <= upper <= 100")
    return any(lower < threshold <= upper for threshold in (60.0, 80.0))


def _audit_blocked_reason(
    *,
    cost_exhausted: bool,
    timeout_exhausted: bool,
    has_available_pages: bool,
) -> str | None:
    if cost_exhausted:
        return "COST_BUDGET_EXHAUSTED_REVIEW"
    if timeout_exhausted:
        return "TIMEOUT_EXHAUSTED_REVIEW"
    if not has_available_pages:
        return "PLAN_EXHAUSTED_REVIEW"
    return None


def _coverage_stopping_reason(
    *,
    coverage_complete: bool,
    atlas_complete: bool,
    forced_not_audited: bool,
    plan_overflow: bool,
    unrouted_mandatory: bool,
) -> str:
    if coverage_complete:
        return "COVERAGE_COMPLETE"
    if not atlas_complete:
        return "SCOUT_INCOMPLETE_REVIEW"
    if forced_not_audited:
        return "FORCED_PAGE_NOT_AUDITED_REVIEW"
    if unrouted_mandatory:
        return "MANDATORY_VISUAL_CRITERION_UNROUTED_REVIEW"
    if plan_overflow:
        return "SELECTION_BUDGET_EXHAUSTED_REVIEW"
    return "UNRESOLVED_VISUAL_RISK_REVIEW"


def _metadata_page_set(
    metadata: Mapping[str, Any],
    key: str,
    *,
    valid_pages: set[int],
) -> set[int]:
    raw = metadata.get(key, ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return set()
    try:
        values = tuple(int(item) for item in raw)
    except (TypeError, ValueError):
        return set()
    if any(item not in valid_pages for item in values):
        return set()
    return set(values)


def _scout_matches_index(
    page_index: VisualPageIndex,
    scout_result: AtlasScoutResult,
) -> bool:
    valid_pages = set(range(1, len(page_index.pages) + 1))
    return (
        (
            scout_result.deck_sha256 is None
            or scout_result.deck_sha256 == page_index.deck_sha256
        )
        and (
            (
                scout_result.rendered_page_set_sha256 is None
                and page_index.rendered_page_set_sha256 is None
            )
            or (
                scout_result.rendered_page_set_sha256 is not None
                and scout_result.rendered_page_set_sha256
                == page_index.rendered_page_set_sha256
            )
        )
        and set(scout_result.covered_page_numbers) <= valid_pages
        and all(
        item.page_number in valid_pages for item in scout_result.findings
        )
    )


def _validate_plan_index(
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
) -> None:
    if page_index.deck_sha256 != plan.deck_sha256:
        raise ValueError("VisualSelectionPlan does not belong to VisualPageIndex")
    if page_index.rendered_page_set_sha256 != plan.rendered_page_set_sha256:
        raise ValueError("VisualSelectionPlan rendered page set does not match")
    if page_index.version != plan.index_version:
        raise ValueError("VisualSelectionPlan index version does not match")
    valid_pages = set(range(1, len(page_index.pages) + 1))
    if any(item.page_number not in valid_pages for item in plan.items):
        raise ValueError("VisualSelectionPlan contains a page outside VisualPageIndex")


def _plan_id(
    *,
    page_index: VisualPageIndex,
    scout_result: AtlasScoutResult,
    items: tuple[VisualSelectionItem, ...],
    common_page_local: tuple[int, ...],
    common_cross_slide: tuple[int, ...],
    ordinary_budget: int,
    forced_pages: tuple[int, ...],
    unresolved_pages: tuple[int, ...],
    metadata: Mapping[str, Any],
) -> str:
    payload = {
        "version": VISUAL_SELECTION_POLICY_VERSION,
        "deck_sha256": page_index.deck_sha256,
        "rendered_page_set_sha256": page_index.rendered_page_set_sha256,
        "index_version": page_index.version,
        "scout_version": scout_result.version,
        "items": [item.to_dict() for item in items],
        "common_page_local": list(common_page_local),
        "common_cross_slide": list(common_cross_slide),
        "ordinary_budget": ordinary_budget,
        "forced_pages": list(forced_pages),
        "unresolved_pages": list(unresolved_pages),
        "metadata": metadata,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"visual-selection-{digest}"


def _expanded_plan_id(
    parent: VisualSelectionPlan,
    *,
    items: tuple[VisualSelectionItem, ...],
    metadata: Mapping[str, Any],
) -> str:
    payload = {
        "version": parent.version,
        "parent_plan_id": parent.plan_id,
        "deck_sha256": parent.deck_sha256,
        "rendered_page_set_sha256": parent.rendered_page_set_sha256,
        "items": [item.to_dict() for item in items],
        "common_page_local": list(parent.common_page_local),
        "common_cross_slide": list(parent.common_cross_slide),
        "ordinary_budget": parent.high_resolution_budget,
        "forced_pages": list(parent.forced_page_numbers),
        "unresolved_pages": list(parent.unresolved_risk_page_numbers),
        "metadata": metadata,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"visual-selection-{digest}"


__all__ = [
    "CROSS_SLIDE_CRITERIA",
    "PAGE_LOCAL_CRITERIA",
    "RULE_METRIC_VISUAL_CRITERIA",
    "VISUAL_SELECTION_POLICY_VERSION",
    "VISUAL_SELECTION_ROUND_SIZE",
    "VisualCriterionProgress",
    "assess_visual_criterion_progress",
    "assess_visual_audit_round",
    "build_visual_coverage_certificate",
    "build_visual_selection_plan",
    "criterion_page_order",
    "expand_visual_selection_plan_for_asset_context",
    "high_resolution_page_budget",
    "next_visual_audit_pages",
]
