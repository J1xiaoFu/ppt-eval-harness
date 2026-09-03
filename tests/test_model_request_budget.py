from __future__ import annotations

import copy
import importlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from ppt_eval.adapters import ModelAuditProviderError, ModelAuditRequest, PptxAdapter
from ppt_eval.application import DagScheduler, EvaluationContext, OracleRegistry
from ppt_eval.application.model_request_budget import ModelRequestBudgetLedger
from ppt_eval.config import default_profile
from ppt_eval.domain import (
    EvalCase,
    EvaluationDag,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoutResult,
)
from ppt_eval.oracles.model_audits import GroundedSingleCriterionVlmOracle
from ppt_eval.oracles.visual_routing import _visual_usage_summary
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.test_atlas_scout import (
    _page_images,
    _runner,
    _ScriptedProvider,
    _valid_response,
)


def _grounded_test_helpers() -> Any:
    # Import dynamically so this strict-mypy unit does not inherit unrelated
    # historical annotation debt from the large visual-audit fixture module.
    return importlib.import_module("tests.test_grounded_visual_audit")


def test_reservation_enforces_upper_bound_and_settle_releases_unused() -> None:
    ledger = ModelRequestBudgetLedger(5)

    first = ledger.reserve(4, owner="flash:composition")
    assert first is not None
    assert ledger.reserve(2, owner="advanced:composition") is None
    assert ledger.snapshot().to_mapping() == {
        "maximum_requests": 5,
        "settled_actual_attempts": 0,
        "in_flight_reserved_attempts": 4,
        "accounted_upper_bound": 4,
        "remaining_attempts": 1,
        "active_reservations": 1,
        "settled_reservations": 0,
        "exhausted": False,
    }

    after_first = ledger.settle(first, actual_attempts=2)
    assert after_first.settled_actual_attempts == 2
    assert after_first.remaining_attempts == 3

    second = ledger.reserve(3, owner="advanced:composition")
    assert second is not None
    assert ledger.snapshot().accounted_upper_bound == 5
    assert ledger.reserve(1) is None

    final = ledger.settle(second, actual_attempts=1)
    assert final.settled_actual_attempts == 3
    assert final.remaining_attempts == 2
    assert final.active_reservations == 0


def test_unreturned_reservation_remains_fail_closed_across_deepcopy() -> None:
    ledger = ModelRequestBudgetLedger(4)
    copied = copy.deepcopy({"request_budget": ledger})
    assert copied["request_budget"] is ledger

    started = threading.Event()
    release = threading.Event()

    def timed_out_worker() -> None:
        reservation = ledger.reserve(4, owner="timed-out-provider")
        assert reservation is not None
        started.set()
        release.wait(timeout=2.0)
        ledger.settle(reservation, actual_attempts=1)

    worker = threading.Thread(target=timed_out_worker)
    worker.start()
    assert started.wait(timeout=1.0)

    # A scheduler may have abandoned the worker, but its worst-case HTTP spend
    # is still in flight and cannot be reused by the next DAG node.
    blocked = ledger.reserve(1, owner="subsequent-node")
    snapshot = ledger.snapshot()
    assert blocked is None
    assert snapshot.in_flight_reserved_attempts == 4
    assert snapshot.remaining_attempts == 0
    assert snapshot.exhausted is True

    release.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert ledger.snapshot().remaining_attempts == 3


def test_concurrent_reservations_cannot_oversubscribe_budget() -> None:
    ledger = ModelRequestBudgetLedger(7)
    barrier = threading.Barrier(17)
    reservations = []
    reservations_lock = threading.Lock()

    def compete() -> None:
        barrier.wait(timeout=2.0)
        reservation = ledger.reserve(1, owner="concurrent-provider")
        if reservation is not None:
            with reservations_lock:
                reservations.append(reservation)

    workers = [threading.Thread(target=compete) for _ in range(16)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=2.0)
    for worker in workers:
        worker.join(timeout=1.0)
        assert not worker.is_alive()

    snapshot = ledger.snapshot()
    assert len(reservations) == 7
    assert snapshot.accounted_upper_bound == 7
    assert snapshot.remaining_attempts == 0


def test_invalid_or_reused_reservations_are_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ModelRequestBudgetLedger(0)
    ledger = ModelRequestBudgetLedger(3)
    with pytest.raises(ValueError, match="positive integer"):
        ledger.reserve(True)
    with pytest.raises(ValueError, match="non-blank"):
        ledger.reserve(1, owner="  ")

    reservation = ledger.reserve(2)
    assert reservation is not None
    with pytest.raises(ValueError, match="reserved provider bound"):
        ledger.settle(reservation, actual_attempts=3)
    # The invalid settlement does not release the outstanding grant.
    assert ledger.snapshot().in_flight_reserved_attempts == 2

    ledger.settle(reservation, actual_attempts=2)
    with pytest.raises(ValueError, match="unknown or already settled"):
        ledger.settle(reservation, actual_attempts=2)


