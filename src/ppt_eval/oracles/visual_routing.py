"""Non-scoring Profile 8.4 visual indexing, Scout, selection and coverage Oracles."""

from __future__ import annotations

import hashlib
import queue
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence, TypeVar

from ppt_eval.adapters import ModelAuditProvider, PptxAdapter
from ppt_eval.application.model_request_budget import ModelRequestBudgetLedger
from ppt_eval.application.oracle import (
    EvaluationContext,
    MetricDefinition,
    OracleDescriptor,
)
from ppt_eval.application.visual_index import build_visual_page_index
from ppt_eval.application.visual_selection import (
    build_visual_coverage_certificate,
    build_visual_selection_plan,
)
from ppt_eval.domain import (
    AtlasScoutResult,
    AtomicObservation,
    EvaluationScope,
    Evidence,
    ExecutionStatus,
    MetricStatus,
    ObservationBatch,
    OracleResult,
    ScoreRole,
    Severity,
    VisualAuditRound,
    VisualPageFeatures,
    VisualPageIndex,
    VisualSelectionPlan,
)
from ppt_eval.infrastructure.atlas_scout import AtlasBuilder, AtlasScoutRunner
from ppt_eval.scoring import PptPdmsAggregator

from .base import load_presentation
from .model_audits import _rendered_images

VISUAL_PAGE_INDEX_ORACLE_ID = "v8.visual.page_index"
ATLAS_SCOUT_ORACLE_ID = "v8.visual.atlas_scout"
VISUAL_SELECTION_ORACLE_ID = "v8.visual.selection"
VISUAL_COVERAGE_ORACLE_ID = "v8.visual.coverage"
VISUAL_ASSET_SEMANTIC_RISK_METRIC_ID = "visual_asset_semantic_risk"

_VISUAL_CONTRACT_MEMO_KEY = "ppt_eval.visual_contracts"
_VISUAL_INDEX_MEMO_KEY = "ppt_eval.visual_page_index"
_ATLAS_SCOUT_MEMO_KEY = "ppt_eval.atlas_scout_result"
_T = TypeVar("_T")


def _contracts(context: EvaluationContext) -> MutableMapping[str, Any]:
    value = context.memo.setdefault(_VISUAL_CONTRACT_MEMO_KEY, {})
    if not isinstance(value, MutableMapping):
        raise TypeError("visual contract memo store must be a mutable mapping")
    return value


def _store_visual_contract(
    context: EvaluationContext,
    *,
    name: str,
    memo_key: str,
    value: Any,
) -> None:
    context.memo[memo_key] = value
    _contracts(context)[name] = value


class VisualPageIndexOracle:
    """Build a deterministic full-deck router before any semantic model call."""

    oracle_id = VISUAL_PAGE_INDEX_ORACLE_ID
    version = "1.0.0"

    def __init__(self, adapter: PptxAdapter | None = None) -> None:
        self.adapter = adapter or PptxAdapter()

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(
                MetricDefinition(
                    VISUAL_ASSET_SEMANTIC_RISK_METRIC_ID,
                    ScoreRole.DIAGNOSTIC,
                ),
            ),
            deterministic=True,
            description=(
                "Full-page structural and low-resolution visual index; routing only."
            ),
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> ObservationBatch:
        presentation = load_presentation(context, self.adapter)
        try:
            model_images = _rendered_images(context)
        except (OSError, TypeError, ValueError):
            model_images = ()
        ocr_value = context.artifacts.get("ocr_text_by_page")
        ocr_text_by_page: Mapping[int, str | None] | None = None
        if isinstance(ocr_value, Mapping):
            normalized_ocr: dict[int, str | None] = {}
            for raw_page, raw_text in ocr_value.items():
                if isinstance(raw_page, bool):
                    continue
                try:
                    page_number = int(raw_page)
                except (TypeError, ValueError):
                    continue
                if raw_text is None or isinstance(raw_text, str):
                    normalized_ocr[page_number] = raw_text
            ocr_text_by_page = normalized_ocr
        observations = tuple(
            item
            for item in context.memo.get("ppt_eval.atomic_observations", ())
            if isinstance(item, AtomicObservation)
        )
        index = build_visual_page_index(
            presentation,
            rendered_images={item.page_number: item for item in model_images},
            observations=observations,
            ocr_text_by_page=ocr_text_by_page,
        )
        _store_visual_contract(
            context,
            name="visual_page_index",
            memo_key=_VISUAL_INDEX_MEMO_KEY,
            value=index,
        )
        routed = tuple(
            _asset_semantic_risk_observation(index, page) for page in index.pages
        )
        return ObservationBatch(
            oracle_id=self.oracle_id,
            observations=routed,
            version=self.version,
            metadata={
                "score_affecting": False,
                "total_pages": len(index.pages),
                "rendered_pages": list(index.rendered_page_numbers),
                "rendered_page_set_sha256": index.rendered_page_set_sha256,
                "layout_cluster_count": len(index.layout_clusters),
                "asset_cluster_count": len(index.asset_clusters),
                "ocr_available": index.ocr_available,
            },
        )


