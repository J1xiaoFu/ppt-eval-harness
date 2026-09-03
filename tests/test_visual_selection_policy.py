from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from ppt_eval.application.visual_selection import (
    assess_visual_audit_round,
    assess_visual_criterion_progress,
    build_visual_coverage_certificate,
    build_visual_selection_plan,
    criterion_page_order,
    expand_visual_selection_plan_for_asset_context,
    high_resolution_page_budget,
    next_visual_audit_pages,
)
from ppt_eval.domain import (
    AtomicObservation,
    EvaluationScope,
    Evidence,
    MetricStatus,
    Severity,
)
from ppt_eval.domain.visual import (
    AtlasScoutResult,
    ScoutFinding,
    VisualAuditRound,
    VisualCluster,
    VisualPageFeatures,
    VisualPageIndex,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page(
    page_number: int,
    total_pages: int,
    *,
    rule_severity: str = "INFO",
    rule_metrics: tuple[str, ...] = (),
    unobservable: tuple[str, ...] = (),
    layout_outlier: bool = False,
    asset_outlier: bool = False,
    image_dominant: bool = False,
    asset_hash: str | None = None,
    duplicate_asset_hash: str | None = None,
    image_area_ratio: float | None = None,
    missing_alt_text_count: int = 0,
    missing_caption_count: int = 0,
    visual_entropy: float | None = None,
    edge_density: float | None = None,
    object_pixel_parity_anomaly: bool = False,
) -> VisualPageFeatures:
    if page_number == 1:
        role = "cover"
    elif page_number == total_pages:
        role = "closing"
    elif page_number == max(2, total_pages // 2):
        role = "data"
    else:
        role = "content"
    return VisualPageFeatures(
        page_number=page_number,
        slide_id=f"slide-{page_number}",
        role=role,
        text_character_count=80,
        text_token_count=20,
        visible_object_count=4,
        object_density=0.2,
        object_area_ratio=0.5,
        image_count=1 if image_dominant or asset_hash else 0,
        image_area_ratio=(
            image_area_ratio
            if image_area_ratio is not None
            else 0.75
            if image_dominant
            else 0.0
        ),
        asset_hashes=(_sha(asset_hash),) if asset_hash else (),
        duplicate_asset_hashes=(
            (_sha(duplicate_asset_hash),) if duplicate_asset_hash else ()
        ),
        layout_silhouette=(1, 2, 4, 8),
        layout_silhouette_hash=_sha(f"layout-{page_number}"),
        content_fingerprint=_sha(f"content-{page_number}"),
        image_dominant=image_dominant,
        missing_alt_text_count=missing_alt_text_count,
        missing_caption_count=missing_caption_count,
        visual_entropy=visual_entropy,
        edge_density=edge_density,
        object_pixel_parity_anomaly=object_pixel_parity_anomaly,
        rule_severity=rule_severity,
        rule_risk_metric_ids=rule_metrics,
        unobservable_metric_ids=unobservable,
        layout_cluster_id="layout-style-main",
        asset_cluster_id="asset-content-main",
        layout_outlier=layout_outlier,
        asset_outlier=asset_outlier,
    )


def _index(
    total_pages: int,
    *,
    page_overrides: dict[int, dict[str, object]] | None = None,
    layout_medoid: int = 1,
    asset_medoid: int = 2,
    deck_seed: str = "deck",
    ocr_available: bool = True,
) -> VisualPageIndex:
    overrides = page_overrides or {}
    pages = tuple(
        _page(page_number, total_pages, **overrides.get(page_number, {}))
        for page_number in range(1, total_pages + 1)
    )
    members = tuple(range(1, total_pages + 1))
    return VisualPageIndex(
        deck_sha256=_sha(deck_seed),
        pages=pages,
        layout_clusters=(
            VisualCluster(
                cluster_id="layout-style-main",
                kind="layout_style",
                member_page_numbers=members,
                medoid_page_number=layout_medoid,
                fingerprint=_sha("layout-cluster"),
                distance_threshold=0.2,
            ),
        ),
        asset_clusters=(
            VisualCluster(
                cluster_id="asset-content-main",
                kind="asset_content",
                member_page_numbers=members,
                medoid_page_number=asset_medoid,
                fingerprint=_sha("asset-cluster"),
                distance_threshold=0.2,
            ),
        ),
        rendered_page_numbers=members,
        ocr_available=ocr_available,
    )


def _scout(
    total_pages: int,
    *findings: ScoutFinding,
    complete: bool = True,
) -> AtlasScoutResult:
    pages = tuple(range(1, total_pages + 1)) if complete else tuple(range(1, total_pages))
    return AtlasScoutResult(
        scout_id=f"scout-{total_pages}",
        findings=findings,
        covered_page_numbers=pages,
        atlas_ids=("atlas-1",),
        coverage_complete=complete,
        error_code=None if complete else "ATLAS_SCOUT_INCOMPLETE",
    )


def test_selection_rejects_same_deck_with_a_different_rendered_page_set() -> None:
    index = replace(
        _index(4),
        rendered_page_set_sha256=_sha("render-set-a"),
    )
    scout = replace(
        _scout(4),
        deck_sha256=index.deck_sha256,
        rendered_page_set_sha256=_sha("render-set-b"),
    )

    with pytest.raises(ValueError, match="Atlas Scout pages"):
        build_visual_selection_plan(index, scout, ())


def _observation(
    page_number: int,
    *,
    metric_id: str,
    severity: Severity,
    critical: bool = False,
    key_unit: bool = False,
) -> AtomicObservation:
    return AtomicObservation(
        observation_id=f"obs-{metric_id}-{page_number}",
        oracle_id=f"v8.{metric_id}",
        metric_id=metric_id,
        scope=EvaluationScope.PAGE,
        unit_key=f"page:{page_number}",
        local_score=0.2,
        severity=severity,
        critical=critical,
        key_unit=key_unit,
        evidence=(
            Evidence(
                evidence_id=f"evidence-{metric_id}-{page_number}",
                kind="rule_candidate",
                message="Requires visual confirmation.",
                page_number=page_number,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("total_pages", "expected"),
    ((1, 1), (20, 9), (50, 12), (100, 14), (500, 16)),
)
def test_high_resolution_budget_follows_profile_84_formula(
    total_pages: int,
    expected: int,
) -> None:
    assert high_resolution_page_budget(total_pages) == expected


def test_plan_is_order_independent_and_uses_strict_priority() -> None:
    index = _index(
        20,
        page_overrides={
            9: {
                "rule_severity": "MAJOR",
                "rule_metrics": ("slide_geometry_integrity",),
            },
            14: {"layout_outlier": True},
        },
    )
    observations = (
        _observation(
            17,
            metric_id="slide_typography_functional",
            severity=Severity.CRITICAL,
            critical=True,
        ),
        _observation(
            11,
            metric_id="slide_pixel_contrast",
            severity=Severity.MAJOR,
        ),
    )
    scout = _scout(
        20,
        ScoutFinding(
            page_number=13,
            risk_code="placeholder_visual_suspected",
            confidence=0.70,
            suggested_criteria=("imagery_data_visualization",),
        ),
    )

    forward = build_visual_selection_plan(index, scout, observations)
    reverse = build_visual_selection_plan(index, scout, reversed(observations))

    assert forward.to_dict() == reverse.to_dict()
    by_page = {item.page_number: item for item in forward.items}
    assert by_page[17].priority == "P0"
    assert by_page[17].mandatory is True
    assert by_page[17].consumes_exploration_budget is False
    assert by_page[13].priority == "P1"
    assert by_page[9].priority == "P2"
    assert by_page[14].priority == "P2"
    assert forward.metadata["priority_order"] == ["P0", "P1", "P2", "P3"]
    assert forward.metadata["cluster_score_propagation"] == "FORBIDDEN"


def test_object_pixel_parity_anomaly_routes_to_render_integrity_as_p1() -> None:
    index = _index(
        20,
        page_overrides={7: {"object_pixel_parity_anomaly": True}},
    )

    plan = build_visual_selection_plan(index, _scout(20), ())

    item = next(item for item in plan.items if item.page_number == 7)
    assert item.priority == "P1"
    assert "object_pixel_parity_anomaly" in item.reasons
    assert "render_integrity" in item.criteria
    assert item.mandatory is False
    assert item.consumes_exploration_budget is True


def test_plan_rejects_scout_artifact_bound_to_another_same_size_deck() -> None:
    index = _index(8, deck_seed="expected-deck")
    wrong_scout = AtlasScoutResult(
        scout_id="scout-wrong-deck",
        findings=(),
        covered_page_numbers=tuple(range(1, 9)),
        deck_sha256=_sha("different-deck"),
        coverage_complete=True,
    )

    with pytest.raises(ValueError, match="supplied VisualPageIndex"):
        build_visual_selection_plan(index, wrong_scout, ())


def test_page_max_severity_does_not_promote_a_major_metric_to_priority_zero() -> None:
    index = _index(
        6,
        page_overrides={
            4: {
                "rule_severity": "CRITICAL",
                "rule_metrics": (
                    "slide_geometry_integrity",
                    "effective_image_resolution",
                ),
            }
        },
    )
    observations = (
        _observation(
            4,
            metric_id="slide_geometry_integrity",
            severity=Severity.CRITICAL,
            critical=True,
        ),
        _observation(
            4,
            metric_id="effective_image_resolution",
            severity=Severity.MAJOR,
        ),
    )

    plan = build_visual_selection_plan(index, _scout(6), observations)

    risk_criteria = plan.metadata["risk_criteria_by_page"]["4"]
    assert risk_criteria["P0"] == ["composition_layout"]
    assert risk_criteria["P2"] == ["imagery_data_visualization"]


def test_object_unit_key_does_not_mistake_numeric_object_id_for_a_page() -> None:
    index = _index(6)
    observation = AtomicObservation(
        observation_id="image-resolution-object-4",
        oracle_id="v8.effective_image_resolution",
        metric_id="effective_image_resolution",
        scope=EvaluationScope.OBJECT,
        unit_key="page:3/object:4",
        local_score=0.1,
        severity=Severity.CRITICAL,
        critical=True,
    )

    plan = build_visual_selection_plan(index, _scout(6), (observation,))

    assert plan.forced_page_numbers == (3,)

    location_unknown = AtomicObservation(
        observation_id="location-unknown-object-4",
        oracle_id="v8.effective_image_resolution",
        metric_id="effective_image_resolution",
        scope=EvaluationScope.OBJECT,
        unit_key="object:4",
        local_score=0.1,
        severity=Severity.CRITICAL,
        critical=True,
    )
    without_invented_page = build_visual_selection_plan(
        index,
        _scout(6),
        (location_unknown,),
    )
    assert without_invented_page.forced_page_numbers == ()


def test_critical_and_outlier_pages_survive_a_long_deck_budget() -> None:
    index = _index(100, page_overrides={89: {"asset_outlier": True}})
    critical = _observation(
        57,
        metric_id="slide_geometry_integrity",
        severity=Severity.CRITICAL,
        critical=True,
    )

    plan = build_visual_selection_plan(index, _scout(100), (critical,))
    by_page = {item.page_number: item for item in plan.items}

    assert plan.high_resolution_budget == 14
    assert plan.forced_page_numbers == (57,)
    assert by_page[57].mandatory is True
    assert by_page[89].priority == "P2"
    assert sum(item.consumes_exploration_budget for item in plan.items) <= 14
    assert len(plan.items) <= 15


def test_small_decorative_icons_without_ocr_do_not_flood_priority_two() -> None:
    index = _index(
        20,
        page_overrides={
            page_number: {
                "asset_hash": f"decorative-icon-{page_number}",
                "image_area_ratio": 0.03,
                "missing_alt_text_count": 1,
                "missing_caption_count": 1,
            }
            for page_number in range(1, 21)
        },
        ocr_available=False,
    )

    plan = build_visual_selection_plan(index, _scout(20), ())

    assert not [item for item in plan.items if item.priority == "P2"]
    assert plan.unresolved_risk_page_numbers == ()


def test_no_risk_diagnostic_na_observations_do_not_promote_key_pages() -> None:
    index = _index(10)
    observations = tuple(
        AtomicObservation(
            observation_id=f"asset-route-{page_number}",
            oracle_id="v8.visual.page_index",
            metric_id="visual_asset_semantic_risk",
            scope=EvaluationScope.PAGE,
            unit_key=f"page:{page_number}",
            metric_status=MetricStatus.NA,
            raw_value="NO_DETERMINISTIC_RISK",
            local_score=None,
            severity=Severity.INFO,
            evidence=(
                Evidence(
                    evidence_id=f"route-evidence-{page_number}",
                    kind="visual_asset_semantic_risk",
                    message="No deterministic asset-semantic routing risk was detected.",
                    page_number=page_number,
                ),
            ),
            metadata={
                "routing_codes": [],
                "routes_to": ["imagery_data_visualization"],
                "score_affecting": False,
            },
        )
        for page_number in range(1, 11)
    )

    plan = build_visual_selection_plan(index, _scout(10), observations)

    assert not [item for item in plan.items if item.priority in {"P1", "P2"}]


def test_non_scoring_asset_route_with_a_real_risk_remains_priority_two() -> None:
    index = _index(6)
    observation = AtomicObservation(
        observation_id="asset-route-risk-5",
        oracle_id="v8.visual.page_index",
        metric_id="visual_asset_semantic_risk",
        scope=EvaluationScope.PAGE,
        unit_key="page:5",
        metric_status=MetricStatus.NA,
        raw_value="ROUTE",
        local_score=None,
        severity=Severity.MINOR,
        metadata={
            "routing_codes": ["image_semantics_unobservable"],
            "routes_to": ["imagery_data_visualization"],
            "score_affecting": False,
        },
    )

    plan = build_visual_selection_plan(index, _scout(6), (observation,))

    routed = next(item for item in plan.items if item.page_number == 5)
    assert routed.priority == "P2"
    assert routed.criteria == ("imagery_data_visualization",)


def test_repeated_small_logo_routes_are_bounded_out_of_priority_two() -> None:
    index = _index(
        20,
        page_overrides={
            page_number: {
                "asset_hash": "shared-logo",
                "duplicate_asset_hash": "shared-logo",
                "image_area_ratio": 0.03,
            }
            for page_number in range(1, 21)
        },
    )
    observations = tuple(
        AtomicObservation(
            observation_id=f"duplicate-logo-route-{page_number}",
            oracle_id="v8.visual.page_index",
            metric_id="visual_asset_semantic_risk",
            scope=EvaluationScope.PAGE,
            unit_key=f"page:{page_number}",
            metric_status=MetricStatus.NA,
            raw_value="ROUTE",
            local_score=None,
            severity=Severity.MINOR,
            metadata={
                "routing_codes": ["duplicated_asset_requires_semantic_check"],
                "routes_to": ["imagery_data_visualization"],
                "score_affecting": False,
            },
        )
        for page_number in range(1, 21)
    )

    plan = build_visual_selection_plan(index, _scout(20), observations)

    assert not [item for item in plan.items if item.priority == "P2"]
    assert plan.unresolved_risk_page_numbers == ()


def test_one_scout_suspicion_does_not_expand_a_shared_logo_to_the_whole_deck() -> None:
    index = _index(
        100,
        page_overrides={
            page_number: {
                "asset_hash": "shared-corporate-logo",
                "image_area_ratio": 0.03,
            }
            for page_number in range(1, 101)
        },
    )
    scout = _scout(
        100,
        ScoutFinding(
            page_number=57,
            risk_code="placeholder_visual_suspected",
            confidence=0.88,
            suggested_criteria=("imagery_data_visualization",),
        ),
    )

    plan = build_visual_selection_plan(index, scout, ())
    priority_two = [item for item in plan.items if item.priority == "P2"]

    assert len(priority_two) <= 5
    assert plan.unresolved_risk_page_numbers == ()


def test_criterion_risk_pages_follow_the_common_cache_prefix() -> None:
    index = _index(12)
    scout = _scout(
        12,
        ScoutFinding(
            page_number=7,
            risk_code="stock_watermark_suspected",
            confidence=0.92,
            suggested_criteria=("imagery_data_visualization",),
        ),
    )
    plan = build_visual_selection_plan(index, scout, ())

    imagery_order = criterion_page_order(plan, "imagery_data_visualization")
    typography_order = criterion_page_order(plan, "typography_legibility")

    assert imagery_order[: len(plan.common_page_local)] == plan.common_page_local
    assert typography_order[: len(plan.common_page_local)] == plan.common_page_local
    assert 7 in imagery_order
    if 7 not in plan.common_page_local:
        assert imagery_order.index(7) >= len(plan.common_page_local)
    assert len(plan.common_page_local) <= 4
    assert len(plan.common_cross_slide) <= 8


def test_criterion_progress_routes_missing_priority_zero_in_two_page_rounds() -> None:
    index = _index(10, layout_medoid=1, asset_medoid=2)
    critical = _observation(
        9,
        metric_id="slide_geometry_integrity",
        severity=Severity.CRITICAL,
        critical=True,
    )
    plan = build_visual_selection_plan(index, _scout(10), (critical,))
    audited = plan.common_page_local

    progress = assess_visual_criterion_progress(
        index,
        plan,
        "composition_layout",
        audited_page_numbers=audited,
        metric_scored=True,
        confidence=0.90,
        composite_lower_bound=82.0,
        composite_upper_bound=88.0,
    )

    assert progress.continue_audit is True
    assert 9 in progress.required_page_numbers
    assert progress.next_page_numbers[0] == 9
    assert len(progress.next_page_numbers) <= 2
    assert "REQUIRED_P0_P1_NOT_AUDITED" in progress.unresolved_codes


def test_criterion_progress_allows_early_stop_with_only_unreviewed_p2() -> None:
    index = _index(
        8,
        page_overrides={
            7: {
                "rule_severity": "MAJOR",
                "rule_metrics": ("slide_geometry_integrity",),
            }
        },
        layout_medoid=1,
        asset_medoid=2,
    )
    plan = build_visual_selection_plan(index, _scout(8), ())

    progress = assess_visual_criterion_progress(
        index,
        plan,
        "composition_layout",
        audited_page_numbers=plan.common_page_local,
        metric_scored=True,
        confidence=0.92,
        composite_lower_bound=82.0,
        composite_upper_bound=87.0,
    )

    assert progress.continue_audit is False
    assert progress.coverage_complete_for_criterion is True
    assert progress.requires_review is False
    assert progress.stopping_reason == "ADAPTIVE_STOP_CONDITIONS_MET"
    assert progress.metadata["priority_two_candidate_pages_not_audited"] == [7]


def test_criterion_progress_requires_the_kind_owned_cluster_medoid() -> None:
    index = _index(8, layout_medoid=3, asset_medoid=2)
    plan = build_visual_selection_plan(index, _scout(8), ())

    progress = assess_visual_criterion_progress(
        index,
        plan,
        "composition_layout",
        audited_page_numbers=(1,),
        metric_scored=True,
        confidence=0.90,
        composite_lower_bound=82.0,
        composite_upper_bound=88.0,
    )

    assert progress.continue_audit is True
    assert progress.uncovered_cluster_ids == ("layout-style-main",)
    assert "CRITERION_CLUSTER_MEDOID_NOT_AUDITED" in progress.unresolved_codes
    assert 3 in progress.remaining_page_numbers


def test_cluster_coverage_has_one_primary_criterion_owner_per_kind() -> None:
    index = _index(8, layout_medoid=3, asset_medoid=2)
    plan = build_visual_selection_plan(index, _scout(8), ())

    for criterion_id in ("cross_slide_consistency", "authorship_specificity"):
        progress = assess_visual_criterion_progress(
            index,
            plan,
            criterion_id,
            audited_page_numbers=(1,),
            metric_scored=True,
            confidence=0.90,
            composite_lower_bound=82.0,
            composite_upper_bound=88.0,
        )

        assert progress.uncovered_cluster_ids == ()
        assert "CRITERION_CLUSTER_MEDOID_NOT_AUDITED" not in progress.unresolved_codes

    imagery = assess_visual_criterion_progress(
        index,
        plan,
        "imagery_data_visualization",
        audited_page_numbers=(1,),
        metric_scored=True,
        confidence=0.90,
        composite_lower_bound=82.0,
        composite_upper_bound=88.0,
    )
    assert imagery.uncovered_cluster_ids == ("asset-content-main",)
    assert "CRITERION_CLUSTER_MEDOID_NOT_AUDITED" in imagery.unresolved_codes


def test_high_resolution_asset_context_expansion_stays_inside_bmax() -> None:
    index = _index(
        20,
        page_overrides={
            1: {"asset_hash": "shared-risk"},
            18: {"asset_hash": "shared-risk"},
            **{
                page: {
                    "rule_severity": "MAJOR",
                    "rule_metrics": ("slide_geometry_integrity",),
                }
                for page in range(2, 6)
            },
        },
    )
    plan = build_visual_selection_plan(index, _scout(20), ())
    original_ordinary = sum(
        item.consumes_exploration_budget for item in plan.items
    )

    expanded, pending = expand_visual_selection_plan_for_asset_context(
        index,
        plan,
        seed_page_numbers=(1,),
        audited_page_numbers=plan.common_page_local,
    )

    assert expanded.deck_sha256 == plan.deck_sha256
    assert expanded.rendered_page_set_sha256 == plan.rendered_page_set_sha256
    assert expanded.common_page_local == plan.common_page_local
    assert expanded.common_cross_slide == plan.common_cross_slide
    assert sum(
        item.consumes_exploration_budget for item in expanded.items
    ) <= expanded.high_resolution_budget
    assert original_ordinary <= expanded.high_resolution_budget
    assert set(pending) <= {item.page_number for item in expanded.items} | set(
        expanded.metadata["unresolved_asset_context_page_numbers"]
    )
    assert 18 in pending
    assert 18 in criterion_page_order(
        expanded,
        "imagery_data_visualization",
    )
    assert expanded.metadata["parent_selection_plan_id"] == plan.plan_id


def test_asset_context_never_evicts_p0_p1_or_cache_prefix_pages() -> None:
    index = _index(
        30,
        page_overrides={
            1: {"asset_hash": "late-context"},
            29: {"asset_hash": "late-context"},
        },
    )
    findings = tuple(
        ScoutFinding(
            page_number=page,
            risk_code="semantic_mismatch_suspected",
            confidence=0.95,
            suggested_criteria=("imagery_data_visualization",),
        )
        for page in range(2, 14)
    )
    plan = build_visual_selection_plan(index, _scout(30, *findings), ())
    original_pages = {item.page_number for item in plan.items}
    protected = ({
        item.page_number
        for item in plan.items
        if item.priority in {"P0", "P1"}
    } | set(plan.common_page_local) | set(plan.common_cross_slide) | {
        cluster.medoid_page_number
        for cluster in (*index.layout_clusters, *index.asset_clusters)
    }) & original_pages

    expanded, pending = expand_visual_selection_plan_for_asset_context(
        index,
        plan,
        seed_page_numbers=(1,),
        audited_page_numbers=plan.common_page_local,
    )

    assert protected <= {item.page_number for item in expanded.items}
    assert len(
        [item for item in expanded.items if item.consumes_exploration_budget]
    ) <= plan.high_resolution_budget
    if 29 not in {item.page_number for item in expanded.items}:
        assert 29 in pending


def test_other_criterion_page_does_not_count_as_imagery_context_audited() -> None:
    index = _index(
        12,
        page_overrides={
            1: {"asset_hash": "shared-context"},
            9: {"asset_hash": "shared-context"},
        },
    )
    plan = build_visual_selection_plan(index, _scout(12), ())

    _expanded, pending = expand_visual_selection_plan_for_asset_context(
        index,
        plan,
        seed_page_numbers=(1,),
        audited_page_numbers=(),
        protected_page_numbers=(9,),
    )
    assert 9 in pending

    _expanded, already_audited_pending = (
        expand_visual_selection_plan_for_asset_context(
            index,
            plan,
            seed_page_numbers=(1,),
            audited_page_numbers=(9,),
        )
    )
    assert 9 not in already_audited_pending


def test_criterion_progress_stops_review_when_required_p1_exceeds_bmax() -> None:
    index = _index(30)
    findings = tuple(
        ScoutFinding(
            page_number=page_number,
            risk_code="semantic_mismatch_suspected",
            confidence=0.91,
            suggested_criteria=("imagery_data_visualization",),
        )
        for page_number in range(1, 31)
    )
    plan = build_visual_selection_plan(index, _scout(30, *findings), ())
    audited = criterion_page_order(plan, "imagery_data_visualization")

    progress = assess_visual_criterion_progress(
        index,
        plan,
        "imagery_data_visualization",
        audited_page_numbers=audited,
        metric_scored=True,
        confidence=0.90,
        composite_lower_bound=82.0,
        composite_upper_bound=88.0,
    )

    assert progress.continue_audit is False
    assert progress.requires_review is True
    assert progress.stopping_reason == "SELECTION_BUDGET_EXHAUSTED_REVIEW"
    assert progress.metadata["unselectable_required_page_numbers"]


def test_criterion_progress_does_not_spend_more_pages_after_model_failure() -> None:
    index = _index(8)
    plan = build_visual_selection_plan(index, _scout(8), ())

    progress = assess_visual_criterion_progress(
        index,
        plan,
        "composition_layout",
        audited_page_numbers=plan.common_page_local,
        metric_scored=False,
        confidence=None,
    )

    assert progress.continue_audit is False
    assert progress.next_page_numbers == ()
    assert progress.requires_review is True
    assert progress.stopping_reason == "MODEL_UNRESOLVED_REVIEW"


def test_each_round_adds_two_unique_pages_and_continues_for_new_risk() -> None:
    index = _index(8)
    plan = build_visual_selection_plan(index, _scout(8), ())
    first = next_visual_audit_pages(plan)
    second = next_visual_audit_pages(plan, first)

    assert len(first) == 2
    assert len(second) <= 2
    assert set(first).isdisjoint(second)

    audit_round = assess_visual_audit_round(
        index,
        plan,
        round_number=1,
        page_numbers=first,
        criterion_pages={"composition_layout": first},
        new_major_count=1,
        composite_lower_bound=75.0,
        composite_upper_bound=83.0,
    )

    assert audit_round.continue_audit is True
    assert audit_round.stopping_reason is None
    assert "NEW_MAJOR_OR_CRITICAL" in audit_round.usage["continuation_triggers"]
    assert (
        "DECISION_INTERVAL_CROSSES_THRESHOLD"
        in audit_round.usage["continuation_triggers"]
    )
    assert len(audit_round.usage["next_page_numbers"]) <= 2

    with pytest.raises(ValueError, match="duplicate"):
        assess_visual_audit_round(
            index,
            plan,
            round_number=2,
            page_numbers=(second[0], second[0]),
            criterion_pages={"composition_layout": (second[0],)},
            audited_before=first,
        )


def test_round_stops_only_after_gate_priority_and_cluster_conditions_resolve() -> None:
    index = _index(4, layout_medoid=1, asset_medoid=2)
    plan = build_visual_selection_plan(index, _scout(4), ())
    current = (1, 2)

    audit_round = assess_visual_audit_round(
        index,
        plan,
        round_number=1,
        page_numbers=current,
        criterion_pages={
            "composition_layout": (1,),
            "imagery_data_visualization": (2,),
        },
        composite_lower_bound=81.0,
        composite_upper_bound=85.0,
    )

    assert audit_round.continue_audit is False
    assert audit_round.stopping_reason == "COVERAGE_COMPLETE"
    assert audit_round.uncovered_cluster_ids == ()


def test_round_cannot_resolve_a_gate_with_the_wrong_visual_criterion() -> None:
    index = _index(3, layout_medoid=1, asset_medoid=2)
    critical = _observation(
        3,
        metric_id="slide_geometry_integrity",
        severity=Severity.CRITICAL,
        critical=True,
    )
    plan = build_visual_selection_plan(index, _scout(3), (critical,))

    audit_round = assess_visual_audit_round(
        index,
        plan,
        round_number=1,
        page_numbers=(3,),
        criterion_pages={"typography_legibility": (3,)},
        audited_before=(1, 2),
        audited_criterion_pages_before={
            "composition_layout": (1,),
            "imagery_data_visualization": (2,),
        },
        resolved_hard_gate_pages=(3,),
    )

    assert audit_round.continue_audit is True
    assert audit_round.stopping_reason is None
    assert "UNRESOLVED_HARD_GATE" in audit_round.usage["continuation_triggers"]
    assert (
        audit_round.usage["pending_isomorphic_criterion_pages"]
        ["composition_layout"]
        == [3]
    )


def test_budget_shortage_leaves_forced_gate_na_and_requires_review() -> None:
    index = _index(20)
    critical = _observation(
        19,
        metric_id="slide_geometry_integrity",
        severity=Severity.CRITICAL,
        critical=True,
    )
    scout = _scout(20)
    plan = build_visual_selection_plan(index, scout, (critical,))
    audited = tuple(page for page in plan.common_page_local if page != 19)

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={"composition_layout": audited},
        resolved_hard_gate_pages=(),
    )

    assert certificate.coverage_complete is False
    assert certificate.forced_pages_not_audited == (19,)
    assert "hard_gate_not_resolved" in certificate.unresolved_risk_codes
    assert certificate.stopping_reason == "FORCED_PAGE_NOT_AUDITED_REVIEW"
    assert certificate.metadata["hard_gate_evidence_status"] == "N/A"
    assert certificate.metadata["required_decision"] == "REVIEW"


def test_gate_resolution_requires_explicit_isomorphic_criterion_evidence() -> None:
    index = _index(6)
    critical = _observation(
        5,
        metric_id="slide_geometry_integrity",
        severity=Severity.CRITICAL,
        critical=True,
    )
    scout = _scout(6)
    plan = build_visual_selection_plan(index, scout, (critical,))

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={"typography_legibility": (1, 2, 5)},
        resolved_hard_gate_pages=(5,),
    )

    assert certificate.resolved_hard_gate_pages == ()
    assert "hard_gate_resolution_lacks_isomorphic_evidence" in (
        certificate.unresolved_risk_codes
    )
    assert certificate.metadata["invalid_hard_gate_resolution_page_numbers"] == [5]
    assert certificate.metadata["required_decision"] == "REVIEW"

    with pytest.raises(ValueError, match="unknown visual criterion"):
        build_visual_coverage_certificate(
            index,
            scout,
            plan,
            (),
            criterion_pages={"fake_criterion": (1, 2, 5)},
            resolved_hard_gate_pages=(5,),
        )


def test_scheduled_round_without_successful_criterion_does_not_count_as_coverage() -> None:
    index = _index(3, layout_medoid=1, asset_medoid=2)
    scout = _scout(3)
    plan = build_visual_selection_plan(index, scout, ())
    scheduled_only = VisualAuditRound(
        round_number=1,
        page_numbers=(1, 2),
        criterion_pages={},
        continue_audit=False,
        stopping_reason="PROVIDER_ERROR_REVIEW",
    )

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (scheduled_only,),
        criterion_pages={},
    )

    assert certificate.high_resolution_page_numbers == ()
    assert certificate.covered_cluster_ids == ()
    assert certificate.coverage_complete is False
    assert certificate.metadata["required_decision"] == "REVIEW"


def test_certificate_rejects_repeated_pages_across_progressive_rounds() -> None:
    index = _index(4)
    scout = _scout(4)
    plan = build_visual_selection_plan(index, scout, ())
    first = VisualAuditRound(
        round_number=1,
        page_numbers=(1, 2),
        criterion_pages={"composition_layout": (1, 2)},
        continue_audit=True,
    )
    repeated = VisualAuditRound(
        round_number=2,
        page_numbers=(2, 4),
        criterion_pages={"composition_layout": (2, 4)},
        continue_audit=False,
        stopping_reason="COVERAGE_COMPLETE",
    )

    with pytest.raises(ValueError, match="criterion/page evidence"):
        build_visual_coverage_certificate(
            index,
            scout,
            plan,
            (first, repeated),
            criterion_pages={},
        )


def test_same_page_can_be_independently_audited_by_two_criteria() -> None:
    index = _index(4)
    scout = _scout(4)
    plan = build_visual_selection_plan(index, scout, ())
    composition = VisualAuditRound(
        round_number=1,
        page_numbers=(2,),
        criterion_pages={"composition_layout": (2,)},
        continue_audit=True,
    )
    imagery = VisualAuditRound(
        round_number=2,
        page_numbers=(2,),
        criterion_pages={"imagery_data_visualization": (2,)},
        continue_audit=False,
        stopping_reason="PLAN_EXHAUSTED_REVIEW",
    )

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (composition, imagery),
        criterion_pages={},
    )

    assert certificate.criterion_pages["composition_layout"] == (2,)
    assert certificate.criterion_pages["imagery_data_visualization"] == (2,)
    assert certificate.high_resolution_page_numbers == (2,)