def test_scheduler_creates_one_ledger_only_for_profile84() -> None:
    profile = default_profile(SceneType.READY_MADE)
    context = EvaluationContext(
        case=EvalCase(
            case_id="ledger-v84",
            scene=SceneType.READY_MADE,
            pptx_path="unused.pptx",
        ),
        profile=profile,
        memo={},
    )
    DagScheduler(OracleRegistry()).execute(EvaluationDag(nodes=()), context, profile)

    ledger = context.memo.get("ppt_eval.model_request_budget")
    assert isinstance(ledger, ModelRequestBudgetLedger)
    assert ledger.maximum_requests == 64

    legacy = replace(profile, version="8.3")
    legacy_context = EvaluationContext(
        case=EvalCase(
            case_id="ledger-v83",
            scene=SceneType.READY_MADE,
            pptx_path="unused.pptx",
        ),
        profile=legacy,
        memo={},
    )
    DagScheduler(OracleRegistry()).execute(
        EvaluationDag(nodes=()),
        legacy_context,
        legacy,
    )
    assert "ppt_eval.model_request_budget" not in legacy_context.memo


def test_atlas_scout_settles_actual_adapter_attempts_into_global_ledger(
    tmp_path: Path,
) -> None:
    def retry_response(request: ModelAuditRequest) -> Mapping[str, Any]:
        payload = dict(_valid_response(request))
        evidence = []
        for item in payload["evidence"]:
            copied = dict(item)
            copied_payload = dict(copied["payload"])
            copied_payload.update(
                adapter_retry_count=1,
                adapter_attempts_with_usage=2,
                adapter_usage_complete=True,
            )
            copied["payload"] = copied_payload
            evidence.append(copied)
        payload["evidence"] = evidence
        return payload

    provider = _ScriptedProvider(retry_response)
    ledger = ModelRequestBudgetLedger(2)
    result = _runner(tmp_path, provider).run(
        _page_images(tmp_path, 1),
        case_id="atlas-ledger-retry",
        scene="FINISHED_DECK",
        maximum_model_requests=64,
        request_budget_ledger=ledger,
    )

    assert result.coverage_complete is True
    assert result.audit_metadata["provider_attempt_count"] == 2
    snapshot = ledger.snapshot()
    assert snapshot.settled_actual_attempts == 2
    assert snapshot.in_flight_reserved_attempts == 0
    assert snapshot.remaining_attempts == 0
    assert result.audit_metadata["global_request_budget"] == snapshot.to_mapping()


def test_atlas_fallback_cannot_reuse_primary_attempt_capacity(
    tmp_path: Path,
) -> None:
    def primary_failure(_request: ModelAuditRequest) -> Mapping[str, Any]:
        raise ModelAuditProviderError(
            "redacted primary failure",
            audit_metadata={
                "provider_attempts": 2,
                "provider_attempts_with_usage": 0,
                "provider_usage_complete": False,
            },
        )

    primary = _ScriptedProvider(primary_failure)
    fallback = _ScriptedProvider(_valid_response)
    ledger = ModelRequestBudgetLedger(3)
    result = _runner(tmp_path, primary, fallback).run(
        _page_images(tmp_path, 1),
        case_id="atlas-ledger-fallback",
        scene="FINISHED_DECK",
        maximum_model_requests=64,
        request_budget_ledger=ledger,
    )

    assert len(primary.requests) == 1
    assert fallback.requests == []
    assert result.coverage_complete is False
    assert result.error_code == "ATLAS_SCOUT_REQUEST_BUDGET_EXHAUSTED"
    snapshot = ledger.snapshot()
    assert snapshot.settled_actual_attempts == 2
    assert snapshot.remaining_attempts == 1


def test_atlas_failure_without_attempt_telemetry_charges_full_bound(
    tmp_path: Path,
) -> None:
    def unknown_failure(_request: ModelAuditRequest) -> Mapping[str, Any]:
        raise ModelAuditProviderError("redacted failure without telemetry")

    primary = _ScriptedProvider(unknown_failure)
    fallback = _ScriptedProvider(_valid_response)
    ledger = ModelRequestBudgetLedger(3)
    result = _runner(tmp_path, primary, fallback).run(
        _page_images(tmp_path, 1),
        case_id="atlas-ledger-unknown-attempts",
        scene="FINISHED_DECK",
        maximum_model_requests=64,
        request_budget_ledger=ledger,
    )

    # The primary adapter did not prove how many HTTP attempts it made.  Its
    # full declared/default bound remains charged, leaving too little capacity
    # for a fallback reservation.
    assert len(primary.requests) == 1
    assert fallback.requests == []
    assert result.error_code == "ATLAS_SCOUT_REQUEST_BUDGET_EXHAUSTED"
    assert ledger.snapshot().settled_actual_attempts == 2
    assert ledger.snapshot().remaining_attempts == 1


