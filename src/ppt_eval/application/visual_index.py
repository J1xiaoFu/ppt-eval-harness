"""Dependency-light construction of the Profile 8.4 ``VisualPageIndex``.

The builder scans every parsed page and every supplied low-resolution render.
It performs deterministic feature extraction and clustering only; it never
assigns quality scores and never copies a medoid's observations to another page.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

from ppt_eval.adapters.model_audits import ModelImageInput
from ppt_eval.adapters.pptx import ParsedPresentation, ParsedSlide, SlideObject
from ppt_eval.domain.models import AtomicObservation
from ppt_eval.domain.visual import (
    VisualCluster,
    VisualPageFeatures,
    VisualPageIndex,
    rendered_page_set_sha256,
)

_MEDIA_KINDS = frozenset({"picture", "linked_picture", "media", "image"})
_DATA_KINDS = frozenset({"chart", "table"})
_CLOSING_RE = re.compile(
    r"(?:thank(?:\s+you)?|questions?|q\s*&\s*a|contact|the\s+end|谢谢|感谢|提问|联系我们)",
    re.IGNORECASE,
)
_TEXT_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]")
_PAGE_UNIT_RE = re.compile(r"(?:^|[/|])(?:page|slide):?(\d+)(?:$|[/|])", re.IGNORECASE)
_SEVERITY_RANK = {"INFO": 0, "MINOR": 1, "MAJOR": 2, "CRITICAL": 3}
_GRID_COLUMNS = 6
_GRID_ROWS = 4
_LAYOUT_DISTANCE_THRESHOLD = 0.34
_ASSET_DISTANCE_THRESHOLD = 0.42
_MAX_INDEX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_INDEX_IMAGE_PIXELS = 25_000_000
_OBJECT_PIXEL_PARITY_PROXY_VERSION = "1.0.0"
_INDEX_MEDIA_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


@dataclass(frozen=True, slots=True)
class _RenderedFeatures:
    page_phash: str
    image_phashes: tuple[str, ...]
    color_histogram: tuple[float, ...]
    visual_entropy: float
    edge_density: float


@dataclass(frozen=True, slots=True)
class _RuleSummary:
    severity: str = "INFO"
    risk_metric_ids: tuple[str, ...] = ()
    unobservable_metric_ids: tuple[str, ...] = ()
    role: str | None = None


class VisualPageIndexBuilder:
    """Build a complete, deterministic routing index for one presentation."""

    def __init__(
        self,
        *,
        layout_distance_threshold: float = _LAYOUT_DISTANCE_THRESHOLD,
        asset_distance_threshold: float = _ASSET_DISTANCE_THRESHOLD,
    ) -> None:
        for label, value in (
            ("layout_distance_threshold", layout_distance_threshold),
            ("asset_distance_threshold", asset_distance_threshold),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{label} must be a finite value in [0, 1]")
        self.layout_distance_threshold = float(layout_distance_threshold)
        self.asset_distance_threshold = float(asset_distance_threshold)

    def build(
        self,
        presentation: ParsedPresentation,
        *,
        rendered_images: Mapping[int, str | Path | ModelImageInput] | None = None,
        observations: Sequence[AtomicObservation] = (),
        ocr_text_by_page: Mapping[int, str | None] | None = None,
    ) -> VisualPageIndex:
        """Index every page, with OCR explicitly represented as available or N/A.

        ``rendered_images`` may contain a subset while a renderer is degraded;
        absent or unreadable pages retain structural features and are recorded in
        ``warnings``.  ``ocr_text_by_page=None`` means no OCR adapter is installed,
        not an empty OCR result.
        """

        if not presentation.slides:
            raise ValueError("VisualPageIndexBuilder requires at least one slide")
        slides = tuple(sorted(presentation.slides, key=lambda item: item.page_number))
        if tuple(item.page_number for item in slides) != tuple(range(1, len(slides) + 1)):
            raise ValueError("presentation slides must cover contiguous one-based page numbers")
        renders = dict(rendered_images or {})
        for page_number, value in renders.items():
            if isinstance(value, ModelImageInput):
                if value.page_number != page_number:
                    raise ValueError(
                        "rendered image mapping key must match ModelImageInput.page_number"
                    )
        unknown_render_pages = set(renders) - set(range(1, len(slides) + 1))
        if unknown_render_pages:
            raise ValueError("rendered_images contains pages outside the presentation")
        if ocr_text_by_page is not None:
            unknown_ocr_pages = set(ocr_text_by_page) - set(range(1, len(slides) + 1))
            if unknown_ocr_pages:
                raise ValueError("ocr_text_by_page contains pages outside the presentation")

        summaries = _summarize_rules(observations, len(slides))
        warnings: set[str] = set()
        rendered_pages: list[int] = []
        frozen_renders: dict[int, ModelImageInput] = {}
        preliminary: list[VisualPageFeatures] = []
        terms_by_page: dict[int, frozenset[str]] = {}
        for slide in slides:
            rule_summary = summaries.get(slide.page_number, _RuleSummary())
            visible_objects = slide.visible_objects
            images = tuple(item for item in visible_objects if item.kind in _MEDIA_KINDS)
            image_input = renders.get(slide.page_number)
            rendered: _RenderedFeatures | None = None
            if image_input is not None:
                try:
                    if not isinstance(image_input, ModelImageInput):
                        image_input = ModelImageInput.from_path(
                            image_input,
                            page_number=slide.page_number,
                        )
                    frozen_renders[slide.page_number] = image_input
                    rendered = _analyze_render(image_input, images)
                    rendered_pages.append(slide.page_number)
                except (OSError, ValueError, UnidentifiedImageError):
                    warnings.add(f"render_unreadable:page:{slide.page_number}")
            elif renders:
                warnings.add(f"render_missing:page:{slide.page_number}")

            visible_text = slide.visible_text.strip()
            terms = _content_terms(visible_text)
            terms_by_page[slide.page_number] = terms
            role = rule_summary.role or _classify_role(slide, len(slides))
            text_count = len("".join(visible_text.split()))
            ocr_value = None if ocr_text_by_page is None else ocr_text_by_page.get(slide.page_number)
            ocr_count = None if ocr_value is None else len("".join(str(ocr_value).split()))
            image_text_dense = (
                None
                if ocr_count is None
                else bool(images and ocr_count >= max(80, round(text_count * 1.75 + 40)))
            )
            image_area = _union_area(tuple(item.bbox.as_tuple() for item in images))
            semantic_objects = tuple(
                item
                for item in visible_objects
                if item.kind not in {"group", "connector", "line"} and item.bbox.area > 1e-6
            )
            object_area = _union_area(tuple(item.bbox.as_tuple() for item in semantic_objects))
            text_objects = tuple(item for item in semantic_objects if item.visible_text)
            text_area = _union_area(tuple(item.bbox.as_tuple() for item in text_objects))
            parity_anomaly, parity_metadata = _object_pixel_parity_proxy(
                rendered,
                text_character_count=text_count,
                text_object_count=len(text_objects),
                text_area_ratio=text_area,
                object_area_ratio=object_area,
            )
            has_editable_text = any(item.editable and item.visible_text for item in semantic_objects)
            raster_only = bool(
                any(item.bbox.area >= 0.65 for item in images)
                and not has_editable_text
                and not any(item.kind in _DATA_KINDS for item in semantic_objects)
            )
            missing_alt = sum(not _meaningful_alt_text(item) for item in images)
            missing_caption = sum(
                not _has_caption(item, visible_objects) for item in images
            )
            silhouette = _layout_silhouette(visible_objects)
            silhouette_bytes = bytes(silhouette)
            asset_hashes = tuple(
                sorted(
                    {
                        str(item.media_sha256).lower()
                        for item in images
                        if item.media_sha256 is not None
                        and re.fullmatch(r"[0-9a-fA-F]{64}", str(item.media_sha256))
                    }
                )
            )
            preliminary.append(
                VisualPageFeatures(
                    page_number=slide.page_number,
                    slide_id=slide.slide_id,
                    role=role,
                    text_character_count=text_count,
                    text_token_count=len(_TEXT_TOKEN_RE.findall(visible_text)),
                    visible_object_count=len(visible_objects),
                    object_density=min(1.0, len(visible_objects) / 24.0),
                    object_area_ratio=object_area,
                    image_count=len(images),
                    image_area_ratio=image_area,
                    page_phash=None if rendered is None else rendered.page_phash,
                    image_phashes=() if rendered is None else rendered.image_phashes,
                    asset_hashes=asset_hashes,
                    layout_silhouette=silhouette,
                    layout_silhouette_hash=hashlib.sha256(silhouette_bytes).hexdigest(),
                    content_fingerprint=hashlib.sha256(
                        _normalized_content(visible_text).encode("utf-8")
                    ).hexdigest(),
                    color_histogram=() if rendered is None else rendered.color_histogram,
                    visual_entropy=None if rendered is None else rendered.visual_entropy,
                    edge_density=None if rendered is None else rendered.edge_density,
                    object_pixel_parity_anomaly=parity_anomaly,
                    raster_only=raster_only,
                    image_dominant=raster_only or image_area >= 0.50,
                    missing_alt_text_count=missing_alt,
                    missing_caption_count=missing_caption,
                    ocr_text_character_count=ocr_count,
                    image_text_dense=image_text_dense,
                    rule_severity=rule_summary.severity,
                    rule_risk_metric_ids=rule_summary.risk_metric_ids,
                    unobservable_metric_ids=rule_summary.unobservable_metric_ids,
                    metadata={
                        "ocr_status": "N/A" if ocr_count is None else "AVAILABLE",
                        "feature_source": "object-tree+render" if rendered is not None else "object-tree",
                        "object_pixel_parity_proxy": parity_metadata,
                    },
                )
            )

        duplicates = _duplicate_asset_hashes(preliminary)
        preliminary = [
            replace(
                page,
                duplicate_asset_hashes=tuple(
                    sorted(asset_hash for asset_hash in page.asset_hashes if asset_hash in duplicates)
                ),
            )
            for page in preliminary
        ]
        layout_clusters = _build_clusters(
            preliminary,
            kind="layout_style",
            threshold=self.layout_distance_threshold,
            distance=_layout_distance,
        )
        asset_clusters = _build_clusters(
            preliminary,
            kind="asset_content",
            threshold=self.asset_distance_threshold,
            distance=lambda left, right: _asset_distance(
                left,
                right,
                terms_by_page[left.page_number],
                terms_by_page[right.page_number],
            ),
        )
        layout_membership = {
            page_number: cluster
            for cluster in layout_clusters
            for page_number in cluster.member_page_numbers
        }
        asset_membership = {
            page_number: cluster
            for cluster in asset_clusters
            for page_number in cluster.member_page_numbers
        }
        pages = tuple(
            replace(
                page,
                layout_cluster_id=layout_membership[page.page_number].cluster_id,
                asset_cluster_id=asset_membership[page.page_number].cluster_id,
                layout_outlier=layout_membership[page.page_number].is_outlier,
                asset_outlier=asset_membership[page.page_number].is_outlier,
            )
            for page in preliminary
        )
        page_set_digest = (
            rendered_page_set_sha256(
                presentation.source_sha256.lower(),
                {page: image.sha256 for page, image in frozen_renders.items()},
            )
            if tuple(sorted(frozen_renders)) == tuple(range(1, len(slides) + 1))
            else None
        )
        return VisualPageIndex(
            deck_sha256=presentation.source_sha256.lower(),
            pages=pages,
            layout_clusters=layout_clusters,
            asset_clusters=asset_clusters,
            rendered_page_set_sha256=page_set_digest,
            rendered_page_numbers=tuple(rendered_pages),
            ocr_available=ocr_text_by_page is not None,
            warnings=tuple(sorted(warnings)),
        )


def build_visual_page_index(
    presentation: ParsedPresentation,
    *,
    rendered_images: Mapping[int, str | Path | ModelImageInput] | None = None,
    observations: Sequence[AtomicObservation] = (),
    ocr_text_by_page: Mapping[int, str | None] | None = None,
) -> VisualPageIndex:
    """Convenience entry point using the versioned default feature policy."""

    return VisualPageIndexBuilder().build(
        presentation,
        rendered_images=rendered_images,
        observations=observations,
        ocr_text_by_page=ocr_text_by_page,
    )


def _summarize_rules(
    observations: Sequence[AtomicObservation],
    slide_count: int,
) -> dict[int, _RuleSummary]:
    severities: dict[int, str] = {}
    risks: dict[int, set[str]] = {}
    unobservable: dict[int, set[str]] = {}
    roles: dict[int, str] = {}
    for observation in observations:
        pages = _observation_pages(observation, slide_count)
        if not pages:
            continue
        severity = "CRITICAL" if observation.critical else observation.severity.value
        if observation.metric_status.value == "FAIL" and _SEVERITY_RANK[severity] < 2:
            severity = "MAJOR"
        observability = observation.metadata.get("observability")
        observability_low = (
            not isinstance(observability, bool)
            and isinstance(observability, (int, float))
            and math.isfinite(float(observability))
            and float(observability) < 0.60
        )
        is_unobservable = (
            observation.metric_status.value in {"NA", "ERROR"}
            or observation.execution_status.value != "SUCCESS"
            or observability_low
        )
        for page_number in pages:
            current = severities.get(page_number, "INFO")
            if _SEVERITY_RANK[severity] > _SEVERITY_RANK[current]:
                severities[page_number] = severity
            if _SEVERITY_RANK[severity] >= 2:
                risks.setdefault(page_number, set()).add(observation.metric_id)
            if is_unobservable:
                unobservable.setdefault(page_number, set()).add(observation.metric_id)
            if observation.metric_id == "slide_role" and isinstance(observation.raw_value, str):
                value = observation.raw_value.strip()
                if value:
                    roles[page_number] = value
    return {
        page_number: _RuleSummary(
            severity=severities.get(page_number, "INFO"),
            risk_metric_ids=tuple(sorted(risks.get(page_number, set()))),
            unobservable_metric_ids=tuple(sorted(unobservable.get(page_number, set()))),
            role=roles.get(page_number),
        )
        for page_number in range(1, slide_count + 1)
    }


def _observation_pages(observation: AtomicObservation, slide_count: int) -> tuple[int, ...]:
    pages = {
        item.page_number
        for item in observation.evidence
        if item.page_number is not None and 1 <= item.page_number <= slide_count
    }
    match = _PAGE_UNIT_RE.search(observation.unit_key)
    if match is not None:
        page_number = int(match.group(1))
        if 1 <= page_number <= slide_count:
            pages.add(page_number)
    return tuple(sorted(pages))


def _classify_role(slide: ParsedSlide, slide_count: int) -> str:
    if slide.page_number == 1:
        return "cover"
    text = slide.visible_text.strip()
    if slide.page_number == slide_count and _CLOSING_RE.search(text):
        return "closing"
    if any(item.kind in _DATA_KINDS for item in slide.visible_objects):
        return "data"
    text_objects = [item for item in slide.visible_objects if item.visible_text]
    if text and len("".join(text.split())) <= 90 and 1 <= len(text_objects) <= 2:
        return "section"
    return "content"


def _normalized_content(value: str) -> str:
    return " ".join(token.lower() for token in _TEXT_TOKEN_RE.findall(value))


def _content_terms(value: str) -> frozenset[str]:
    normalized = _normalized_content(value)
    tokens = normalized.split()
    terms = set(tokens)
    compact_cjk = "".join(token for token in tokens if re.fullmatch(r"[\u3400-\u9fff]", token))
    terms.update(compact_cjk[index : index + 2] for index in range(max(0, len(compact_cjk) - 1)))
    return frozenset(term for term in terms if term)


def _meaningful_alt_text(item: SlideObject) -> bool:
    value = str(item.metadata.get("alt_text", "")).strip()
    if len(value) < 3:
        return False
    normalized = value.casefold()
    name = item.name.strip().casefold()
    if normalized == name or re.fullmatch(r"(?:picture|image|photo|graphic)\s*\d*", normalized):
        return False
    return True


def _has_caption(image: SlideObject, objects: Sequence[SlideObject]) -> bool:
    if str(image.metadata.get("caption", "")).strip():
        return True
    image_left = image.bbox.x
    image_right = image.bbox.x + image.bbox.width
    image_bottom = image.bbox.y + image.bbox.height
    for item in objects:
        if item.object_id == image.object_id or not item.visible_text:
            continue
        item_left = item.bbox.x
        item_right = item.bbox.x + item.bbox.width
        overlap = max(0.0, min(image_right, item_right) - max(image_left, item_left))
        horizontal_ratio = overlap / max(1e-9, min(image.bbox.width, item.bbox.width))
        near_below = image_bottom - 0.01 <= item.bbox.y <= image_bottom + 0.12
        lower_overlay = (
            image.bbox.y + image.bbox.height * 0.72 <= item.bbox.y <= image_bottom
        )
        if horizontal_ratio >= 0.30 and (near_below or lower_overlay):
            return True
    return False


def _layout_silhouette(objects: Sequence[SlideObject]) -> tuple[int, ...]:
    cells = [0] * (_GRID_COLUMNS * _GRID_ROWS)
    for item in objects:
        if item.hidden or item.bbox.area <= 1e-8:
            continue
        bit = _object_bit(item)
        left = max(0, min(_GRID_COLUMNS - 1, math.floor(item.bbox.x * _GRID_COLUMNS)))
        top = max(0, min(_GRID_ROWS - 1, math.floor(item.bbox.y * _GRID_ROWS)))
        right_coordinate = max(0.0, min(1.0, item.bbox.x + item.bbox.width) - 1e-9)
        bottom_coordinate = max(0.0, min(1.0, item.bbox.y + item.bbox.height) - 1e-9)
        right = max(0, min(_GRID_COLUMNS - 1, math.floor(right_coordinate * _GRID_COLUMNS)))
        bottom = max(0, min(_GRID_ROWS - 1, math.floor(bottom_coordinate * _GRID_ROWS)))
        if right < left or bottom < top:
            continue
        for row in range(top, bottom + 1):
            for column in range(left, right + 1):
                cells[row * _GRID_COLUMNS + column] |= bit
    return tuple(cells)


def _object_bit(item: SlideObject) -> int:
    if item.kind in _MEDIA_KINDS:
        return 2
    if item.kind in _DATA_KINDS:
        return 4
    if item.visible_text:
        return 1
    return 8


def _union_area(boxes: Sequence[tuple[float, float, float, float]]) -> float:
    rectangles = []
    for x, y, width, height in boxes:
        left = max(0.0, min(1.0, x))
        top = max(0.0, min(1.0, y))
        right = max(0.0, min(1.0, x + width))
        bottom = max(0.0, min(1.0, y + height))
        if right > left and bottom > top:
            rectangles.append((left, top, right, bottom))
    if not rectangles:
        return 0.0
    x_values = sorted({value for rectangle in rectangles for value in (rectangle[0], rectangle[2])})
    area = 0.0
    for left, right in zip(x_values, x_values[1:]):
        intervals = sorted(
            (top, bottom)
            for rect_left, top, rect_right, bottom in rectangles
            if rect_left < right and rect_right > left
        )
        covered = 0.0
        if intervals:
            current_top, current_bottom = intervals[0]
            for top, bottom in intervals[1:]:
                if top <= current_bottom:
                    current_bottom = max(current_bottom, bottom)
                else:
                    covered += current_bottom - current_top
                    current_top, current_bottom = top, bottom
            covered += current_bottom - current_top
        area += (right - left) * covered
    return min(1.0, max(0.0, area))


def _analyze_render(
    image_input: ModelImageInput,
    images: Sequence[SlideObject],
) -> _RenderedFeatures:
    path = Path(image_input.uri)
    if not path.is_file():
        raise OSError("rendered page is unavailable")
    if path.stat().st_size <= 0 or path.stat().st_size > _MAX_INDEX_IMAGE_BYTES:
        raise ValueError("rendered page exceeds the safe byte limit")
    payload = path.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(),
        image_input.sha256.lower(),
    ):
        raise ValueError("rendered page failed frozen digest validation")
    with Image.open(io.BytesIO(payload)) as source:
        actual_media_type = _INDEX_MEDIA_BY_FORMAT.get(str(source.format or "").upper())
        declared_media_type = image_input.media_type.strip().lower()
        if declared_media_type == "image/jpg":
            declared_media_type = "image/jpeg"
        if actual_media_type is None or declared_media_type != actual_media_type:
            raise ValueError("rendered page media type does not match its content")
        if getattr(source, "n_frames", 1) != 1:
            raise ValueError("animated rendered pages are unsupported")
        if source.width * source.height > _MAX_INDEX_IMAGE_PIXELS:
            raise ValueError("rendered page exceeds the safe pixel limit")
        source.load()
        rgb = source.convert("RGB")
    if rgb.width < 1 or rgb.height < 1:
        raise ValueError("rendered page has invalid dimensions")
    sample = rgb.copy()
    sample.thumbnail((192, 108), Image.Resampling.BILINEAR)
    if sample.width < 2 or sample.height < 2:
        sample = sample.resize((2, 2), Image.Resampling.NEAREST)
    image_phashes = []
    for item in sorted(images, key=lambda value: value.object_id):
        left = max(0, min(sample.width - 1, round(item.bbox.x * sample.width)))
        top = max(0, min(sample.height - 1, round(item.bbox.y * sample.height)))
        right = max(left + 1, min(sample.width, round((item.bbox.x + item.bbox.width) * sample.width)))
        bottom = max(top + 1, min(sample.height, round((item.bbox.y + item.bbox.height) * sample.height)))
        if right > left and bottom > top:
            image_phashes.append(_difference_hash(sample.crop((left, top, right, bottom))))
    analysis = sample.resize((64, 36), Image.Resampling.BILINEAR)
    gray = analysis.convert("L")
    return _RenderedFeatures(
        page_phash=_difference_hash(sample),
        image_phashes=tuple(image_phashes),
        color_histogram=_color_histogram(analysis),
        visual_entropy=_visual_entropy(gray),
        edge_density=_edge_density(gray),
    )


def _object_pixel_parity_proxy(
    rendered: _RenderedFeatures | None,
    *,
    text_character_count: int,
    text_object_count: int,
    text_area_ratio: float,
    object_area_ratio: float,
) -> tuple[bool, Mapping[str, object]]:
    """Route only a near-certain object-tree/render disagreement.

    A flat, monochrome, minimal, or dark design is never suspicious by itself.
    The proxy requires a substantial editable text structure in the object tree
    *and* an almost information-free render.  It is deliberately too strict for
    scoring: the original page must still be inspected by ``render_integrity``.
    """

    substantial_text_structure = bool(
        text_character_count >= 160
        and text_object_count >= 3
        and text_area_ratio >= 0.18
        and object_area_ratio >= 0.30
    )
    render_nearly_uniform = bool(
        rendered is not None
        and rendered.visual_entropy <= 0.015
        and rendered.edge_density <= 0.001
    )
    anomaly = substantial_text_structure and render_nearly_uniform
    status = (
        "UNOBSERVABLE"
        if rendered is None
        else "ANOMALY_SUSPECTED"
        if anomaly
        else "NO_HIGH_CONFIDENCE_ANOMALY"
    )
    return anomaly, {
        "version": _OBJECT_PIXEL_PARITY_PROXY_VERSION,
        "status": status,
        "routing_only": True,
        "score_affecting": False,
        "text_character_count": text_character_count,
        "text_object_count": text_object_count,
        "text_area_ratio": round(text_area_ratio, 8),
        "object_area_ratio": round(object_area_ratio, 8),
        "visual_entropy": (
            None if rendered is None else round(rendered.visual_entropy, 8)
        ),
        "edge_density": (
            None if rendered is None else round(rendered.edge_density, 8)
        ),
        "thresholds": {
            "minimum_text_character_count": 160,
            "minimum_text_object_count": 3,
            "minimum_text_area_ratio": 0.18,
            "minimum_object_area_ratio": 0.30,
            "maximum_visual_entropy": 0.015,
            "maximum_edge_density": 0.001,
        },
    }


def _difference_hash(image: Image.Image) -> str:
    gray = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = list(gray.tobytes())
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(pixels[offset + column] >= pixels[offset + column + 1])
    return f"{value:016x}"


def _color_histogram(image: Image.Image) -> tuple[float, ...]:
    histogram = image.histogram()
    pixels = image.width * image.height
    bins = []
    for channel in range(3):
        channel_start = channel * 256
        raw = [
            sum(histogram[channel_start + offset : channel_start + offset + 32])
            for offset in range(0, 256, 32)
        ]
        normalized = [value / pixels for value in raw]
        # Make the last value the exact residual so the persisted channel sums
        # to one even after bounded decimal rounding.
        rounded = [round(value, 8) for value in normalized[:-1]]
        rounded.append(round(1.0 - sum(rounded), 8))
        bins.extend(rounded)
    return tuple(bins)


def _visual_entropy(image: Image.Image) -> float:
    histogram = image.histogram()
    total = sum(histogram)
    if total <= 0:
        return 0.0
    entropy = -sum(
        (amount / total) * math.log2(amount / total)
        for amount in histogram
        if amount > 0
    )
    return min(1.0, max(0.0, entropy / 8.0))


def _edge_density(image: Image.Image) -> float:
    pixels = list(image.tobytes())
    width, height = image.size
    edges = 0
    comparisons = 0
    for row in range(height):
        offset = row * width
        for column in range(width):
            current = pixels[offset + column]
            if column + 1 < width:
                comparisons += 1
                edges += abs(current - pixels[offset + column + 1]) >= 24
            if row + 1 < height:
                comparisons += 1
                edges += abs(current - pixels[offset + width + column]) >= 24
    return edges / max(1, comparisons)


def _duplicate_asset_hashes(pages: Sequence[VisualPageFeatures]) -> frozenset[str]:
    page_sets: dict[str, set[int]] = {}
    for page in pages:
        for asset_hash in page.asset_hashes:
            page_sets.setdefault(asset_hash, set()).add(page.page_number)
    return frozenset(asset_hash for asset_hash, members in page_sets.items() if len(members) >= 2)


def _layout_distance(left: VisualPageFeatures, right: VisualPageFeatures) -> float:
    components: list[tuple[float, float]] = [
        (0.56, _silhouette_distance(left.layout_silhouette, right.layout_silhouette)),
        (0.14, abs(left.object_density - right.object_density)),
        (0.08, abs(left.image_area_ratio - right.image_area_ratio)),
        (0.07, 0.0 if left.role == right.role else 1.0),
    ]
    if left.color_histogram and right.color_histogram:
        components.append((0.15, _histogram_distance(left.color_histogram, right.color_histogram)))
    return _weighted_distance(components)


def _asset_distance(
    left: VisualPageFeatures,
    right: VisualPageFeatures,
    left_terms: frozenset[str],
    right_terms: frozenset[str],
) -> float:
    if not left.asset_hashes and not right.asset_hashes:
        # Without images or embeddings, lexical fingerprints would make every
        # normal text page a singleton.  Cluster by observable content *form*
        # instead (amount, role and layout); Scout supplies the missing semantic
        # signal.  This keeps cluster coverage meaningful for long text decks.
        text_scale = max(1, left.text_character_count, right.text_character_count)
        return _weighted_distance(
            (
                (
                    0.45,
                    abs(left.text_character_count - right.text_character_count)
                    / text_scale,
                ),
                (
                    0.30,
                    _silhouette_distance(
                        left.layout_silhouette,
                        right.layout_silhouette,
                    ),
                ),
                (0.15, 0.0 if left.role == right.role else 1.0),
                (0.10, abs(left.object_density - right.object_density)),
            )
        )
    components: list[tuple[float, float]] = []
    if left.asset_hashes and right.asset_hashes:
        if set(left.asset_hashes) & set(right.asset_hashes):
            asset_distance = 0.0
        elif left.image_phashes and right.image_phashes:
            asset_distance = min(
                _phash_distance(left_hash, right_hash)
                for left_hash in left.image_phashes
                for right_hash in right.image_phashes
            )
        else:
            asset_distance = 1.0
        components.append((0.50, asset_distance))
    elif bool(left.asset_hashes) != bool(right.asset_hashes):
        components.append((0.65, 1.0))
    components.extend(
        (
            (0.10, abs(left.image_area_ratio - right.image_area_ratio)),
            (
                0.10,
                abs(left.image_count - right.image_count)
                / max(1, left.image_count, right.image_count),
            ),
            (
                0.10,
                _silhouette_distance(
                    left.layout_silhouette,
                    right.layout_silhouette,
                ),
            ),
        )
    )
    if left_terms or right_terms:
        components.append((0.08, _set_distance(left_terms, right_terms)))
    if left.page_phash is not None and right.page_phash is not None:
        components.append((0.12, _phash_distance(left.page_phash, right.page_phash)))
    if not components:
        return _silhouette_distance(left.layout_silhouette, right.layout_silhouette)
    return _weighted_distance(components)


def _silhouette_distance(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    intersection = sum((left_item & right_item).bit_count() for left_item, right_item in zip(left, right))
    union = sum((left_item | right_item).bit_count() for left_item, right_item in zip(left, right))
    return 0.0 if union == 0 else 1.0 - intersection / union


def _histogram_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    # Three independently normalized RGB channels make the L1 maximum six.
    return min(1.0, sum(abs(a - b) for a, b in zip(left, right)) / 6.0)


def _phash_distance(left: str, right: str) -> float:
    return (int(left, 16) ^ int(right, 16)).bit_count() / 64.0


def _set_distance(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return 0.0 if not union else 1.0 - len(left & right) / len(union)


def _weighted_distance(components: Sequence[tuple[float, float]]) -> float:
    total_weight = sum(weight for weight, _ in components)
    return sum(weight * value for weight, value in components) / max(1e-9, total_weight)


def _build_clusters(
    pages: Sequence[VisualPageFeatures],
    *,
    kind: str,
    threshold: float,
    distance: Callable[[VisualPageFeatures, VisualPageFeatures], float],
) -> tuple[VisualCluster, ...]:
    ordered = tuple(sorted(pages, key=lambda item: item.page_number))
    parent = {page.page_number: page.page_number for page in ordered}

    def find(page_number: int) -> int:
        root = page_number
        while parent[root] != root:
            root = parent[root]
        while parent[page_number] != page_number:
            previous = parent[page_number]
            parent[page_number] = root
            page_number = previous
        return root

    def union(left_page: int, right_page: int) -> None:
        left_root = find(left_page)
        right_root = find(right_page)
        if left_root == right_root:
            return
        parent[max(left_root, right_root)] = min(left_root, right_root)

    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if distance(left, right) <= threshold:
                union(left.page_number, right.page_number)
    groups: dict[int, list[VisualPageFeatures]] = {}
    for page in ordered:
        groups.setdefault(find(page.page_number), []).append(page)
    group_values = sorted(groups.values(), key=lambda value: min(item.page_number for item in value))
    largest = max(len(group) for group in group_values)
    outlier_limit = max(1, math.ceil(len(ordered) * 0.05))
    clusters = []
    for group in group_values:
        members = tuple(sorted(item.page_number for item in group))
        medoid = min(
            group,
            key=lambda candidate: (
                sum(distance(candidate, other) for other in group),
                candidate.page_number,
            ),
        )
        canonical = "|".join(
            f"{page.page_number}:{_cluster_page_signature(page, kind)}" for page in group
        )
        fingerprint = hashlib.sha256(f"{kind}|{canonical}".encode("utf-8")).hexdigest()
        cluster_id = f"{kind.replace('_', '-')}-{fingerprint[:16]}"
        is_outlier = largest >= 3 and len(group) <= outlier_limit and len(group) * 4 <= largest
        mean_distance = sum(distance(medoid, other) for other in group) / len(group)
        clusters.append(
            VisualCluster(
                cluster_id=cluster_id,
                kind=kind,
                member_page_numbers=members,
                medoid_page_number=medoid.page_number,
                fingerprint=fingerprint,
                distance_threshold=threshold,
                is_outlier=is_outlier,
                metadata={
                    "member_count": len(group),
                    "medoid_mean_distance": round(mean_distance, 8),
                    "score_propagation_allowed": False,
                },
            )
        )
    return tuple(clusters)


def _cluster_page_signature(page: VisualPageFeatures, kind: str) -> str:
    if kind == "layout_style":
        return "|".join(
            (
                page.layout_silhouette_hash,
                ",".join(f"{value:.8f}" for value in page.color_histogram),
                f"{page.object_density:.8f}",
                f"{page.image_area_ratio:.8f}",
                page.role,
            )
        )
    return "|".join(
        (
            ",".join(page.asset_hashes),
            ",".join(page.image_phashes),
            page.content_fingerprint,
            page.page_phash or "",
        )
    )


__all__ = ["VisualPageIndexBuilder", "build_visual_page_index"]