def test_unrouted_mandatory_rule_is_not_mislabeled_as_budget_overflow() -> None:
    index = _index(
        4,
        page_overrides={
            3: {
                "rule_severity": "CRITICAL",
                "rule_metrics": ("non_visual_unknown_gate",),
            }
        },
    )
    scout = _scout(4)
    plan = build_visual_selection_plan(index, scout, ())

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={"composition_layout": (1, 2, 3)},
        resolved_hard_gate_pages=(3,),
    )

    assert "mandatory_visual_criterion_unrouted" in certificate.unresolved_risk_codes
    assert "selection_budget_overflow" not in certificate.unresolved_risk_codes
    assert certificate.stopping_reason == "MANDATORY_VISUAL_CRITERION_UNROUTED_REVIEW"
    assert certificate.metadata["actual_budget_overflow_page_numbers"] == []


def test_priority_one_overflow_is_persisted_instead_of_silently_dropped() -> None:
    index = _index(30)
    findings = tuple(
        ScoutFinding(
            page_number=page_number,
            risk_code="semantic_mismatch_suspected",
            confidence=0.91,
            suggested_criteria=("imagery_data_visualization",),
        )
        for page_number in range(1, 31)
    )

    plan = build_visual_selection_plan(index, _scout(30, *findings), ())

    assert plan.high_resolution_budget == 10
    assert len([item for item in plan.items if item.priority == "P1"]) == 10
    assert len(plan.unresolved_risk_page_numbers) == 20
    assert set(plan.unresolved_risk_page_numbers).isdisjoint(
        item.page_number for item in plan.items
    )


