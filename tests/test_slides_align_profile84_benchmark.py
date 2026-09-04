from __future__ import annotations

import hashlib
import json
import math
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, EvalProfile, SceneType
from scripts.benchmarks import evaluate_slides_align_profile84 as benchmark
from scripts.benchmarks.evaluate_slides_align_profile84 import (
    CaseSpec,
    RenderSpec,
    SuiteSpec,
    _checkpoint_identity,
    _checkpoint_path,
    _load_resumable_case,
    _model_response_legality,
    _runtime_root,
    _stored_report_path,
    _visual_usage,
    _write_outputs,
    aggregate_suite,
    build_suite_html,
    evaluation_case,
    invoke_evaluation,
    summarize_topic,
    validate_suite,
    verify_runtime_evidence,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _minimal_pptx(path: Path, text: str) -> bytes:
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("ppt/slides/slide1.xml", f"<slide>{text}</slide>")
    return path.read_bytes()


def _file_record(path: str, payload: bytes) -> dict[str, object]:
    return {"path": path, "bytes": len(payload), "sha256": _sha(payload)}


def _dataset_fixture(root: Path) -> Path:
    topic_root = root / "topics" / "sample_topic"
    (topic_root / "decks").mkdir(parents=True)
    (topic_root / "renders" / "alpha_rank_1").mkdir(parents=True)
    (topic_root / "renders" / "beta_rank_2").mkdir(parents=True)
    pptx_a_path = topic_root / "decks" / "alpha_rank_1.pptx"
    pptx_b_path = topic_root / "decks" / "beta_rank_2.pptx"
    pptx_a = _minimal_pptx(pptx_a_path, "alpha")
    pptx_b = _minimal_pptx(pptx_b_path, "beta")
    image_a = b"fixture-image-alpha"
    image_b = b"fixture-image-beta"
    image_a_path = topic_root / "renders" / "alpha_rank_1" / "slide_0001.png"
    image_b_path = topic_root / "renders" / "beta_rank_2" / "slide_0001.png"
    image_a_path.write_bytes(image_a)
    image_b_path.write_bytes(image_b)
    (topic_root / "rankings").mkdir()
    ranking_payload = {
        "schema_version": "1.0",
        "source_revision": "fixture-revision",
        "topic": "Sample_Topic",
        "ranked_product_count": 2,
        "paired_product_count": 2,
        "results": [
            {"product": "Alpha", "rank": 1, "selected": True},
            {"product": "Beta", "rank": 2, "selected": True},
        ],
    }
    ranking = (json.dumps(ranking_payload, sort_keys=True) + "\n").encode()
    (topic_root / "rankings" / "sample_topic.json").write_bytes(ranking)

    local_records = [
        _file_record("decks/alpha_rank_1.pptx", pptx_a),
        _file_record("decks/beta_rank_2.pptx", pptx_b),
        _file_record("renders/alpha_rank_1/slide_0001.png", image_a),
        _file_record("renders/beta_rank_2/slide_0001.png", image_b),
        _file_record("rankings/sample_topic.json", ranking),
    ]
    artifacts = []
    for product, rank, pptx_name, pptx_payload, image_relative, image_payload in (
        (
            "Alpha",
            1,
            "alpha_rank_1.pptx",
            pptx_a,
            "renders/alpha_rank_1/slide_0001.png",
            image_a,
        ),
        (
            "Beta",
            2,
            "beta_rank_2.pptx",
            pptx_b,
            "renders/beta_rank_2/slide_0001.png",
            image_b,
        ),
    ):
        artifacts.append(
            {
                "product_label": product,
                "human_rank": rank,
                "artifact_bytes": len(pptx_payload) + len(image_payload),
                "pptx": {
                    "local_path": f"decks/{pptx_name}",
                    "bytes": len(pptx_payload),
                    "sha256": _sha(pptx_payload),
                },
                "rendered_slides": {
                    "count": 1,
                    "files": [
                        {
                            "local_path": image_relative,
                            "bytes": len(image_payload),
                            "sha256": _sha(image_payload),
                        }
                    ],
                },
            }
        )
    artifact_bytes = sum(int(item["artifact_bytes"]) for item in artifacts)
    topic_manifest = {
        "schema_version": "1.2",
        "dataset_id": "slides-align-sample-topic",
        "selection": {
            "topic": "Sample_Topic",
            "human_ranking_subset": "rankings/sample_topic.json",
        },
        "artifacts": artifacts,
        "files": local_records,
        "integrity": {
            "manifest_files": len(local_records),
            "artifact_bytes": artifact_bytes,
            "rendered_slide_count": 2,
        },
    }
    (topic_root / "manifest.json").write_text(
        json.dumps(topic_manifest), encoding="utf-8"
    )
    root_records = [
        {**record, "path": f"topics/sample_topic/{record['path']}"}
        for record in local_records
    ]
    suite_manifest = {
        "schema_version": "1.0",
        "dataset_id": "slides-align-fixture",
        "source": {"revision": "fixture-revision", "revision_pinned": True},
        "topics": [
            {
                "topic": "Sample_Topic",
                "slug": "sample_topic",
                "local_directory": "topics/sample_topic",
                "manifest_path": "topics/sample_topic/manifest.json",
                "paired_products": 2,
                "available_human_ranks": [1, 2],
                "rendered_slides": 2,
                "artifact_bytes": artifact_bytes,
            }
        ],
        "files": root_records,
        "integrity": {
            "manifest_files": len(root_records),
            "paired_deck_count": 2,
            "rendered_slide_count": 2,
            "artifact_bytes": artifact_bytes,
        },
    }
    (root / "manifest.json").write_text(json.dumps(suite_manifest), encoding="utf-8")
    return root


def test_slides_align_manifest_verification_and_official_render_specs(
    tmp_path: Path,
) -> None:
    root = _dataset_fixture(tmp_path / "suite")

    suite = validate_suite(root, requested_topics=("sample_topic",))

    assert suite.dataset_id == "slides-align-fixture"
    assert suite.topics == ("Sample_Topic",)
    assert len(suite.cases) == 2
    assert [case.human_rank for case in suite.cases] == [1, 2]
    assert all(len(case.renders) == 1 for case in suite.cases)
    assert all(case.renders[0].path.is_file() for case in suite.cases)

    first_image = suite.cases[0].renders[0].path
    first_image.write_bytes(b"tampered")
    try:
        validate_suite(root)
    except ValueError as exc:
        assert "size mismatch" in str(exc) or "SHA-256 mismatch" in str(exc)
    else:
        raise AssertionError("tampered official render passed manifest verification")


def test_human_rank_must_match_the_hash_pinned_ranking_subset(
    tmp_path: Path,
) -> None:
    root = _dataset_fixture(tmp_path / "suite")
    manifest_path = root / "topics" / "sample_topic" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["human_rank"] = 2
    manifest["artifacts"][1]["human_rank"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="disagrees with pinned ranking"):
        validate_suite(root)


def test_topic_slug_cannot_escape_benchmark_output(tmp_path: Path) -> None:
    root = _dataset_fixture(tmp_path / "suite")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["topics"][0]["slug"] = "../../escaped"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="suite topic slug"):
        validate_suite(root)


