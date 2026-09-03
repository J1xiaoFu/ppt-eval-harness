from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ppt_eval.adapters import ModelAuditProviderError, ModelAuditRequest
from ppt_eval.config import default_profile
from ppt_eval.domain import (
    EvalCase,
    ExecutionStatus,
    MetricStatus,
    OracleResult,
    SceneType,
    ScoutResult,
)
from ppt_eval.infrastructure import sha256_file
from ppt_eval.oracles.visual_routing import _visual_usage_summary
from ppt_eval.runtime import LocalEvaluationRuntime
from tests.fixtures.api_client import make_test_client
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx
from tests.test_grounded_visual_audit import GroundedFakeProvider

VISUAL_CONTRACT_ROLES = {
    "visual_page_index",
    "atlas_scout",
    "visual_selection_plan",
    "visual_audit_rounds",
    "visual_coverage_certificate",
}


class _SlowScoutProvider(GroundedFakeProvider):
    def audit(self, request: ModelAuditRequest):
        if request.metric_id == "visual_atlas_scout_routing":
            time.sleep(0.25)
        return super().audit(request)


def test_scout_internal_timeout_still_persists_complete_failure_contracts(
    tmp_path: Path,
) -> None:
    deck, images = _deck_and_images(tmp_path, 3)
    provider = _SlowScoutProvider()
    profile = replace(
        default_profile(SceneType.READY_MADE),
        oracle_timeout_seconds=0.15,
    )
    runtime = LocalEvaluationRuntime(tmp_path / "timeout-var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="scout-internal-timeout",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile,
        artifacts={"slide_images": images},
    )

    scout = next(
        item
        for item in report["results"]
        if item["metric_id"] == "visual_atlas_scout_routing"
    )
    assert scout["execution_status"] == "SUCCESS"
    assert scout["metric_status"] == "NA"
    assert scout["metadata"]["reason_code"] == "ATLAS_SCOUT_INTERNAL_TIMEOUT"
    assert report["decision"] == "REVIEW"
    assert report["visual_audit_summary"]["coverage_complete"] is False
    assert set(report["visual_audit_artifacts"]) == VISUAL_CONTRACT_ROLES
    assert set(report["manifest"]["artifact_hashes"]) >= VISUAL_CONTRACT_ROLES


def test_visual_usage_never_turns_partial_telemetry_into_zero_tokens() -> None:
    scout = ScoutResult(
        scout_id="scout-usage-partial",
        findings=(),
        covered_page_numbers=(1,),
        coverage_complete=True,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost": 0.0,
            "cost_known": False,
        },
        audit_metadata={
            "attempts": [
                {"usage": {"input_tokens": 100, "output_tokens": 20}},
                {"outcome": "transport_error"},
            ]
        },
    )
    criterion = OracleResult(
        oracle_id="v8.visual.composition_layout",
        metric_id="structured_vlm_composition_layout",
        execution_status=ExecutionStatus.SUCCESS,
        metric_status=MetricStatus.SCORED,
        normalized_score=0.8,
        raw_value=0.8,
        metadata={
            "criterion_id": "composition_layout",
            "routing_usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "reported_cost": 0.0,
                "cost_known": False,
                "attempt_count": 1,
                "usage_complete": True,
            },
        },
    )

    usage = _visual_usage_summary(scout, (criterion,))

    assert usage["usage_complete"] is False
    assert usage["cost_known"] is False
    assert "input_tokens" not in usage
    assert "output_tokens" not in usage
    assert "total_tokens" not in usage
    assert "image_tokens" not in usage


def test_visual_usage_honors_partial_scout_adapter_retry_telemetry() -> None:
    scout = ScoutResult(
        scout_id="scout-usage-partial-adapter-retry",
        findings=(),
        covered_page_numbers=(1,),
        coverage_complete=True,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost": 0.0,
            "cost_known": False,
            "usage_complete": False,
        },
        audit_metadata={
            "attempts": [
                {
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "usage_complete": False,
                }
            ]
        },
    )

    usage = _visual_usage_summary(scout, ())

    assert usage["usage_complete"] is False
    assert usage["cost_known"] is False
    assert "input_tokens" not in usage
    assert "output_tokens" not in usage
    assert "total_tokens" not in usage