def test_inflight_atlas_call_blocks_a_second_runner_after_scheduler_timeout(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def block(request: ModelAuditRequest) -> Mapping[str, Any]:
        started.set()
        release.wait(timeout=2.0)
        return _valid_response(request)

    first_provider = _ScriptedProvider(block)
    second_provider = _ScriptedProvider(_valid_response)
    ledger = ModelRequestBudgetLedger(2)
    images = _page_images(tmp_path, 1)
    results = []

    def run_first() -> None:
        results.append(
            _runner(tmp_path / "first", first_provider).run(
                images,
                case_id="atlas-inflight-first",
                scene="FINISHED_DECK",
                maximum_model_requests=64,
                request_budget_ledger=ledger,
            )
        )

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(timeout=1.0)

    blocked = _runner(tmp_path / "second", second_provider).run(
        images,
        case_id="atlas-inflight-second",
        scene="FINISHED_DECK",
        maximum_model_requests=64,
        request_budget_ledger=ledger,
    )
    assert second_provider.requests == []
    assert blocked.error_code == "ATLAS_SCOUT_REQUEST_BUDGET_EXHAUSTED"
    assert ledger.snapshot().in_flight_reserved_attempts == 2

    release.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].coverage_complete is True
    assert ledger.snapshot().settled_actual_attempts == 1


def test_grounded_criterion_repair_uses_one_bounded_reservation(
    tmp_path: Path,
) -> None:
    class CriterionRepairProvider:
        def __init__(self) -> None:
            self.call_count = 0

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.call_count += 1
            payload: dict[str, Any] = (
                _grounded_test_helpers()._grounded_response(request)
            )
            if self.call_count == 1:
                payload["evidence"][0]["payload"].pop(
                    "positive_quality_signals"
                )
            return payload

    context = _grounded_test_helpers()._single_page_context(tmp_path)
    ledger = ModelRequestBudgetLedger(4)
    context.memo["ppt_eval.model_request_budget"] = ledger
    provider = CriterionRepairProvider()

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert result.normalized_score is not None
    assert provider.call_count == 2
    assert result.metadata["criterion_retry_count"] == 1
    reservation = result.metadata["model_request_reservation"]
    assert reservation["reserved_attempts"] == 4
    assert reservation["actual_attempts"] == 2
    snapshot = ledger.snapshot()
    assert snapshot.settled_actual_attempts == 2
    assert snapshot.remaining_attempts == 2


def test_grounded_criterion_does_not_call_provider_without_full_repair_bound(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(_grounded_test_helpers()._grounded_response)
    context = _grounded_test_helpers()._single_page_context(tmp_path)
    ledger = ModelRequestBudgetLedger(3)
    context.memo["ppt_eval.model_request_budget"] = ledger

    result = GroundedSingleCriterionVlmOracle(
        "composition_layout",
        provider,
        PptxAdapter(backend="ooxml"),
    ).evaluate(context)

    assert provider.requests == []
    assert result.metadata["reason_code"] == "MODEL_REQUEST_BUDGET_EXHAUSTED"
    assert ledger.snapshot().settled_actual_attempts == 0
    assert ledger.snapshot().remaining_attempts == 3


def test_visual_usage_is_incomplete_until_ledger_has_no_inflight_reservation() -> None:
    scout = ScoutResult(
        scout_id="scout-ledger-usage",
        findings=(),
        covered_page_numbers=(1,),
        coverage_complete=True,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost": 0.01,
            "cost_known": True,
            "usage_complete": True,
        },
        audit_metadata={
            "attempts": [
                {
                    "structured_response_attempt_count": 1,
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "usage_complete": True,
                }
            ]
        },
    )
    criterion = OracleResult(
        oracle_id="v8.visual.initial.composition_layout",
        metric_id="structured_vlm_initial_composition_layout",
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        raw_value=0.8,
        normalized_score=0.8,
        metadata={
            "accounted_routing_usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "reported_cost": 0.01,
                "cost_known": True,
                "attempt_count": 1,
                "usage_complete": True,
            }
        },
    )
    ledger = ModelRequestBudgetLedger(4)
    reservation = ledger.reserve(4, owner="timed-out-criterion")
    assert reservation is not None

    in_flight = _visual_usage_summary(
        scout,
        (criterion,),
        request_budget_ledger=ledger,
    )
    assert in_flight["usage_complete"] is False
    assert in_flight["request_count"] == 0
    assert in_flight["request_count_reported_by_results"] == 2
    assert in_flight["request_count_upper_bound"] == 4
    assert in_flight["in_flight_reserved_attempts"] == 4
    assert in_flight["request_budget_reconciled"] is False
    assert "total_tokens" not in in_flight

    ledger.settle(reservation, actual_attempts=2)
    settled = _visual_usage_summary(
        scout,
        (criterion,),
        request_budget_ledger=ledger,
    )
    assert settled["usage_complete"] is True
    assert settled["request_count"] == 2
    assert settled["request_count_upper_bound"] == 2
    assert settled["request_budget_reconciled"] is True
    assert settled["total_tokens"] == 180