def test_evaluation_case_never_contains_human_rank_or_product_identity(
    tmp_path: Path,
) -> None:
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"pptx")
    image = (
        tmp_path
        / "renders"
        / "human-preferred-product_rank_1"
        / "slide.png"
    )
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    case = CaseSpec(
        topic="Secret topic label",
        topic_slug="secret_topic_label",
        dataset_id="topic-dataset",
        product="Human-preferred-product",
        human_rank=1,
        case_id="case-a",
        pptx_path=pptx,
        pptx_relative_path="topics/secret/deck.pptx",
        pptx_sha256=_sha(b"pptx"),
        pptx_bytes=4,
        renders=(
            RenderSpec(
                page_number=1,
                path=image,
                relative_path="topics/secret/slide.png",
                sha256=_sha(b"image"),
                byte_count=5,
            ),
        ),
    )
    suite = SuiteSpec(
        root=tmp_path,
        dataset_id="suite",
        revision="revision",
        cases=(case,),
        manifest_file_count=2,
        manifest_bytes=9,
    )

    request_case = evaluation_case(suite, case)

    serialized = json.dumps(request_case.metadata, ensure_ascii=False).casefold()
    assert "rank" not in serialized
    assert "human-preferred-product".casefold() not in serialized
    assert "secret topic label".casefold() not in serialized
    assert request_case.metadata["artifact_hashes"] == {
        "source_pptx": case.pptx_sha256
    }
    assert "rank" not in request_case.case_id
    assert "human-preferred-product" not in request_case.case_id.casefold()

    class CapturingRuntime:
        received_case: EvalCase | None = None
        received_artifacts: Mapping[str, Any] | None = None

        def evaluate(
            self,
            eval_case: EvalCase,
            profile: EvalProfile,
            *,
            artifacts: Mapping[str, Any] | None = None,
        ) -> Mapping[str, Any]:
            del profile
            self.received_case = eval_case
            self.received_artifacts = artifacts
            return {"ok": True}

    runtime = CapturingRuntime()
    anonymous = tmp_path / "runtime" / "inputs" / f"{case.evaluation_case_id}.pptx"
    anonymous.parent.mkdir(parents=True)
    anonymous.write_bytes(b"pptx")
    result = invoke_evaluation(
        runtime,
        suite,
        case,
        default_profile(SceneType.READY_MADE),
        anonymous_pptx_path=anonymous,
    )

    assert result == {"ok": True}
    assert runtime.received_case is not None
    received = json.dumps(
        {
            "case_id": runtime.received_case.case_id,
            "pptx_path": runtime.received_case.pptx_path,
            "metadata": runtime.received_case.metadata,
        },
        ensure_ascii=False,
    ).casefold()
    assert "rank" not in received
    assert "human-preferred-product".casefold() not in received
    assert "secret topic label".casefold() not in received
    assert runtime.received_artifacts is not None
    assert runtime.received_artifacts["slide_images"][0]["sha256"] == _sha(
        b"image"
    )
    received_image_path = str(
        runtime.received_artifacts["slide_images"][0]["path"]
    ).casefold()
    assert "rank_1" not in received_image_path
    assert "human-preferred-product" not in received_image_path
    assert Path(received_image_path).name.startswith("page-0001-")