def _deck_and_images(tmp_path: Path, page_count: int) -> tuple[Path, tuple[Path, ...]]:
    deck = build_pptx(
        tmp_path / f"adaptive-{page_count}.pptx",
        tuple(
            (
                {
                    "kind": "text",
                    "text": f"Page {page_number} specific claim",
                    "x": 600_000,
                    "y": 500_000,
                    "w": 9_000_000,
                    "h": 900_000,
                    "font_pt": 26,
                },
            )
            for page_number in range(1, page_count + 1)
        ),
    )
    images = []
    for page_number in range(1, page_count + 1):
        image = tmp_path / f"render-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    return deck, tuple(images)


def test_profile84_persists_hash_bound_visual_contracts(tmp_path: Path) -> None:
    deck, images = _deck_and_images(tmp_path, 4)
    provider = GroundedFakeProvider()
    provider.image_transport_mode = "base64"
    provider.context_cache_enabled = True
    runtime = LocalEvaluationRuntime(tmp_path / "var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="adaptive-contracts",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": images},
    )

    assert report["profile_version"] == "8.4"
    assert report["schema_version"] == "1.0"
    assert set(report["visual_audit_artifacts"]) == VISUAL_CONTRACT_ROLES
    for role in VISUAL_CONTRACT_ROLES:
        reference = report["visual_audit_artifacts"][role]
        assert report["manifest"]["artifact_hashes"][role] == reference["sha256"]
        path, metadata = runtime.review_artifact(report["run_id"], role)
        assert sha256_file(path) == reference["sha256"] == metadata["sha256"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)

    page_index_path, _ = runtime.review_artifact(
        report["run_id"], "visual_page_index"
    )
    page_index = json.loads(page_index_path.read_text(encoding="utf-8"))
    scout_path, _ = runtime.review_artifact(report["run_id"], "atlas_scout")
    scout = json.loads(scout_path.read_text(encoding="utf-8"))
    plan_path, _ = runtime.review_artifact(
        report["run_id"], "visual_selection_plan"
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert page_index["deck_sha256"] == scout["deck_sha256"] == sha256_file(deck)
    assert (
        page_index["rendered_page_set_sha256"]
        == scout["rendered_page_set_sha256"]
        == plan["rendered_page_set_sha256"]
    )
    assert report["visual_audit_summary"]["total_pages"] == 4
    assert report["visual_audit_summary"]["atlas_covered_pages"] == 4
    # Public rounds represent actual two-page refinement calls.  A deck whose
    # shared cohort is sufficient truthfully has no refinement round.
    assert report["visual_audit_summary"]["round_count"] == 0
    assert report["visual_audit_summary"]["request_count"] == len(
        provider.requests
    )
    request_metrics = [request.metric_id for request in provider.requests]
    assert "visual_atlas_scout_routing" in request_metrics
    assert "structured_vlm_render_integrity" not in request_metrics
    local_requests = [
        request
        for request in provider.requests
        if request.metric_id
        in {
            "structured_vlm_composition_layout",
            "structured_vlm_typography_legibility",
            "structured_vlm_color_contrast",
            "structured_vlm_imagery_data_visualization",
        }
    ]
    assert local_requests
    assert len(
        {
            tuple(request.context["cache_prefix_pages"])
            for request in local_requests
        }
    ) == 1
    assert all(
        request.context["qwen_context_cache_profile_enabled"] is True
        for request in local_requests
    )
    assert runtime.audit_log.verify() == (True, None)


def test_profile84_model_request_budget_is_a_global_preflight_cap(
    tmp_path: Path,
) -> None:
    deck, images = _deck_and_images(tmp_path, 4)
    provider = GroundedFakeProvider()
    profile = default_profile(SceneType.READY_MADE)
    visual_policy = dict(profile.metadata["visual_audit"])
    visual_policy["maximum_model_requests"] = 2
    profile = replace(
        profile,
        metadata={**dict(profile.metadata), "visual_audit": visual_policy},
    )
    runtime = LocalEvaluationRuntime(tmp_path / "budget-var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="adaptive-global-request-budget",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        profile,
        artifacts={"slide_images": images},
    )

    assert [request.metric_id for request in provider.requests] == [
        "visual_atlas_scout_routing"
    ]
    budgeted = [
        item
        for item in report["results"]
        if item["metadata"].get("reason_code")
        == "VISUAL_MODEL_REQUEST_BUDGET_EXHAUSTED"
    ]
    assert budgeted
    assert all(
        item["metadata"]["request_budget"]["reservation_policy"]
        == "PROVIDER_HTTP_MAX_X_CRITERION_REPAIR_MAX"
        for item in budgeted
    )
    assert report["visual_audit_summary"]["request_count"] == 1
    assert report["visual_audit_summary"]["coverage_complete"] is False
    assert report["decision"] == "REVIEW"


def test_profile84_all_four_scenes_produce_legal_reports_without_model_keys(
    tmp_path: Path,
) -> None:
    deck, images = _deck_and_images(tmp_path, 3)
    source = tmp_path / "source.txt"
    source.write_text("Page specific claim with a 20 percent result.", encoding="utf-8")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG_1X1)

    for scene in SceneType:
        runtime = LocalEvaluationRuntime(tmp_path / f"var-{scene.value}")
        report = runtime.evaluate(
            EvalCase(
                case_id=f"profile84-{scene.value}",
                scene=scene,
                pptx_path=str(deck),
                request="Create a concise three-page presentation.",
                audience="Project reviewers",
                source_materials=(str(source),),
                assets=((str(asset),) if scene == SceneType.MULTIMODAL else ()),
            ),
            default_profile(scene),
            artifacts={"slide_images": images},
        )

        assert report["profile_version"] == "8.4"
        assert report["decision"] in {"PASS", "REVIEW", "FAIL"}
        assert report["decision"] != "ERROR"
        assert report["manifest"]["profile_version"] == "8.4"
        assert set(report["visual_audit_artifacts"]) == VISUAL_CONTRACT_ROLES
        assert report["visual_audit_summary"]["atlas_coverage_complete"] is False
        assert report["visual_audit_summary"]["coverage_complete"] is False
        assert runtime.audit_log.verify() == (True, None)


