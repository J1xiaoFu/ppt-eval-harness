"""Evaluate the pinned Slides-Align market-analysis sample and build an HTML report."""

from __future__ import annotations

import argparse
import html
import json
import math
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
                "metrics": selected_metrics(results),
                "model_audit_statuses": model_statuses,
                "model_audit_routing": model_routing_events(
                    audit_path,
                    report["run_id"],
                ),
                "review_reasons": list(report.get("review_reasons", ())),
                "model_versions": dict(report["manifest"].get("model_versions", {})),
                "prompt_versions": dict(report["manifest"].get("prompt_versions", {})),
                "model_token_usage": {
                    metric_id: results[metric_id].get("metadata", {}).get("usage", {})
                    for metric_id in results
                    if results[metric_id].get("metadata", {}).get("audit_type") == "model"
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
    rank_statistics_eligible = structured_visual_replay is None or structured_visual_replay["eligible"]
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
        "schema_version": "1.2",
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
.warn{{background:#fae9e2;color:#8b341e}} .gallery{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0}}
.gallery img{{width:100%;aspect-ratio:16/9;object-fit:contain;background:#111;border-radius:7px;border:1px solid #ccd4da}}
table{{border-collapse:collapse;width:100%;margin:16px 0}} th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left}} th{{color:var(--muted);font-weight:600}}
.two{{display:grid;grid-template-columns:1.1fr .9fr;gap:22px}} code{{font-family:Consolas,monospace;font-size:12px}}
details{{margin-top:12px}} .note{{border-left:4px solid var(--warm);padding:10px 14px;background:#fff3ed}}
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
{structured_visual_section}
{case_sections}
<section><h2>限制与下一步</h2><ul>{limitations}</ul></section>
</main></body></html>"""


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


def case_html(item: Mapping[str, Any], output: Path, dataset_root: Path) -> str:
    render_files = item["renders"]["files"]
    images = "".join(
        f'<img loading="lazy" src="{html.escape(Path("..", record["local_path"]).as_posix())}" '
        f'alt="{html.escape(item["product"])} slide {index}">'
        for index, record in enumerate(render_files[:6], start=1)
    )
    all_images = "".join(
        f'<a href="{html.escape(Path("..", record["local_path"]).as_posix())}">slide {index}</a> '
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
    route_summary = (
        " -> ".join(f"{event['stage']}:{event['route']}" for event in item.get("model_audit_routing", ()))
        or "not enabled"
    )
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
    return f"""<section class="case"><div class="case-head"><div><h2>{html.escape(item["product"])}</h2>
<span class="{badge_class}">{badge}</span><span class="badge">人评 #{item["human_rank"]}</span>
{reference_badge}<span class="badge">{current_label} {item["base_score"]:.2f}</span><span class="badge">确定性视觉代理 {item["deterministic_visual_proxy"]:.2f}</span>{structured_badges}</div>
<div><b>{item["decision"]} / {item["coverage"]}</b><br><code>{html.escape(item["run_id"])}</code></div></div>
<div class="gallery">{images}</div><details><summary>打开全部 {len(render_files)} 页</summary>{all_images}</details>
<div class="two"><div><h3>关键 Metric</h3><table><thead><tr><th>Metric</th><th>分数</th><th>证据数</th><th>证据类型</th></tr></thead>
<tbody>{metric_rows}</tbody></table></div><div><h3>对照解释</h3><p>{html.escape(explanation)}</p>
<p>{html.escape(rank_summary)}</p>
<p><b>模型路由：</b>{html.escape(route_summary)}<br>
<b>处置原因：</b>{html.escape(review_summary)}<br>
<b>模型 token：</b>{total_model_tokens}</p>
<p><a href="{html.escape(pptx_link)}">打开原始 PPTX</a></p></div></div></section>"""


def comparison_explanation(item: Mapping[str, Any]) -> str:
    if not item.get("rank_comparison_eligible", True):
        return (
            "该 deck 或同切片其他 deck 未通过 FULL、六维完整性、当前 Profile/Prompt "
            "与单调用投影契约检查，因此不解释其相对排序。"
        )
    metrics = item["metrics"]
    order_note = (
        f"当前样本集中人评顺序为 {item['selected_human_order']}，Harness 顺序为 {item['baseline_order']}"
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