def test_complete_certificate_requires_atlas_clusters_and_no_unresolved_signal() -> None:
    index = _index(3, layout_medoid=1, asset_medoid=2)
    scout = _scout(3)
    plan = build_visual_selection_plan(index, scout, ())

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={
            "composition_layout": (1,),
            "imagery_data_visualization": (2,),
        },
    )

    assert certificate.atlas_coverage_complete is True
    assert certificate.coverage_complete is True
    assert certificate.covered_cluster_ids == (
        "asset-content-main",
        "layout-style-main",
    )
    assert certificate.uncovered_cluster_ids == ()
    assert certificate.metadata["required_decision"] is None
    assert certificate.metadata["unobserved_pages_received_no_inferred_score"] is True


def test_cluster_certificate_does_not_accept_a_secondary_criterion_as_owner() -> None:
    index = _index(3, layout_medoid=1, asset_medoid=2)
    scout = _scout(3)
    plan = build_visual_selection_plan(index, scout, ())

    secondary_only = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={
            "cross_slide_consistency": (1, 2),
            "authorship_specificity": (1, 2),
        },
    )

    assert secondary_only.coverage_complete is False
    assert secondary_only.covered_cluster_ids == ()
    assert secondary_only.uncovered_cluster_ids == (
        "asset-content-main",
        "layout-style-main",
    )

    primary_owners = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={
            "composition_layout": (1,),
            "imagery_data_visualization": (2,),
        },
    )
    assert primary_owners.coverage_complete is True
    assert primary_owners.uncovered_cluster_ids == ()