def test_resume_uses_profile_fingerprint_and_derived_report_path(
    tmp_path: Path,
) -> None:
    suite = validate_suite(_dataset_fixture(tmp_path / "suite"))
    case = suite.cases[0]
    profile = default_profile(SceneType.READY_MADE)
    output = (tmp_path / "output").resolve()
    run_id = "run-fixture"
    runtime_root = _runtime_root(output, case)
    report_path = _stored_report_path(runtime_root, run_id)
    report_path.parent.mkdir(parents=True)
    report = {
        "schema_version": "1.0",
        "run_id": run_id,
        "case_id": case.evaluation_case_id,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    checkpoint = {
        "schema_version": "1.0",
        "status": "COMPLETED",
        **_checkpoint_identity(suite, case, profile),
        "run_id": run_id,
        "report_relative_path": "../../outside.json",
        "runtime_relative_path": "../../outside",
        "report_file_sha256": _sha(report_path.read_bytes()),
    }
    checkpoint_path = _checkpoint_path(output, case)
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    original_verify = benchmark.verify_runtime_evidence
    original_legality = benchmark._report_legality
    benchmark.verify_runtime_evidence = lambda _report, _root: {"valid": True}
    benchmark._report_legality = lambda *_args, **_kwargs: {"valid": True}
    try:
        resumed = _load_resumable_case(suite, case, profile, output)

        assert resumed is not None
        resumed_checkpoint = resumed["checkpoint"]
        assert resumed_checkpoint["report_relative_path"] == report_path.relative_to(
            output
        ).as_posix()
        assert ".." not in resumed_checkpoint["runtime_relative_path"]

        changed_profile = replace(profile, pass_threshold=profile.pass_threshold - 1)
        assert _load_resumable_case(suite, case, changed_profile, output) is None
    finally:
        benchmark.verify_runtime_evidence = original_verify
        benchmark._report_legality = original_legality


def test_report_legality_binds_evaluation_git_sha(tmp_path: Path) -> None:
    suite = validate_suite(_dataset_fixture(tmp_path / "suite"))
    case = suite.cases[0]
    profile = default_profile(SceneType.READY_MADE)
    checkpoint = {
        "status": "COMPLETED",
        **_checkpoint_identity(suite, case, profile),
    }
    report = {
        "schema_version": "1.0",
        "run_id": "run-git-bound",
        "case_id": case.evaluation_case_id,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "decision": "REVIEW",
        "coverage": "DEGRADED",
        "base_score": 70.0,
        "results": [],
        "manifest": {
            "run_id": "run-git-bound",
            "case_id": case.evaluation_case_id,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "result_hash": "f" * 64,
            "git_sha": checkpoint["evaluation_git_sha"],
        },
    }

    legal = benchmark._report_legality(
        report,
        checkpoint,
        case,
        profile,
        {},
        (),
    )
    mismatched = benchmark._report_legality(
        {
            **report,
            "manifest": {**report["manifest"], "git_sha": "0" * 40},
        },
        checkpoint,
        case,
        profile,
        {},
        (),
    )

    assert legal["valid"] is True
    assert mismatched["valid"] is False
    assert "manifest_git_sha_mismatch" in mismatched["reasons"]


def test_live_benchmark_requires_clean_git_identity() -> None:
    original_run = benchmark.subprocess.run

    class Completed:
        returncode = 0
        stdout = ""

    try:
        benchmark.subprocess.run = lambda *_args, **_kwargs: Completed()
        benchmark.require_clean_evaluation_checkout()
        Completed.stdout = " M tracked.py\n"
        with pytest.raises(RuntimeError, match="clean evaluation checkout"):
            benchmark.require_clean_evaluation_checkout()
    finally:
        benchmark.subprocess.run = original_run


def test_visual_usage_and_response_legality_come_from_verified_contracts(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    def reference(name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        path = runtime_root / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return {"uri": str(path), "sha256": _sha(path.read_bytes())}

    scout = reference(
        "scout",
        {
            "audit_metadata": {
                "batch_count": 1,
                "provider_attempt_count": 2,
                "valid_response_count": 1,
                "attempts": [
                    {"batch_index": 1, "outcome": "transport_error"},
                    {"batch_index": 1, "outcome": "valid"},
                ],
            }
        },
    )
    certificate = reference(
        "certificate",
        {
            "metadata": {
                "usage": {
                    "usage_complete": True,
                    "cost_known": True,
                    "request_count": 5,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "image_tokens": 80,
                    "cached_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "request_bytes": 4096,
                    "reported_cost": 0.12,
                }
            }
        },
    )
    successful_attempt = {
        "tier": "FLASH",
        "configured_provider": "Provider",
        "configured_model": "model",
        "request_fingerprint": "a" * 64,
        "response_fingerprint": "b" * 64,
        "model_request_count": 2,
        "execution_status": "SUCCESS",
        "metric_status": "SCORED",
        "error_code": None,
    }
    failed_attempt = {
        "tier": "ADVANCED",
        "configured_provider": "Provider",
        "configured_model": "fallback",
        "request_fingerprint": "c" * 64,
        "model_request_count": 1,
        "execution_status": "ERROR",
        "metric_status": "ERROR",
        "error_code": "MODEL_RESPONSE_INVALID",
    }
    report = {
        "manifest": {
            "artifact_hashes": {
                "atlas_scout": scout["sha256"],
                "visual_coverage_certificate": certificate["sha256"],
            }
        },
        "visual_audit_artifacts": {
            "atlas_scout": scout,
            "visual_coverage_certificate": certificate,
        },
        "results": [
            {
                "execution_status": "SUCCESS",
                "metric_status": "SCORED",
                "metadata": {"routing_attempts": [successful_attempt, failed_attempt]},
            },
            # The initial/final pipeline may expose the same attempt twice.  It
            # must not inflate either the numerator or denominator.
            {
                "execution_status": "SUCCESS",
                "metric_status": "SCORED",
                "metadata": {"routing_attempts": [successful_attempt]},
            },
            {
                "execution_status": "SUCCESS",
                "metric_status": "NA",
                "metadata": {"routing_attempts": []},
            },
        ],
    }

    usage = _visual_usage(report, runtime_root)
    legality = _model_response_legality(report, runtime_root)

    assert usage["cache_creation_input_tokens"] == 5
    assert usage["request_bytes"] == 4096
    assert usage["reported_cost"] == 0.12
    assert legality["valid_response_count"] == 2
    assert legality["response_attempt_count"] == 2
    assert legality["logical_audit_count"] == 2
    assert legality["legal_response_rate"] == 1.0
    assert legality["meets_threshold"] is True
    assert legality["counting_contract"] == "POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2"


def test_model_node_timeout_counts_as_an_invalid_logical_audit(tmp_path: Path) -> None:
    report = {
        "results": [
            {
                "oracle_id": "v8.visual.composition_layout",
                "metric_id": "structured_vlm_composition_layout",
                "execution_status": "ERROR",
                "metric_status": "ERROR",
                "error_code": "ORACLE_EXCEPTION",
                "metadata": {},
            },
            {
                "oracle_id": "v8.visual.page_index",
                "metric_id": "visual_asset_semantic_risk",
                "execution_status": "ERROR",
                "metric_status": "ERROR",
                "error_code": "ORACLE_EXCEPTION",
                "metadata": {},
            },
        ]
    }

    legality = _model_response_legality(report, tmp_path)

    assert legality["valid_response_count"] == 0
    assert legality["logical_audit_count"] == 1
    assert legality["legal_response_rate"] == 0.0
    assert legality["meets_threshold"] is False


def test_runtime_integrity_requires_one_hash_linked_completed_event(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    artifact_hashes: dict[str, str] = {}

    def artifact(role: str) -> Mapping[str, Any]:
        path = runtime_root / f"{role}.json"
        path.write_text("{}", encoding="utf-8")
        digest = _sha(path.read_bytes())
        artifact_hashes[role] = digest
        return {"uri": str(path), "sha256": digest}

    observation = artifact("atomic_observations")
    visual_roles = (
        "visual_page_index",
        "atlas_scout",
        "visual_selection_plan",
        "visual_audit_rounds",
        "visual_coverage_certificate",
    )
    visual = {role: artifact(role) for role in visual_roles}
    result_hash = "f" * 64
    report = {
        "run_id": "run-integrity",
        "manifest": {
            "result_hash": result_hash,
            "artifact_hashes": artifact_hashes,
        },
        "observation_artifact": observation,
        "visual_audit_artifacts": visual,
    }
    audit = benchmark.JsonlAuditLog(runtime_root / "audit" / "events.jsonl")
    audit.append(
        run_id="run-integrity",
        event_type="RUN_FAILED",
        actor="test",
        payload={"result_hash": result_hash},
    )

    failed = verify_runtime_evidence(report, runtime_root)

    assert failed["valid"] is False
    assert failed["failed_event_present"] is True
    assert failed["completed_event_present"] is False

    completed_root = tmp_path / "completed"
    completed_root.mkdir()
    completed_hashes: dict[str, str] = {}

    def completed_artifact(role: str) -> Mapping[str, Any]:
        path = completed_root / f"{role}.json"
        path.write_text("{}", encoding="utf-8")
        digest = _sha(path.read_bytes())
        completed_hashes[role] = digest
        return {"uri": str(path), "sha256": digest}

    completed_observation = completed_artifact("atomic_observations")
    completed_visual = {role: completed_artifact(role) for role in visual_roles}
    completed_report = {
        "run_id": "run-integrity",
        "manifest": {
            "result_hash": result_hash,
            "artifact_hashes": completed_hashes,
        },
        "observation_artifact": completed_observation,
        "visual_audit_artifacts": completed_visual,
    }
    completed_audit = benchmark.JsonlAuditLog(
        completed_root / "audit" / "events.jsonl"
    )
    completed_audit.append(
        run_id="run-integrity",
        event_type="RUN_COMPLETED",
        actor="test",
        payload={"result_hash": result_hash},
    )

    completed = verify_runtime_evidence(completed_report, completed_root)

    assert completed["valid"] is True
    assert completed["result_hash_linked"] is True


def _summary_case(
    *,
    case_id: str,
    human_rank: int,
    score: float,
    coverage: str = "FULL",
) -> dict[str, object]:
    return {
        "case_key": f"sample_topic/{case_id}",
        "case_id": case_id,
        "topic": "Sample_Topic",
        "topic_slug": "sample_topic",
        "product": case_id,
        "human_rank": human_rank,
        "page_count": 1,
        "status": "COMPLETED",
        "run_id": f"run-{case_id}",
        "profile_id": "finished-deck-v8",
        "profile_version": "8.4",
        "decision": "PASS" if score >= 80 else "REVIEW",
        "coverage": coverage,
        "base_score": score,
        "full_score": score,
        "report_relative_path": f"runtime/sample_topic/{case_id}/runs/run-{case_id}.json",
        "audit_integrity": {"valid": True},
        "report_legality": {"valid": True, "reasons": []},
        "visual_audit_summary": {
            "usage_complete": True,
            "coverage_complete": coverage == "FULL",
        },
        "visual_usage": {
            "usage_complete": True,
            "cost_known": True,
            "request_count": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "image_tokens": 8,
            "cached_tokens": 1,
            "cache_creation_input_tokens": 0,
            "request_bytes": 100,
            "reported_cost": 0.01,
        },
        "model_response_legality": {
            "valid_response_count": 1,
            "response_attempt_count": 1,
            "legal_response_rate": 1.0,
            "meets_threshold": True,
        },
        "result_status_counts": {
            "metric_status": {"SCORED": 1},
            "execution_status": {"SUCCESS": 1},
        },
        "composite_scores": {"composition_craft": score / 100.0},
        "composite_metric_statuses": {"composition_craft": "SCORED"},
    }


def test_topic_statistics_are_within_topic_and_degraded_suppresses_formal() -> None:
    cases = (
        _summary_case(case_id="a", human_rank=1, score=90),
        _summary_case(case_id="b", human_rank=2, score=80),
        _summary_case(case_id="c", human_rank=3, score=70),
    )
    eligible = summarize_topic("Sample_Topic", "sample_topic", cases)
    assert eligible["rank_statistics_eligible"] is True
    assert math.isclose(
        eligible["statistics"]["spearman_base_vs_human"], 1.0
    )
    assert eligible["statistics"]["pairwise_base_accuracy"] == 1.0

    degraded_cases = [dict(item) for item in cases]
    degraded_cases[1]["coverage"] = "DEGRADED"
    degraded = summarize_topic(
        "Sample_Topic", "sample_topic", degraded_cases
    )

    assert degraded["rank_statistics_eligible"] is False
    assert degraded["statistics"]["spearman_base_vs_human"] is None
    assert degraded["statistics"]["pairwise_base_accuracy"] is None
    assert math.isclose(
        degraded["exploratory_statistics"]["all_available_cases"][
            "spearman_base_vs_human"
        ],
        1.0,
    )
    assert degraded["rank_gate_reasons"] == [
        "coverage_not_full:sample_topic/b:DEGRADED"
    ]
    visual_incomplete_cases = [dict(item) for item in cases]
    visual_incomplete_cases[0] = {
        **visual_incomplete_cases[0],
        "visual_audit_summary": {
            **visual_incomplete_cases[0]["visual_audit_summary"],
            "coverage_complete": False,
        },
    }
    visual_incomplete = summarize_topic(
        "Sample_Topic",
        "sample_topic",
        visual_incomplete_cases,
    )
    assert visual_incomplete["rank_statistics_eligible"] is False
    assert "visual_coverage_incomplete:sample_topic/a" in visual_incomplete[
        "rank_gate_reasons"
    ]
    composite = eligible["exploratory_statistics"]["composite_metrics"][
        "composition_craft"
    ]
    assert math.isclose(composite["spearman_vs_human"], 1.0)
    assert composite["population_variance"] > 0


def test_formal_statistics_require_expected_profile_and_every_finite_score() -> None:
    cases = [
        _summary_case(case_id="a", human_rank=1, score=90),
        _summary_case(case_id="b", human_rank=2, score=80),
        _summary_case(case_id="c", human_rank=3, score=70),
    ]
    cases[1]["base_score"] = None
    cases[1]["profile_id"] = "unexpected-profile"

    topic = summarize_topic("Sample_Topic", "sample_topic", cases)

    assert topic["rank_statistics_eligible"] is False
    assert topic["statistics"]["spearman_base_vs_human"] is None
    assert "profile_id_mismatch:sample_topic/b" in topic["rank_gate_reasons"]
    assert "base_score_not_finite:sample_topic/b" in topic["rank_gate_reasons"]


def test_suite_output_labels_diagnostic_statistics_as_non_gating(
    tmp_path: Path,
) -> None:
    case = CaseSpec(
        topic="Sample_Topic",
        topic_slug="sample_topic",
        dataset_id="topic-dataset",
        product="Alpha",
        human_rank=1,
        case_id="alpha",
        pptx_path=tmp_path / "alpha.pptx",
        pptx_relative_path="topics/sample/alpha.pptx",
        pptx_sha256="a" * 64,
        pptx_bytes=1,
        renders=(),
    )
    second_case = replace(
        case,
        product="Beta",
        human_rank=2,
        case_id="beta",
        pptx_path=tmp_path / "beta.pptx",
        pptx_relative_path="topics/sample/beta.pptx",
        pptx_sha256="b" * 64,
    )
    suite = SuiteSpec(
        root=tmp_path,
        dataset_id="suite",
        revision="revision",
        cases=(case, second_case),
        manifest_file_count=1,
        manifest_bytes=1,
    )
    topic = summarize_topic(
        "Sample_Topic",
        "sample_topic",
        (
            _summary_case(case_id="a", human_rank=1, score=90),
            _summary_case(
                case_id="b", human_rank=2, score=80, coverage="DEGRADED"
            ),
        ),
    )

    payload = aggregate_suite(
        suite,
        default_profile(SceneType.READY_MADE),
        (topic,),
    )
    document = build_suite_html(payload)

    assert payload["aggregate"]["formal_macro_spearman_base_vs_human"] is None
    assert math.isclose(
        payload["aggregate"]["exploratory_unqualified"][
            "macro_spearman_base_vs_human"
        ],
        1.0,
    )
    assert payload["methodology"]["global_rank_statistics_prohibited"] is True
    assert payload["methodology"]["human_rank_visible_to_oracles"] is False
    assert payload["methodology"]["official_render_paths_anonymized"] is True
    assert payload["aggregate"]["result_status_counts"]["metric_status"] == {
        "SCORED": 2
    }
    assert payload["aggregate"]["visual_usage"]["request_count_all_cases_complete"] == 2
    assert payload["aggregate"]["visual_usage"]["known_reported_cost"] == 0.02
    assert payload["aggregate"]["model_response_legality"][
        "legal_response_rate"
    ] == 1.0
    assert payload["aggregate"]["model_response_legality"][
        "counting_contract"
    ] == "POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2"
    assert payload["aggregate"]["all_topics_rank_eligible"] is False
    assert payload["aggregate"]["validation_gate"]["passed"] is True
    assert "composition_craft" in payload["aggregate"][
        "exploratory_unqualified"
    ]["composite_metrics"]
    assert "正式统计已抑制" in document
    assert "诊断" in document
    assert "OracleResult N/A" in document
    assert "不得门禁、拟合权重或跨主题混排" in document
    assert "<b>0.020</b>可验证模型成本" in document

    unknown_cost_payload = json.loads(json.dumps(payload))
    unknown_cost_payload["aggregate"]["visual_usage"]["known_reported_cost"] = None
    unknown_cost_payload["aggregate"]["visual_usage"][
        "reported_cost_when_reported"
    ] = 0.0
    unknown_cost_document = build_suite_html(unknown_cost_payload)
    assert "<b>N/A</b>可验证模型成本" in unknown_cost_document
    assert "<b>0.000</b>可验证模型成本" not in unknown_cost_document


def test_html_escapes_labels_and_output_writer_rejects_unsafe_slug(
    tmp_path: Path,
) -> None:
    suite = validate_suite(_dataset_fixture(tmp_path / "suite"))
    cases = (
        _summary_case(case_id="a", human_rank=1, score=90),
        _summary_case(case_id="b", human_rank=2, score=80),
    )
    topic = dict(summarize_topic("<script>alert(1)</script>", "sample_topic", cases))
    topic_cases = [dict(item) for item in topic["cases"]]
    topic_cases[0]["product"] = '"><img src=x onerror=alert(1)>'
    topic["cases"] = topic_cases
    payload = aggregate_suite(
        suite,
        default_profile(SceneType.READY_MADE),
        (topic,),
    )

    document = build_suite_html(payload)

    assert "<script>alert(1)</script>" not in document
    assert "<img src=x onerror=alert(1)>" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document

    unsafe_payload = dict(payload)
    unsafe_topic = dict(topic)
    unsafe_topic["topic_slug"] = "../../escaped"
    unsafe_payload["topics"] = [unsafe_topic]
    with pytest.raises(ValueError, match="topic output slug"):
        _write_outputs(unsafe_payload, suite, tmp_path / "output")
