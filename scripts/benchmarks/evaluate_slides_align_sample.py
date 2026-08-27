"""Evaluate the pinned Slides-Align market-analysis sample and build an HTML report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ppt_eval.config import load_profile  # noqa: E402
from ppt_eval.domain import EvalCase, EvalProfile, SceneType  # noqa: E402
from ppt_eval.infrastructure import JsonlAuditLog  # noqa: E402
from ppt_eval.oracles.model_audits import (  # noqa: E402
    GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    GROUNDED_STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
    GROUNDED_VLM_CRITERION_PROMPTS,
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_DIMENSIONS_VLM_ORACLE_ID,
    STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
    STRUCTURED_VLM_VISUAL_DIMENSION_METRICS,
    STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT,
)
from ppt_eval.runtime import (  # noqa: E402
    LocalEvaluationRuntime,
    build_runtime_from_environment,
)

VISUAL_PROXY_WEIGHTS = {
    "visual_hierarchy": 0.12,
    "layout": 0.12,
    "typography": 0.10,
    "style_consistency": 0.10,
    "multimedia_quality": 0.08,
}
STRUCTURED_VLM_DIMENSION_IDS = tuple(
    metric_id for _criterion_id, metric_id in STRUCTURED_VLM_VISUAL_DIMENSION_METRICS
)
STRUCTURED_VLM_CRITERION_BY_METRIC = {
    metric_id: criterion_id for criterion_id, metric_id in STRUCTURED_VLM_VISUAL_DIMENSION_METRICS
}
STRUCTURED_VLM_DIMENSION_ID_SET = frozenset(STRUCTURED_VLM_DIMENSION_IDS)
V8_ADDITIONAL_MODEL_METRIC_IDS = ("structured_vlm_authorship_specificity",)
V8_RASTER_TEXT_MODEL_METRIC_IDS = (
    "structured_vlm_raster_content_structure",
    "structured_vlm_raster_language_consistency",
)
STRUCTURED_VLM_SENSITIVITY_SHARES = (0.10, 0.15, 0.20, 0.25)


def _structured_dimension_contract_spec(
    profile: EvalProfile,
) -> Mapping[str, Any] | None:
    candidates = (
        {
            "composite_id": STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
            "mode": "SHARED_BATCH",
            "oracle_ids": {
                metric_id: STRUCTURED_DIMENSIONS_VLM_ORACLE_ID
                for metric_id in STRUCTURED_VLM_DIMENSION_IDS
            },
            "version": STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
            "prompts": {
                metric_id: STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT
                for metric_id in STRUCTURED_VLM_DIMENSION_IDS
            },
        },
        {
            "composite_id": GROUNDED_STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
            "mode": "ATOMIC_CRITERION_CALLS",
            "oracle_ids": {
                metric_id: f"grounded_vlm_{criterion_id}_audit_oracle"
                for criterion_id, metric_id in STRUCTURED_VLM_VISUAL_DIMENSION_METRICS
            },
            "version": GROUNDED_STRUCTURED_VLM_DIMENSIONS_ORACLE_VERSION,
            "prompts": {
                metric_id: GROUNDED_VLM_CRITERION_PROMPTS[criterion_id]
                for criterion_id, metric_id in STRUCTURED_VLM_VISUAL_DIMENSION_METRICS
            },
        },
    )
    selected = [
        candidate
        for candidate in candidates
        if candidate["composite_id"] in profile.enabled_oracle_ids
    ]
    if len(selected) > 1:
        raise ValueError("profile enables more than one structured visual contract")
    return selected[0] if selected else None


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks, with larger values receiving larger ranks."""

    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + 1 + end) / 2.0
        for position in range(index, end):
            result[ordered[position][0]] = average
        index = end
    return result


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_norm = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_norm = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return numerator / (left_norm * right_norm)


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    return pearson(average_ranks(left), average_ranks(right))


def pairwise_accuracy(human_ranks: Sequence[int], scores: Sequence[float]) -> float | None:
    credits: list[float] = []
    for left in range(len(scores)):
        for right in range(left + 1, len(scores)):
            human_direction = human_ranks[right] - human_ranks[left]
            score_direction = scores[left] - scores[right]
            if score_direction == 0:
                credits.append(0.5)
            else:
                credits.append(1.0 if human_direction * score_direction > 0 else 0.0)
    return sum(credits) / len(credits) if credits else None


def rank_order(
    cases: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], float]
) -> dict[str, int]:
    ordered = sorted(cases, key=key, reverse=True)
    return {str(item["case_id"]): index for index, item in enumerate(ordered, start=1)}


