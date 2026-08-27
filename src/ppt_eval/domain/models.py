"""Versioned, dependency-free contracts for PPT evaluation.

Dataclasses are used instead of framework models so the domain can be used by
the CLI, workers, tests and future service adapters without importing their
SDKs.  Persistence adapters should serialize enum values, not enum names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .enums import (
    CoverageStatus,
    DagNodeKind,
    EvaluationDecision,
    ExecutionStatus,
    MetricStatus,
    SceneType,
    ScoreRole,
    Severity,
    SupervisorState,
)

SCHEMA_VERSION = "1.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: str
    message: str
    page_number: int | None = None
    object_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_uri: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be one-based")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must contain exactly four coordinates")


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    scene: SceneType
    pptx_path: str
    request: str | None = None
    audience: str | None = None
    source_materials: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be blank")
        if not self.pptx_path.strip():
            raise ValueError("pptx_path must not be blank")
        if not isinstance(self.scene, SceneType):
            object.__setattr__(self, "scene", SceneType(self.scene))


DEFAULT_BASE_WEIGHTS: Mapping[str, float] = {
    "content_clarity": 0.14,
    "narrative": 0.12,
    "visual_hierarchy": 0.12,
    "layout": 0.12,
    "typography": 0.10,
    "style_consistency": 0.10,
    "multimedia_quality": 0.08,
    "editability": 0.08,
    "compatibility": 0.08,
    "accessibility": 0.06,
}

DEFAULT_SCENE_WEIGHTS: Mapping[SceneType, Mapping[str, float]] = {
    SceneType.TEXT_TO_PPT: {
        "instruction_coverage": 0.45,
        "audience_fit": 0.25,
        "fact_quality": 0.30,
    },
    SceneType.PROJECT_SUMMARY: {
        "source_faithfulness": 0.30,
        "key_point_recall": 0.25,
        "numeric_accuracy": 0.20,
        "compression_quality": 0.15,
        "traceability": 0.10,
    },
    SceneType.MULTIMODAL: {
        "asset_compliance": 0.30,
        "asset_presentation": 0.25,
        "crop_clarity": 0.15,
        "chart_data_accuracy": 0.20,
        "media_availability": 0.10,
    },
    SceneType.READY_MADE: {},
}

DEFAULT_BASE_MULTIPLIERS = (
    "file_deliverability",
    "critical_content_visibility",
    "internal_data_consistency",
)

DEFAULT_SCENE_MULTIPLIERS: Mapping[SceneType, tuple[str, ...]] = {
    SceneType.TEXT_TO_PPT: ("critical_instruction_compliance",),
    SceneType.PROJECT_SUMMARY: ("critical_source_consistency",),
    SceneType.MULTIMODAL: (
        "required_asset_compliance",
        "critical_chart_data_accuracy",
    ),
    SceneType.READY_MADE: (),
}

DEFAULT_LAMBDA: Mapping[SceneType, float] = {
    SceneType.TEXT_TO_PPT: 0.55,
    SceneType.PROJECT_SUMMARY: 0.40,
    SceneType.MULTIMODAL: 0.45,
    SceneType.READY_MADE: 1.0,
}

# Model-assisted shadow execution is introduced only by v2 defaults.  Keeping
# the v1 weights and routing unchanged is important for historical replay.
V2_OPTIONAL_MODEL_METRIC_IDS = (
    "llm_content_quality_audit",
    "vlm_visual_quality_audit",
    "llm_scenario_compliance_audit",
)
V2_MODEL_COMPOSITE_ORACLE_ID = "high_cost.model_audits"

V3_FLASH_BASE_WEIGHTS: Mapping[str, float] = {
    "template_residue": 0.08,
    "llm_content_quality_audit": 0.08,
    "vlm_visual_quality_audit": 0.12,
}
V3_FLASH_SCENE_WEIGHTS: Mapping[str, float] = {
    "llm_scenario_compliance_audit": 0.10,
}
V3_METRIC_REVIEW_THRESHOLDS: Mapping[str, float] = {
    "template_residue": 0.85,
    "layout": 0.65,
    "typography": 0.70,
    "llm_content_quality_audit": 0.70,
    "vlm_visual_quality_audit": 0.70,
}

FLAT_WEIGHTED_MEAN = "FLAT_WEIGHTED_MEAN"
CONSTRUCT_WEIGHTED_MEAN = "CONSTRUCT_WEIGHTED_MEAN"


@dataclass(frozen=True, slots=True)
class EvalProfile:
    profile_id: str
    version: str
    scene: SceneType
    base_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_BASE_WEIGHTS)
    )
    scene_weights: Mapping[str, float] = field(default_factory=dict)
    base_multiplier_metric_ids: tuple[str, ...] = DEFAULT_BASE_MULTIPLIERS
    scene_multiplier_metric_ids: tuple[str, ...] = ()
    required_metric_ids: tuple[str, ...] | None = None
    enabled_oracle_ids: tuple[str, ...] = ()
    lambda_base: float | None = None
    hard_gate_min_confidence: float = 0.90
    pass_threshold: float = 80.0
    review_threshold: float = 60.0
    metric_review_thresholds: Mapping[str, float] = field(default_factory=dict)
    aggregation_strategy: str = FLAT_WEIGHTED_MEAN
    base_metric_constructs: Mapping[str, str] = field(default_factory=dict)
    base_construct_weights: Mapping[str, float] = field(default_factory=dict)
    scene_metric_constructs: Mapping[str, str] = field(default_factory=dict)
    scene_construct_weights: Mapping[str, float] = field(default_factory=dict)
    max_retries: int = 1
    oracle_timeout_seconds: float = 60.0
    cost_budget: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scene, SceneType):
            object.__setattr__(self, "scene", SceneType(self.scene))
        if not self.profile_id.strip() or not self.version.strip():
            raise ValueError("profile_id and version must not be blank")
        if self.lambda_base is None:
            object.__setattr__(self, "lambda_base", DEFAULT_LAMBDA[self.scene])
        configured_metric_ids = tuple(
            dict.fromkeys(
                (*self.base_weights, *self.scene_weights)
                + self.base_multiplier_metric_ids
                + self.scene_multiplier_metric_ids
            )
        )
        if self.required_metric_ids is None:
            # Required means that NA is an incomplete evaluation, not that the
            # metric is assigned a zero. Multimedia quality is genuinely
            # optional for a deck containing no media in the ready-made case.
            required = list(configured_metric_ids)
            if self.scene == SceneType.READY_MADE:
                required = [
                    metric_id
                    for metric_id in required
                    if metric_id != "multimedia_quality"
                ]
            object.__setattr__(self, "required_metric_ids", tuple(required))
        else:
            object.__setattr__(
                self, "required_metric_ids", tuple(self.required_metric_ids)
            )
        lambda_base = self.lambda_base
        if lambda_base is None or not 0.0 <= float(lambda_base) <= 1.0:
            raise ValueError("lambda_base must be between zero and one")
        if not 0.0 <= self.hard_gate_min_confidence <= 1.0:
            raise ValueError("hard_gate_min_confidence must be between zero and one")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.oracle_timeout_seconds <= 0:
            raise ValueError("oracle_timeout_seconds must be positive")
        if not 0 <= self.review_threshold <= self.pass_threshold <= 100:
            raise ValueError("thresholds must satisfy 0 <= review <= pass <= 100")
        self._validate_weights("base_weights", self.base_weights)
        self._validate_weights("scene_weights", self.scene_weights)
        self._validate_metric_review_thresholds(configured_metric_ids)
        self._validate_no_double_penalty()
        unknown_required = set(self.required_metric_ids or ()) - set(
            configured_metric_ids
        )
        if unknown_required:
            raise ValueError(
                "required_metric_ids contains unconfigured metrics: "
                + ", ".join(sorted(unknown_required))
            )
        self._validate_construct_aggregation()

    @staticmethod
    def _validate_weights(name: str, weights: Mapping[str, float]) -> None:
        if any(not key for key in weights):
            raise ValueError(f"{name} contains a blank metric id")
        if any(value < 0 for value in weights.values()):
            raise ValueError(f"{name} cannot contain negative weights")
        if weights and sum(weights.values()) <= 0:
            raise ValueError(f"{name} must contain at least one positive weight")

    def _validate_no_double_penalty(self) -> None:
        additive = set(self.base_weights) | set(self.scene_weights)
        multipliers = set(self.base_multiplier_metric_ids) | set(
            self.scene_multiplier_metric_ids
        )
        overlap = additive & multipliers
        if overlap:
            raise ValueError(
                "metrics cannot be both additive and multiplicative: "
                + ", ".join(sorted(overlap))
            )
        duplicate_multipliers = len(multipliers) != (
            len(self.base_multiplier_metric_ids)
            + len(self.scene_multiplier_metric_ids)
        )
        if duplicate_multipliers:
            raise ValueError("a multiplier cannot be used by both base and scene scores")

    def _validate_metric_review_thresholds(
        self, configured_metric_ids: tuple[str, ...]
    ) -> None:
        unknown = set(self.metric_review_thresholds) - set(configured_metric_ids)
        if unknown:
            raise ValueError(
                "metric_review_thresholds contains unconfigured metrics: "
                + ", ".join(sorted(unknown))
            )
        for metric_id, threshold in self.metric_review_thresholds.items():
            if not metric_id or not 0.0 <= float(threshold) <= 1.0:
                raise ValueError(
                    "metric_review_thresholds must map metric ids to values in [0,1]"
                )

    def _validate_construct_aggregation(self) -> None:
        strategy = str(self.aggregation_strategy).strip().upper()
        object.__setattr__(self, "aggregation_strategy", strategy)
        configured = {
            "base": (
                self.base_weights,
                self.base_metric_constructs,
                self.base_construct_weights,
            ),
            "scene": (
                self.scene_weights,
                self.scene_metric_constructs,
                self.scene_construct_weights,
            ),
        }
        if strategy == FLAT_WEIGHTED_MEAN:
            if any(assignments or weights for _, assignments, weights in configured.values()):
                raise ValueError(
                    "construct mappings require CONSTRUCT_WEIGHTED_MEAN"
                )
            return
        if strategy != CONSTRUCT_WEIGHTED_MEAN:
            raise ValueError(f"unknown aggregation_strategy {strategy!r}")

        required = set(self.required_metric_ids or ())
        for label, (metric_weights, assignments, construct_weights) in configured.items():
            positive_metrics = {
                metric_id for metric_id, weight in metric_weights.items() if weight > 0
            }
            if not positive_metrics:
                if assignments or construct_weights:
                    raise ValueError(f"{label} constructs configured without metrics")
                continue
            if set(assignments) != positive_metrics:
                missing = positive_metrics - set(assignments)
                unknown = set(assignments) - positive_metrics
                details = []
                if missing:
                    details.append("missing=" + ",".join(sorted(missing)))
                if unknown:
                    details.append("unknown=" + ",".join(sorted(unknown)))
                raise ValueError(
                    f"{label}_metric_constructs must assign each positive metric once: "
                    + "; ".join(details)
                )
            construct_ids = {str(value).strip() for value in assignments.values()}
            if "" in construct_ids:
                raise ValueError(f"{label}_metric_constructs contains a blank construct")
            if set(construct_weights) != construct_ids:
                raise ValueError(
                    f"{label}_construct_weights must match assigned constructs"
                )
            self._validate_weights(f"{label}_construct_weights", construct_weights)
            if any(float(weight) <= 0 for weight in construct_weights.values()):
                raise ValueError(f"{label}_construct_weights must all be positive")
            for construct_id in construct_ids:
                if not any(
                    metric_id in required and assignments[metric_id] == construct_id
                    for metric_id in positive_metrics
                ):
                    raise ValueError(
                        f"construct {construct_id!r} must contain a required metric"
                    )

    @classmethod
    def default(cls, scene: SceneType, version: str = "3.1") -> "EvalProfile":
        scene = SceneType(scene)
        try:
            major_version = int(version.split(".", 1)[0])
        except ValueError:
            major_version = 1
        base_weights = dict(DEFAULT_BASE_WEIGHTS)
        scene_weights = dict(DEFAULT_SCENE_WEIGHTS[scene])
        enabled_oracle_ids = {
            SceneType.TEXT_TO_PPT: ("scenario.instruction_alignment",),
            SceneType.PROJECT_SUMMARY: ("scenario.source_faithfulness",),
            SceneType.MULTIMODAL: ("scenario.asset_compliance",),
            SceneType.READY_MADE: (),
        }[scene]
        required_metric_ids: tuple[str, ...] | None = None
        metric_review_thresholds: Mapping[str, float] = {}
        metadata: Mapping[str, Any] = {}
        if major_version >= 2:
            enabled_oracle_ids = (
                *enabled_oracle_ids,
                V2_MODEL_COMPOSITE_ORACLE_ID,
            )
            configured = tuple(
                dict.fromkeys(
                    (*base_weights, *scene_weights)
                    + DEFAULT_BASE_MULTIPLIERS
                    + DEFAULT_SCENE_MULTIPLIERS[scene]
                )
            )
            required_metric_ids = tuple(
                metric_id
                for metric_id in configured
                if metric_id not in V2_OPTIONAL_MODEL_METRIC_IDS
                and not (
                    scene == SceneType.READY_MADE
                    and metric_id == "multimedia_quality"
                )
            )
        if major_version >= 3:
            base_weights.update(V3_FLASH_BASE_WEIGHTS)
            if scene != SceneType.READY_MADE:
                scene_weights.update(V3_FLASH_SCENE_WEIGHTS)
            configured = tuple(
                dict.fromkeys(
                    (*base_weights, *scene_weights)
                    + DEFAULT_BASE_MULTIPLIERS
                    + DEFAULT_SCENE_MULTIPLIERS[scene]
                )
            )
            required_metric_ids = tuple(
                metric_id
                for metric_id in configured
                if not (
                    scene == SceneType.READY_MADE
                    and metric_id == "multimedia_quality"
                )
            )
            metric_review_thresholds = {
                **V3_METRIC_REVIEW_THRESHOLDS,
                **(
                    {"llm_scenario_compliance_audit": 0.70}
                    if scene != SceneType.READY_MADE
                    else {}
                ),
            }
            metadata = {
                "lifecycle": "PRE_RESEARCH",
                "model_audit_routing": (
                    "FLASH_PLUS_HUMAN"
                    if version == "3.0"
                    else "FLASH_ADVANCED_HUMAN"
                ),
                "flash_model": "qwen3.7-flash",
                **(
                    {"plus_model": "qwen3.7-plus"}
                    if version == "3.0"
                    else {"advanced_model": "qwen3.8-flash"}
                ),
            }
        return cls(
            profile_id=f"default-{scene.value}",
            version=version,
            scene=scene,
            base_weights=base_weights,
            scene_weights=scene_weights,
            base_multiplier_metric_ids=DEFAULT_BASE_MULTIPLIERS,
            scene_multiplier_metric_ids=DEFAULT_SCENE_MULTIPLIERS[scene],
            required_metric_ids=required_metric_ids,
            lambda_base=DEFAULT_LAMBDA[scene],
            enabled_oracle_ids=enabled_oracle_ids,
            metric_review_thresholds=metric_review_thresholds,
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class OracleResult:
    oracle_id: str
    metric_id: str
    execution_status: ExecutionStatus
    metric_status: MetricStatus
    score_role: ScoreRole = ScoreRole.DIAGNOSTIC
    raw_value: float | str | bool | None = None
    normalized_score: float | None = None
    multiplier: float | None = None
    confidence: float = 1.0
    severity: Severity = Severity.INFO
    evidence: tuple[Evidence, ...] = ()
    version: str = "1.0"
    duration_ms: int = 0
    cost: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for attribute, enum_type in (
            ("execution_status", ExecutionStatus),
            ("metric_status", MetricStatus),
            ("score_role", ScoreRole),
            ("severity", Severity),
        ):
            value = getattr(self, attribute)
            if not isinstance(value, enum_type):
                object.__setattr__(self, attribute, enum_type(value))
        if not self.oracle_id or not self.metric_id:
            raise ValueError("oracle_id and metric_id must not be blank")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        if self.normalized_score is not None and not 0.0 <= self.normalized_score <= 1.0:
            raise ValueError("normalized_score must be between zero and one")
        if self.multiplier is not None and self.multiplier not in (0.0, 0.5, 1.0):
            raise ValueError("multiplier must be one of 0, 0.5, or 1")
        if self.duration_ms < 0 or self.cost < 0:
            raise ValueError("duration_ms and cost cannot be negative")
        execution_failed = self.execution_status == ExecutionStatus.ERROR
        metric_failed = self.metric_status == MetricStatus.ERROR
        if execution_failed != metric_failed:
            raise ValueError(
                "execution ERROR and metric ERROR must be set together; "
                "quality FAIL is not an execution error"
            )
        if self.metric_status == MetricStatus.SCORED and self.normalized_score is None:
            raise ValueError("SCORED results require normalized_score")

    @classmethod
    def error(
        cls,
        *,
        oracle_id: str,
        metric_id: str,
        error_code: str,
        error_message: str,
        score_role: ScoreRole = ScoreRole.DIAGNOSTIC,
        version: str = "1.0",
    ) -> "OracleResult":
        return cls(
            oracle_id=oracle_id,
            metric_id=metric_id,
            execution_status=ExecutionStatus.ERROR,
            metric_status=MetricStatus.ERROR,
            score_role=score_role,
            confidence=0.0,
            severity=Severity.MAJOR,
            version=version,
            error_code=error_code,
            error_message=error_message,
        )


@dataclass(frozen=True, slots=True)
class DagNode:
    node_id: str
    oracle_id: str
    dependencies: tuple[str, ...] = ()
    kind: DagNodeKind = DagNodeKind.SCENE
    mandatory: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationDag:
    nodes: tuple[DagNode, ...]

    def __post_init__(self) -> None:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("DAG node ids must be unique")
        known = set(ids)
        for node in self.nodes:
            unknown = set(node.dependencies) - known
            if unknown:
                raise ValueError(
                    f"node {node.node_id} has unknown dependencies: {sorted(unknown)}"
                )
            if node.node_id in node.dependencies:
                raise ValueError(f"node {node.node_id} cannot depend on itself")
        unresolved = list(self.nodes)
        resolved: set[str] = set()
        while unresolved:
            ready = [
                node for node in unresolved if set(node.dependencies) <= resolved
            ]
            if not ready:
                raise ValueError("evaluation DAG contains a dependency cycle")
            for node in ready:
                resolved.add(node.node_id)
                unresolved.remove(node)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    base_additive: float | None
    scene_additive: float | None
    base_multiplier: float
    scene_multiplier: float
    base_score: float | None
    full_score: float | None
    coverage: CoverageStatus
    base_complete: bool
    scene_complete: bool
    unresolved_metric_ids: tuple[str, ...] = ()
    low_confidence_gate_ids: tuple[str, ...] = ()
    base_construct_scores: Mapping[str, float] = field(default_factory=dict)
    scene_construct_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalReport:
    report_id: str
    run_id: str
    case_id: str
    profile_id: str
    profile_version: str
    scene: SceneType
    coverage: CoverageStatus
    decision: EvaluationDecision
    base_score: float | None
    scene_score: float | None
    full_score: float | None
    overall_score: float | None
    results: tuple[OracleResult, ...]
    review_reasons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ReviewCase:
    review_id: str
    run_id: str
    reason_codes: tuple[str, ...]
    status: str = "OPEN"
    assignee: str | None = None
    resolution: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    case_id: str
    profile_id: str
    profile_version: str
    state: SupervisorState
    state_history: tuple[SupervisorState, ...]
    coverage: CoverageStatus | None = None
    input_hash: str | None = None
    profile_fingerprint: str | None = None
    result_hash: str | None = None
    git_sha: str | None = None
    container_image: str | None = None
    font_fingerprint: str | None = None
    oracle_versions: Mapping[str, str] = field(default_factory=dict)
    model_versions: Mapping[str, str] = field(default_factory=dict)
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    renderer_versions: Mapping[str, str] = field(default_factory=dict)
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    random_seed: int = 0
    cost: float = 0.0
    duration_ms: int = 0
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None
    error: str | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    run_id: str
    event_type: str
    occurred_at: str
    actor: str
    payload: Mapping[str, Any]
    previous_hash: str | None = None
    event_hash: str | None = None
    supersedes: str | None = None
    schema_version: str = SCHEMA_VERSION
