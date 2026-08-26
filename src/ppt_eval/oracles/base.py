"""Shared Oracle machinery.

Oracles depend only on stable domain contracts and the vendor-neutral PPTX
adapter.  The supervisor can therefore treat every leaf as the same command
without knowing how a metric is calculated.
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from ppt_eval.adapters.pptx import ParsedPresentation, PptxAdapter, PptxAdapterError
from ppt_eval.application.oracle import MetricDefinition, OracleDescriptor
from ppt_eval.domain.enums import (
    ExecutionStatus,
    MetricStatus,
    ScoreRole,
    Severity,
)
from ppt_eval.domain.models import Evidence, OracleResult

ORACLE_VERSION = "1.0"
_PARSED_KEY = "ppt_eval.parsed_presentation"
_PARSE_ERROR_KEY = "ppt_eval.parse_error"


class AtomicOracle(ABC):
    """Template method for a single, evidence-producing metric."""

    oracle_id: str
    metric_id: str
    score_role: ScoreRole
    version = ORACLE_VERSION

    def __init__(self, adapter: PptxAdapter | None = None) -> None:
        self.adapter = adapter or PptxAdapter()

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=(MetricDefinition(self.metric_id, self.score_role),),
            deterministic=True,
            description=self.__doc__ or "Deterministic atomic PPT evaluation metric",
        )

    def supports(self, context: object) -> bool:
        return bool(getattr(_case(context), "pptx_path", ""))

    def evaluate(self, context: object) -> OracleResult:
        started = time.perf_counter()
        try:
            result = self._evaluate(context)
        except PptxAdapterError as exc:
            result = OracleResult.error(
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                error_code="PPTX_PARSE_ERROR",
                error_message=str(exc),
                score_role=self.score_role,
                version=self.version,
            )
        except Exception as exc:  # an Oracle failure is not a quality failure
            result = OracleResult.error(
                oracle_id=self.oracle_id,
                metric_id=self.metric_id,
                error_code="ORACLE_ERROR",
                error_message=f"{type(exc).__name__}: {exc}",
                score_role=self.score_role,
                version=self.version,
            )
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        return replace(result, duration_ms=elapsed)

    @abstractmethod
    def _evaluate(self, context: object) -> OracleResult:
        raise NotImplementedError

    def presentation(self, context: object) -> ParsedPresentation:
        return load_presentation(context, self.adapter)

    def scored(
        self,
        score: float,
        evidence: Sequence[Evidence] = (),
        *,
        raw_value: float | str | bool | None = None,
        confidence: float = 1.0,
        severity: Severity | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OracleResult:
        score = clamp(score)
        if severity is None:
            severity = _score_severity(score)
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.SCORED,
            score_role=self.score_role,
            raw_value=score if raw_value is None else raw_value,
            normalized_score=score,
            confidence=clamp(confidence),
            severity=severity,
            evidence=tuple(evidence),
            version=self.version,
            metadata=metadata or {},
        )

    def multiplied(
        self,
        multiplier: float,
        evidence: Sequence[Evidence] = (),
        *,
        raw_value: float | str | bool | None = None,
        confidence: float = 1.0,
        severity: Severity | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OracleResult:
        if multiplier not in (0.0, 0.5, 1.0):
            raise ValueError("hard multipliers must be 0, 0.5, or 1")
        if severity is None:
            severity = (
                Severity.INFO
                if multiplier == 1
                else Severity.MAJOR
                if multiplier == 0.5
                else Severity.CRITICAL
            )
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.PASS if multiplier == 1 else MetricStatus.FAIL,
            score_role=self.score_role,
            raw_value=multiplier if raw_value is None else raw_value,
            multiplier=multiplier,
            confidence=clamp(confidence),
            severity=severity,
            evidence=tuple(evidence),
            version=self.version,
            metadata=metadata or {},
        )

    def not_applicable(
        self,
        reason: str,
        *,
        code: str = "INSUFFICIENT_EVIDENCE",
        source_uri: str | None = None,
    ) -> OracleResult:
        return OracleResult(
            oracle_id=self.oracle_id,
            metric_id=self.metric_id,
            execution_status=ExecutionStatus.SUCCESS,
            metric_status=MetricStatus.NA,
            score_role=self.score_role,
            confidence=1.0,
            severity=Severity.INFO,
            evidence=(
                evidence(
                    self.metric_id,
                    code.lower(),
                    "insufficient_evidence",
                    reason,
                    source_uri=source_uri,
                    payload={"reason_code": code},
                ),
            ),
            version=self.version,
            metadata={"reason_code": code},
        )


class CompositeOracle:
    """Composite pattern: evaluate children independently and preserve failures."""

    oracle_id: str
    metric_id: str
    version = ORACLE_VERSION

    def __init__(self, children: Sequence[AtomicOracle]) -> None:
        self.children = tuple(children)
        child_ids = [child.oracle_id for child in self.children]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("composite Oracle children must have unique ids")

    def describe(self) -> OracleDescriptor:
        return OracleDescriptor(
            oracle_id=self.oracle_id,
            name=self.__class__.__name__,
            version=self.version,
            metrics=tuple(
                MetricDefinition(child.metric_id, child.score_role)
                for child in self.children
            ),
            deterministic=True,
            description=f"Composite of: {', '.join(child.oracle_id for child in self.children)}",
        )

    def supports(self, context: object) -> bool:
        return bool(self.children) and any(child.supports(context) for child in self.children)

    def evaluate(self, context: object) -> tuple[OracleResult, ...]:
        results = []
        for child in self.children:
            if child.supports(context):
                results.append(child.evaluate(context))
            else:
                results.append(
                    OracleResult(
                        oracle_id=child.oracle_id,
                        metric_id=child.metric_id,
                        execution_status=ExecutionStatus.SKIPPED,
                        metric_status=MetricStatus.NA,
                        score_role=child.score_role,
                        confidence=1.0,
                        severity=Severity.INFO,
                        version=child.version,
                        error_code="UNSUPPORTED_SCENE",
                        error_message="Oracle does not support this scene",
                    )
                )
        return tuple(results)


def load_presentation(context: object, adapter: PptxAdapter) -> ParsedPresentation:
    artifacts = getattr(context, "artifacts", None)
    if isinstance(artifacts, Mapping):
        for key in (_PARSED_KEY, "parsed_presentation", "presentation"):
            value = artifacts.get(key)
            if isinstance(value, ParsedPresentation):
                return value

    memo = _memo(context)
    value = memo.get(_PARSED_KEY)
    if isinstance(value, ParsedPresentation):
        return value
    prior_error = memo.get(_PARSE_ERROR_KEY)
    if isinstance(prior_error, Exception):
        raise prior_error

    case = _case(context)
    source = getattr(case, "pptx_path", None)
    if not source:
        error = PptxAdapterError("evaluation case does not contain pptx_path")
        memo[_PARSE_ERROR_KEY] = error
        raise error
    try:
        parsed = adapter.parse(source)
    except PptxAdapterError as exc:
        memo[_PARSE_ERROR_KEY] = exc
        raise
    memo[_PARSED_KEY] = parsed
    return parsed


def _case(context: object) -> object:
    return getattr(context, "case", context)


def case_metadata(context: object) -> Mapping[str, Any]:
    value = getattr(_case(context), "metadata", {})
    return value if isinstance(value, Mapping) else {}


def _memo(context: object) -> MutableMapping[str, Any]:
    value = getattr(context, "memo", None)
    if isinstance(value, MutableMapping):
        return value
    # Direct EvalCase calls remain supported for unit tests and plugin authors.
    local = getattr(context, "_ppt_eval_memo", None)
    if isinstance(local, MutableMapping):
        return local
    return {}


def evidence(
    metric_id: str,
    key: str,
    kind: str,
    message: str,
    *,
    page_number: int | None = None,
    object_id: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    source_uri: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Evidence:
    digest = hashlib.sha256(
        f"{metric_id}|{key}|{page_number}|{object_id}|{source_uri}".encode("utf-8")
    ).hexdigest()[:16]
    return Evidence(
        evidence_id=f"ev-{digest}",
        kind=kind,
        message=message,
        page_number=page_number,
        object_id=object_id,
        bbox=bbox,
        source_uri=source_uri,
        payload=payload or {},
    )


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _score_severity(score: float) -> Severity:
    if score < 0.35:
        return Severity.MAJOR
    if score < 0.70:
        return Severity.MINOR
    return Severity.INFO


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())


_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "this",
    "that",
    "from",
    "page",
    "slide",
    "ppt",
    "以及",
    "进行",
    "一个",
    "需要",
    "可以",
    "内容",
    "页面",
    "幻灯片",
    "展示",
}


def text_tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}|\d+(?:\.\d+)?%?", lowered)
        if token not in _STOPWORDS
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens - _STOPWORDS


def token_recall(needle: str, haystack: str) -> float:
    normalized_needle = normalize_text(needle)
    normalized_haystack = normalize_text(haystack)
    if normalized_needle and normalized_needle in normalized_haystack:
        return 1.0
    required = text_tokens(needle)
    if not required:
        return 1.0 if normalized_needle in normalized_haystack else 0.0
    return len(required & text_tokens(haystack)) / len(required)


def read_materials(materials: Sequence[str], maximum_bytes: int = 2_000_000) -> str:
    chunks: list[str] = []
    remaining = maximum_bytes
    for material in materials:
        if remaining <= 0:
            break
        path = Path(material)
        try:
            is_file = path.is_file()
        except OSError:
            is_file = False
        if is_file:
            try:
                data = path.read_bytes()[:remaining]
                text = data.decode("utf-8", errors="replace")
            except OSError:
                text = material
        else:
            text = material
        encoded = text.encode("utf-8")[:remaining]
        chunks.append(encoded.decode("utf-8", errors="ignore"))
        remaining -= len(encoded)
    return "\n".join(chunks).strip()


def locate_text(
    presentation: ParsedPresentation, fragment: str
) -> tuple[int | None, str | None, tuple[float, float, float, float] | None]:
    normalized = normalize_text(fragment)
    for slide in presentation.slides:
        for item in slide.visible_objects:
            if normalized and normalized in normalize_text(item.text):
                return slide.page_number, item.object_id, item.bbox.as_tuple()
    return None, None, None