def deterministic_visual_proxy(results: Mapping[str, Mapping[str, Any]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for metric_id, weight in VISUAL_PROXY_WEIGHTS.items():
        result = results.get(metric_id)
        if not result or result.get("normalized_score") is None:
            continue
        numerator += weight * float(result["normalized_score"])
        denominator += weight
    return 100.0 * numerator / denominator if denominator else 0.0


# Backward-compatible import name for existing notebooks/tests.  New reports
# use ``deterministic_visual_proxy`` so it cannot be mistaken for Qwen VLM.
visual_proxy = deterministic_visual_proxy


def _matches_configured_model(actual_model: str, configured_model: str) -> bool:
    return actual_model == configured_model or actual_model.startswith(
        configured_model + "-"
    )


def _manifest_model_id(value: object) -> str | None:
    if not isinstance(value, str) or "/" not in value or "@" not in value:
        return None
    actual_model = value.rsplit("/", 1)[-1].split("@", 1)[0]
    return actual_model or None


def _metric_score(result: Mapping[str, Any] | None) -> float | None:
    if result is None:
        return None
    status = str(result.get("metric_status", ""))
    if status == "PASS":
        return 1.0
    if status == "FAIL":
        return 0.0
    if status != "SCORED":
        return None
    value = result.get("normalized_score")
    return None if value is None else float(value)


def _weighted_complete_score(
    results: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    *,
    require_complete: bool = True,
) -> tuple[float | None, tuple[str, ...]]:
    missing: list[str] = []
    numerator = 0.0
    denominator = 0.0
    for metric_id, weight_value in weights.items():
        weight = float(weight_value)
        if weight <= 0:
            continue
        value = _metric_score(results.get(metric_id))
        if value is None:
            missing.append(metric_id)
            continue
        numerator += weight * value
        denominator += weight
    if (require_complete and missing) or denominator == 0.0:
        return None, tuple(missing)
    return numerator / denominator, tuple(missing)


def _structured_dimension_weights(profile: EvalProfile) -> Mapping[str, float]:
    weights = {
        metric_id: float(profile.base_weights.get(metric_id, 0.0))
        for metric_id in STRUCTURED_VLM_DIMENSION_IDS
    }
    if any(weight <= 0.0 for weight in weights.values()):
        raise ValueError("structured visual profile must positively weight all six dimensions")
    if any(
        profile.base_metric_constructs.get(metric_id) != "visual_vlm"
        for metric_id in STRUCTURED_VLM_DIMENSION_IDS
    ):
        raise ValueError("all six structured visual metrics must belong to visual_vlm")
    total = sum(weights.values())
    normalized = {metric_id: weight / total for metric_id, weight in weights.items()}
    declared = profile.metadata.get("vlm_dimension_budget")
    if isinstance(declared, Mapping):
        if set(declared) != STRUCTURED_VLM_DIMENSION_ID_SET:
            raise ValueError("vlm_dimension_budget must name exactly the six Oracle metrics")
        declared_total = sum(float(value) for value in declared.values())
        if declared_total <= 0.0:
            raise ValueError("vlm_dimension_budget must have positive mass")
        for metric_id, weight in normalized.items():
            declared_weight = float(declared[metric_id]) / declared_total
            if not math.isclose(weight, declared_weight, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(f"profile weight and vlm_dimension_budget disagree for {metric_id}")
    deterministic_construct_weight = float(profile.base_construct_weights.get("visual_deterministic", 0.0))
    vlm_construct_weight = float(profile.base_construct_weights.get("visual_vlm", 0.0))
    visual_construct_weight = deterministic_construct_weight + vlm_construct_weight
    declared_share = float(profile.metadata.get("vlm_visual_construct_share", -1.0))
    declared_overall_share = float(profile.metadata.get("vlm_overall_score_share", -1.0))
    if (
        visual_construct_weight <= 0.0
        or not math.isclose(
            vlm_construct_weight / visual_construct_weight,
            declared_share,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            vlm_construct_weight,
            declared_overall_share,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or profile.metadata.get("vlm_share_semantics") != "HARD_CAP_VIA_SEPARATE_CONSTRUCT"
    ):
        raise ValueError("Profile visual construct weights do not enforce the declared VLM hard cap")
    return normalized


def structured_visual_sensitivity_scores(
    results: Mapping[str, Mapping[str, Any]],
    profile: EvalProfile,
    *,
    shares: Sequence[float] = STRUCTURED_VLM_SENSITIVITY_SHARES,
) -> dict[str, Any]:
    """Recompute only the visual construct under predeclared VLM-share scenarios.

    This intentionally excludes delivery and handoff constructs: Slides-Align ranks
    visible preference, not editability, accessibility, or delivery readiness.
    """

    dimension_weights = _structured_dimension_weights(profile)
    deterministic_weights = {
        metric_id: float(profile.base_weights[metric_id])
        for metric_id, construct_id in profile.base_metric_constructs.items()
        if construct_id == "visual_deterministic" and float(profile.base_weights.get(metric_id, 0.0)) > 0.0
    }
    deterministic, deterministic_missing = _weighted_complete_score(
        results,
        deterministic_weights,
        require_complete=False,
    )
    structured_vlm, dimension_missing = _weighted_complete_score(
        results,
        dimension_weights,
    )
    scores: dict[str, float] = {}
    effective_shares: dict[str, float] = {}
    if deterministic is not None and structured_vlm is not None:
        for share_value in shares:
            share = float(share_value)
            if not 0.0 <= share <= 1.0:
                raise ValueError("VLM visual share must be in [0,1]")
            key = f"{share:.2f}"
            scores[key] = (1.0 - share) * deterministic + share * structured_vlm
            effective_shares[key] = share
    return {
        "deterministic_visual_score": deterministic,
        "structured_vlm_score": structured_vlm,
        "dimension_scores": {
            metric_id: _metric_score(results.get(metric_id)) for metric_id in STRUCTURED_VLM_DIMENSION_IDS
        },
        "scores_by_vlm_share": scores,
        "effective_vlm_share_by_nominal_share": effective_shares,
        "missing_deterministic_metric_ids": list(deterministic_missing),
        "missing_dimension_metric_ids": list(dimension_missing),
    }


def _structured_projection_contract(
    entries: Sequence[Mapping[str, Any]],
    profile: EvalProfile,
) -> dict[str, Any]:
    failures: list[str] = []
    contract = _structured_dimension_contract_spec(profile)
    if contract is None:
        return {
            "oracle_projection_contract_ok": False,
            "oracle_projection_contract_failures": ["profile:structured_contract_missing"],
            "request_fingerprint": None,
            "response_fingerprint": None,
            "model_ids": [],
            "prompt_reference": None,
            "usage_owner_metric_id": None,
            "criterion_confidences": {},
            "criterion_observability": {},
        }
    prompts = contract["prompts"]
    oracle_ids = contract["oracle_ids"]
    if not isinstance(prompts, Mapping) or not isinstance(oracle_ids, Mapping):
        raise ValueError("structured visual contract mappings are invalid")
    contract_mode = str(contract["mode"])
    expected_version = str(contract["version"])
    expected_owner = STRUCTURED_VLM_DIMENSION_IDS[0]
    request_fingerprints: set[str] = set()
    response_fingerprints: set[str] = set()
    model_ids: set[str] = set()
    owners: list[str] = []
    criterion_confidences: dict[str, float | None] = {}
    criterion_observability: dict[str, str | None] = {}
    configured_confidence_floor = profile.metadata.get(
        "vlm_dimension_min_confidence"
    )
    if (
        isinstance(configured_confidence_floor, bool)
        or not isinstance(configured_confidence_floor, (int, float))
        or not 0.0 <= float(configured_confidence_floor) <= 1.0
    ):
        configured_confidence_floor = None
        failures.append("profile:vlm_dimension_min_confidence")
    else:
        configured_confidence_floor = float(configured_confidence_floor)
    allocated_cost_total = 0.0
    owner_usage_cost: float | None = None
    atomic_usage_cost_total = 0.0
    for entry in entries:
        metric_id = str(entry.get("metric_id", ""))
        if metric_id not in STRUCTURED_VLM_DIMENSION_ID_SET:
            continue
        metadata = entry.get("metadata")
        if not isinstance(metadata, Mapping):
            failures.append(f"{metric_id}:metadata")
            continue
        expected_prompt_spec = prompts.get(metric_id)
        expected_oracle_id = oracle_ids.get(metric_id)
        if expected_prompt_spec is None or expected_oracle_id is None:
            failures.append(f"{metric_id}:contract_spec")
            continue
        expected_prompt = dict(expected_prompt_spec.reference())
        if entry.get("oracle_id") != expected_oracle_id:
            failures.append(f"{metric_id}:oracle_id")
        if entry.get("version") != expected_version:
            failures.append(f"{metric_id}:oracle_version")
        if metadata.get("prompt") != expected_prompt:
            failures.append(f"{metric_id}:prompt_reference")
        if metadata.get("structured_contract_version") != expected_version:
            failures.append(f"{metric_id}:structured_contract_version")
        if contract_mode == "SHARED_BATCH":
            if metadata.get("output_mode") != "SINGLE_CALL_DIMENSION_PROJECTION":
                failures.append(f"{metric_id}:output_mode")
            if metadata.get("batch_request_metric_id") != (
                "structured_vlm_visual_dimensions_batch"
            ):
                failures.append(f"{metric_id}:batch_request_metric_id")
            if metadata.get("dimension_batch_validated") is not True:
                failures.append(f"{metric_id}:dimension_batch_validated")
        else:
            if metadata.get("call_granularity") != (
                "ONE_CRITERION_BOUNDED_PAGE_SAMPLE"
            ):
                failures.append(f"{metric_id}:call_granularity")
            if metadata.get("atomic_criterion_validated") is not True:
                failures.append(f"{metric_id}:atomic_criterion_validated")
            if metadata.get("visual_page_grounding_validated") is not True:
                failures.append(f"{metric_id}:visual_page_grounding_validated")
        if metadata.get("criterion_id") != STRUCTURED_VLM_CRITERION_BY_METRIC[metric_id]:
            failures.append(f"{metric_id}:criterion_id")
        if metadata.get("model_global_score_used_for_metric") is not False:
            failures.append(f"{metric_id}:model_global_score_used")
        observability = metadata.get("criterion_observability")
        criterion_observability[metric_id] = (
            observability if isinstance(observability, str) else None
        )
        criterion_confidence = metadata.get("criterion_confidence")
        result_confidence = entry.get("confidence")
        if (
            isinstance(criterion_confidence, bool)
            or not isinstance(criterion_confidence, (int, float))
            or not 0.0 <= float(criterion_confidence) <= 1.0
            or isinstance(result_confidence, bool)
            or not isinstance(result_confidence, (int, float))
            or not math.isclose(
                float(criterion_confidence),
                float(result_confidence),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            criterion_confidences[metric_id] = None
            failures.append(f"{metric_id}:criterion_confidence")
        else:
            criterion_confidences[metric_id] = float(criterion_confidence)
        result_confidence_floor = metadata.get("criterion_confidence_floor")
        if (
            configured_confidence_floor is None
            or isinstance(result_confidence_floor, bool)
            or not isinstance(result_confidence_floor, (int, float))
            or not math.isclose(
                float(result_confidence_floor),
                configured_confidence_floor,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            failures.append(f"{metric_id}:criterion_confidence_floor")
        metric_status = str(entry.get("metric_status", ""))
        score = entry.get("normalized_score")
        criterion_score = metadata.get("criterion_score")
        if metric_status == "SCORED":
            if observability not in {"FULL", "PARTIAL"}:
                failures.append(f"{metric_id}:criterion_observability")
            if metadata.get("criterion_score_used_for_metric") is not True:
                failures.append(f"{metric_id}:criterion_score_used")
            if (
                configured_confidence_floor is None
                or not isinstance(criterion_confidence, (int, float))
                or float(criterion_confidence) < configured_confidence_floor
            ):
                failures.append(f"{metric_id}:scored_below_confidence_floor")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or isinstance(criterion_score, bool)
                or not isinstance(criterion_score, (int, float))
                or not math.isclose(
                    float(score),
                    float(criterion_score),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                failures.append(f"{metric_id}:criterion_score")
        elif metric_status == "NA":
            if score is not None or metadata.get("criterion_score_used_for_metric") is not False:
                failures.append(f"{metric_id}:na_projection")
            if observability == "INSUFFICIENT":
                validation_reason = metadata.get("criterion_validation_reason")
                expected_reason = (
                    validation_reason
                    if isinstance(validation_reason, str) and validation_reason
                    else "CRITERION_OBSERVABILITY_INSUFFICIENT"
                )
                if (
                    criterion_score is not None
                    or metadata.get("reason_code") != expected_reason
                ):
                    failures.append(f"{metric_id}:insufficient_projection")
            elif observability in {"FULL", "PARTIAL"}:
                if (
                    configured_confidence_floor is None
                    or not isinstance(criterion_confidence, (int, float))
                    or float(criterion_confidence) >= configured_confidence_floor
                    or isinstance(criterion_score, bool)
                    or not isinstance(criterion_score, (int, float))
                    or metadata.get("reason_code")
                    != "CRITERION_CONFIDENCE_BELOW_PROFILE_FLOOR"
                ):
                    failures.append(f"{metric_id}:low_confidence_projection")
            else:
                failures.append(f"{metric_id}:criterion_observability")
        else:
            failures.append(f"{metric_id}:projection_state")
        request_fingerprint = metadata.get("request_fingerprint")
        response_fingerprint = metadata.get("response_fingerprint")
        if isinstance(request_fingerprint, str) and request_fingerprint:
            request_fingerprints.add(request_fingerprint)
        else:
            failures.append(f"{metric_id}:request_fingerprint")
        if isinstance(response_fingerprint, str) and response_fingerprint:
            response_fingerprints.add(response_fingerprint)
        else:
            failures.append(f"{metric_id}:response_fingerprint")
        model = metadata.get("model")
        model_id = model.get("model_id") if isinstance(model, Mapping) else None
        if isinstance(model_id, str) and model_id:
            model_ids.add(model_id)
        else:
            failures.append(f"{metric_id}:model_id")
        result_cost = entry.get("cost")
        if contract_mode == "SHARED_BATCH":
            if metadata.get("shared_call_usage_owner_metric_id") != expected_owner:
                failures.append(f"{metric_id}:usage_owner_metric_id")
            is_owner = metadata.get("shared_call_usage_owner") is True
            if is_owner:
                owners.append(metric_id)
                usage = metadata.get("usage")
                if not isinstance(usage, Mapping):
                    failures.append(f"{metric_id}:owner_usage")
                else:
                    usage_cost = usage.get("cost")
                    if isinstance(usage_cost, bool) or not isinstance(
                        usage_cost, (int, float)
                    ):
                        failures.append(f"{metric_id}:owner_usage_cost")
                    else:
                        owner_usage_cost = float(usage_cost)
            elif "usage" in metadata:
                failures.append(f"{metric_id}:duplicate_usage")
            if metadata.get("cost_allocation_method") != "EQUAL_BY_OUTPUT_METRIC":
                failures.append(f"{metric_id}:cost_allocation_method")
            fraction = metadata.get("cost_allocation_fraction")
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not math.isclose(
                    float(fraction),
                    1.0 / len(STRUCTURED_VLM_DIMENSION_IDS),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                failures.append(f"{metric_id}:cost_allocation_fraction")
            allocated_cost = metadata.get("allocated_cost")
            if (
                isinstance(allocated_cost, bool)
                or not isinstance(allocated_cost, (int, float))
                or isinstance(result_cost, bool)
                or not isinstance(result_cost, (int, float))
                or not math.isclose(
                    float(allocated_cost),
                    float(result_cost),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                failures.append(f"{metric_id}:allocated_cost")
            else:
                allocated_cost_total += float(result_cost)
        else:
            usage = metadata.get("usage")
            if not isinstance(usage, Mapping):
                failures.append(f"{metric_id}:atomic_usage")
            else:
                usage_cost = usage.get("cost")
                if (
                    isinstance(usage_cost, bool)
                    or not isinstance(usage_cost, (int, float))
                    or isinstance(result_cost, bool)
                    or not isinstance(result_cost, (int, float))
                    or not math.isclose(
                        float(usage_cost),
                        float(result_cost),
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                ):
                    failures.append(f"{metric_id}:atomic_usage_cost")
                else:
                    atomic_usage_cost_total += float(usage_cost)
        evidence = entry.get("evidence")
        expected_evidence_count = (
            1
            if contract_mode == "SHARED_BATCH"
            or metric_id == "structured_vlm_cross_slide_consistency"
            else metadata.get("observation_count")
        )
        if (
            not isinstance(evidence, list)
            or isinstance(expected_evidence_count, bool)
            or not isinstance(expected_evidence_count, int)
            or expected_evidence_count < 1
            or len(evidence) != expected_evidence_count
        ):
            failures.append(f"{metric_id}:criterion_evidence")
    expected_call_count = (
        1 if contract_mode == "SHARED_BATCH" else len(STRUCTURED_VLM_DIMENSION_IDS)
    )
    if len(request_fingerprints) != expected_call_count:
        failures.append("visual_calls:request_fingerprints")
    if len(response_fingerprints) != expected_call_count:
        failures.append("visual_calls:response_fingerprints")
    expected_model = profile.metadata.get("flash_model")
    if (
        not isinstance(expected_model, str)
        or len(model_ids) != 1
        or not all(
            _matches_configured_model(model_id, expected_model)
            for model_id in model_ids
        )
    ):
        failures.append("shared_call:model_id")
    if contract_mode == "SHARED_BATCH":
        if owners != [expected_owner]:
            failures.append("shared_call:usage_owner")
        if owner_usage_cost is None or not math.isclose(
            allocated_cost_total,
            owner_usage_cost,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            failures.append("shared_call:cost_total")
    return {
        "oracle_projection_contract_ok": not failures,
        "oracle_projection_contract_failures": sorted(set(failures)),
        "request_fingerprint": (
            next(iter(request_fingerprints), None)
            if contract_mode == "SHARED_BATCH"
            else None
        ),
        "response_fingerprint": (
            next(iter(response_fingerprints), None)
            if contract_mode == "SHARED_BATCH"
            else None
        ),
        "request_fingerprints": sorted(request_fingerprints),
        "response_fingerprints": sorted(response_fingerprints),
        "model_ids": sorted(model_ids),
        "prompt_reference": (
            dict(STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT.reference())
            if contract_mode == "SHARED_BATCH"
            else None
        ),
        "prompt_references": {
            metric_id: dict(prompt_spec.reference())
            for metric_id, prompt_spec in prompts.items()
        },
        "call_mode": contract_mode,
        "usage_owner_metric_id": owners[0] if len(owners) == 1 else None,
        "atomic_usage_cost_total": (
            atomic_usage_cost_total
            if contract_mode == "ATOMIC_CRITERION_CALLS"
            else None
        ),
        "criterion_confidences": criterion_confidences,
        "criterion_observability": criterion_observability,
    }


def _structured_manifest_contract(
    report: Mapping[str, Any],
    profile: EvalProfile,
    expected_case_id: str,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    contract = _structured_dimension_contract_spec(profile)
    if contract is None:
        return False, ["profile:structured_contract_missing"]
    manifest = report.get("manifest")
    if not isinstance(manifest, Mapping):
        return False, ["manifest:missing"]
    if (
        manifest.get("case_id") != expected_case_id
        or manifest.get("profile_id") != profile.profile_id
        or manifest.get("profile_version") != profile.version
    ):
        failures.append("manifest:run_identity")
    oracle_versions = manifest.get("oracle_versions")
    if (
        not isinstance(oracle_versions, Mapping)
        or oracle_versions.get(str(contract["composite_id"]))
        != str(contract["version"])
    ):
        failures.append("manifest:oracle_version")
    prompts = contract["prompts"]
    if not isinstance(prompts, Mapping):
        return False, ["profile:structured_prompt_contract_invalid"]
    prompt_versions = manifest.get("prompt_versions")
    if not isinstance(prompt_versions, Mapping) or any(
        prompt_versions.get(metric_id)
        != (
            f"{prompts[metric_id].prompt_id}@{prompts[metric_id].version}"
            f"#{prompts[metric_id].sha256}"
        )
        for metric_id in STRUCTURED_VLM_DIMENSION_IDS
    ):
        failures.append("manifest:prompt_versions")
    expected_model = profile.metadata.get("flash_model")
    model_versions = manifest.get("model_versions")
    actual_manifest_models = (
        [
            _manifest_model_id(model_versions.get(metric_id))
            for metric_id in STRUCTURED_VLM_DIMENSION_IDS
        ]
        if isinstance(model_versions, Mapping)
        else []
    )
    if (
        not isinstance(expected_model, str)
        or not isinstance(model_versions, Mapping)
        or any(model_id is None for model_id in actual_manifest_models)
        or len(set(actual_manifest_models)) != 1
        or not all(
            _matches_configured_model(model_id, expected_model)
            for model_id in actual_manifest_models
            if model_id is not None
        )
    ):
        failures.append("manifest:model_versions")
    return not failures, failures


def structured_visual_case_analysis(
    report: Mapping[str, Any],
    profile: EvalProfile,
    *,
    expected_case_id: str,
) -> dict[str, Any]:
    entries = list(report.get("results", ()))
    indexed = {str(item["metric_id"]): item for item in entries}
    observed = [
        str(item["metric_id"])
        for item in entries
        if str(item.get("metric_id", "")).startswith("structured_vlm_")
    ]
    counts = {metric_id: observed.count(metric_id) for metric_id in set(observed)}
    missing = sorted(STRUCTURED_VLM_DIMENSION_ID_SET - set(observed))
    unexpected = sorted(set(observed) - STRUCTURED_VLM_DIMENSION_ID_SET)
    duplicated = sorted(metric_id for metric_id, count in counts.items() if count != 1)
    projection_contract = _structured_projection_contract(entries, profile)
    manifest_contract_ok, manifest_contract_failures = _structured_manifest_contract(
        report,
        profile,
        expected_case_id,
    )
    sensitivity = structured_visual_sensitivity_scores(indexed, profile)
    profile_share = float(profile.metadata.get("vlm_visual_construct_share", 0.10))
    breakdown = report.get("score_breakdown")
    construct_scores = breakdown.get("base_construct_scores", {}) if isinstance(breakdown, Mapping) else {}
    reported_deterministic_raw = construct_scores.get("visual_deterministic")
    reported_vlm_raw = construct_scores.get("visual_vlm")
    reported_deterministic = None if reported_deterministic_raw is None else float(reported_deterministic_raw)
    reported_vlm = None if reported_vlm_raw is None else float(reported_vlm_raw)
    deterministic_delta = (
        None
        if reported_deterministic is None or sensitivity["deterministic_visual_score"] is None
        else sensitivity["deterministic_visual_score"] - reported_deterministic
    )
    vlm_delta = (
        None
        if reported_vlm is None or sensitivity["structured_vlm_score"] is None
        else sensitivity["structured_vlm_score"] - reported_vlm
    )
    profile_alignment_ok = (
        deterministic_delta is not None
        and vlm_delta is not None
        and math.isclose(deterministic_delta, 0.0, rel_tol=0.0, abs_tol=2e-6)
        and math.isclose(vlm_delta, 0.0, rel_tol=0.0, abs_tol=2e-6)
    )
    exact_dimension_contract = (
        len(observed) == len(STRUCTURED_VLM_DIMENSION_IDS)
        and not missing
        and not unexpected
        and not duplicated
    )
    full_coverage = str(report.get("coverage")) == "FULL"
    all_dimensions_scored = not sensitivity["missing_dimension_metric_ids"] and all(
        str(indexed[metric_id].get("metric_status")) == "SCORED"
        for metric_id in STRUCTURED_VLM_DIMENSION_IDS
        if metric_id in indexed
    )
    profile_identity_ok = (
        report.get("case_id") == expected_case_id
        and report.get("profile_id") == profile.profile_id
        and report.get("profile_version") == profile.version
    )
    return {
        **sensitivity,
        **projection_contract,
        "eligible": (
            full_coverage
            and exact_dimension_contract
            and all_dimensions_scored
            and profile_alignment_ok
            and profile_identity_ok
            and projection_contract["oracle_projection_contract_ok"]
            and manifest_contract_ok
        ),
        "full_coverage": full_coverage,
        "exact_dimension_contract": exact_dimension_contract,
        "all_dimensions_scored": all_dimensions_scored,
        "observed_structured_metric_ids": observed,
        "missing_structured_metric_ids": missing,
        "unexpected_structured_metric_ids": unexpected,
        "duplicated_structured_metric_ids": duplicated,
        "profile_vlm_visual_share": profile_share,
        "reported_visual_deterministic_construct_score": reported_deterministic,
        "reported_visual_vlm_construct_score": reported_vlm,
        "visual_deterministic_alignment_delta": deterministic_delta,
        "visual_vlm_alignment_delta": vlm_delta,
        "profile_alignment_ok": profile_alignment_ok,
        "profile_identity_ok": profile_identity_ok,
        "manifest_contract_ok": manifest_contract_ok,
        "manifest_contract_failures": manifest_contract_failures,
    }


def structured_visual_replay_analysis(
    cases: Sequence[Mapping[str, Any]],
    profile: EvalProfile,
    *,
    expected_cases: Sequence[tuple[str, str, int]],
) -> dict[str, Any] | None:
    if _structured_dimension_contract_spec(profile) is None:
        return None
    excluded = [
        {
            "case_id": str(item["case_id"]),
            "coverage": str(item["coverage"]),
            "missing_structured_metric_ids": list(
                item["structured_visual_analysis"]["missing_structured_metric_ids"]
            ),
            "unexpected_structured_metric_ids": list(
                item["structured_visual_analysis"]["unexpected_structured_metric_ids"]
            ),
            "duplicated_structured_metric_ids": list(
                item["structured_visual_analysis"]["duplicated_structured_metric_ids"]
            ),
            "all_dimensions_scored": bool(item["structured_visual_analysis"]["all_dimensions_scored"]),
            "profile_alignment_ok": bool(item["structured_visual_analysis"]["profile_alignment_ok"]),
            "profile_identity_ok": bool(item["structured_visual_analysis"]["profile_identity_ok"]),
            "oracle_projection_contract_failures": list(
                item["structured_visual_analysis"]["oracle_projection_contract_failures"]
            ),
            "manifest_contract_failures": list(
                item["structured_visual_analysis"]["manifest_contract_failures"]
            ),
        }
        for item in cases
        if not item["structured_visual_analysis"]["eligible"]
    ]
    expected_identities = set(expected_cases)
    observed_identities = {
        (str(item["case_id"]), str(item["product"]), int(item["human_rank"])) for item in cases
    }
    expected_case_count = len(expected_cases)
    identity_uniqueness_ok = (
        len({identity[0] for identity in expected_cases}) == expected_case_count
        and len({identity[1] for identity in expected_cases}) == expected_case_count
        and len({identity[2] for identity in expected_cases}) == expected_case_count
        and len({str(item["case_id"]) for item in cases}) == len(cases)
        and len({str(item["product"]) for item in cases}) == len(cases)
        and len({int(item["human_rank"]) for item in cases}) == len(cases)
    )
    manifest_identity_match = (
        len(cases) == expected_case_count
        and observed_identities == expected_identities
        and identity_uniqueness_ok
    )
    eligible = manifest_identity_match and not excluded
    human_ranks = [int(item["human_rank"]) for item in cases]
    human_utility = [-float(rank) for rank in human_ranks]
    primary_share = float(profile.metadata.get("vlm_visual_construct_share", 0.10))
    statistics: dict[str, Any] = {}
    for share in STRUCTURED_VLM_SENSITIVITY_SHARES:
        key = f"{share:.2f}"
        scores = (
            [100.0 * float(item["structured_visual_analysis"]["scores_by_vlm_share"][key]) for item in cases]
            if eligible
            else []
        )
        effective_shares = (
            [
                float(item["structured_visual_analysis"]["effective_vlm_share_by_nominal_share"][key])
                for item in cases
            ]
            if eligible
            else []
        )
        statistics[key] = {
            "analysis_role": (
                "PREREGISTERED_PRIMARY"
                if math.isclose(share, primary_share, abs_tol=1e-12)
                else "SENSITIVITY_ONLY"
            ),
            "spearman_visual_construct_vs_human": (spearman(scores, human_utility) if eligible else None),
            "pairwise_visual_construct_accuracy": (
                pairwise_accuracy(human_ranks, scores) if eligible else None
            ),
            "effective_vlm_share_range": (
                [min(effective_shares), max(effective_shares)] if effective_shares else []
            ),
            "order": (
                [
                    str(item["product"])
                    for item in sorted(
                        cases,
                        key=lambda item: float(
                            item["structured_visual_analysis"]["scores_by_vlm_share"][key]
                        ),
                        reverse=True,
                    )
                ]
                if eligible
                else []
            ),
        }
    return {
        "eligible": eligible,
        "eligibility_rule": (
            "the exact unique case/product/rank identities from the pinned manifest must be present; "
            "every deck must be FULL, "
            "contain exactly the six SCORED structured VLM metrics once, and match "
            "the current Profile, Oracle/prompt, shared-call, and visual-construct contracts"
        ),
        "expected_case_count": expected_case_count,
        "observed_case_count": len(cases),
        "manifest_identity_match": manifest_identity_match,
        "identity_uniqueness_ok": identity_uniqueness_ok,
        "missing_manifest_identities": sorted(expected_identities - observed_identities),
        "unexpected_report_identities": sorted(observed_identities - expected_identities),
        "full_case_count": sum(item["coverage"] == "FULL" for item in cases),
        "complete_six_dimension_case_count": sum(
            item["structured_visual_analysis"]["exact_dimension_contract"]
            and item["structured_visual_analysis"]["all_dimensions_scored"]
            for item in cases
        ),
        "comparable_pairs": len(cases) * (len(cases) - 1) // 2 if eligible else 0,
        "expected_comparable_pairs": (expected_case_count * (expected_case_count - 1) // 2),
        "primary_vlm_visual_share": primary_share,
        "sensitivity_vlm_visual_shares": list(STRUCTURED_VLM_SENSITIVITY_SHARES),
        "rank_fit_used": bool(profile.metadata.get("rank_fit_used", False)),
        "delivery_handoff_used_for_rank_calibration": False,
        "excluded_cases": excluded,
        "statistics": statistics,
        "interpretation_guardrails": [
            f"The {primary_share:.0%} share is the preregistered primary result.",
            "The 10%/15% down-weight and 25% up-weight rows are sensitivity analyses; none may be selected by rank fit.",
            "Overall human ranks are not gold labels for any of the six VLM dimensions.",
            "Slides-Align ranks visible preference; delivery and handoff constructs are not calibrated here.",
            "This one-topic seven-deck slice is diagnostic, not production calibration evidence.",
        ],
    }


def selected_metrics(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    ids = (
        "v8_functional_integrity",
        "content_structure",
        "language_consistency",
        "composition_craft",
        "typography_craft",
        "palette_craft",
        "visual_communication",
        "visual_system_sequence",
        "authorship_specificity_v2",
        "content_clarity",
        "narrative",
        "visual_hierarchy",
        "layout",
        "typography",
        "style_consistency",
        "multimedia_quality",
        "editability",
        "accessibility",
        "template_residue",
        "llm_content_quality_audit",
        "vlm_visual_quality_audit",
        "structured_vlm_visual_audit",
        *STRUCTURED_VLM_DIMENSION_IDS,
        *V8_ADDITIONAL_MODEL_METRIC_IDS,
        *V8_RASTER_TEXT_MODEL_METRIC_IDS,
        "advanced_llm_content_review",
        "advanced_vlm_visual_review",
    )
    return {
        metric_id: {
            "oracle_id": results[metric_id].get("oracle_id"),
            "oracle_version": results[metric_id].get("version"),
            "metric_status": results[metric_id].get("metric_status"),
            "score_role": results[metric_id].get("score_role"),
            "score": results[metric_id].get("normalized_score"),
            "confidence": results[metric_id].get("confidence"),
            "cost": results[metric_id].get("cost", 0.0),
            "evidence_count": len(results[metric_id].get("evidence", [])),
            "evidence_kinds": sorted({item.get("kind") for item in results[metric_id].get("evidence", [])}),
            "metadata": results[metric_id].get("metadata", {}),
        }
        for metric_id in ids
        if metric_id in results
    }


def _file_sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_observation_artifact(
    report: Mapping[str, Any], output: Path, case_id: str
) -> dict[str, Any]:
    """Expose the immutable full observation artifact beside the HTML report."""

    reference = report.get("observation_artifact")
    if not isinstance(reference, Mapping):
        return {}
    exported = {
        key: reference.get(key)
        for key in ("sha256", "size_bytes", "media_type", "original_name")
        if reference.get(key) is not None
    }
    source_value = reference.get("uri")
    if not isinstance(source_value, str) or not source_value:
        return exported
    source = Path(source_value)
    if not source.is_file():
        exported["available"] = False
        exported["hash_valid"] = False
        return exported
    source_sha256, source_size = _file_sha256_and_size(source)
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    artifact_hashes = manifest.get("artifact_hashes")
    artifact_hashes = artifact_hashes if isinstance(artifact_hashes, Mapping) else {}
    manifest_sha256 = artifact_hashes.get("atomic_observations")
    reference_sha256 = reference.get("sha256")
    reference_size = reference.get("size_bytes")
    hash_valid = (
        isinstance(reference_sha256, str)
        and isinstance(manifest_sha256, str)
        and source_sha256 == reference_sha256 == manifest_sha256
        and (
            reference_size is None
            or (
                isinstance(reference_size, int)
                and not isinstance(reference_size, bool)
                and source_size == reference_size
            )
        )
    )
    exported.update(
        {
            "source_sha256": source_sha256,
            "manifest_sha256": manifest_sha256,
            "source_size_bytes": source_size,
            "source_hash_valid": hash_valid,
        }
    )
    if not hash_valid:
        exported["available"] = False
        exported["hash_valid"] = False
        return exported
    target = output / "artifacts" / f"{case_id}.observations.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    target_sha256, target_size = _file_sha256_and_size(target)
    target_hash_valid = (
        hash_valid
        and target_sha256 == reference_sha256 == manifest_sha256
        and target_size == source_size
    )
    exported.update(
        {
            "target_sha256": target_sha256,
            "target_size_bytes": target_size,
            "hash_valid": target_hash_valid,
        }
    )
    if not target_hash_valid:
        exported["available"] = False
        return exported
    exported.update(
        {
            "available": True,
            "href": target.relative_to(output).as_posix(),
        }
    )
    return exported


def _observation_findings(
    report: Mapping[str, Any], *, display_limit: int = 180
) -> dict[str, Any]:
    """Return a bounded reviewer view while preserving the full artifact separately."""

    findings: list[dict[str, Any]] = []
    for result in report.get("results", ()):
        if not isinstance(result, Mapping):
            continue
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if metadata.get("audit_type") != "model":
            continue
        for evidence in result.get("evidence", ()):
            if not isinstance(evidence, Mapping):
                continue
            findings.append(
                {
                    "observation_id": evidence.get("evidence_id"),
                    "metric_id": result.get("metric_id"),
                    "scope": "MODEL_AUDIT",
                    "unit_key": None,
                    "metric_status": result.get("metric_status"),
                    "severity": result.get("severity") or metadata.get("defect_severity") or "INFO",
                    "score": result.get("normalized_score"),
                    "confidence": result.get("confidence"),
                    "kind": evidence.get("kind"),
                    "message": evidence.get("message"),
                    "page_number": evidence.get("page_number"),
                    "object_id": evidence.get("object_id"),
                    "bbox": evidence.get("bbox"),
                }
            )
    reference = report.get("observation_artifact")
    source_value = reference.get("uri") if isinstance(reference, Mapping) else None
    payload: list[Any] = []
    if isinstance(source_value, str) and Path(source_value).is_file():
        candidate = json.loads(Path(source_value).read_text(encoding="utf-8"))
        if isinstance(candidate, list):
            payload = candidate

    severity_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}
    for observation in payload:
        if not isinstance(observation, Mapping):
            continue
        severity = str(observation.get("severity") or "INFO")
        status = str(observation.get("metric_status") or "")
        local_score = observation.get("local_score")
        actionable = (
            severity in {"CRITICAL", "MAJOR", "MINOR"}
            or status in {"ERROR", "FAIL"}
            or (
                isinstance(local_score, (int, float))
                and not isinstance(local_score, bool)
                and float(local_score) < 0.80
            )
        )
        if not actionable:
            continue
        evidence_items = observation.get("evidence")
        if not isinstance(evidence_items, list) or not evidence_items:
            evidence_items = [{}]
        for evidence in evidence_items:
            if not isinstance(evidence, Mapping):
                continue
            findings.append(
                {
                    "observation_id": observation.get("observation_id"),
                    "metric_id": observation.get("metric_id"),
                    "scope": observation.get("scope"),
                    "unit_key": observation.get("unit_key"),
                    "metric_status": status,
                    "severity": severity,
                    "score": local_score,
                    "confidence": observation.get("confidence"),
                    "kind": evidence.get("kind"),
                    "message": evidence.get("message"),
                    "page_number": evidence.get("page_number"),
                    "object_id": evidence.get("object_id"),
                    "bbox": evidence.get("bbox"),
                }
            )
    findings.sort(
        key=lambda item: (
            severity_order.get(str(item["severity"]), 4),
            2.0 if item["score"] is None else float(item["score"]),
            int(item["page_number"] or 10**9),
            str(item["metric_id"] or ""),
        )
    )
    shown = findings[:display_limit]
    return {
        "total_actionable": len(findings),
        "displayed": len(shown),
        "truncated": len(findings) > len(shown),
        "items": shown,
    }


def audit_case_payload(
    report: Mapping[str, Any], output: Path, case_id: str
) -> dict[str, Any]:
    """Build the backwards-compatible reviewer-facing v8 audit extension."""

    reducers: dict[str, Any] = {}
    gates: list[dict[str, Any]] = []
    for result in report.get("results", ()):
        if not isinstance(result, Mapping):
            continue
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if result.get("oracle_id") != "v8.quality_reducers" and not metadata.get(
            "reducer_id"
        ):
            continue
        metric_id = str(result.get("metric_id") or "")
        lineage = metadata.get("lineage")
        lineage = lineage if isinstance(lineage, Mapping) else {}
        reducers[metric_id] = {
            "metric_status": result.get("metric_status"),
            "score_role": result.get("score_role"),
            "score": result.get("normalized_score"),
            "confidence": result.get("confidence"),
            "severity": result.get("severity"),
            "reducer_id": metadata.get("reducer_id"),
            "reducer_version": metadata.get("reducer_version"),
            "reducer_kind": metadata.get("reducer_kind"),
            "observability": metadata.get("observability"),
            "coverage": metadata.get("coverage"),
            "reason_code": metadata.get("reason_code"),
            "components": metadata.get("components", {}),
            "fusion_mode": metadata.get("fusion_mode"),
            "rule_score": metadata.get("rule_score"),
            "model_score": metadata.get("model_score"),
            "critical_cap_applied": metadata.get("critical_cap_applied"),
            "critical_observation_count": len(
                metadata.get("critical_observation_ids", ())
            ),
            "uncapped_score": metadata.get("uncapped_score"),
            "lineage": {
                "input_metric_ids": list(lineage.get("input_metric_ids", ())),
                "observation_count": len(lineage.get("observation_ids", ())),
                "applicable_observation_count": len(
                    lineage.get("applicable_observation_ids", ())
                ),
                "unavailable_observation_count": len(
                    lineage.get("unavailable_observation_ids", ())
                ),
                "error_observation_count": len(
                    lineage.get("error_observation_ids", ())
                ),
            },
        }
        if str(result.get("score_role") or "").endswith("MULTIPLIER") or metric_id.endswith(
            "integrity"
        ):
            gates.append(
                {
                    "metric_id": metric_id,
                    "verdict": result.get("metric_status"),
                    "reason_code": metadata.get("reason_code"),
                    "severity": result.get("severity"),
                    "critical_observation_count": len(
                        metadata.get("critical_observation_ids", ())
                    ),
                    "major_prevalence": metadata.get("major_prevalence"),
                }
            )
    return {
        "training_eligibility": report.get("training_eligibility", {}),
        "score_breakdown": report.get("score_breakdown", {}),
        "observation_summary": report.get("observation_summary", {}),
        "observation_artifact": _copy_observation_artifact(report, output, case_id),
        "reducers": reducers,
        "gate_verdicts": gates,
        "findings": _observation_findings(report),
        "errors": list(report.get("errors", ())),
        "degradation_reasons": list(report.get("degradation_reasons", ())),
    }


def model_routing_events(path: Path, run_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("run_id") != run_id:
            continue
        if payload.get("event_type") != "MODEL_AUDIT_ROUTING":
            continue
        route = payload.get("payload", {})
        events.append(
            {
                "stage": route.get("stage"),
                "route": route.get("route"),
                "advanced_call_status": route.get("advanced_call_status"),
                "final_recommendation": route.get("final_recommendation"),
                "escalation_reasons": list(route.get("escalation_reasons", ())),
                "human_review_reasons": list(route.get("human_review_reasons", ())),
            }
        )
    return events


def atomic_model_routing_events(
    results: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize v8 per-criterion provider attempts for JSON and HTML audit."""

    events: list[dict[str, Any]] = []
    for metric_id in (
        *STRUCTURED_VLM_DIMENSION_IDS,
        *V8_ADDITIONAL_MODEL_METRIC_IDS,
        *V8_RASTER_TEXT_MODEL_METRIC_IDS,
    ):
        result = results.get(metric_id)
        if not isinstance(result, Mapping):
            continue
        metadata = result.get("metadata", {})
        if not isinstance(metadata, Mapping):
            continue
        attempts = metadata.get("routing_attempts")
        if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
            continue
        route_parts: list[str] = []
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            model = attempt.get("model")
            actual_model = (
                str(model.get("model_id") or "").strip()
                if isinstance(model, Mapping)
                else ""
            )
            configured_model = str(attempt.get("configured_model") or "").strip()
            label = actual_model or configured_model or str(attempt.get("tier") or "model")
            status = str(attempt.get("metric_status") or "UNKNOWN")
            route_parts.append(f"{label}:{status}")
        if not route_parts:
            continue
        reason = str(metadata.get("escalation_reason") or "").strip()
        route = " -> ".join(route_parts)
        if reason:
            route += f" ({reason})"
        events.append(
            {
                "stage": str(metadata.get("criterion_id") or metric_id),
                "route": route,
                "attempts": [dict(item) for item in attempts if isinstance(item, Mapping)],
            }
        )
    return events


def v8_model_case_analysis(
    report: Mapping[str, Any], *, expected_case_id: str
) -> dict[str, Any]:
    """Validate that one v8 case is safe to include in rank statistics."""

    required_metric_ids = (*STRUCTURED_VLM_DIMENSION_IDS, *V8_ADDITIONAL_MODEL_METRIC_IDS)
    results = [item for item in report.get("results", ()) if isinstance(item, Mapping)]
    failures: list[str] = []
    status_by_metric: dict[str, str] = {}
    route_by_metric: dict[str, str] = {}
    for metric_id in required_metric_ids:
        matching = [item for item in results if item.get("metric_id") == metric_id]
        if len(matching) != 1:
            failures.append(f"metric_count:{metric_id}:{len(matching)}")
            continue
        result = matching[0]
        status = str(result.get("metric_status") or "UNKNOWN")
        status_by_metric[metric_id] = status
        if status != "SCORED" or result.get("normalized_score") is None:
            failures.append(f"metric_not_scored:{metric_id}:{status}")
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        attempts = [
            attempt
            for attempt in metadata.get("routing_attempts", ())
            if isinstance(attempt, Mapping)
        ]
        selected = [attempt for attempt in attempts if attempt.get("selected") is True]
        if not attempts or len(selected) != 1:
            failures.append(f"route_contract:{metric_id}")
        route_by_metric[metric_id] = "->".join(
            f"{attempt.get('tier', 'MODEL')}:{attempt.get('metric_status', 'UNKNOWN')}"
            for attempt in attempts
        )
        usage = metadata.get("routing_usage")
        if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
            failures.append(f"usage_incomplete:{metric_id}")
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    if report.get("case_id") != expected_case_id or manifest.get("case_id") != expected_case_id:
        failures.append("case_identity")
    if str(report.get("coverage") or "") != "FULL":
        failures.append(f"coverage:{report.get('coverage')}")
    return {
        "eligible": not failures,
        "required_metric_ids": list(required_metric_ids),
        "metric_statuses": status_by_metric,
        "routes": route_by_metric,
        "failures": failures,
    }


def v8_replay_analysis(
    cases: Sequence[Mapping[str, Any]],
    *,
    expected_cases: Sequence[tuple[str, str, int]],
) -> dict[str, Any]:
    expected = set(expected_cases)
    observed = {
        (str(item["case_id"]), str(item["product"]), int(item["human_rank"]))
        for item in cases
    }
    unique = (
        len(cases) == len({str(item["case_id"]) for item in cases})
        == len({str(item["product"]) for item in cases})
        == len({int(item["human_rank"]) for item in cases})
    )
    identity_match = len(cases) == len(expected_cases) and observed == expected and unique
    case_failures = {
        str(item["case_id"]): list(item.get("v8_model_analysis", {}).get("failures", ()))
        for item in cases
        if not item.get("v8_model_analysis", {}).get("eligible", False)
    }
    eligible = identity_match and not case_failures
    return {
        "eligible": eligible,
        "eligibility_rule": (
            "exact pinned seven-case identity, FULL coverage, exactly seven v8 model criteria "
            "SCORED, one selected routing attempt per criterion, and complete usage accounting"
        ),
        "expected_case_count": len(expected_cases),
        "observed_case_count": len(cases),
        "identity_match": identity_match,
        "case_failures": case_failures,
        "valid_case_count": sum(
            bool(item.get("v8_model_analysis", {}).get("eligible", False)) for item in cases
        ),
    }


def replay_rank_statistics_eligible(
    structured_visual_replay: Mapping[str, Any] | None,
    v8_replay: Mapping[str, Any] | None,
) -> bool:
    """Centralize the fail-closed rank-statistics gate for every replay generation."""

    if structured_visual_replay is not None:
        return bool(structured_visual_replay.get("eligible"))
    if v8_replay is not None:
        return bool(v8_replay.get("eligible"))
    return True


def verify_run_audit_chain(path: Path, run_id: str) -> tuple[bool, str | None]:
    if not path.is_file():
        return False, "missing_audit_log"
    audit_log = JsonlAuditLog(path)
    valid, broken_event = audit_log.verify()
    if not valid:
        return False, broken_event
    run_events = [event for event in audit_log.read() if event.get("run_id") == run_id]
    if not run_events:
        return False, "missing_run_events"
    if not any(event.get("event_type") in {"RUN_COMPLETED", "RUN_FAILED"} for event in run_events):
        return False, "missing_terminal_event"
    return True, None


def evaluate(
    dataset_root: Path,
    output: Path,
    *,
    qwen_v3: bool = False,
    flash_only: bool = False,
    rerun_products: frozenset[str] | None = None,
    reuse_reports_from: Path | None = None,
    reference_report_dir: Path | None = None,
    profile_path: Path | None = None,
    include_products: frozenset[str] | None = None,
) -> dict[str, Any]:
    manifest = json.loads((dataset_root / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    if profile_path is None:
        profile_name = "finished_deck_v3.json" if qwen_v3 else "finished_deck_v2.json"
        profile_path = ROOT / "configs" / "profiles" / profile_name
    profile = load_profile(profile_path)
    structured_dimensions_enabled = _structured_dimension_contract_spec(profile) is not None
    v8_model_enabled = str(profile.version).split(".", 1)[0] == "8"
    if structured_dimensions_enabled and not qwen_v3:
        raise ValueError(
            "the structured visual model-audit profile requires --qwen-v3 so the "
            "configured model provider and pinned rendered slides are used"
        )
    reference_dir = reference_report_dir or (dataset_root / "report")
    if flash_only:
        if not qwen_v3:
            raise ValueError("flash_only requires qwen_v3")
        profile = replace(
            profile,
            metadata={
                **dict(profile.metadata),
                "model_audit_routing": "FLASH_ONLY_BENCHMARK",
            },
        )
    available_products = {str(artifact["product_label"]) for artifact in manifest["artifacts"]}
    if include_products is not None:
        unknown_includes = include_products - available_products
        if unknown_includes:
            raise ValueError("unknown included products: " + ", ".join(sorted(unknown_includes)))
        dataset_artifacts = [
            artifact
            for artifact in manifest["artifacts"]
            if str(artifact["product_label"]) in include_products
        ]
    else:
        dataset_artifacts = list(manifest["artifacts"])
    if rerun_products is not None:
        unknown = rerun_products - available_products
        if unknown:
            raise ValueError("unknown products: " + ", ".join(sorted(unknown)))
        if not qwen_v3:
            raise ValueError("--rerun-products requires --qwen-v3")
    cases: list[dict[str, Any]] = []
    for artifact in dataset_artifacts:
        pptx = dataset_root / artifact["pptx"]["local_path"]
        case_id = Path(artifact["pptx"]["local_path"]).stem
        report_path = output / f"{case_id}.report.json"
        seed_report_path = (
            None if reuse_reports_from is None else reuse_reports_from / f"{case_id}.report.json"
        )
        reusable_report_path = (
            report_path
            if report_path.is_file()
            else (seed_report_path if seed_report_path is not None and seed_report_path.is_file() else None)
        )
        if rerun_products is not None:
            should_evaluate = str(artifact["product_label"]) in rerun_products
        elif reuse_reports_from is not None:
            should_evaluate = reusable_report_path is None
        else:
            should_evaluate = True
        audit_path = output / "runtime" / case_id / "audit" / "events.jsonl"
        if not should_evaluate:
            if reusable_report_path is None:
                raise FileNotFoundError(f"cannot reuse missing report for {artifact['product_label']}")
            report = json.loads(reusable_report_path.read_text(encoding="utf-8"))
            if reusable_report_path != report_path:
                if reuse_reports_from is None:
                    raise RuntimeError("external reusable report has no source directory")
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                audit_path = reuse_reports_from / "runtime" / case_id / "audit" / "events.jsonl"
        elif qwen_v3:
            runtime = build_runtime_from_environment(
                output / "runtime" / case_id,
                environment=(
                    {**dict(os.environ), "PPT_EVAL_ZHIPU_AUDIT_ENABLED": "false"}
                    if flash_only
                    else None
                ),
                workspace_root=ROOT,
            )
            model_artifacts: Mapping[str, Any] | None = {
                "slide_images": tuple(
                    {
                        "page_number": page_number,
                        "path": str((dataset_root / record["local_path"]).resolve()),
                        "media_type": "image/png",
                        "sha256": record["sha256"],
                    }
                    for page_number, record in enumerate(
                        artifact["rendered_slides"]["files"],
                        start=1,
                    )
                )
            }
            audit_path = runtime.paths.audit
        else:
            runtime = LocalEvaluationRuntime(output / "runtime" / case_id)
            model_artifacts = None
            audit_path = runtime.paths.audit
        if should_evaluate:
            report = runtime.evaluate(
                EvalCase(
                    case_id=case_id,
                    scene=SceneType.READY_MADE,
                    pptx_path=str(pptx.resolve()),
                    metadata={
                        "dataset_id": manifest["dataset_id"],
                        "human_rank": artifact["human_rank"],
                    },
                ),
                profile,
                artifacts=model_artifacts,
            )
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        results = {item["metric_id"]: item for item in report["results"]}
        reference_v2_path = reference_dir / f"{case_id}.report.json"
        reference_v2_score = None
        if reference_v2_path.is_file():
            reference_v2 = json.loads(reference_v2_path.read_text(encoding="utf-8"))
            if reference_v2.get("base_score") is not None:
                reference_v2_score = float(reference_v2["base_score"])
        model_statuses = {
            metric_id: results[metric_id]["metric_status"]
            for metric_id in (
                "llm_content_quality_audit",
                "vlm_visual_quality_audit",
                "structured_vlm_visual_audit",
                *STRUCTURED_VLM_DIMENSION_IDS,
                *V8_ADDITIONAL_MODEL_METRIC_IDS,
                *V8_RASTER_TEXT_MODEL_METRIC_IDS,
                "llm_scenario_compliance_audit",
                "advanced_llm_content_review",
                "advanced_vlm_visual_review",
                "advanced_llm_scenario_review",
            )
            if metric_id in results
        }
        structured_visual_analysis = (
            structured_visual_case_analysis(
                report,
                profile,
                expected_case_id=case_id,
            )
            if structured_dimensions_enabled
            else None
        )
        v8_analysis = (
            v8_model_case_analysis(report, expected_case_id=case_id)
            if v8_model_enabled
            else None
        )
        result_status_counts: dict[str, int] = {}
        for result in report.get("results", ()):
            status = str(result.get("metric_status") or "UNKNOWN")
            result_status_counts[status] = result_status_counts.get(status, 0) + 1
        model_token_usage = {
            metric_id: (
                results[metric_id].get("metadata", {}).get("routing_usage")
                or results[metric_id].get("metadata", {}).get("usage", {})
            )
            for metric_id in results
            if results[metric_id].get("metadata", {}).get("audit_type") == "model"
        }
        usage_records = [value for value in model_token_usage.values() if isinstance(value, Mapping)]
        audit_chain_valid, broken_audit_event = verify_run_audit_chain(
            audit_path, str(report["run_id"])
        )
        cases.append(
            {
                "case_id": case_id,
                "product": artifact["product_label"],
                "human_rank": int(artifact["human_rank"]),
                "pptx": artifact["pptx"],
                "renders": artifact["rendered_slides"],
                "run_id": report["run_id"],
                "profile_id": report["profile_id"],
                "profile_version": report["profile_version"],
                "decision": report["decision"],
                "coverage": report["coverage"],
                "base_score": float(report["base_score"]),
                "reference_v2_score": reference_v2_score,
                "score_delta_vs_v2": (
                    None
                    if reference_v2_score is None
                    else round(float(report["base_score"]) - reference_v2_score, 6)
                ),
                "full_score": (None if report["full_score"] is None else float(report["full_score"])),
                "deterministic_visual_proxy": deterministic_visual_proxy(results),
                "visual_proxy": deterministic_visual_proxy(results),
                "structured_visual_analysis": structured_visual_analysis,
                "v8_model_analysis": v8_analysis,
                "metrics": selected_metrics(results),
                "audit": audit_case_payload(report, output, case_id),
                "report_href": report_path.relative_to(output).as_posix(),
                "result_status_counts": result_status_counts,
                "audit_chain": {
                    "valid": audit_chain_valid,
                    "broken_event": broken_audit_event,
                },
                "model_audit_statuses": model_statuses,
                "model_audit_routing": [
                    *model_routing_events(audit_path, report["run_id"]),
                    *atomic_model_routing_events(results),
                ],
                "review_reasons": list(report.get("review_reasons", ())),
                "model_versions": dict(report["manifest"].get("model_versions", {})),
                "prompt_versions": dict(report["manifest"].get("prompt_versions", {})),
                "model_token_usage": model_token_usage,
                "model_usage_summary": {
                    "total_tokens": sum(
                        int(value.get("total_tokens", 0) or 0) for value in usage_records
                    ),
                    "usage_complete": bool(usage_records)
                    and all(value.get("usage_complete", True) is True for value in usage_records),
                    "cost_known": bool(usage_records)
                    and all(value.get("cost_known") is True for value in usage_records),
                    "reported_cost": sum(
                        float(value.get("reported_cost", value.get("cost", 0.0)) or 0.0)
                        for value in usage_records
                    ),
                },
            }
        )

    expected_cases = tuple(
        (
            Path(artifact["pptx"]["local_path"]).stem,
            str(artifact["product_label"]),
            int(artifact["human_rank"]),
        )
        for artifact in manifest["artifacts"]
    )
    structured_visual_replay = structured_visual_replay_analysis(
        cases,
        profile,
        expected_cases=expected_cases,
    )
    v8_replay = (
        v8_replay_analysis(cases, expected_cases=expected_cases)
        if v8_model_enabled
        else None
    )
    rank_statistics_eligible = replay_rank_statistics_eligible(
        structured_visual_replay,
        v8_replay,
    )
    human_order = rank_order(cases, lambda item: -float(item["human_rank"]))
    baseline_order = rank_order(cases, lambda item: float(item["base_score"]))
    visual_order = rank_order(
        cases,
        lambda item: float(item["deterministic_visual_proxy"]),
    )
    for item in cases:
        case_id = item["case_id"]
        item["rank_comparison_eligible"] = rank_statistics_eligible
        item["selected_human_order"] = human_order[case_id] if rank_statistics_eligible else None
        item["baseline_order"] = baseline_order[case_id] if rank_statistics_eligible else None
        item["visual_proxy_order"] = visual_order[case_id] if rank_statistics_eligible else None
        item["baseline_rank_delta"] = (
            baseline_order[case_id] - human_order[case_id] if rank_statistics_eligible else None
        )
        item["visual_rank_delta"] = (
            visual_order[case_id] - human_order[case_id] if rank_statistics_eligible else None
        )

    human_utility = [-float(item["human_rank"]) for item in cases]
    base_scores = [float(item["base_score"]) for item in cases]
    visual_scores = [float(item["deterministic_visual_proxy"]) for item in cases]
    comparable_pairs = len(cases) * (len(cases) - 1) // 2 if rank_statistics_eligible else 0
    comparison: dict[str, Any] = {
        "schema_version": "1.3",
        "dataset_id": manifest["dataset_id"],
        "dataset_revision": manifest["source"]["revision"],
        "topic": manifest["selection"]["topic"],
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "model_audit_mode": (
            "QWEN_V3_FLASH_ONLY"
            if flash_only
            else (f"QWEN_PROFILE:{profile.profile_id}" if qwen_v3 else "V2_SHADOW_OFFLINE")
        ),
        "comparison_scope": (f"{len(cases)} same-topic decks with exact per-deck human ranks"),
        "statistics": {
            "spearman_base_vs_human": (
                spearman(base_scores, human_utility) if rank_statistics_eligible else None
            ),
            "spearman_visual_proxy_vs_human": (
                spearman(visual_scores, human_utility) if rank_statistics_eligible else None
            ),
            "spearman_deterministic_visual_proxy_vs_human": (
                spearman(visual_scores, human_utility) if rank_statistics_eligible else None
            ),
            "pairwise_base_accuracy": (
                pairwise_accuracy([int(item["human_rank"]) for item in cases], base_scores)
                if rank_statistics_eligible
                else None
            ),
            "pairwise_visual_proxy_accuracy": (
                pairwise_accuracy([int(item["human_rank"]) for item in cases], visual_scores)
                if rank_statistics_eligible
                else None
            ),
            "pairwise_deterministic_visual_proxy_accuracy": (
                pairwise_accuracy([int(item["human_rank"]) for item in cases], visual_scores)
                if rank_statistics_eligible
                else None
            ),
            "comparable_pairs": comparable_pairs,
        },
        "structured_visual_replay": structured_visual_replay,
        "v8_replay": v8_replay,
        "reference_v2_report_dir": str(reference_dir),
        "reference_v2_statistics": _reference_v2_statistics(reference_dir),
        "cases": sorted(cases, key=lambda item: item["human_rank"]),
        "limitations": [
            (
                f"{len(cases)} decks from one topic are diagnostic and cannot estimate "
                "cross-topic generalization."
            ),
            "Human ranks are ordinal within one topic; rank gaps are not interval-scale score gaps.",
            "Slides-Align provides overall visible-preference ranks, not six dimension-level gold labels; "
            "it cannot validate or fit the internal VLM dimension budget.",
            "The deterministic visual proxy does not measure color harmony, pixel-level contrast, "
            "semantic image relevance, or rendered cross-slide aesthetics.",
            "The observed rank agreement may be partly spurious: object-tree penalties can hit intentional "
            "decorative overlap while visible spacing or unresolved placeholders remain undetected.",
            (
                "Qwen scores are single-run judgments; repeatability and calibration error have not "
                "yet been estimated. VLM uploads at most 12 deterministically sampled pages per deck."
                if qwen_v3
                else "Model-assisted audits are v2 Shadow NA because no Provider is configured."
            ),
        ],
    }
    (output / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "index.html").write_text(build_html(comparison, output, dataset_root), encoding="utf-8")
    return comparison


def build_html(comparison: Mapping[str, Any], output: Path, dataset_root: Path) -> str:
    statistics = comparison["statistics"]
    reference_v2 = comparison.get("reference_v2_statistics") or {}
    reference_note = (
        ""
        if (
            reference_v2.get("spearman_base_vs_human") is None
            or reference_v2.get("comparable_pairs") != statistics.get("comparable_pairs")
        )
        else (
            '<p class="note"><b>v2 历史参照：</b>同三例总分 Spearman '
            f"{float(reference_v2['spearman_base_vs_human']):.2f}；当前 v3 为 "
            f"{float(statistics['spearman_base_vs_human']):.2f}。两者都只是单一 topic 的描述值。</p>"
        )
    )
    structured_visual_section = _structured_visual_replay_html(comparison.get("structured_visual_replay"))
    v8_replay_section = _v8_replay_html(comparison.get("v8_replay"))
    case_sections = "".join(case_html(item, output, dataset_root) for item in comparison["cases"])
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in comparison["limitations"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>真实 PPT 人评对照</title><style>
:root{{--ink:#16202a;--muted:#647383;--line:#dce3e9;--paper:#f7f5f0;--accent:#126b5d;--warm:#c85b37}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "Segoe UI",sans-serif}}
main{{max-width:1240px;margin:auto;padding:44px 28px 80px}} h1{{font:700 42px/1.12 Georgia,serif;margin:0 0 12px}}
h2{{font:700 28px/1.2 Georgia,serif}} h3{{margin:0 0 6px}} .lead{{font-size:18px;color:var(--muted);max-width:900px}}
.summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}} .stat,.case{{background:white;border:1px solid var(--line);border-radius:12px}}
.stat{{padding:18px}} .stat b{{display:block;font-size:26px;color:var(--accent)}} .case{{padding:24px;margin:28px 0}}
.case-head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}} .badge{{display:inline-block;padding:4px 9px;border-radius:999px;background:#e6f1ee;color:var(--accent);font-weight:700;margin-right:5px}}
.warn{{background:#fae9e2;color:#8b341e}} .gallery{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}}
.slide-thumb{{display:block;width:100%;padding:0;border:1px solid #ccd4da;border-radius:8px;background:#111;overflow:hidden;cursor:zoom-in;text-align:left;color:inherit}}
.slide-thumb img{{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#111}}
.slide-thumb span{{display:block;padding:6px 9px;background:#fff;font-size:12px;color:var(--muted)}}
table{{border-collapse:collapse;width:100%;margin:16px 0}} th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left}} th{{color:var(--muted);font-weight:600}}
.two{{display:grid;grid-template-columns:1.1fr .9fr;gap:22px}} code{{font-family:Consolas,monospace;font-size:12px}}
details{{margin-top:12px}} details>summary{{cursor:pointer;font-weight:650}} .note{{border-left:4px solid var(--warm);padding:10px 14px;background:#fff3ed}}
.audit-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}} .audit-card{{padding:12px;border:1px solid var(--line);border-radius:9px;background:#fbfcfc}}
.audit-card b{{display:block;font-size:18px}} .status-TRAIN,.status-PASS{{color:#126b5d}} .status-REVIEW,.status-NA{{color:#9a5b18}} .status-REJECT,.status-FAIL,.status-ERROR{{color:#9b2f20}}
.evidence-list{{list-style:none;padding:0;margin:12px 0}} .evidence-list li{{padding:10px 0;border-bottom:1px solid var(--line)}} .evidence-meta{{color:var(--muted);font-size:12px}}
.page-link{{display:inline-block;padding:1px 7px;border-radius:999px;background:#e6f1ee;color:var(--accent);font-weight:650;text-decoration:none}}
.route{{padding:9px 0;border-bottom:1px solid var(--line)}} .lineage{{color:var(--muted);font-size:12px}}
.modal{{position:fixed;inset:0;z-index:50;background:rgba(5,10,15,.93);display:none;align-items:center;justify-content:center;padding:60px 72px}}
.modal.open{{display:flex}} .modal img{{max-width:100%;max-height:calc(100vh - 120px);object-fit:contain;box-shadow:0 10px 50px #000}}
.modal button{{position:absolute;border:1px solid #71808d;background:#18232d;color:white;border-radius:8px;padding:10px 14px;cursor:pointer}}
.modal-close{{right:20px;top:18px}} .modal-prev{{left:18px;top:50%}} .modal-next{{right:18px;top:50%}} .modal-caption{{position:absolute;left:20px;bottom:15px;color:white}}
@media(max-width:800px){{.summary,.two,.gallery{{grid-template-columns:1fr}}.case-head{{display:block}}}}
</style></head><body><main>
<h1>真实 PPT：人评排名 vs 当前 Harness</h1>
<p class="lead">Slides-Align 固定 revision，同一 market_analysis 主题。{len(comparison["cases"])} 份 PPTX 通过当前
<b>{html.escape(str(comparison["profile_id"]))}</b> 评测；人评 rank 越小越好，Harness 分越高越好。</p>
<div class="summary"><div class="stat"><b>{_display_stat(statistics["spearman_base_vs_human"])}</b>总分 Spearman</div>
<div class="stat"><b>{_display_stat(statistics["spearman_deterministic_visual_proxy_vs_human"])}</b>确定性视觉代理 Spearman</div>
<div class="stat"><b>{_display_stat(statistics["pairwise_base_accuracy"], percent=True)}</b>总分两两一致</div>
<div class="stat"><b>{_display_stat(statistics["pairwise_deterministic_visual_proxy_accuracy"], percent=True)}</b>确定性视觉代理两两一致</div></div>
{reference_note}
<p class="note"><b>范围限制：</b>这只是 1 个 topic、{len(comparison["cases"])} 份可配对 deck 的诊断切片，不是跨 topic 相关性结论。</p>
{v8_replay_section}
{structured_visual_section}
{case_sections}
<section><h2>限制与下一步</h2><ul>{limitations}</ul></section>
</main><div class="modal" id="slide-modal" role="dialog" aria-modal="true" aria-label="幻灯片查看器">
<button class="modal-close" type="button">关闭 ×</button><button class="modal-prev" type="button">← 上一页</button>
<img alt="放大的幻灯片"><button class="modal-next" type="button">下一页 →</button><div class="modal-caption"></div></div>
<script>
(()=>{{
  const modal=document.getElementById('slide-modal'), image=modal.querySelector('img'), caption=modal.querySelector('.modal-caption');
  let slides=[], index=0;
  const render=()=>{{const item=slides[index]; if(!item)return; image.src=item.dataset.slideSrc; image.alt=item.dataset.slideLabel; caption.textContent=`${{item.dataset.slideLabel}} · ${{index+1}}/${{slides.length}}`;}};
  const open=(button)=>{{const caseId=button.dataset.caseId; slides=[...document.querySelectorAll(`.slide-thumb[data-case-id="${{CSS.escape(caseId)}}"]`)]; index=Math.max(0,slides.indexOf(button)); render(); modal.classList.add('open'); document.body.style.overflow='hidden';}};
  const close=()=>{{modal.classList.remove('open');document.body.style.overflow='';image.removeAttribute('src');}};
  document.addEventListener('click',(event)=>{{const button=event.target.closest('.slide-thumb');if(button)open(button);const jump=event.target.closest('[data-open-slide]');if(jump){{event.preventDefault();const target=document.getElementById(jump.dataset.openSlide);if(target){{target.scrollIntoView({{behavior:'smooth',block:'center'}});open(target);}}}}}});
  modal.querySelector('.modal-close').onclick=close; modal.querySelector('.modal-prev').onclick=()=>{{index=(index-1+slides.length)%slides.length;render();}}; modal.querySelector('.modal-next').onclick=()=>{{index=(index+1)%slides.length;render();}};
  modal.addEventListener('click',(event)=>{{if(event.target===modal)close();}}); document.addEventListener('keydown',(event)=>{{if(!modal.classList.contains('open'))return;if(event.key==='Escape')close();if(event.key==='ArrowLeft')modal.querySelector('.modal-prev').click();if(event.key==='ArrowRight')modal.querySelector('.modal-next').click();}});
}})();
</script></body></html>"""


def _v8_replay_html(replay: object) -> str:
    if not isinstance(replay, Mapping):
        return ""
    if replay.get("eligible"):
        return (
            '<section><h2>v8 重放资格</h2><p class="note"><b>排名统计已启用：</b>'
            f"{int(replay.get('valid_case_count', 0))}/{int(replay.get('expected_case_count', 0))} "
            "份均为 FULL，七个原子模型 criterion 均为合法 SCORED 响应，且路由与 usage 合同完整。</p></section>"
        )
    failures = replay.get("case_failures")
    failure_text = (
        "；".join(
            f"{case_id}: {', '.join(str(value) for value in values)}"
            for case_id, values in failures.items()
        )
        if isinstance(failures, Mapping)
        else ""
    )
    return (
        '<section><h2>v8 重放资格</h2><p class="note"><b>排名统计已抑制：</b>'
        f"仅 {int(replay.get('valid_case_count', 0))}/{int(replay.get('expected_case_count', 0))} "
        f"份通过完整性合同。{html.escape(failure_text)}</p></section>"
    )


def _structured_visual_replay_html(replay: object) -> str:
    if not isinstance(replay, Mapping):
        return ""
    if not replay.get("eligible"):
        return (
            '<section><h2>六维 VLM 视觉构念敏感性</h2><p class="note">'
            "排名统计已抑制：必须先满足完整固定切片、逐份 FULL、每份恰好六个 "
            "SCORED 维度且 Profile 算术对齐。当前 "
            f"{int(replay.get('full_case_count', 0))}/"
            f"{int(replay.get('expected_case_count', 0))} FULL，六维完整 "
            f"{int(replay.get('complete_six_dimension_case_count', 0))}/"
            f"{int(replay.get('expected_case_count', 0))}。</p></section>"
        )
    rows = "".join(
        "<tr>"
        f"<td>{100.0 * float(share):.0f}%</td>"
        f"<td>{html.escape(str(values['analysis_role']))}</td>"
        f"<td>{_display_share_range(values['effective_vlm_share_range'])}</td>"
        f"<td>{_display_stat(values['spearman_visual_construct_vs_human'])}</td>"
        f"<td>{_display_stat(values['pairwise_visual_construct_accuracy'], percent=True)}</td>"
        f"<td>{html.escape(' > '.join(str(item) for item in values['order']))}</td>"
        "</tr>"
        for share, values in replay["statistics"].items()
    )
    primary_percent = 100.0 * float(replay["primary_vlm_visual_share"])
    return f"""<section><h2>六维 VLM 视觉构念敏感性</h2>
<p>主结论固定使用 visual 内 <b>{primary_percent:.0f}%</b> VLM；10%/15% 是降权敏感性，
25% 是扩权敏感性，均不得按本切片排名择优。统计只比较 visual construct，
不用 Slides-Align 校准 delivery/handoff。</p>
<table><thead><tr><th>VLM / visual</th><th>角色</th><th>逐例有效份额</th><th>Visual Spearman</th>
<th>Visual 两两一致</th><th>顺序</th></tr></thead><tbody>{rows}</tbody></table></section>"""


def _display_stat(value: object, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    numeric = float(value)
    return f"{numeric:.0%}" if percent else f"{numeric:.2f}"


def _display_share_range(value: object) -> str:
    if not isinstance(value, list) or len(value) != 2:
        return "N/A"
    return f"{float(value[0]):.0%}–{float(value[1]):.0%}"


def _dom_token(value: object) -> str:
    return "".join(character if character.isalnum() else "-" for character in str(value))


def _score_cell(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{100.0 * float(value):.1f}"


def _audit_details_html(item: Mapping[str, Any], slide_ids: Mapping[int, str]) -> str:
    audit = item.get("audit")
    if not isinstance(audit, Mapping) or not audit:
        return '<p class="note">该历史报告没有 v8 原子审计扩展；旧字段仍可正常查看。</p>'

    training = audit.get("training_eligibility")
    training = training if isinstance(training, Mapping) else {}
    training_cards = "".join(
        '<div class="audit-card"><span>{track}</span><b class="status-{status}">{status}</b>'
        '<small>{score}</small><div class="lineage">{reasons}</div></div>'.format(
            track=html.escape(str(decision.get("track") or "unknown")),
            status=html.escape(str(decision.get("status") or "N/A")),
            score=(
                "score —"
                if decision.get("score") is None
                else f"score {float(decision['score']):.1f}"
            ),
            reasons=html.escape(", ".join(str(code) for code in decision.get("reason_codes", ()))),
        )
        for decision in training.get("decisions", ())
        if isinstance(decision, Mapping)
    )
    gate_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(gate.get('metric_id') or ''))}</code></td>"
        f"<td class=\"status-{html.escape(str(gate.get('verdict') or 'N/A'))}\">{html.escape(str(gate.get('verdict') or 'N/A'))}</td>"
        f"<td>{html.escape(str(gate.get('reason_code') or '—'))}</td>"
        f"<td>{int(gate.get('critical_observation_count') or 0)}</td>"
        f"<td>{_display_stat(gate.get('major_prevalence'), percent=True)}</td></tr>"
        for gate in audit.get("gate_verdicts", ())
        if isinstance(gate, Mapping)
    )
    reducers = audit.get("reducers")
    reducers = reducers if isinstance(reducers, Mapping) else {}
    reducer_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(metric_id))}</code></td>"
        f"<td>{_score_cell(reducer.get('score'))}</td>"
        f"<td>{html.escape(str(reducer.get('metric_status') or ''))}</td>"
        f"<td>{_display_stat(reducer.get('observability'), percent=True)}</td>"
        f"<td>{_score_cell(reducer.get('rule_score'))} / {_score_cell(reducer.get('model_score'))}</td>"
        f"<td>{'CRITICAL CAP' if reducer.get('critical_cap_applied') else '—'}</td>"
        f"<td>{int(reducer.get('lineage', {}).get('applicable_observation_count', 0))}/"
        f"{int(reducer.get('lineage', {}).get('observation_count', 0))}</td>"
        f"<td class=\"lineage\">{html.escape(', '.join(str(value) for value in reducer.get('lineage', {}).get('input_metric_ids', ())) or str(reducer.get('fusion_mode') or '—'))}</td></tr>"
        for metric_id, reducer in reducers.items()
        if isinstance(reducer, Mapping) and reducer.get("score_role") != "DIAGNOSTIC"
    )
    summary = audit.get("observation_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    summary_cards = "".join(
        f'<div class="audit-card"><span>{html.escape(label)}</span><b>{html.escape(value)}</b></div>'
        for label, value in (
            ("原子观察", str(summary.get("count", 0))),
            ("作用域", ", ".join(f"{key}:{value}" for key, value in summary.get("by_scope", {}).items())),
            ("状态", ", ".join(f"{key}:{value}" for key, value in summary.get("by_status", {}).items())),
            ("指标数", str(len(summary.get("metric_ids", ())))),
        )
    )
    findings = audit.get("findings")
    findings = findings if isinstance(findings, Mapping) else {}
    finding_rows = "".join(
        _finding_html(finding, slide_ids)
        for finding in findings.get("items", ())
        if isinstance(finding, Mapping)
    )
    artifact = audit.get("observation_artifact")
    artifact = artifact if isinstance(artifact, Mapping) else {}
    artifact_link = (
        f'<a href="{html.escape(str(artifact["href"]))}">下载完整原子观察 JSON</a> · hash 已三方校验 · '
        if artifact.get("href")
        else (
            "完整原子观察 artifact hash 校验失败，不提供下载 · "
            if artifact.get("hash_valid") is False
            else "完整原子观察 artifact 当前不可下载 · "
        )
    )
    finding_note = (
        f"显示 {int(findings.get('displayed', 0))}/{int(findings.get('total_actionable', 0))} 条；"
        "其余请查看完整 artifact。"
        if findings.get("truncated")
        else f"共 {int(findings.get('total_actionable', 0))} 条。"
    )
    errors = [*audit.get("errors", ()), *audit.get("degradation_reasons", ())]
    error_note = (
        '<p class="note"><b>运行异常/降级：</b>'
        + html.escape(" | ".join(str(value) for value in errors))
        + "</p>"
        if errors
        else ""
    )
    return f"""<h3>训练准入</h3><div class="audit-grid">{training_cards or '<div class="audit-card">未提供训练准入</div>'}</div>
{error_note}<details open><summary>硬门判定</summary><table><thead><tr><th>Gate</th><th>判定</th><th>原因</th><th>Critical</th><th>Major 占比</th></tr></thead><tbody>{gate_rows or '<tr><td colspan="5">无 v8 硬门记录</td></tr>'}</tbody></table></details>
<details open><summary>Composite / Reducer 结果与 lineage</summary><table><thead><tr><th>构念</th><th>融合分</th><th>状态</th><th>可观察度</th><th>规则 / VLM</th><th>Cap</th><th>输入观察</th><th>输入 Metric / 融合</th></tr></thead><tbody>{reducer_rows or '<tr><td colspan="8">无 v8 Reducer 记录</td></tr>'}</tbody></table></details>
<details><summary>原子观察摘要</summary><div class="audit-grid">{summary_cards}</div><p>{artifact_link}<code>sha256:{html.escape(str(artifact.get('sha256') or 'N/A'))}</code></p></details>
<details><summary>人工审计证据（可跳转幻灯片）</summary><p>{html.escape(finding_note)}</p><ul class="evidence-list">{finding_rows or '<li>没有可展示的异常或模型证据。</li>'}</ul></details>"""


def _finding_html(finding: Mapping[str, Any], slide_ids: Mapping[int, str]) -> str:
    page_value = finding.get("page_number")
    page_number = int(page_value) if isinstance(page_value, (int, float)) else None
    page_html = ""
    if page_number is not None and page_number in slide_ids:
        page_html = (
            f'<a class="page-link" href="#{slide_ids[page_number]}" '
            f'data-open-slide="{slide_ids[page_number]}">第 {page_number} 页</a> '
        )
    elif page_number is not None:
        page_html = f"第 {page_number} 页 · "
    score = _score_cell(finding.get("score"))
    message = str(finding.get("message") or "未提供文字说明")
    return (
        "<li>"
        f"{page_html}<b>{html.escape(str(finding.get('severity') or 'INFO'))}</b> "
        f"<code>{html.escape(str(finding.get('metric_id') or 'unknown'))}</code> · {html.escape(message)}"
        f"<div class=\"evidence-meta\">score {score} · confidence {_display_stat(finding.get('confidence'), percent=True)} · "
        f"{html.escape(str(finding.get('kind') or 'evidence'))} · {html.escape(str(finding.get('object_id') or finding.get('unit_key') or 'deck'))}</div></li>"
    )


def _routing_html(item: Mapping[str, Any]) -> str:
    rows = "".join(
        '<div class="route"><b>{stage}</b><br><code>{route}</code>{attempts}</div>'.format(
            stage=html.escape(str(event.get("stage") or "model")),
            route=html.escape(str(event.get("route") or "not enabled")),
            attempts=(
                '<div class="lineage">'
                + " · ".join(
                    html.escape(
                        f"{attempt.get('tier', 'model')} "
                        f"{attempt.get('metric_status', 'UNKNOWN')} "
                        f"tokens={_routing_attempt_tokens(attempt)} "
                        f"cost_known={attempt.get('cost_known', False)}"
                    )
                    for attempt in event.get("attempts", ())
                    if isinstance(attempt, Mapping)
                )
                + "</div>"
                if event.get("attempts")
                else ""
            ),
        )
        for event in item.get("model_audit_routing", ())
        if isinstance(event, Mapping)
    )
    return rows or '<div class="route">未启用模型审计</div>'


def _routing_attempt_tokens(attempt: Mapping[str, Any]) -> object:
    usage = attempt.get("usage")
    return usage.get("total_tokens", 0) if isinstance(usage, Mapping) else 0


def case_html(item: Mapping[str, Any], output: Path, dataset_root: Path) -> str:
    del output, dataset_root
    render_files = item["renders"]["files"]
    case_token = _dom_token(item["case_id"])
    slide_ids = {
        index: f"{case_token}-slide-{index}"
        for index, _record in enumerate(render_files, start=1)
    }
    images = "".join(
        f'<button type="button" class="slide-thumb" id="{slide_ids[index]}" data-case-id="{case_token}" '
        f'data-slide-src="{html.escape(Path("..", record["local_path"]).as_posix())}" '
        f'data-slide-label="{html.escape(str(item["product"]))} · 第 {index} 页">'
        f'<img loading="lazy" src="{html.escape(Path("..", record["local_path"]).as_posix())}" '
        f'alt="{html.escape(str(item["product"]))} slide {index}"><span>第 {index} 页 · 点击放大</span></button>'
        for index, record in enumerate(render_files, start=1)
    )
    metric_rows = "".join(
        f"<tr><td><code>{html.escape(metric_id)}</code></td>"
        f"<td>{100.0 * float(metric['score']):.1f}</td>"
        f"<td>{int(metric['evidence_count'])}</td>"
        f"<td>{html.escape(', '.join(str(value) for value in metric['evidence_kinds']))}</td></tr>"
        for metric_id, metric in item["metrics"].items()
        if metric["score"] is not None
    )
    rank_eligible = bool(item.get("rank_comparison_eligible", True))
    agreement = item["baseline_rank_delta"] == 0 and item["visual_rank_delta"] == 0
    badge = (
        "排序一致" if rank_eligible and agreement else ("存在排名偏差" if rank_eligible else "排名统计已抑制")
    )
    badge_class = "badge" if rank_eligible and agreement else "badge warn"
    explanation = comparison_explanation(item)
    review_summary = ", ".join(item.get("review_reasons", ())) or "none"
    total_model_tokens = sum(
        int(usage.get("total_tokens", 0) or 0) for usage in item.get("model_token_usage", {}).values()
    )
    pptx_link = Path("..", item["pptx"]["local_path"]).as_posix()
    current_major = str(item.get("profile_version", "")).split(".", 1)[0]
    current_label = f"v{current_major}" if current_major else "Harness"
    reference_badge = (
        ""
        if item.get("reference_v2_score") is None or current_major == "2"
        else f'<span class="badge">v2 {float(item["reference_v2_score"]):.2f}</span>'
    )
    structured = item.get("structured_visual_analysis")
    structured_badges = ""
    if isinstance(structured, Mapping):
        vlm_score = structured.get("structured_vlm_score")
        share_key = f"{float(structured.get('profile_vlm_visual_share', 0.10)):.2f}"
        visual_score = structured.get("scores_by_vlm_share", {}).get(share_key)
        if vlm_score is not None and visual_score is not None:
            structured_badges = (
                f'<span class="badge">六维 VLM {100.0 * float(vlm_score):.1f}</span>'
                f'<span class="badge">Visual@{100.0 * float(share_key):.0f}% '
                f"{100.0 * float(visual_score):.1f}</span>"
            )
    rank_summary = (
        f"当前样本集的人评顺序 #{item['selected_human_order']}；"
        f"Baseline 顺序 #{item['baseline_order']}；"
        f"确定性视觉代理顺序 #{item['visual_proxy_order']}。"
        if rank_eligible
        else "当前运行未满足完整重放资格，逐例系统顺序与排名偏差不予展示。"
    )
    audit_html = _audit_details_html(item, slide_ids)
    routing_html = _routing_html(item)
    status_counts = ", ".join(
        f"{key}:{value}" for key, value in item.get("result_status_counts", {}).items()
    ) or "N/A"
    audit_chain = item.get("audit_chain", {})
    chain_valid = bool(audit_chain.get("valid")) if isinstance(audit_chain, Mapping) else False
    usage_summary = item.get("model_usage_summary", {})
    usage_summary = usage_summary if isinstance(usage_summary, Mapping) else {}
    known_cost = usage_summary.get("reported_cost") if usage_summary.get("cost_known") else None
    cost_label = "未知" if known_cost is None else f"{float(known_cost):.6f}"
    criterion_statuses = item.get("v8_model_analysis", {})
    criterion_statuses = (
        criterion_statuses.get("metric_statuses", {})
        if isinstance(criterion_statuses, Mapping)
        else {}
    )
    criterion_status_text = ", ".join(
        f"{metric_id.removeprefix('structured_vlm_')}:{status}"
        for metric_id, status in criterion_statuses.items()
    ) or "N/A"
    report_link = (
        f'<a href="{html.escape(str(item["report_href"]))}">打开完整 EvaluationReport JSON</a> · '
        if item.get("report_href")
        else ""
    )
    return f"""<section class="case"><div class="case-head"><div><h2>{html.escape(item["product"])}</h2>
<span class="{badge_class}">{badge}</span><span class="badge">人评 #{item["human_rank"]}</span>
{reference_badge}<span class="badge">{current_label} {item["base_score"]:.2f}</span><span class="badge">确定性视觉代理 {item["deterministic_visual_proxy"]:.2f}</span>{structured_badges}</div>
<div><b>{item["decision"]} / {item["coverage"]}</b><br><code>{html.escape(item["run_id"])}</code></div></div>
<details open><summary>查看全部 {len(render_files)} 页幻灯片（点击放大，支持方向键）</summary><div class="gallery">{images}</div></details>
<div class="two"><div><h3>关键 Metric</h3><table><thead><tr><th>Metric</th><th>分数</th><th>证据数</th><th>证据类型</th></tr></thead>
<tbody>{metric_rows}</tbody></table></div><div><h3>对照解释</h3><p>{html.escape(explanation)}</p>
<p>{html.escape(rank_summary)}</p>
<p><b>处置原因：</b>{html.escape(review_summary)}<br><b>模型 token：</b>{total_model_tokens}</p>
<p>{report_link}<a href="{html.escape(pptx_link)}">打开原始 PPTX</a></p></div></div>
<div class="audit-grid"><div class="audit-card"><span>OracleResult 状态</span><b>{html.escape(status_counts)}</b></div>
<div class="audit-card"><span>审计链</span><b class="status-{'PASS' if chain_valid else 'ERROR'}">{'VALID' if chain_valid else 'INVALID'}</b><small>{html.escape(str(audit_chain.get('broken_event') or 'hash chain complete')) if isinstance(audit_chain, Mapping) else ''}</small></div>
<div class="audit-card"><span>模型 usage</span><b>{int(usage_summary.get('total_tokens', 0) or 0)} tokens</b><small>usage_complete={html.escape(str(usage_summary.get('usage_complete', False)).lower())} · cost={cost_label} · cost_known={html.escape(str(usage_summary.get('cost_known', False)).lower())}</small></div>
<div class="audit-card"><span>7 个模型 criterion</span><b>{'VALID' if all(value == 'SCORED' for value in criterion_statuses.values()) and len(criterion_statuses) == 7 else 'INCOMPLETE'}</b><small>{html.escape(criterion_status_text)}</small></div></div>
<h3>模型路由与 usage</h3>{routing_html}{audit_html}</section>"""


def comparison_explanation(item: Mapping[str, Any]) -> str:
    metrics = item["metrics"]
    order_note = (
        f"当前样本集中人评顺序为 {item['selected_human_order']}，Harness 顺序为 {item['baseline_order']}"
        if item.get("rank_comparison_eligible", True)
        else (
            "当前切片未通过完整重放资格，正式 Spearman、系统顺序与排名偏差均已抑制；"
            "以下只解释该 deck 自身的可观察证据"
        )
    )
    llm = _metric_percent(metrics, "llm_content_quality_audit")
    vlm = _metric_percent(metrics, "vlm_visual_quality_audit")
    if vlm == "N/A":
        vlm = _metric_percent(metrics, "structured_vlm_visual_audit")
    structured = item.get("structured_visual_analysis")
    if vlm == "N/A" and isinstance(structured, Mapping):
        structured_score = structured.get("structured_vlm_score")
        if structured_score is not None:
            vlm = f"{100.0 * float(structured_score):.1f}"
    if item["product"] == "Kimi-Banana":
        editability = _metric_percent(metrics, "editability")
        return (
            f"{order_note}。该 deck 为整页栅格图，editability {editability}；"
            f"渲染语义内容/视觉为 {llm}/{vlm}。人评可见偏好与可编辑交付质量"
            "不是同一构念，不能通过调单一权重消除这个差异。"
        )
    if item["product"] == "Skywork-Banana":
        return (
            f"{order_note}。确定性 layout/typography 较高，但 Flash 内容与视觉分别为"
            f" {llm}/{vlm}，表明对象树的“结构整齐”不等于真实交付观感和内容质量。"
        )
    if item["product"] == "Quake":
        residue = _metric_percent(metrics, "template_residue")
        return (
            f"{order_note}。模板残留仅 {residue}，对应结束页日期/报告人占位符；"
            f"Flash 内容/视觉为 {llm}/{vlm}。该例能检验新模板 Oracle 是否修正旧基线的"
            "高分补偿问题。"
        )
    layout = _metric_percent(metrics, "layout")
    return (
        f"{order_note}。Layout {layout}，Flash 内容/视觉为 {llm}/{vlm}。"
        "若 VLM 视觉分明显高于人评相对名次，说明当前 Prompt/权重可能偏好"
        "表面规整度，而没有充分捕捉内容价值或人类偏好。"
    )


def _metric_percent(metrics: Mapping[str, Any], metric_id: str) -> str:
    value = metrics.get(metric_id, {}).get("score")
    return "N/A" if value is None else f"{100.0 * float(value):.1f}"


def _reference_v2_statistics(reference_dir: Path) -> Mapping[str, Any] | None:
    path = reference_dir / "comparison.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    statistics = payload.get("statistics")
    return dict(statistics) if isinstance(statistics, Mapping) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample" / "report",
    )
    parser.add_argument(
        "--qwen-v3",
        action="store_true",
        help="enable Qwen providers for the selected model Profile and use pinned upstream renders",
    )
    parser.add_argument(
        "--flash-only",
        action="store_true",
        help="score with Flash but disable conditional Plus calls for calibration runs",
    )
    parser.add_argument(
        "--rerun-products",
        nargs="+",
        metavar="PRODUCT",
        help="rerun only these products and reuse the other reports in --output",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="make comparison/HTML only from existing per-product reports",
    )
    parser.add_argument(
        "--reuse-reports-from",
        type=Path,
        help="reuse matching reports from another output and evaluate only missing products",
    )
    parser.add_argument(
        "--reference-report-dir",
        type=Path,
        help="same-code baseline report directory used for score/statistic deltas",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        help="explicit Profile JSON; --qwen-v3 is required for model-enabled Profiles",
    )
    parser.add_argument(
        "--products",
        nargs="+",
        metavar="PRODUCT",
        help="evaluate only these products from the manifest",
    )
    args = parser.parse_args()
    if args.reuse_existing and args.rerun_products is not None:
        parser.error("--reuse-existing and --rerun-products are mutually exclusive")
    if args.flash_only and not args.qwen_v3:
        parser.error("--flash-only requires --qwen-v3")
    comparison = evaluate(
        args.dataset_root.resolve(),
        args.output.resolve(),
        qwen_v3=args.qwen_v3,
        flash_only=args.flash_only,
        rerun_products=(
            frozenset()
            if args.reuse_existing
            else (None if args.rerun_products is None else frozenset(args.rerun_products))
        ),
        reuse_reports_from=(None if args.reuse_reports_from is None else args.reuse_reports_from.resolve()),
        reference_report_dir=(
            None if args.reference_report_dir is None else args.reference_report_dir.resolve()
        ),
        profile_path=None if args.profile is None else args.profile.resolve(),
        include_products=(None if args.products is None else frozenset(args.products)),
    )
    print(json.dumps(comparison["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