class AtlasScoutOracle:
    """Run the full-page Atlas router without creating a score or verdict."""

    oracle_id = ATLAS_SCOUT_ORACLE_ID
    metric_id = "visual_atlas_scout_routing"
    version = "1.0.0"

    def __init__(
        self,
        primary_provider: ModelAuditProvider | None,
        fallback_provider: ModelAuditProvider | None,
    ) -> None:
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.DIAGNOSTIC),),
            deterministic=False,
            description="Qwen-first full-page low-resolution Atlas routing; never scores.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        index = context.memo.get(_VISUAL_INDEX_MEMO_KEY)
        if not isinstance(index, VisualPageIndex):
            return self._unavailable(
                context,
                total_pages=0,
                code="VISUAL_PAGE_INDEX_UNAVAILABLE",
            )
        try:
            images = _rendered_images(context)
        except (OSError, TypeError, ValueError):
            images = ()
        expected_pages = tuple(range(1, len(index.pages) + 1))
        if tuple(item.page_number for item in images) != expected_pages:
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_RENDER_COVERAGE_INCOMPLETE",
            )
        if self.primary_provider is None:
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_SCOUT_PROVIDER_UNCONFIGURED",
            )
        visual_policy = context.profile.metadata.get("visual_audit")
        maximum_scout_batches = (
            visual_policy.get("maximum_scout_batches", 4)
            if isinstance(visual_policy, Mapping)
            else 4
        )
        maximum_model_requests = (
            visual_policy.get("maximum_model_requests", 64)
            if isinstance(visual_policy, Mapping)
            else 64
        )
        if (
            isinstance(maximum_scout_batches, bool)
            or not isinstance(maximum_scout_batches, int)
            or maximum_scout_batches < 1
            or isinstance(maximum_model_requests, bool)
            or not isinstance(maximum_model_requests, int)
            or maximum_model_requests < 1
        ):
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_SCOUT_PROFILE_BUDGET_INVALID",
            )
        if len(index.pages) > maximum_scout_batches * 192:
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_SCOUT_PROFILE_REQUEST_BUDGET_EXCEEDED",
            )
        output_value = context.artifacts.get("visual_atlas_dir")
        if not isinstance(output_value, (str, Path)):
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_ARTIFACT_DIRECTORY_UNAVAILABLE",
            )
        runner = AtlasScoutRunner(
            self.primary_provider,
            self.fallback_provider,
            atlas_builder=AtlasBuilder(output_value),
        )
        internal_cancel = threading.Event()
        try:
            ledger_value = context.memo.get("ppt_eval.model_request_budget")
            scout = _call_before_scheduler_timeout(
                lambda: runner.run(
                    images,
                    case_id=context.case.case_id,
                    scene=context.case.scene.value,
                    deck_sha256=index.deck_sha256,
                    rendered_page_set_sha256=index.rendered_page_set_sha256,
                    cancelled=lambda: (
                        internal_cancel.is_set() or _evaluation_cancelled(context)
                    ),
                    maximum_model_requests=maximum_model_requests,
                    request_budget_ledger=(
                        ledger_value
                        if isinstance(ledger_value, ModelRequestBudgetLedger)
                        else None
                    ),
                ),
                timeout_seconds=max(
                    0.001,
                    float(context.profile.oracle_timeout_seconds) * 0.90,
                ),
                cancel_event=internal_cancel,
            )
        except TimeoutError:
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_SCOUT_INTERNAL_TIMEOUT",
            )
        except (OSError, TypeError, ValueError):
            return self._unavailable(
                context,
                total_pages=len(index.pages),
                code="ATLAS_SCOUT_EXECUTION_FAILED",
            )
        _store_visual_contract(
            context,
            name="atlas_scout",
            memo_key=_ATLAS_SCOUT_MEMO_KEY,
            value=scout,
        )
        evidence = tuple(
            Evidence(
                evidence_id=(
                    f"atlas-scout-{finding.page_number}-{finding.risk_code}"
                ),
                kind="atlas_scout_route",
                message=(
                    "Low-resolution Atlas routing identified a page for targeted "
                    "high-resolution visual inspection."
                ),
                page_number=finding.page_number,
                payload={
                    "risk_code": finding.risk_code,
                    "confidence": finding.confidence,
                    "suggested_criteria": list(finding.suggested_criteria),
                    "atlas_id": finding.atlas_id,
                    "score_affecting": False,
                },
            )
            for finding in scout.findings
        )
        usage_cost = scout.usage.get("cost", 0.0)
        cost = (
            float(usage_cost)
            if not isinstance(usage_cost, bool)
            and isinstance(usage_cost, (int, float))
            and float(usage_cost) >= 0.0
            else 0.0
        )
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.NA,
            score_role=ScoreRole.DIAGNOSTIC,
            raw_value=len(scout.findings),
            confidence=1.0 if scout.coverage_complete else 0.0,
            severity=(Severity.INFO if scout.coverage_complete else Severity.MAJOR),
            evidence=evidence,
            version=self.version,
            cost=cost,
            metadata={
                "score_affecting": False,
                "scout_id": scout.scout_id,
                "coverage_complete": scout.coverage_complete,
                "covered_page_numbers": list(scout.covered_page_numbers),
                "atlas_ids": list(scout.atlas_ids),
                "finding_count": len(scout.findings),
                "provider_id": scout.provider_id,
                "model_id": scout.model_id,
                "error_code": scout.error_code,
                "usage": dict(scout.usage),
                "audit_metadata": dict(scout.audit_metadata),
            },
        )

    def _unavailable(
        self,
        context: EvaluationContext,
        *,
        total_pages: int,
        code: str,
    ) -> OracleResult:
        index = context.memo.get(_VISUAL_INDEX_MEMO_KEY)
        scout = AtlasScoutResult(
            scout_id=_unavailable_scout_id(context.case.case_id, code),
            findings=(),
            covered_page_numbers=(),
            coverage_complete=False,
            deck_sha256=(
                index.deck_sha256 if isinstance(index, VisualPageIndex) else None
            ),
            rendered_page_set_sha256=(
                index.rendered_page_set_sha256
                if isinstance(index, VisualPageIndex)
                else None
            ),
            error_code=code,
            audit_metadata={
                "score_affecting": False,
                "total_pages": total_pages,
            },
        )
        _store_visual_contract(
            context,
            name="atlas_scout",
            memo_key=_ATLAS_SCOUT_MEMO_KEY,
            value=scout,
        )
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.NA,
            score_role=ScoreRole.DIAGNOSTIC,
            confidence=0.0,
            severity=Severity.MAJOR,
            evidence=(
                Evidence(
                    evidence_id=f"atlas-scout-unavailable-{code.lower()}",
                    kind="atlas_scout_unavailable",
                    message=(
                        "Atlas semantic routing was unavailable; deterministic rules "
                        "and clusters remain usable, but semantic coverage is incomplete."
                    ),
                    payload={
                        "reason_code": code,
                        "total_pages": total_pages,
                        "score_affecting": False,
                    },
                ),
            ),
            version=self.version,
            metadata={
                "reason_code": code,
                "score_affecting": False,
                "coverage_complete": False,
            },
        )


