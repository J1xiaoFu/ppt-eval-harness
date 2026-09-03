from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import os
import time
import uuid
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from ppt_eval.adapters.model_audits import ModelImageInput
from ppt_eval.infrastructure.local import sha256_file, validated_sha256

VISUAL_ASSET_ROUTE = "/v1/model-assets"
VISUAL_ASSET_TRANSPORT_VERSION = "1.0.0"
CANONICAL_MODEL_IMAGE_CAS_VERSION = "1.0.0"
DEFAULT_VISUAL_ASSET_TTL_SECONDS = 15 * 60
DEFAULT_VISUAL_ASSET_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_VISUAL_ASSET_MAX_PIXELS = 25_000_000
DEFAULT_VISUAL_ASSET_MAX_DIMENSION = 16_384
_MINIMUM_SIGNING_SECRET_BYTES = 32
_MAXIMUM_VISUAL_ASSET_TTL_SECONDS = 60 * 60
_SUPPORTED_MEDIA_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_SUPPORTED_IMAGE_FORMATS = {
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
_IMAGE_FORMAT_DETAILS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


@dataclass(frozen=True, slots=True)
class VerifiedRasterImage:
    """A bounded, integrity-checked raster snapshot read from one local file."""

    data: bytes = field(repr=False)
    image_format: str
    media_type: str
    width: int
    height: int
    sha256: str


class VisualAssetVariant(str, Enum):
    """Rendered visual derivatives that may be exposed to a model provider."""

    SLIDE = "slide"
    ATLAS = "atlas"
    CROP = "crop"


class VisualAssetConfigurationError(ValueError):
    """The optional signed visual-asset transport is not safely configured."""


class VisualAssetAccessError(ValueError):
    """A path is not an eligible, registered rendered visual asset."""


class VisualAssetGrantInvalid(ValueError):
    """A visual-asset grant is malformed or has an invalid signature."""


class VisualAssetGrantExpired(VisualAssetGrantInvalid):
    """A correctly shaped visual-asset grant is no longer valid."""


@dataclass(frozen=True, slots=True)
class VisualAssetTransportConfig:
    """Validated transport settings; Base64 remains the zero-config default."""

    mode: str = "base64"
    public_base_url: str | None = None
    signing_secret: bytes | None = field(default=None, repr=False)
    ttl_seconds: int = DEFAULT_VISUAL_ASSET_TTL_SECONDS

    @property
    def signed_url_enabled(self) -> bool:
        return self.mode == "signed-url"

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "VisualAssetTransportConfig":
        env = os.environ if environment is None else environment
        raw_mode = str(env.get("PPT_EVAL_VISUAL_ASSET_TRANSPORT") or "").strip()
        raw_base_url = str(
            env.get("PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL") or ""
        ).strip()
        raw_secret = str(
            env.get("PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET") or ""
        ).strip()
        raw_ttl = str(
            env.get("PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS") or ""
        ).strip()

        configured_parts = bool(raw_base_url or raw_secret or raw_ttl)
        mode = raw_mode.casefold() if raw_mode else "base64"
        if mode not in {"base64", "signed-url"}:
            raise VisualAssetConfigurationError(
                "PPT_EVAL_VISUAL_ASSET_TRANSPORT must be base64 or signed-url"
            )
        if mode == "base64":
            if configured_parts:
                raise VisualAssetConfigurationError(
                    "signed visual-asset settings require "
                    "PPT_EVAL_VISUAL_ASSET_TRANSPORT=signed-url"
                )
            return cls()

        missing = [
            name
            for name, value in (
                ("PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL", raw_base_url),
                ("PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET", raw_secret),
            )
            if not value
        ]
        if missing:
            raise VisualAssetConfigurationError(
                "signed-url visual-asset transport requires: " + ", ".join(missing)
            )
        secret = raw_secret.encode("utf-8")
        if len(secret) < _MINIMUM_SIGNING_SECRET_BYTES:
            raise VisualAssetConfigurationError(
                "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET must contain at least 32 bytes"
            )
        ttl_seconds = _parse_ttl(raw_ttl)
        return cls(
            mode=mode,
            public_base_url=_validated_public_https_base_url(raw_base_url),
            signing_secret=secret,
            ttl_seconds=ttl_seconds,
        )


@dataclass(frozen=True, slots=True)
class KnownVisualAsset:
    """A content-addressed visual derivative registered from an allowed root."""

    sha256: str
    variant: VisualAssetVariant
    path: Path = field(repr=False, compare=False)
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SignedVisualAssetGrant:
    asset_sha256: str
    variant: VisualAssetVariant
    expires: int
    signature: str = field(repr=False)


def verified_raster_image(
    source: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_media_type: str | None = None,
    max_bytes: int = DEFAULT_VISUAL_ASSET_MAX_BYTES,
    max_pixels: int = DEFAULT_VISUAL_ASSET_MAX_PIXELS,
    max_dimension: int = DEFAULT_VISUAL_ASSET_MAX_DIMENSION,
    require_exact_container: bool = True,
) -> VerifiedRasterImage:
    """Read and validate one bounded PNG/JPEG/WebP snapshot.

    ``require_exact_container`` rejects bytes after the image container.  The
    canonicalization ingress deliberately sets it to ``False`` so a valid
    image with metadata or a trailer can be decoded and re-encoded without the
    hidden bytes.  Every provider-facing read keeps the strict default.
    """

    for label, value in (
        ("max_bytes", max_bytes),
        ("max_pixels", max_pixels),
        ("max_dimension", max_dimension),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
    path = Path(source)
    try:
        size = path.stat().st_size
        if not path.is_file() or size <= 0:
            raise OSError("not a non-empty regular file")
        if size > max_bytes:
            raise VisualAssetAccessError("visual asset exceeds the size limit")
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except VisualAssetAccessError:
        raise
    except OSError as exc:
        raise VisualAssetAccessError("visual asset is unavailable") from exc
    if len(payload) != size or len(payload) > max_bytes:
        raise VisualAssetAccessError("visual asset changed or exceeded the size limit")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and not hmac.compare_digest(
        digest,
        validated_sha256(expected_sha256),
    ):
        raise VisualAssetAccessError("visual asset failed integrity validation")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                image_format = str(image.format or "").upper()
                details = _IMAGE_FORMAT_DETAILS.get(image_format)
                if details is None:
                    raise VisualAssetAccessError(
                        "only rendered PNG, JPEG, and WebP assets are supported"
                    )
                media_type, _suffix = details
                if getattr(image, "n_frames", 1) != 1:
                    raise VisualAssetAccessError(
                        "animated visual assets are not supported"
                    )
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > max_dimension
                    or height > max_dimension
                    or width * height > max_pixels
                ):
                    raise VisualAssetAccessError(
                        "visual asset dimensions exceed the safe limit"
                    )
                image.verify()
    except VisualAssetAccessError:
        raise
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise VisualAssetAccessError(
            "visual asset is not a valid rendered raster image"
        ) from exc

    if expected_media_type is not None:
        normalized_expected = _normalized_image_media_type(expected_media_type)
        if normalized_expected != media_type:
            raise VisualAssetAccessError(
                "visual asset media type does not match its content"
            )
    if require_exact_container and not _has_exact_image_container(
        payload,
        image_format=image_format,
    ):
        raise VisualAssetAccessError(
            "visual asset contains trailing or malformed container data"
        )
    return VerifiedRasterImage(
        data=payload,
        image_format=image_format,
        media_type=media_type,
        width=width,
        height=height,
        sha256=digest,
    )


class CanonicalModelImageCAS:
    """Store only deterministic, metadata-free PNG pixels for Profile 8.4."""

    version = CANONICAL_MODEL_IMAGE_CAS_VERSION

    def __init__(
        self,
        root: str | Path,
        *,
        max_asset_bytes: int = DEFAULT_VISUAL_ASSET_MAX_BYTES,
        max_pixels: int = DEFAULT_VISUAL_ASSET_MAX_PIXELS,
        max_dimension: int = DEFAULT_VISUAL_ASSET_MAX_DIMENSION,
    ) -> None:
        for label, value in (
            ("max_asset_bytes", max_asset_bytes),
            ("max_pixels", max_pixels),
            ("max_dimension", max_dimension),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"model image CAS {label} must be a positive integer")
        self.root = Path(root)
        self.max_asset_bytes = max_asset_bytes
        self.max_pixels = max_pixels
        self.max_dimension = max_dimension
        self._lock = RLock()

    def prepare_slide_images(
        self,
        value: object,
        *,
        expected_page_count: int | None = None,
    ) -> tuple[ModelImageInput, ...]:
        """Normalize a complete page set into immutable canonical PNG objects."""

        if isinstance(value, (str, bytes, Path)) or not isinstance(value, Sequence):
            raise TypeError("slide_images must be a sequence")
        images = tuple(
            self._canonicalize_item(item, default_page_number=index)
            for index, item in enumerate(value, start=1)
        )
        page_numbers = tuple(item.page_number for item in images)
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("rendered slide page numbers must be unique")
        ordered = tuple(sorted(images, key=lambda item: item.page_number))
        ordered_pages = tuple(item.page_number for item in ordered)
        if expected_page_count is not None:
            if (
                isinstance(expected_page_count, bool)
                or not isinstance(expected_page_count, int)
                or expected_page_count < 1
            ):
                raise ValueError("expected_page_count must be a positive integer")
            expected_pages = tuple(range(1, expected_page_count + 1))
            if ordered_pages != expected_pages:
                raise ValueError(
                    "rendered slide images must cover every presentation page exactly once"
                )
        elif ordered_pages != tuple(range(1, len(ordered) + 1)):
            raise ValueError("rendered slide page numbers must be contiguous and one-based")
        return ordered

    def _canonicalize_item(
        self,
        item: object,
        *,
        default_page_number: int,
    ) -> ModelImageInput:
        if isinstance(item, ModelImageInput):
            page_number = item.page_number
            source = item.uri
            expected_sha256: str | None = item.sha256
            expected_media_type: str | None = item.media_type
        elif isinstance(item, Mapping):
            page_number = _page_number(item.get("page_number", default_page_number))
            source = str(item.get("uri") or item.get("path") or "")
            if not source:
                raise ValueError("rendered image mapping requires uri or path")
            raw_sha256 = item.get("sha256")
            raw_media_type = item.get("media_type")
            if bool(raw_sha256) != bool(raw_media_type):
                raise ValueError(
                    "rendered image digest and media type must be supplied together"
                )
            expected_sha256 = str(raw_sha256) if raw_sha256 else None
            expected_media_type = str(raw_media_type) if raw_media_type else None
        else:
            page_number = default_page_number
            source = str(item)
            expected_sha256 = None
            expected_media_type = None

        snapshot = verified_raster_image(
            source,
            expected_sha256=expected_sha256,
            expected_media_type=expected_media_type,
            max_bytes=self.max_asset_bytes,
            max_pixels=self.max_pixels,
            max_dimension=self.max_dimension,
            # Trailers and metadata are accepted only at this one ingress and
            # are removed by the deterministic pixel re-encode below.
            require_exact_container=False,
        )
        canonical = _canonical_png(snapshot)
        if len(canonical) > self.max_asset_bytes:
            raise VisualAssetAccessError(
                "canonical model image exceeds the size limit"
            )
        digest = hashlib.sha256(canonical).hexdigest()
        destination = self._store_png(digest, canonical)
        return ModelImageInput(
            page_number=page_number,
            uri=str(destination),
            media_type="image/png",
            sha256=digest,
        )

    def _store_png(self, digest: str, payload: bytes) -> Path:
        root = self.root.resolve()
        parent = root / digest[:2]
        destination = parent / f"{digest}.png"
        with self._lock:
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.resolve().is_relative_to(root):
                raise VisualAssetAccessError("model image CAS escaped its root")
            if destination.is_symlink() or not destination.resolve().is_relative_to(root):
                raise VisualAssetAccessError("model image CAS entry escaped its root")
            if destination.exists():
                existing = verified_raster_image(
                    destination,
                    expected_sha256=digest,
                    expected_media_type="image/png",
                    max_bytes=self.max_asset_bytes,
                    max_pixels=self.max_pixels,
                    max_dimension=self.max_dimension,
                )
                if not hmac.compare_digest(existing.data, payload):
                    raise VisualAssetAccessError(
                        "model image CAS entry failed integrity validation"
                    )
                return destination.resolve()
            temporary = parent / f".{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destination.resolve()


class VisualAssetCAS:
    """Copy caller-supplied rendered images into a bounded content-addressed root."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_asset_bytes: int = DEFAULT_VISUAL_ASSET_MAX_BYTES,
    ) -> None:
        if (
            isinstance(max_asset_bytes, bool)
            or not isinstance(max_asset_bytes, int)
            or max_asset_bytes <= 0
        ):
            raise ValueError("visual CAS max_asset_bytes must be a positive integer")
        self.root = Path(root)
        self.max_asset_bytes = max_asset_bytes
        self._lock = RLock()

    def import_image(
        self,
        source: str | Path,
        *,
        variant: VisualAssetVariant,
        expected_sha256: str | None = None,
        expected_media_type: str | None = None,
    ) -> KnownVisualAsset:
        raw_source = Path(source)
        if raw_source.is_symlink():
            raise VisualAssetAccessError("visual CAS does not accept symbolic links")
        source_path = raw_source.resolve()
        try:
            source_size = source_path.stat().st_size
        except OSError as exc:
            raise VisualAssetAccessError("caller-supplied visual asset is unavailable") from exc
        if not source_path.is_file() or source_size <= 0:
            raise VisualAssetAccessError("caller-supplied visual asset is unavailable")
        if source_size > self.max_asset_bytes:
            raise VisualAssetAccessError("caller-supplied visual asset exceeds the size limit")

        image_format, media_type, suffix = _inspected_image(source_path)
        del image_format
        if expected_media_type is not None:
            normalized_media_type = str(expected_media_type).strip().casefold()
            if normalized_media_type != media_type:
                raise VisualAssetAccessError(
                    "caller-supplied visual asset media type does not match its content"
                )
        digest = sha256_file(source_path)
        if expected_sha256 is not None and not hmac.compare_digest(
            digest,
            validated_sha256(expected_sha256),
        ):
            raise VisualAssetAccessError(
                "caller-supplied visual asset failed integrity validation"
            )

        root = self.root.resolve()
        destination_parent = root / variant.value / digest[:2]
        destination = destination_parent / f"{digest}{suffix}"
        with self._lock:
            destination_parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = destination_parent.resolve()
            if not resolved_parent.is_relative_to(root):
                raise VisualAssetAccessError("visual CAS directory escaped its root")
            resolved_destination = destination.resolve()
            if not resolved_destination.is_relative_to(root):
                raise VisualAssetAccessError("visual CAS destination escaped its root")
            if destination.exists():
                return self._validated_entry(
                    destination,
                    variant=variant,
                    expected_sha256=digest,
                    expected_media_type=media_type,
                    expected_size=source_size,
                )

            temporary = destination_parent / f".{uuid.uuid4().hex}.tmp{suffix}"
            try:
                copied = 0
                with source_path.open("rb") as source_handle, temporary.open(
                    "xb"
                ) as destination_handle:
                    while block := source_handle.read(1024 * 1024):
                        copied += len(block)
                        if copied > self.max_asset_bytes:
                            raise VisualAssetAccessError(
                                "caller-supplied visual asset changed beyond the size limit"
                            )
                        destination_handle.write(block)
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
                if copied != source_size or sha256_file(temporary) != digest:
                    raise VisualAssetAccessError(
                        "caller-supplied visual asset changed while being imported"
                    )
                _format, copied_media_type, _suffix = _inspected_image(temporary)
                if copied_media_type != media_type:
                    raise VisualAssetAccessError(
                        "caller-supplied visual asset changed while being imported"
                    )
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return self._validated_entry(
                destination,
                variant=variant,
                expected_sha256=digest,
                expected_media_type=media_type,
                expected_size=source_size,
            )

    def _validated_entry(
        self,
        path: Path,
        *,
        variant: VisualAssetVariant,
        expected_sha256: str,
        expected_media_type: str,
        expected_size: int,
    ) -> KnownVisualAsset:
        root = self.root.resolve()
        if path.is_symlink():
            raise VisualAssetAccessError("visual CAS entry escaped its root")
        candidate = path.resolve()
        if not candidate.is_relative_to(root):
            raise VisualAssetAccessError("visual CAS entry escaped its root")
        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            raise VisualAssetAccessError("visual CAS entry failed size validation")
        if not hmac.compare_digest(sha256_file(candidate), expected_sha256):
            raise VisualAssetAccessError("visual CAS entry failed integrity validation")
        _format, media_type, _suffix = _inspected_image(candidate)
        if media_type != expected_media_type:
            raise VisualAssetAccessError("visual CAS entry failed media validation")
        return KnownVisualAsset(
            sha256=expected_sha256,
            variant=variant,
            path=candidate,
            media_type=media_type,
            size_bytes=expected_size,
        )


class VisualAssetCatalog:
    """Thread-safe allow-list for rendered images; it never accepts PPTX files."""

    def __init__(
        self,
        allowed_roots: Mapping[VisualAssetVariant, tuple[str | Path, ...]],
    ) -> None:
        self._allowed_roots = {
            variant: tuple(Path(root).resolve() for root in roots)
            for variant, roots in allowed_roots.items()
        }
        self._assets: dict[tuple[VisualAssetVariant, str], KnownVisualAsset] = {}
        self._lock = RLock()

    def register(
        self,
        path: str | Path,
        *,
        variant: VisualAssetVariant,
        expected_sha256: str | None = None,
    ) -> KnownVisualAsset:
        candidate = Path(path).resolve()
        roots = self._allowed_roots.get(variant, ())
        if not roots or not any(candidate.is_relative_to(root) for root in roots):
            raise VisualAssetAccessError(
                "visual asset is outside the allowed directory for its variant"
            )
        media_type = _SUPPORTED_MEDIA_TYPES.get(candidate.suffix.casefold())
        if media_type is None:
            raise VisualAssetAccessError(
                "only rendered PNG, JPEG, and WebP assets may be registered"
            )
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise VisualAssetAccessError("visual asset is unavailable")
        actual_format, actual_media_type, _suffix = _inspected_image(candidate)
        if actual_format != _SUPPORTED_IMAGE_FORMATS[candidate.suffix.casefold()]:
            raise VisualAssetAccessError(
                "visual asset content does not match its image suffix"
            )
        if actual_media_type != media_type:
            raise VisualAssetAccessError("visual asset has an invalid image media type")
        digest = sha256_file(candidate)
        if expected_sha256 is not None and digest != validated_sha256(expected_sha256):
            raise VisualAssetAccessError("visual asset failed integrity validation")
        asset = KnownVisualAsset(
            sha256=digest,
            variant=variant,
            path=candidate,
            media_type=media_type,
            size_bytes=candidate.stat().st_size,
        )
        with self._lock:
            self._assets[(variant, digest)] = asset
        return asset

    def resolve(
        self,
        *,
        variant: VisualAssetVariant,
        asset_sha256: str,
    ) -> KnownVisualAsset:
        digest = validated_sha256(asset_sha256)
        with self._lock:
            asset = self._assets.get((variant, digest))
        if asset is None:
            raise FileNotFoundError(digest)
        roots = self._allowed_roots.get(variant, ())
        path = asset.path.resolve()
        if not any(path.is_relative_to(root) for root in roots):
            raise VisualAssetAccessError("registered visual asset escaped its allowed root")
        if (
            not path.is_file()
            or path.stat().st_size != asset.size_bytes
            or sha256_file(path) != digest
        ):
            raise VisualAssetAccessError("registered visual asset changed after registration")
        return asset

    def verified_snapshot(
        self,
        *,
        variant: VisualAssetVariant,
        asset_sha256: str,
        max_bytes: int = DEFAULT_VISUAL_ASSET_MAX_BYTES,
    ) -> tuple[KnownVisualAsset, bytes]:
        """Return immutable bytes that were size- and digest-checked after reading."""

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("visual snapshot max_bytes must be a positive integer")
        asset = self.resolve(variant=variant, asset_sha256=asset_sha256)
        if asset.size_bytes > max_bytes:
            raise VisualAssetAccessError("registered visual asset exceeds the size limit")
        try:
            with asset.path.open("rb") as handle:
                payload = handle.read(asset.size_bytes + 1)
        except OSError as exc:
            raise VisualAssetAccessError(
                "registered visual asset became unavailable while being read"
            ) from exc
        if len(payload) != asset.size_bytes or not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(),
            asset.sha256,
        ):
            raise VisualAssetAccessError(
                "registered visual asset changed while being read"
            )
        return asset, payload


class VisualAssetSigner:
    """Issue short-lived HMAC grants over only variant, digest, and expiry."""

    def __init__(self, *, signing_secret: bytes, ttl_seconds: int) -> None:
        if len(signing_secret) < _MINIMUM_SIGNING_SECRET_BYTES:
            raise VisualAssetConfigurationError(
                "visual-asset signing secret must contain at least 32 bytes"
            )
        if not 1 <= ttl_seconds <= _MAXIMUM_VISUAL_ASSET_TTL_SECONDS:
            raise VisualAssetConfigurationError(
                "visual-asset URL TTL must be between 1 and 3600 seconds"
            )
        self._signing_secret = bytes(signing_secret)
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        asset: KnownVisualAsset,
        *,
        now: int | None = None,
    ) -> SignedVisualAssetGrant:
        current = int(time.time()) if now is None else int(now)
        expires = current + self.ttl_seconds
        signature = self._signature(asset.variant, asset.sha256, expires)
        return SignedVisualAssetGrant(
            asset_sha256=asset.sha256,
            variant=asset.variant,
            expires=expires,
            signature=signature,
        )

    def verify(
        self,
        asset: KnownVisualAsset,
        *,
        expires: int,
        signature: str,
        now: int | None = None,
    ) -> None:
        current = int(time.time()) if now is None else int(now)
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise VisualAssetGrantInvalid("visual-asset expiry must be an integer")
        if expires <= current:
            raise VisualAssetGrantExpired("visual-asset grant has expired")
        if expires - current > self.ttl_seconds:
            raise VisualAssetGrantInvalid("visual-asset grant exceeds the configured TTL")
        if (
            len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            raise VisualAssetGrantInvalid("visual-asset signature is malformed")
        expected = self._signature(asset.variant, asset.sha256, expires)
        if not hmac.compare_digest(expected, signature):
            raise VisualAssetGrantInvalid("visual-asset signature is invalid")

    def _signature(
        self,
        variant: VisualAssetVariant,
        asset_sha256: str,
        expires: int,
    ) -> str:
        digest = validated_sha256(asset_sha256)
        canonical = (
            f"ppt-eval-visual-asset@{VISUAL_ASSET_TRANSPORT_VERSION}\n"
            f"{variant.value}\n{digest}\n{expires}"
        ).encode("ascii")
        return hmac.new(self._signing_secret, canonical, hashlib.sha256).hexdigest()


class SignedUrlVisualAssetTransport:
    """Publish registered visual derivatives as opaque, short-lived HTTPS URLs."""

    mode = "signed-url"

    def __init__(
        self,
        *,
        config: VisualAssetTransportConfig,
        catalog: VisualAssetCatalog,
        content_store: VisualAssetCAS | None = None,
    ) -> None:
        if (
            not config.signed_url_enabled
            or config.public_base_url is None
            or config.signing_secret is None
        ):
            raise VisualAssetConfigurationError(
                "signed URL transport requires validated signed-url settings"
            )
        self._base_url = config.public_base_url
        self.catalog = catalog
        self.content_store = content_store
        self.signer = VisualAssetSigner(
            signing_secret=config.signing_secret,
            ttl_seconds=config.ttl_seconds,
        )
        self._grants: dict[
            tuple[VisualAssetVariant, str], SignedVisualAssetGrant
        ] = {}
        self._lock = RLock()

    def prepare_slide_images(self, value: object) -> tuple[ModelImageInput, ...]:
        """Normalize untrusted caller images into the controlled visual CAS."""

        if self.content_store is None:
            raise VisualAssetConfigurationError(
                "signed URL transport has no visual content store"
            )
        if isinstance(value, (str, bytes, Path)) or not isinstance(value, Sequence):
            raise TypeError("slide_images must be a sequence")
        images: list[ModelImageInput] = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, ModelImageInput):
                page_number = item.page_number
                uri = item.uri
                expected_sha256: str | None = item.sha256
                expected_media_type: str | None = item.media_type
            elif isinstance(item, Mapping):
                page_number = _page_number(item.get("page_number", index))
                uri = str(item.get("uri") or item.get("path") or "")
                if not uri:
                    raise ValueError("rendered image mapping requires uri or path")
                raw_sha256 = item.get("sha256")
                raw_media_type = item.get("media_type")
                if bool(raw_sha256) != bool(raw_media_type):
                    # Preserve the existing adapter contract: partial metadata is
                    # not authoritative, so content inspection supplies both.
                    expected_sha256 = None
                    expected_media_type = None
                else:
                    expected_sha256 = str(raw_sha256) if raw_sha256 else None
                    expected_media_type = (
                        str(raw_media_type) if raw_media_type else None
                    )
            else:
                page_number = index
                uri = str(item)
                expected_sha256 = None
                expected_media_type = None
            asset = self.content_store.import_image(
                uri,
                variant=VisualAssetVariant.SLIDE,
                expected_sha256=expected_sha256,
                expected_media_type=expected_media_type,
            )
            images.append(
                ModelImageInput(
                    page_number=page_number,
                    uri=str(asset.path),
                    media_type=asset.media_type,
                    sha256=asset.sha256,
                )
            )
        page_numbers = [item.page_number for item in images]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("rendered slide page numbers must be unique")
        return tuple(sorted(images, key=lambda item: item.page_number))

    def publish(
        self,
        path: str | Path,
        *,
        variant: VisualAssetVariant,
        expected_sha256: str | None = None,
        now: int | None = None,
    ) -> str:
        asset = self.catalog.register(
            path,
            variant=variant,
            expected_sha256=expected_sha256,
        )
        current = int(time.time()) if now is None else int(now)
        key = (asset.variant, asset.sha256)
        with self._lock:
            grant = self._grants.get(key)
            minimum_remaining = max(1, self.signer.ttl_seconds // 4)
            if grant is None or grant.expires - current <= minimum_remaining:
                grant = self.signer.issue(asset, now=current)
                self._grants[key] = grant
        return self.url_for(grant)

    def url_for(self, grant: SignedVisualAssetGrant) -> str:
        base = self._base_url.rstrip("/")
        return (
            f"{base}{VISUAL_ASSET_ROUTE}/{quote(grant.variant.value, safe='')}/"
            f"{quote(grant.asset_sha256, safe='')}?expires={grant.expires}"
            f"&signature={grant.signature}"
        )


def _parse_ttl(raw_value: str) -> int:
    if not raw_value:
        return DEFAULT_VISUAL_ASSET_TTL_SECONDS
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise VisualAssetConfigurationError(
            "PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS must be an integer"
        ) from exc
    if not 1 <= value <= _MAXIMUM_VISUAL_ASSET_TTL_SECONDS:
        raise VisualAssetConfigurationError(
            "PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS must be between 1 and 3600"
        )
    return value


def _page_number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("rendered image page_number must be a positive integer")
    if isinstance(value, int):
        page_number = value
    elif isinstance(value, str):
        try:
            page_number = int(value)
        except ValueError as exc:
            raise ValueError(
                "rendered image page_number must be a positive integer"
            ) from exc
    else:
        raise ValueError(
            "rendered image page_number must be a positive integer"
        )
    if page_number < 1:
        raise ValueError("rendered image page_number must be a positive integer")
    return page_number


def _normalized_image_media_type(value: str) -> str:
    normalized = str(value).strip().casefold()
    if normalized == "image/jpg":
        return "image/jpeg"
    if normalized not in {"image/png", "image/jpeg", "image/webp"}:
        raise VisualAssetAccessError(
            "only image/png, image/jpeg, and image/webp are supported"
        )
    return normalized


def _has_exact_image_container(payload: bytes, *, image_format: str) -> bool:
    if image_format == "PNG":
        signature = b"\x89PNG\r\n\x1a\n"
        if not payload.startswith(signature):
            return False
        offset = len(signature)
        while offset + 12 <= len(payload):
            chunk_size = int.from_bytes(payload[offset : offset + 4], "big")
            chunk_type = payload[offset + 4 : offset + 8]
            end = offset + 12 + chunk_size
            if end > len(payload):
                return False
            offset = end
            if chunk_type == b"IEND":
                return chunk_size == 0 and offset == len(payload)
        return False
    if image_format == "JPEG":
        return len(payload) >= 4 and payload.startswith(b"\xff\xd8") and payload.endswith(
            b"\xff\xd9"
        )
    if image_format == "WEBP":
        return (
            len(payload) >= 12
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP"
            and int.from_bytes(payload[4:8], "little") + 8 == len(payload)
        )
    return False


def _canonical_png(snapshot: VerifiedRasterImage) -> bytes:
    try:
        with Image.open(io.BytesIO(snapshot.data)) as source:
            source.load()
            transposed = ImageOps.exif_transpose(source)
            if "A" in transposed.getbands():
                rgba = transposed.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                background.alpha_composite(rgba)
                normalized = background.convert("RGB")
                background.close()
                rgba.close()
            else:
                normalized = transposed.convert("RGB")
            if transposed is not source:
                transposed.close()
            encoded = io.BytesIO()
            try:
                normalized.save(
                    encoded,
                    format="PNG",
                    optimize=False,
                    compress_level=6,
                )
                return encoded.getvalue()
            finally:
                normalized.close()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise VisualAssetAccessError(
            "visual asset could not be canonicalized safely"
        ) from exc


def _inspected_image(path: Path) -> tuple[str, str, str]:
    snapshot = verified_raster_image(path)
    image_format = snapshot.image_format
    details = _IMAGE_FORMAT_DETAILS[image_format]
    media_type, suffix = details
    return image_format, media_type, suffix


def _validated_public_https_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise VisualAssetConfigurationError(
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL must be an absolute HTTPS URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VisualAssetConfigurationError(
            "public visual-asset base URL cannot contain credentials, query, or fragment"
        )
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise VisualAssetConfigurationError(
            "public visual-asset base URL cannot use a local hostname"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise VisualAssetConfigurationError(
            "public visual-asset base URL cannot use a non-public IP address"
        )
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(
        segment in {".", ".."} for segment in decoded_path.split("/")
    ):
        raise VisualAssetConfigurationError("public visual-asset base URL has an unsafe path")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            normalized_path,
            "",
            "",
        )
    )
