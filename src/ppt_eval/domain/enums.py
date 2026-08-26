"""Stable vocabulary shared by the evaluation domain.

The values of these enums are part of the persisted audit contract.  New
values may be added, but existing values must not be renamed.
"""

from __future__ import annotations

from enum import Enum


class SceneType(str, Enum):
    """Supported PPT evaluation scenarios."""

    TEXT_TO_PPT = "text_to_ppt"
    PROJECT_SUMMARY = "project_summary"
    MULTIMODAL = "multimodal"
    READY_MADE = "ready_made"


class ExecutionStatus(str, Enum):
    """Whether an Oracle invocation itself completed successfully."""

    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class MetricStatus(str, Enum):
    """The quality conclusion, deliberately separate from execution state."""

    PASS = "PASS"
    FAIL = "FAIL"
    SCORED = "SCORED"
    NA = "NA"
    ERROR = "ERROR"


class Severity(str, Enum):
    INFO = "INFO"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class ScoreRole(str, Enum):
    """A metric can affect one and only one part of PPT-PDMS."""

    BASE_ADDITIVE = "BASE_ADDITIVE"
    SCENE_ADDITIVE = "SCENE_ADDITIVE"
    BASE_MULTIPLIER = "BASE_MULTIPLIER"
    SCENE_MULTIPLIER = "SCENE_MULTIPLIER"
    DIAGNOSTIC = "DIAGNOSTIC"


class CoverageStatus(str, Enum):
    """How much of the requested evaluation was completed."""

    FULL = "FULL"
    DEGRADED = "DEGRADED"
    BASE_ONLY = "BASE_ONLY"
    UNASSESSABLE = "UNASSESSABLE"


class EvaluationDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    ERROR = "ERROR"


class SupervisorState(str, Enum):
    OBSERVE = "OBSERVE"
    PLAN = "PLAN"
    ACT = "ACT"
    VERIFY = "VERIFY"
    FINALIZE = "FINALIZE"
    REVIEW = "REVIEW"


class DagNodeKind(str, Enum):
    BASELINE = "BASELINE"
    SCENE = "SCENE"

