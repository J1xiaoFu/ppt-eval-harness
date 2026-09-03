"""Vendor-neutral contracts for expensive LLM/VLM audits.

Provider SDKs belong in infrastructure adapters.  They receive an immutable,
JSON-friendly request and must translate their vendor response into the strict
mapping accepted by :class:`ModelAuditResponse`.  Keeping validation here means
that free-form model output can never directly enter scoring or routing.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ppt_eval.domain.models import Evidence

MODEL_AUDIT_SCHEMA_VERSION = "1.0"


class ModelAuditContractError(ValueError):
    """A provider response does not satisfy the model-audit contract."""


class ModelAuditProviderError(RuntimeError):
    """Safe provider failure with optional auditable usage telemetry."""

    def __init__(
        self,
        message: str,
        *,
        audit_metadata: Mapping[str, Any] | None = None,
        cost: float = 0.0,
    ) -> None:
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost))
            or float(cost) < 0.0
        ):
            raise ValueError("provider error cost must be a non-negative finite number")
        super().__init__(message)
        self.audit_metadata = dict(audit_metadata or {})
        self.cost = float(cost)


class ModelAuditModality(str, Enum):
    LLM = "LLM"
    VLM = "VLM"


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """Versioned trusted instructions sent separately from untrusted deck data."""

    prompt_id: str
    version: str
    instructions: str

    def __post_init__(self) -> None:
        _require_text(self.prompt_id, "prompt_id")
        _require_text(self.version, "prompt version")
        _require_text(self.instructions, "prompt instructions")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()

    def reference(self) -> Mapping[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ModelImageInput:
    """A rendered-slide reference; the provider adapter decides how to upload it."""

    page_number: int
    uri: str
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise ValueError("image page_number must be a positive integer")
        _require_text(self.uri, "image uri")
        media_type = _require_text(self.media_type, "image media_type")
        if not media_type.startswith("image/"):
            raise ValueError("image media_type must use the image/* media type")
        _require_sha256(self.sha256, "image sha256")

    @classmethod
    def from_path(cls, path: str | Path, *, page_number: int) -> "ModelImageInput":
        source = Path(path)
        data = source.read_bytes()
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return cls(
            page_number=page_number,
            uri=str(source),
            media_type=media_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ModelAuditRequest:
    """Provider-neutral request containing trusted policy and untrusted inputs."""

    audit_id: str
    metric_id: str
    modality: ModelAuditModality
    prompt: PromptSpec
    case_id: str
    scene: str
    slides: tuple[Mapping[str, Any], ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    images: tuple[ModelImageInput, ...] = ()
    schema_version: str = MODEL_AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.audit_id, "audit_id")
        _require_text(self.metric_id, "metric_id")
        _require_text(self.case_id, "case_id")
        _require_text(self.scene, "scene")
        if not isinstance(self.modality, ModelAuditModality):
            object.__setattr__(self, "modality", ModelAuditModality(self.modality))
        if not self.slides:
            raise ValueError("model audit requests require at least one slide")
        pages: list[int] = []
        for slide in self.slides:
            if not isinstance(slide, Mapping):
                raise ValueError("each model audit slide must be a mapping")
            pages.append(_positive_int(slide.get("page_number"), "slide.page_number"))
        if pages != list(range(1, len(self.slides) + 1)):
            raise ValueError("model audit slide pages must be ordered and contiguous")
        _json_mapping(self.context, "request.context")
        if self.modality == ModelAuditModality.VLM and not self.images:
            raise ValueError("VLM audit requests require rendered slide images")
        if any(item.page_number not in pages for item in self.images):
            raise ValueError("model audit images must reference a request slide")

    @property
    def fingerprint(self) -> str:
        payload = self.to_mapping(include_instructions=True)
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_mapping(self, *, include_instructions: bool = True) -> Mapping[str, Any]:
        """Return the stable payload provider adapters can serialize to an API."""

        prompt: dict[str, Any] = dict(self.prompt.reference())
        if include_instructions:
            prompt["instructions"] = self.prompt.instructions
        return {
            "schema_version": self.schema_version,
            "audit_id": self.audit_id,
            "metric_id": self.metric_id,
            "modality": self.modality.value,
            "prompt": prompt,
            "case": {
                "case_id": self.case_id,
                "scene": self.scene,
                **dict(self.context),
            },
            "slides": [dict(slide) for slide in self.slides],
            "images": [
                {
                    "page_number": item.page_number,
                    "uri": item.uri,
                    "media_type": item.media_type,
                    "sha256": item.sha256,
                }
                for item in self.images
            ],
        }


class ModelAuditProvider(Protocol):
    """Port implemented later by an OpenAI, Anthropic, Gemini, or local adapter."""

    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    provider: str
    model_id: str
    version: str

    @classmethod
    def from_mapping(cls, value: object) -> "ModelIdentity":
        payload = _strict_mapping(
            value,
            "model",
            required=frozenset(("provider", "model_id", "version")),
        )
        return cls(
            provider=_require_text(payload["provider"], "model.provider"),
            model_id=_require_text(payload["model_id"], "model.model_id"),
            version=_require_text(payload["version"], "model.version"),
        )

    def to_mapping(self) -> Mapping[str, str]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PromptReference:
    prompt_id: str
    version: str
    sha256: str

    @classmethod
    def from_mapping(cls, value: object) -> "PromptReference":
        payload = _strict_mapping(
            value,
            "prompt",
            required=frozenset(("prompt_id", "version", "sha256")),
        )
        return cls(
            prompt_id=_require_text(payload["prompt_id"], "prompt.prompt_id"),
            version=_require_text(payload["version"], "prompt.version"),
            sha256=_require_sha256(payload["sha256"], "prompt.sha256"),
        )

    def validate_matches(self, prompt: PromptSpec) -> None:
        expected = (prompt.prompt_id, prompt.version, prompt.sha256)
        actual = (self.prompt_id, self.version, self.sha256)
        if actual != expected:
            raise ModelAuditContractError(
                "provider prompt reference does not match the requested prompt"
            )

    def to_mapping(self) -> Mapping[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cost: float
    image_tokens: int | None = None
    cached_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    request_bytes: int | None = None
    cost_known: bool | None = None

    @classmethod
    def from_mapping(cls, value: object) -> "ModelUsage":
        payload = _strict_mapping(
            value,
            "usage",
            required=frozenset(("input_tokens", "output_tokens", "cost")),
            optional=frozenset(
                (
                    "image_tokens",
                    "cached_tokens",
                    "cache_creation_input_tokens",
                    "request_bytes",
                    "cost_known",
                )
            ),
        )
        return cls(
            input_tokens=_nonnegative_int(payload["input_tokens"], "usage.input_tokens"),
            output_tokens=_nonnegative_int(payload["output_tokens"], "usage.output_tokens"),
            cost=_bounded_number(payload["cost"], "usage.cost", minimum=0.0),
            image_tokens=_optional_nonnegative_int(
                payload.get("image_tokens"), "usage.image_tokens"
            ),
            cached_tokens=_optional_nonnegative_int(
                payload.get("cached_tokens"), "usage.cached_tokens"
            ),
            cache_creation_input_tokens=_optional_nonnegative_int(
                payload.get("cache_creation_input_tokens"),
                "usage.cache_creation_input_tokens",
            ),
            request_bytes=_optional_nonnegative_int(
                payload.get("request_bytes"), "usage.request_bytes"
            ),
            cost_known=_optional_bool(payload.get("cost_known"), "usage.cost_known"),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_mapping(self) -> Mapping[str, int | float | bool]:
        result: dict[str, int | float | bool] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
        }
        optional_values: tuple[tuple[str, int | bool | None], ...] = (
            ("image_tokens", self.image_tokens),
            ("cached_tokens", self.cached_tokens),
            ("cache_creation_input_tokens", self.cache_creation_input_tokens),
            ("request_bytes", self.request_bytes),
            ("cost_known", self.cost_known),
        )
        result.update(
            (key, value) for key, value in optional_values if value is not None
        )
        return result


@dataclass(frozen=True, slots=True)
class ModelAuditEvidence:
    evidence_id: str
    kind: str
    message: str
    page_number: int | None = None
    object_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_uri: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        slide_count: int,
        allowed_page_numbers: frozenset[int] | None = None,
        known_objects: Mapping[int, frozenset[str]],
        known_source_uris: frozenset[str],
    ) -> "ModelAuditEvidence":
        payload = _strict_mapping(
            value,
            "evidence item",
            required=frozenset(("evidence_id", "kind", "message")),
            optional=frozenset(
                ("page_number", "object_id", "bbox", "source_uri", "payload")
            ),
        )
        page_number = payload.get("page_number")
        if page_number is not None:
            page_number = _positive_int(page_number, "evidence.page_number")
            if page_number > slide_count:
                raise ModelAuditContractError(
                    f"evidence.page_number {page_number} exceeds slide count {slide_count}"
                )
            if (
                allowed_page_numbers is not None
                and page_number not in allowed_page_numbers
            ):
                allowed = ", ".join(
                    str(item) for item in sorted(allowed_page_numbers)
                )
                raise ModelAuditContractError(
                    "evidence.page_number "
                    f"{page_number} was not supplied as visual evidence; "
                    f"allowed rendered pages: [{allowed}]"
                )
        object_id = _optional_text(payload.get("object_id"), "evidence.object_id")
        source_uri = _optional_text(payload.get("source_uri"), "evidence.source_uri")
        if page_number is None and source_uri is None:
            raise ModelAuditContractError(
                "each evidence item must locate a slide or a source_uri"
            )
        if source_uri is not None and source_uri not in known_source_uris:
            raise ModelAuditContractError(
                f"evidence.source_uri {source_uri!r} is not present in the audit request"
            )
        if object_id is not None:
            if page_number is None:
                raise ModelAuditContractError(
                    "evidence.object_id requires evidence.page_number"
                )
            if object_id not in known_objects.get(page_number, frozenset()):
                raise ModelAuditContractError(
                    f"evidence.object_id {object_id!r} is not present on slide {page_number}"
                )
        bbox = _optional_bbox(payload.get("bbox"))
        detail = payload.get("payload", {})
        detail = _json_mapping(detail, "evidence.payload")
        return cls(
            evidence_id=_require_text(payload["evidence_id"], "evidence.evidence_id"),
            kind=_require_text(payload["kind"], "evidence.kind"),
            message=_require_text(payload["message"], "evidence.message"),
            page_number=page_number,
            object_id=object_id,
            bbox=bbox,
            source_uri=source_uri,
            payload=dict(detail),
        )

    def to_domain(self) -> Evidence:
        return Evidence(
            evidence_id=self.evidence_id,
            kind=self.kind,
            message=self.message,
            page_number=self.page_number,
            object_id=self.object_id,
            bbox=self.bbox,
            source_uri=self.source_uri,
            payload=dict(self.payload),
        )


@dataclass(frozen=True, slots=True)
class ModelAuditResponse:
    score: float
    confidence: float
    model: ModelIdentity
    prompt: PromptReference
    usage: ModelUsage
    evidence: tuple[ModelAuditEvidence, ...]
    response_fingerprint: str

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        request: ModelAuditRequest,
    ) -> "ModelAuditResponse":
        payload = _strict_mapping(
            value,
            "model audit response",
            required=frozenset(
                ("score", "confidence", "model", "prompt", "usage", "evidence")
            ),
        )
        score = _bounded_number(payload["score"], "score", minimum=0.0, maximum=1.0)
        confidence = _bounded_number(
            payload["confidence"], "confidence", minimum=0.0, maximum=1.0
        )
        model = ModelIdentity.from_mapping(payload["model"])
        prompt = PromptReference.from_mapping(payload["prompt"])
        prompt.validate_matches(request.prompt)
        usage = ModelUsage.from_mapping(payload["usage"])

        raw_evidence = payload["evidence"]
        if isinstance(raw_evidence, (str, bytes)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise ModelAuditContractError("evidence must be a non-empty sequence")
        known_objects = {
            int(slide["page_number"]): frozenset(
                str(item["object_id"])
                for item in slide.get("objects", ())
                if isinstance(item, Mapping) and item.get("object_id")
            )
            for slide in request.slides
        }
        known_source_uris = frozenset(
            str(item)
            for item in (
                *request.context.get("source_uris", ()),
                *request.context.get("asset_uris", ()),
                *(image.uri for image in request.images),
            )
            if str(item)
        )
        allowed_page_numbers = (
            frozenset(image.page_number for image in request.images)
            if request.modality == ModelAuditModality.VLM
            else None
        )
        evidence_items = tuple(
            ModelAuditEvidence.from_mapping(
                item,
                slide_count=len(request.slides),
                allowed_page_numbers=allowed_page_numbers,
                known_objects=known_objects,
                known_source_uris=known_source_uris,
            )
            for item in raw_evidence
        )
        if not evidence_items:
            raise ModelAuditContractError("evidence must contain at least one item")
        evidence_ids = [item.evidence_id for item in evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ModelAuditContractError("evidence_id values must be unique")
        response_fingerprint = _canonical_fingerprint(payload)
        return cls(
            score,
            confidence,
            model,
            prompt,
            usage,
            evidence_items,
            response_fingerprint,
        )


def _strict_mapping(
    value: object,
    label: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelAuditContractError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ModelAuditContractError(f"{label} keys must be strings")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ModelAuditContractError(
            f"{label} is missing required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ModelAuditContractError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelAuditContractError(f"{label} must be a non-blank string")
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, label)


def _require_sha256(value: object, label: str) -> str:
    text = _require_text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ModelAuditContractError(f"{label} must be a 64-character hexadecimal digest")
    return text


def _bounded_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelAuditContractError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ModelAuditContractError(f"{label} must be a finite number")
    if result < minimum or (maximum is not None and result > maximum):
        upper = "" if maximum is None else f" and {maximum}"
        raise ModelAuditContractError(f"{label} must be between {minimum}{upper}")
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelAuditContractError(f"{label} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _optional_bool(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ModelAuditContractError(f"{label} must be a boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ModelAuditContractError(f"{label} must be positive")
    return result


def _optional_bbox(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ModelAuditContractError("evidence.bbox must contain four coordinates")
    coordinates = (
        _bounded_number(value[0], "evidence.bbox coordinate", minimum=0.0, maximum=1.0),
        _bounded_number(value[1], "evidence.bbox coordinate", minimum=0.0, maximum=1.0),
        _bounded_number(value[2], "evidence.bbox coordinate", minimum=0.0, maximum=1.0),
        _bounded_number(value[3], "evidence.bbox coordinate", minimum=0.0, maximum=1.0),
    )
    x, y, width, height = coordinates
    if x + width > 1.000001 or y + height > 1.000001:
        raise ModelAuditContractError("evidence.bbox must stay within the slide")
    return coordinates


def _json_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelAuditContractError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ModelAuditContractError(f"{label} keys must be strings")
    return {str(key): _json_value(item, f"{label}.{key}") for key, item in value.items()}


def _json_value(value: object, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelAuditContractError(f"{label} must not contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        return _json_mapping(value, label)
    if isinstance(value, (tuple, list)):
        return [_json_value(item, f"{label}[]") for item in value]
    raise ModelAuditContractError(f"{label} must contain only JSON-compatible values")


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    normalized = _json_mapping(value, "model audit response")
    canonical = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "MODEL_AUDIT_SCHEMA_VERSION",
    "ModelAuditContractError",
    "ModelAuditEvidence",
    "ModelAuditModality",
    "ModelAuditProvider",
    "ModelAuditProviderError",
    "ModelAuditRequest",
    "ModelAuditResponse",
    "ModelIdentity",
    "ModelImageInput",
    "ModelUsage",
    "PromptReference",
    "PromptSpec",
]