def _call_before_scheduler_timeout(
    call: Callable[[], _T],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event,
) -> _T:
    """Return before the outer DAG timeout so a failure contract can persist.

    Provider transports are synchronous and cannot be forcefully interrupted.
    Their global request reservation therefore remains charged while this
    daemon finishes, but the Oracle can still persist a hash-bound failed Scout
    and let deterministic Selection/Coverage converge to REVIEW.
    """

    completed: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            completed.put((True, call()))
        except BaseException as exc:  # pragma: no cover - re-raised below
            completed.put((False, exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        cancel_event.set()
        raise TimeoutError("Atlas Scout exceeded its internal persistence deadline")
    succeeded, payload = completed.get_nowait()
    if not succeeded:
        if isinstance(payload, BaseException):
            raise payload
        raise RuntimeError("Atlas Scout worker failed without an exception")
    return payload  # type: ignore[return-value]


class VisualSelectionOracle:
    """Freeze the shared P0-P3 high-resolution plan before criterion calls."""

    oracle_id = VISUAL_SELECTION_ORACLE_ID
    metric_id = "visual_selection_plan"
    version = "3.0.0"

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.DIAGNOSTIC),),
            deterministic=True,
            description="Deterministic shared P0-P3 page routing; never scores.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        index = context.memo.get(_VISUAL_INDEX_MEMO_KEY)
        scout = context.memo.get(_ATLAS_SCOUT_MEMO_KEY)
        if not isinstance(index, VisualPageIndex) or not isinstance(
            scout, AtlasScoutResult
        ):
            return OracleResult(
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.NA,
                score_role=ScoreRole.DIAGNOSTIC,
                confidence=0.0,
                severity=Severity.MAJOR,
                version=self.version,
                metadata={
                    "reason_code": "VISUAL_ROUTING_INPUT_UNAVAILABLE",
                    "score_affecting": False,
                },
            )
        observations = tuple(
            item
            for item in context.memo.get("ppt_eval.atomic_observations", ())
            if isinstance(item, AtomicObservation)
        )
        plan = build_visual_selection_plan(index, scout, observations)
        _store_visual_contract(
            context,
            name="visual_selection_plan",
            memo_key="ppt_eval.visual_selection_plan",
            value=plan,
        )
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.NA,
            score_role=ScoreRole.DIAGNOSTIC,
            raw_value=len(plan.items),
            confidence=1.0,
            severity=(
                Severity.MINOR
                if plan.unresolved_risk_page_numbers
                else Severity.INFO
            ),
            evidence=(
                Evidence(
                    evidence_id=f"visual-selection-{plan.plan_id}",
                    kind="visual_selection_plan",
                    message=(
                        "The Harness froze one shared high-resolution page plan for "
                        "independent visual criteria."
                    ),
                    payload={
                        "plan_id": plan.plan_id,
                        "selected_page_count": len(plan.items),
                        "forced_page_numbers": list(plan.forced_page_numbers),
                        "ordinary_budget": plan.high_resolution_budget,
                        "score_affecting": False,
                    },
                ),
            ),
            version=self.version,
            metadata={
                "score_affecting": False,
                "plan_id": plan.plan_id,
                "selected_page_numbers": list(
                    plan.metadata.get("selection_order", ())
                ),
                "forced_page_numbers": list(plan.forced_page_numbers),
                "common_page_local": list(plan.common_page_local),
                "common_cross_slide": list(plan.common_cross_slide),
                "high_resolution_budget": plan.high_resolution_budget,
                "unresolved_risk_page_numbers": list(
                    plan.unresolved_risk_page_numbers
                ),
            },
        )