def test_profile84_caps_scout_flash_advanced_repair_and_raster_attempts(
    tmp_path: Path,
) -> None:
    helpers = _grounded_test_helpers()
    pptx_helpers = importlib.import_module("tests.fixtures.pptx_factory")

    class TwoHttpAttemptsPerInvocationProvider:
        maximum_http_attempts_per_audit = 2

        def __init__(self, model_id: str) -> None:
            self.model_id = model_id
            self.logical_calls = 0
            self.metric_ids: list[str] = []

        def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
            self.logical_calls += 1
            self.metric_ids.append(request.metric_id)
            if request.metric_id == "visual_atlas_scout_routing":
                payload: dict[str, Any] = helpers._atlas_scout_response(request)
            else:
                payload = helpers._grounded_response(request)
                if "response_repair" not in request.context:
                    payload["evidence"][0]["payload"].pop(
                        "positive_quality_signals"
                    )
                else:
                    for item in payload["evidence"]:
                        item["payload"]["criterion_confidence"] = 0.50
            payload["model"] = {
                "provider": "bounded-fixture",
                "model_id": self.model_id,
                "version": "test",
            }
            for item in payload["evidence"]:
                item_payload = item["payload"]
                item_payload.update(
                    adapter_retry_count=1,
                    adapter_attempts_with_usage=2,
                    adapter_usage_complete=True,
                    adapter_cost_known=True,
                )
            return payload

    deck = pptx_helpers.build_pptx(
        tmp_path / "bounded-raster.pptx",
        tuple(
            (
                {
                    "kind": "image",
                    "x": 0,
                    "y": 0,
                    "w": 12_192_000,
                    "h": 6_858_000,
                },
            )
            for _ in range(2)
        ),
    )
    images = []
    for page_number in (1, 2):
        image = tmp_path / f"bounded-raster-{page_number}.png"
        image.write_bytes(pptx_helpers.PNG_1X1)
        images.append(image)

    flash = TwoHttpAttemptsPerInvocationProvider("qwen3.8-flash")
    advanced = TwoHttpAttemptsPerInvocationProvider("glm-5.3-flash")
    runtime = LocalEvaluationRuntime(
        tmp_path / "bounded-var",
        vlm_provider=flash,
        advanced_vlm_provider=advanced,
    )
    report = runtime.evaluate(
        EvalCase(
            case_id="bounded-all-visual-routes",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
    )

    coverage_path, _ = runtime.review_artifact(
        report["run_id"],
        "visual_coverage_certificate",
    )
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    usage = coverage["metadata"]["usage"]
    conceptual_http_attempts = 2 * (
        flash.logical_calls + advanced.logical_calls
    )

    assert "visual_atlas_scout_routing" in flash.metric_ids
    assert advanced.logical_calls > 0
    assert {
        "structured_vlm_raster_content_structure",
        "structured_vlm_raster_language_consistency",
    } <= set(flash.metric_ids + advanced.metric_ids)
    # This fixture drives every route and reaches the last safe reservation:
    # 62 attempts are charged and the next four-attempt repair envelope is
    # refused, rather than overshooting the 64-attempt Profile cap.
    assert conceptual_http_attempts == 62
    assert usage["maximum_model_requests"] == 64
    assert usage["request_count"] == conceptual_http_attempts
    assert usage["request_count_reported_by_results"] == conceptual_http_attempts
    assert usage["request_count_upper_bound"] == conceptual_http_attempts
    assert usage["in_flight_reserved_attempts"] == 0
    assert usage["request_budget_reconciled"] is True
    assert usage["remaining_model_requests"] == 64 - conceptual_http_attempts
    assert runtime.audit_log.verify() == (True, None)
