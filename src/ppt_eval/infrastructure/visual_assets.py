from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError

from ppt_eval.adapters.model_audits import ModelImageInput
from ppt_eval.infrastructure.local import sha256_file, validated_sha256

VISUAL_ASSET_ROUTE = "/v1/model-assets"
VISUAL_ASSET_TRANSPORT_VERSION = "1.0.0"
DEFAULT_VISUAL_ASSET_TTL_SECONDS = 15 * 60
DEFAULT_VISUAL_ASSET_MAX_BYTES = 20 * 1024 * 1024
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


def _inspected_image(path: Path) -> tuple[str, str, str]:
    try:
        with Image.open(path) as image:
            image_format = str(image.format or "").upper()
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError) as exc:
        raise VisualAssetAccessError(
            "visual asset is not a valid rendered image"
        ) from exc
    details = _IMAGE_FORMAT_DETAILS.get(image_format)
    if details is None:
        raise VisualAssetAccessError(
            "only rendered PNG, JPEG, and WebP assets may be registered"
        )
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