class VisualCoverageOracle:
    """Certify routing/audit completeness and emit one semantic REVIEW signal."""

    oracle_id = VISUAL_COVERAGE_ORACLE_ID
    metric_id = "visual_audit_coverage"
    version = "1.0.0"

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, ScoreRole.DIAGNOSTIC),),
            deterministic=True,
            description="Traceable visual coverage certificate; never a quality score.",
        )

    def supports(self, context: EvaluationContext) -> bool:
        return bool(context.case.pptx_path)

    def evaluate(self, context: EvaluationContext) -> OracleResult:
        index = context.memo.get(_VISUAL_INDEX_MEMO_KEY)
        scout = context.memo.get(_ATLAS_SCOUT_MEMO_KEY)
        plan = context.memo.get("ppt_eval.visual_selection_plan")
        if (
            not isinstance(index, VisualPageIndex)
            or not isinstance(scout, AtlasScoutResult)
            or not isinstance(plan, VisualSelectionPlan)
        ):
            return OracleResult(
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                execution_status=ExecutionStatus.SUCCESS,
                metric_status=MetricStatus.NA,
                score_role=ScoreRole.DIAGNOSTIC,
                confidence=0.0,
                severity=Severity.MAJOR,
                version=self.version,
                metadata={
                    "reason_code": "VISUAL_COVERAGE_INPUT_UNAVAILABLE",
                    "required_decision": "REVIEW",
                    "score_affecting": False,
                },
            )
        raw_criterion_pages = context.memo.get(
            "ppt_eval.visual_criterion_pages", {}
        )
        criterion_pages = (
            {
                str(criterion): tuple(
                    int(page_number) for page_number in pages
                )
                for criterion, pages in raw_criterion_pages.items()
                if isinstance(pages, Sequence)
                and not isinstance(pages, (str, bytes))
            }
            if isinstance(raw_criterion_pages, Mapping)
            else {}
        )
        prior_results = tuple(
            item
            for item in context.memo.get("ppt_eval.oracle_results", ())
            if isinstance(item, OracleResult)
        )
        resolved_hard_gates = _resolved_hard_gate_pages(
            plan,
            criterion_pages,
            prior_results,
        )
        projected_interval = _projected_pdms_interval(context, prior_results)
        rounds = _global_visual_audit_rounds(
            index,
            plan,
            criterion_pages,
            prior_results,
            resolved_hard_gate_pages=resolved_hard_gates,
            composite_interval=projected_interval,
        )
        context.memo["ppt_eval.visual_audit_rounds"] = rounds
        _contracts(context)["visual_audit_rounds"] = rounds
        unresolved_codes = _unresolved_visual_risk_codes(
            scout,
            plan,
            prior_results,
        )
        raw_pending_context = context.memo.get(
            "ppt_eval.visual_pending_asset_context_pages", ()
        )
        pending_asset_context = (
            tuple(
                int(page)
                for page in raw_pending_context
                if isinstance(page, int) and not isinstance(page, bool)
            )
            if isinstance(raw_pending_context, Sequence)
            and not isinstance(raw_pending_context, (str, bytes))
            else ()
        )
        certificate = build_visual_coverage_certificate(
            index,
            scout,
            plan,
            rounds,
            criterion_pages=criterion_pages,
            resolved_hard_gate_pages=resolved_hard_gates,
            unresolved_risk_codes=unresolved_codes,
            pending_asset_context_pages=pending_asset_context,
        )
        ledger_value = context.memo.get("ppt_eval.model_request_budget")
        usage = _visual_usage_summary(
            scout,
            prior_results,
            request_budget_ledger=(
                ledger_value
                if isinstance(ledger_value, ModelRequestBudgetLedger)
                else None
            ),
        )
        certificate = replace(
            certificate,
            metadata={
                **dict(certificate.metadata),
                "usage": usage,
            },
        )
        _store_visual_contract(
            context,
            name="visual_coverage_certificate",
            memo_key="ppt_eval.visual_coverage_certificate",
            value=certificate,
        )
        unresolved_pages = tuple(
            sorted(
                {
                    *certificate.forced_pages_not_audited,
                    *plan.unresolved_risk_page_numbers,
                }
            )
        )
        if certificate.coverage_complete:
            evidence = (
                Evidence(
                    evidence_id="visual-coverage-complete",
                    kind="visual_coverage_complete",
                    message=(
                        "Atlas, mandatory-page, cluster and criterion coverage "
                        "requirements were satisfied."
                    ),
                    payload={"score_affecting": False},
                ),
            )
            metric_status = MetricStatus.PASS
            confidence = 1.0
            severity = Severity.INFO
        else:
            evidence = (
                Evidence(
                    evidence_id="visual-coverage-incomplete",
                    kind="visual_coverage_incomplete",
                    message=(
                        "Visual evidence coverage is incomplete; review the unresolved "
                        "semantic risks or mandatory pages before accepting this deck."
                    ),
                    page_number=(unresolved_pages[0] if unresolved_pages else None),
                    payload={
                        "unresolved_risk_codes": list(
                            certificate.unresolved_risk_codes
                        ),
                        "unresolved_page_numbers": list(unresolved_pages),
                        "stopping_reason": certificate.stopping_reason,
                        "required_decision": "REVIEW",
                        "score_affecting": False,
                    },
                ),
            )
            metric_status = MetricStatus.NA
            confidence = 0.0
            severity = Severity.MAJOR
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=metric_status,
            score_role=ScoreRole.DIAGNOSTIC,
            raw_value=certificate.coverage_complete,
            confidence=confidence,
            severity=severity,
            evidence=evidence,
            version=self.version,
            metadata={
                "score_affecting": False,
                "coverage_complete": certificate.coverage_complete,
                "required_decision": (
                    None if certificate.coverage_complete else "REVIEW"
                ),
                "stopping_reason": certificate.stopping_reason,
                "unresolved_risk_codes": list(
                    certificate.unresolved_risk_codes
                ),
                "forced_pages_not_audited": list(
                    certificate.forced_pages_not_audited
                ),
                "criterion_pages": {
                    key: list(value)
                    for key, value in certificate.criterion_pages.items()
                },
                "usage": usage,
            },
        )