def test_priority_two_candidate_alone_does_not_defeat_a_valid_early_stop() -> None:
    index = _index(
        8,
        page_overrides={
            7: {
                "rule_severity": "MAJOR",
                "rule_metrics": ("slide_geometry_integrity",),
            }
        },
        layout_medoid=1,
        asset_medoid=2,
    )
    scout = _scout(8)
    plan = build_visual_selection_plan(index, scout, ())

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={
            "composition_layout": (1,),
            "imagery_data_visualization": (2,),
        },
    )

    assert certificate.coverage_complete is True
    assert certificate.unresolved_risk_codes == ()
    assert certificate.metadata["priority_two_pages_not_audited"] == [7]
    assert certificate.metadata["required_decision"] is None


def test_scout_failure_marks_semantic_coverage_incomplete_even_with_rule_pages() -> None:
    index = _index(6)
    scout = _scout(6, complete=False)
    plan = build_visual_selection_plan(index, scout, ())
    audited = tuple(cluster.medoid_page_number for cluster in (
        *index.layout_clusters,
        *index.asset_clusters,
    ))

    certificate = build_visual_coverage_certificate(
        index,
        scout,
        plan,
        (),
        criterion_pages={"composition_layout": audited},
    )

    assert certificate.atlas_coverage_complete is False
    assert certificate.semantic_coverage_complete is False
    assert certificate.coverage_complete is False
    assert certificate.stopping_reason == "SCOUT_INCOMPLETE_REVIEW"
    assert certificate.metadata["required_decision"] == "REVIEW"