def test_incomplete_visual_coverage_collapses_to_one_semantic_attention_issue(
    tmp_path: Path,
) -> None:
    deck, images = _deck_and_images(tmp_path, 3)
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    report = runtime.evaluate(
        EvalCase(
            case_id="coverage-attention",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": images},
    )

    issues = runtime.review_task(report["run_id"])["issues"]
    coverage_issues = [
        issue
        for issue in issues
        if "visual_audit_coverage" in issue["lineage"]["metric_ids"]
    ]
    assert len(coverage_issues) == 1
    assert coverage_issues[0]["semantic_family"] == "SYSTEM_INTEGRITY"
    assert coverage_issues[0]["consensus"]["status"] == "INSUFFICIENT"
    collapsed = {
        "v8_functional_integrity",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
    }
    assert all(
        not (
            set(issue["lineage"]["metric_ids"]) & collapsed
            and issue is not coverage_issues[0]
        )
        for issue in issues
    )


class _ExpansionFailureProvider(GroundedFakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.imagery_calls = 0

    def audit(self, request: ModelAuditRequest):
        if request.metric_id == "visual_atlas_scout_routing":
            payload = dict(super().audit(request))
            evidence = [dict(item) for item in payload["evidence"]]
            first = {**evidence[0], "payload": dict(evidence[0]["payload"])}
            first["payload"]["findings"] = [
                {
                    "original_page_number": page_number,
                    "risk_code": "placeholder_visual_suspected",
                    "confidence": 0.91,
                    "suggested_criteria": ["imagery_data_visualization"],
                }
                for page_number in range(1, 7)
            ]
            evidence[0] = first
            payload["evidence"] = evidence
            return payload
        if request.metric_id == "structured_vlm_imagery_data_visualization":
            self.imagery_calls += 1
            if self.imagery_calls == 2:
                self.requests.append(request)
                raise ModelAuditProviderError("simulated expansion transport failure")
        return super().audit(request)


def test_failed_expansion_page_never_counts_as_successful_coverage(
    tmp_path: Path,
) -> None:
    deck, images = _deck_and_images(tmp_path, 9)
    provider = _ExpansionFailureProvider()
    runtime = LocalEvaluationRuntime(tmp_path / "var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="failed-adaptive-expansion",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": images},
    )

    imagery = next(
        item
        for item in report["results"]
        if item["metric_id"] == "structured_vlm_imagery_data_visualization"
    )
    attempted = set(imagery["metadata"]["adaptive_attempted_pages"])
    audited = set(imagery["metadata"]["adaptive_audited_pages"])
    assert imagery["metadata"]["adaptive_call_count"] == 2
    assert imagery["metadata"]["stopping_reason"] == "MODEL_UNRESOLVED_REVIEW"
    assert imagery["metadata"]["coverage_complete_for_criterion"] is False
    assert attempted > audited
    assert report["visual_audit_summary"]["coverage_complete"] is False
    assert "criterion_unresolved:imagery_data_visualization" in report[
        "visual_audit_summary"
    ]["unresolved_risks"]
    assert report["decision"] == "REVIEW"
    rounds_path, _ = runtime.review_artifact(
        report["run_id"],
        "visual_audit_rounds",
    )
    rounds = json.loads(rounds_path.read_text(encoding="utf-8"))
    assert len(rounds) == report["visual_audit_summary"]["round_count"] == 1
    assert rounds[0]["usage"]["round_source"] == "ACTUAL_CRITERION_MODEL_CALL"
    assert rounds[0]["usage"]["criterion_id"] == (
        "imagery_data_visualization"
    )
    assert set(rounds[0]["page_numbers"]) == attempted - audited
    assert rounds[0]["criterion_pages"] == {}


def test_rule_critical_page_is_forced_into_isomorphic_high_resolution_audit(
    tmp_path: Path,
) -> None:
    slides = []
    for page_number in range(1, 9):
        slides.append(
            (
                {
                    "kind": "text",
                    "text": f"Page {page_number}",
                    "x": 11_800_000 if page_number == 7 else 600_000,
                    "y": 500_000,
                    "w": 2_000_000 if page_number == 7 else 8_000_000,
                    "h": 700_000,
                    "font_pt": 28,
                },
            )
        )
    deck = build_pptx(tmp_path / "critical-page-7.pptx", tuple(slides))
    images = []
    for page_number in range(1, 9):
        image = tmp_path / f"critical-render-{page_number}.png"
        image.write_bytes(PNG_1X1)
        images.append(image)
    provider = GroundedFakeProvider()
    runtime = LocalEvaluationRuntime(tmp_path / "var", vlm_provider=provider)

    report = runtime.evaluate(
        EvalCase(
            case_id="critical-page-7",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": tuple(images)},
    )
    plan_path, _ = runtime.review_artifact(
        report["run_id"], "visual_selection_plan"
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    composition_pages = {
        image.page_number
        for request in provider.requests
        if request.metric_id == "structured_vlm_composition_layout"
        for image in request.images
    }

    assert plan["forced_page_numbers"] == [7]
    assert 7 in composition_pages
    functional = next(
        item
        for item in report["results"]
        if item["metric_id"] == "v8_functional_integrity"
    )
    geometry = next(
        item
        for item in functional["metadata"]["gate_verdicts"]
        if item["metric_id"] == "slide_geometry_integrity"
    )
    assert geometry["verdict"] == "UNRESOLVED"
    assert report["decision"] == "REVIEW"


def test_review_api_exposes_visual_contracts_only_in_full_audit(
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("python_multipart")
    from ppt_eval.api import create_app

    deck, images = _deck_and_images(tmp_path, 2)
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        vlm_provider=GroundedFakeProvider(),
    )
    report = runtime.evaluate(
        EvalCase(
            case_id="visual-audit-api",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        artifacts={"slide_images": images},
    )
    client = make_test_client(lambda: create_app(runtime))

    detail = client.get(f"/v1/review/tasks/{report['run_id']}").json()
    assert "visual_contract_urls" in detail["artifacts"]
    assert set(detail["artifacts"]["visual_contract_urls"]) == VISUAL_CONTRACT_ROLES
    assert "visual_audit_summary" not in detail

    audit = client.get(f"/v1/review/tasks/{report['run_id']}/audit").json()
    assert audit["visual_audit_summary"]["total_pages"] == 2
    assert set(audit["visual_contract_artifacts"]) == VISUAL_CONTRACT_ROLES
    for role, artifact in audit["visual_contract_artifacts"].items():
        assert artifact["url"].endswith(f"/artifacts/{role}")
        response = client.get(artifact["url"])
        assert response.status_code == 200
        assert sha256_file(runtime.review_artifact(report["run_id"], role)[0]) == (
            artifact["sha256"]
        )