def _asset_semantic_risk_observation(
    index: VisualPageIndex,
    page: VisualPageFeatures,
) -> AtomicObservation:
    risk_codes: set[str] = set()
    if page.duplicate_asset_hashes:
        risk_codes.add("duplicated_asset_requires_semantic_check")
    if page.image_text_dense is True:
        risk_codes.add("image_text_dense")
    if page.image_dominant and (
        page.missing_alt_text_count > 0 or page.missing_caption_count > 0
    ):
        risk_codes.add("image_semantics_unobservable")
    if page.image_dominant and page.visual_entropy is not None and page.visual_entropy < 0.08:
        risk_codes.add("low_entropy_placeholder_suspected")
    if (
        (page.image_dominant or page.image_area_ratio >= 0.25)
        and page.ocr_text_character_count is None
    ):
        risk_codes.add("embedded_text_unobservable_without_ocr")
    severity = Severity.MAJOR if {
        "image_text_dense",
        "low_entropy_placeholder_suspected",
    } & risk_codes else Severity.MINOR if risk_codes else Severity.INFO
    digest = hashlib.sha256(
        (
            f"{index.deck_sha256}|{VISUAL_ASSET_SEMANTIC_RISK_METRIC_ID}|"
            f"page:{page.page_number}|{'|'.join(sorted(risk_codes))}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    return AtomicObservation(
        observation_id=f"obs-{digest}",
        oracle_id=VISUAL_PAGE_INDEX_ORACLE_ID,
        metric_id=VISUAL_ASSET_SEMANTIC_RISK_METRIC_ID,
        scope=EvaluationScope.PAGE,
        unit_key=f"page:{page.page_number}",
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.NA,
        raw_value=("ROUTE" if risk_codes else "NO_DETERMINISTIC_RISK"),
        local_score=None,
        confidence=1.0,
        severity=severity,
        importance=1.0,
        evidence=(
            Evidence(
                evidence_id=f"visual-asset-route-page-{page.page_number}",
                kind="visual_asset_semantic_risk",
                message=(
                    "Deterministic features cannot establish the visual asset's "
                    "semantic fitness; route the page to the imagery criterion."
                    if risk_codes
                    else "No deterministic asset-semantic routing risk was detected."
                ),
                page_number=page.page_number,
                payload={
                    "routing_codes": sorted(risk_codes),
                    "routes_to": ["imagery_data_visualization"],
                    "score_affecting": False,
                    "primary_owner": "visual_communication",
                },
            ),
        ),
        version="2.2.0",
        metadata={
            "routing_codes": sorted(risk_codes),
            "routes_to": ["imagery_data_visualization"],
            "score_affecting": False,
            "primary_owner": "visual_communication",
        },
    )


def _resolved_hard_gate_pages(
    plan: VisualSelectionPlan,
    criterion_pages: Mapping[str, Sequence[int]],
    results: Sequence[OracleResult],
) -> tuple[int, ...]:
    resolved_criteria: set[tuple[str, int]] = set()
    for result in results:
        criterion = result.metadata.get("criterion_id")
        if (
            not isinstance(criterion, str)
            or result.metric_status != MetricStatus.SCORED
            or result.confidence < 0.60
        ):
            continue
        for page_number in criterion_pages.get(criterion, ()):
            resolved_criteria.add((criterion, int(page_number)))
    resolved: list[int] = []
    for item in plan.items:
        if item.page_number not in plan.forced_page_numbers:
            continue
        p0_criteria = _priority_criteria_for_page(
            plan,
            item.page_number,
            priority="P0",
        )
        if not p0_criteria:
            continue
        relevant = tuple(
            criterion
            for criterion in p0_criteria
            if criterion in criterion_pages
        )
        if len(relevant) == len(p0_criteria) and all(
            (criterion, item.page_number) in resolved_criteria
            for criterion in relevant
        ):
            resolved.append(item.page_number)
    return tuple(sorted(resolved))


def _priority_criteria_for_page(
    plan: VisualSelectionPlan,
    page_number: int,
    *,
    priority: str,
) -> tuple[str, ...]:
    raw_by_page = plan.metadata.get("risk_criteria_by_page")
    if isinstance(raw_by_page, Mapping):
        raw_priorities = raw_by_page.get(str(page_number))
        if isinstance(raw_priorities, Mapping):
            raw_values = raw_priorities.get(priority, ())
            if isinstance(raw_values, Sequence) and not isinstance(
                raw_values, (str, bytes)
            ):
                return tuple(
                    sorted({str(value) for value in raw_values if str(value)})
                )
    item = next(
        (candidate for candidate in plan.items if candidate.page_number == page_number),
        None,
    )
    if item is None or item.priority != priority:
        return ()
    return tuple(item.criteria)


def _global_visual_audit_rounds(
    page_index: VisualPageIndex,
    plan: VisualSelectionPlan,
    criterion_pages: Mapping[str, Sequence[int]],
    results: Sequence[OracleResult],
    *,
    resolved_hard_gate_pages: Sequence[int],
    composite_interval: tuple[float, float] | None,
) -> tuple[VisualAuditRound, ...]:
    del criterion_pages, resolved_hard_gate_pages
    valid_pages = set(range(1, len(page_index.pages) + 1))
    plan_pages = {item.page_number for item in plan.items}
    actual_calls: list[tuple[str, OracleResult, Mapping[str, Any]]] = []
    final_results: list[OracleResult] = []
    for result in results:
        if result.oracle_id.startswith("v8.visual.initial."):
            continue
        criterion = result.metadata.get("criterion_id")
        if not isinstance(criterion, str) or result.metadata.get("adaptive_visual") is not True:
            continue
        final_results.append(result)
        raw_calls = result.metadata.get("adaptive_calls", ())
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            continue
        for call in raw_calls:
            if not isinstance(call, Mapping) or call.get("phase") != "REFINEMENT":
                continue
            actual_calls.append((criterion, result, call))
    if not actual_calls:
        return ()

    rounds: list[VisualAuditRound] = []
    for round_number, (criterion, result, call) in enumerate(actual_calls, start=1):
        raw_pages = call.get("new_page_numbers", ())
        if not isinstance(raw_pages, Sequence) or isinstance(raw_pages, (str, bytes)):
            raise ValueError("adaptive call lineage has invalid new_page_numbers")
        pages = tuple(
            int(page)
            for page in raw_pages
            if isinstance(page, int) and not isinstance(page, bool)
        )
        if (
            not pages
            or len(pages) != len(set(pages))
            or len(pages) > plan.round_size
            or not set(pages) <= valid_pages
            or not set(pages) <= plan_pages
        ):
            raise ValueError("adaptive call lineage is not bound to a legal plan round")
        status = str(call.get("metric_status") or "")
        criterion_pages_for_round = (
            {criterion: pages} if status == MetricStatus.SCORED.value else {}
        )
        affected = {
            int(page)
            for page in call.get("affected_page_numbers", ())
            if isinstance(page, int) and not isinstance(page, bool)
        } & set(pages)
        severity = str(call.get("defect_severity") or "NONE")
        low_confidence = (
            (criterion,)
            if status != MetricStatus.SCORED.value
            or not isinstance(call.get("confidence"), (int, float))
            or float(call["confidence"]) < 0.60
            else ()
        )
        escalation = str(call.get("escalation_reason") or "")
        conflicts = (
            (escalation,)
            if "DISAGREEMENT" in escalation
            and (
                call.get("selected_tier") != "ADVANCED"
                or call.get("advanced_rule_disagreement") is True
            )
            else ()
        )
        continues = round_number < len(actual_calls)
        lower: float | None = None
        upper: float | None = None
        if not continues and composite_interval is not None:
            lower, upper = composite_interval
        stopping_reason = None
        if not continues:
            stopping_reason = (
                "ADAPTIVE_STOP_CONDITIONS_MET"
                if all(
                    item.metadata.get("coverage_required") is False
                    or item.metadata.get("coverage_complete_for_criterion") is True
                    for item in final_results
                )
                else "UNRESOLVED_VISUAL_RISK_REVIEW"
            )
        rounds.append(
            VisualAuditRound(
                round_number=round_number,
                page_numbers=pages,
                criterion_pages=criterion_pages_for_round,
                new_major_count=(len(affected) if severity == "MAJOR" else 0),
                new_critical_count=(
                    len(affected) if severity == "CRITICAL" else 0
                ),
                low_confidence_criteria=low_confidence,
                conflict_codes=conflicts,
                uncovered_cluster_ids=(),
                composite_lower_bound=lower,
                composite_upper_bound=upper,
                continue_audit=continues,
                stopping_reason=stopping_reason,
                usage={
                    "round_source": "ACTUAL_CRITERION_MODEL_CALL",
                    "criterion_id": criterion,
                    "criterion_call_index": call.get("call_index"),
                    "request_fingerprint": call.get("request_fingerprint"),
                    "response_fingerprint": call.get("response_fingerprint"),
                    "usage_accounted_by": call.get("usage_accounted_by"),
                    "actual_audit_continued": continues,
                },
            )
        )
    return tuple(rounds)


def _projected_pdms_interval(
    context: EvaluationContext,
    prior_results: Sequence[OracleResult],
) -> tuple[float, float] | None:
    """Project Profile-weighted PDMS bounds without persisting provisional scores."""

    for result in reversed(prior_results):
        if result.oracle_id.startswith("v8.visual.initial."):
            continue
        lower = result.metadata.get("composite_interval_lower_bound")
        upper = result.metadata.get("composite_interval_upper_bound")
        if (
            not isinstance(lower, bool)
            and isinstance(lower, (int, float))
            and not isinstance(upper, bool)
            and isinstance(upper, (int, float))
            and 0.0 <= float(lower) <= float(upper) <= 100.0
        ):
            return (float(lower), float(upper))

    # If no criterion carried a bound, only publish an exact complete PDMS.
    # Never renormalize a subset of required constructs into a fake interval.
    from .v8_composites import V8QualityReducerOracle

    provisional = V8QualityReducerOracle().evaluate(context)
    try:
        breakdown = PptPdmsAggregator().aggregate(
            context.profile,
            (*prior_results, *provisional),
        )
    except (TypeError, ValueError):
        return None
    if breakdown.unresolved_metric_ids:
        return None
    score = (
        breakdown.full_score
        if breakdown.full_score is not None
        else breakdown.base_score
    )
    return None if score is None else (score, score)


def _unresolved_visual_risk_codes(
    scout: AtlasScoutResult,
    plan: VisualSelectionPlan,
    results: Sequence[OracleResult],
) -> tuple[str, ...]:
    codes: set[str] = set()
    if not scout.coverage_complete:
        codes.add(scout.error_code or "atlas_scout_incomplete")
    if plan.unresolved_risk_page_numbers:
        codes.add("selection_budget_overflow")
    for result in results:
        if not result.oracle_id.startswith("v8.visual."):
            continue
        if result.oracle_id.startswith("v8.visual.initial."):
            # Phase-one seeds are diagnostic acquisition records.  Their
            # matching final criterion owns coverage completion.
            continue
        criterion = result.metadata.get("criterion_id")
        if not isinstance(criterion, str):
            continue
        if result.metadata.get("coverage_required") is False:
            continue
        if (
            result.metric_status != MetricStatus.SCORED
            or result.metadata.get("coverage_complete_for_criterion") is False
        ):
            codes.add(f"criterion_unresolved:{criterion}")
    return tuple(sorted(codes))


def _visual_usage_summary(
    scout: AtlasScoutResult,
    results: Sequence[OracleResult],
    *,
    request_budget_ledger: ModelRequestBudgetLedger | None = None,
) -> Mapping[str, Any]:
    route_usage_items: list[Mapping[str, Any]] = []
    transports: set[str] = set()
    scout_attempts = scout.audit_metadata.get("attempts")
    normalized_scout_attempts = (
        tuple(item for item in scout_attempts if isinstance(item, Mapping))
        if isinstance(scout_attempts, Sequence)
        and not isinstance(scout_attempts, (str, bytes))
        else ()
    )
    if normalized_scout_attempts:
        transports.update(
            str(item.get("asset_transport"))
            for item in normalized_scout_attempts
            if item.get("asset_transport") in {"base64", "signed-url"}
        )
    for result in results:
        routing_usage = result.metadata.get(
            "accounted_routing_usage",
            result.metadata.get("routing_usage"),
        )
        if isinstance(routing_usage, Mapping):
            route_usage_items.append(routing_usage)
        attempts = result.metadata.get("routing_attempts")
        if isinstance(attempts, Sequence) and not isinstance(
            attempts, (str, bytes)
        ):
            transports.update(
                str(item.get("image_transport_mode"))
                for item in attempts
                if isinstance(item, Mapping)
                and item.get("image_transport_mode") in {"base64", "signed-url"}
            )
    scout_attempt_count = sum(
        int(item.get("structured_response_attempt_count", 1))
        for item in normalized_scout_attempts
    )
    scout_usage_complete = bool(normalized_scout_attempts) and all(
        isinstance(item.get("usage"), Mapping)
        and item.get("usage_complete") is True
        for item in normalized_scout_attempts
    )
    usage_items = [
        *([scout.usage] if scout_attempt_count and scout.usage else []),
        *route_usage_items,
    ]
    result_reported_request_count = scout_attempt_count + sum(
        int(item.get("attempt_count", 0))
        for item in route_usage_items
        if isinstance(item.get("attempt_count"), int)
        and not isinstance(item.get("attempt_count"), bool)
    )
    ledger_snapshot = (
        request_budget_ledger.snapshot()
        if request_budget_ledger is not None
        else None
    )
    ledger_reconciled = (
        ledger_snapshot is None
        or (
            ledger_snapshot.active_reservations == 0
            and ledger_snapshot.settled_actual_attempts
            == result_reported_request_count
        )
    )
    usage_complete = (
        bool(usage_items)
        and scout_usage_complete
        and scout.usage.get("usage_complete") is True
        and all(
            item.get("usage_complete") is True for item in route_usage_items
        )
        and ledger_reconciled
    )
    summary: dict[str, Any] = {
        "request_count": (
            ledger_snapshot.settled_actual_attempts
            if ledger_snapshot is not None
            else result_reported_request_count
        ),
        "request_count_reported_by_results": result_reported_request_count,
        "usage_complete": usage_complete,
        "usage_source_count": len(usage_items),
        "cost_known": usage_complete
        and all(item.get("cost_known") is True for item in usage_items),
        "asset_transport": (
            next(iter(transports))
            if len(transports) == 1
            else "mixed" if transports else "unknown"
        ),
    }
    if ledger_snapshot is not None:
        summary.update(
            {
                "request_count_upper_bound": (
                    ledger_snapshot.accounted_upper_bound
                ),
                "in_flight_reserved_attempts": (
                    ledger_snapshot.in_flight_reserved_attempts
                ),
                "maximum_model_requests": ledger_snapshot.maximum_requests,
                "remaining_model_requests": ledger_snapshot.remaining_attempts,
                "request_budget_reconciled": ledger_reconciled,
                "request_budget": ledger_snapshot.to_mapping(),
            }
        )
    if usage_complete and all(
        isinstance(item.get("input_tokens"), int)
        and not isinstance(item.get("input_tokens"), bool)
        and isinstance(item.get("output_tokens"), int)
        and not isinstance(item.get("output_tokens"), bool)
        for item in usage_items
    ):
        summary["input_tokens"] = sum(
            int(item["input_tokens"]) for item in usage_items
        )
        summary["output_tokens"] = sum(
            int(item["output_tokens"]) for item in usage_items
        )
        summary["total_tokens"] = (
            summary["input_tokens"] + summary["output_tokens"]
        )
    summary["reported_cost"] = sum(
        float(item.get("reported_cost", item.get("cost", 0.0)))
        for item in usage_items
        if not isinstance(item.get("reported_cost", item.get("cost", 0.0)), bool)
        and isinstance(
            item.get("reported_cost", item.get("cost", 0.0)),
            (int, float),
        )
    )
    for key in (
        "image_tokens",
        "cached_tokens",
        "cache_creation_input_tokens",
        "request_bytes",
    ):
        if usage_complete and usage_items and all(
            isinstance(item.get(key), int)
            and not isinstance(item.get(key), bool)
            and int(item[key]) >= 0
            for item in usage_items
        ):
            summary[key] = sum(int(item[key]) for item in usage_items)
    return summary


def _unavailable_scout_id(case_id: str, code: str) -> str:
    digest = hashlib.sha256(f"{case_id}|{code}".encode("utf-8")).hexdigest()
    return f"atlas-scout-unavailable-{digest}"


def _evaluation_cancelled(context: EvaluationContext) -> bool:
    event = context.memo.get("ppt_eval.cancel_event")
    is_set = getattr(event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


__all__ = [
    "ATLAS_SCOUT_ORACLE_ID",
    "AtlasScoutOracle",
    "VISUAL_ASSET_SEMANTIC_RISK_METRIC_ID",
    "VISUAL_COVERAGE_ORACLE_ID",
    "VISUAL_PAGE_INDEX_ORACLE_ID",
    "VISUAL_SELECTION_ORACLE_ID",
    "VisualPageIndexOracle",
    "VisualSelectionOracle",
    "VisualCoverageOracle",
]
