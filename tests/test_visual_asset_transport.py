from __future__ import annotations

import io
import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from PIL import Image, PngImagePlugin

from ppt_eval.adapters import ModelImageInput, RenderResult
from ppt_eval.api import create_app
from ppt_eval.config import default_profile, profile_for_version
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure.visual_assets import (
    DEFAULT_VISUAL_ASSET_TTL_SECONDS,
    CanonicalModelImageCAS,
    SignedUrlVisualAssetTransport,
    VisualAssetAccessError,
    VisualAssetCAS,
    VisualAssetCatalog,
    VisualAssetConfigurationError,
    VisualAssetGrantExpired,
    VisualAssetGrantInvalid,
    VisualAssetSigner,
    VisualAssetTransportConfig,
    VisualAssetVariant,
    verified_raster_image,
)
from ppt_eval.runtime import (
    LocalEvaluationRuntime,
    RuntimePaths,
    _visual_asset_variant_for_path,
    build_runtime_from_environment,
)
from tests.fixtures.api_client import make_test_client
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

_ENVIRONMENT_KEYS = (
    "PPT_EVAL_VISUAL_ASSET_TRANSPORT",
    "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL",
    "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET",
    "PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS",
)
_SECRET = "test-only-signing-secret-with-at-least-32-bytes"


class _TrailerRenderer:
    renderer_id = "trailer-renderer"
    version = "1.0"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def render(self, pptx_path, output_dir) -> RenderResult:
        del pptx_path
        target = Path(output_dir) / "Slide1.png"
        target.write_bytes(self.payload)
        return RenderResult(self.renderer_id, self.version, (target,))


def test_adaptive_visual_paths_route_to_their_narrow_signed_asset_variant(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.under(tmp_path / "var")

    assert _visual_asset_variant_for_path(
        paths.artifacts / "visual-atlases" / "atlas.png",
        paths,
    ) == VisualAssetVariant.ATLAS
    assert _visual_asset_variant_for_path(
        paths.artifacts / "visual-crops" / "crop.png",
        paths,
    ) == VisualAssetVariant.CROP
    assert _visual_asset_variant_for_path(
        paths.render_cache / "cache" / "slide.png",
        paths,
    ) == VisualAssetVariant.SLIDE
    assert _visual_asset_variant_for_path(
        tmp_path / "outside.png",
        paths,
    ) == VisualAssetVariant.SLIDE


def _visual_environment(**values: str) -> dict[str, str]:
    environment = {key: "" for key in _ENVIRONMENT_KEYS}
    environment.update(values)
    return environment


def _signed_url_environment() -> dict[str, str]:
    return _visual_environment(
        PPT_EVAL_VISUAL_ASSET_TRANSPORT="signed-url",
        PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL=(
            "https://model-assets.example.com/eval"
        ),
        PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET=_SECRET,
    )


def test_visual_asset_transport_is_base64_and_endpoint_is_absent_by_default(
    tmp_path: Path,
) -> None:
    with patch.dict(os.environ, _visual_environment()):
        config = VisualAssetTransportConfig.from_environment()
        client = make_test_client(
            lambda: create_app(LocalEvaluationRuntime(tmp_path / "var"))
        )
    assert config == VisualAssetTransportConfig()
    assert config.mode == "base64"
    assert config.ttl_seconds == DEFAULT_VISUAL_ASSET_TTL_SECONDS
    assert client.app.state.visual_asset_transport_config == {
        "mode": "base64",
        "signed_url_enabled": False,
        "ttl_seconds": DEFAULT_VISUAL_ASSET_TTL_SECONDS,
    }
    assert not hasattr(client.app.state, "visual_asset_transport")
    missing = client.get(f"/v1/model-assets/slide/{'0' * 64}")
    assert missing.status_code == 404


def test_partial_signed_url_configuration_fails_closed() -> None:
    environments = (
        {"PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url"},
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com",
        },
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
        },
        {"PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com"},
        {"PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET},
        {"PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS": "300"},
    )
    for environment in environments:
        with pytest.raises(VisualAssetConfigurationError):
            VisualAssetTransportConfig.from_environment(environment)


def test_signed_url_configuration_requires_a_safe_public_https_base() -> None:
    base_urls = (
        "http://assets.example.com",
        "https://localhost:8000",
        "https://127.0.0.1",
        "https://10.0.0.8",
        "https://user:password@assets.example.com",
        "https://assets.example.com?secret=value",
        "https://assets.example.com/#fragment",
        "https://assets.example.com/../private",
        "https://assets.example.com/%2e%2e/private",
    )
    for base_url in base_urls:
        with pytest.raises(VisualAssetConfigurationError):
            VisualAssetTransportConfig.from_environment(
                {
                    "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
                    "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": base_url,
                    "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
                }
            )


def test_catalog_accepts_only_integrity_checked_images_under_variant_root(
    tmp_path: Path,
) -> None:
    slide_root = tmp_path / "slides"
    slide_root.mkdir()
    image = slide_root / "slide-1.png"
    image.write_bytes(PNG_1X1)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_1X1)
    source = slide_root / "source.pptx"
    source.write_bytes(b"not-an-image")
    disguised_source = slide_root / "source.png"
    disguised_source.write_bytes(b"PK\x03\x04not-a-rendered-image")
    catalog = VisualAssetCatalog({VisualAssetVariant.SLIDE: (slide_root,)})

    asset = catalog.register(image, variant=VisualAssetVariant.SLIDE)
    assert catalog.resolve(
        variant=VisualAssetVariant.SLIDE,
        asset_sha256=asset.sha256,
    ) == asset
    with pytest.raises(VisualAssetAccessError):
        catalog.register(outside, variant=VisualAssetVariant.SLIDE)
    with pytest.raises(VisualAssetAccessError):
        catalog.register(source, variant=VisualAssetVariant.SLIDE)
    with pytest.raises(VisualAssetAccessError):
        catalog.register(disguised_source, variant=VisualAssetVariant.SLIDE)
    with pytest.raises(VisualAssetAccessError):
        catalog.register(image, variant=VisualAssetVariant.CROP)

    image.write_bytes(PNG_1X1 + b"changed")
    with pytest.raises(VisualAssetAccessError, match="changed"):
        catalog.resolve(
            variant=VisualAssetVariant.SLIDE,
            asset_sha256=asset.sha256,
        )


def test_signer_binds_digest_variant_and_expiry_without_exposing_secret(
    tmp_path: Path,
) -> None:
    image = tmp_path / "slide.png"
    image.write_bytes(PNG_1X1)
    catalog = VisualAssetCatalog({VisualAssetVariant.SLIDE: (tmp_path,)})
    asset = catalog.register(image, variant=VisualAssetVariant.SLIDE)
    signer = VisualAssetSigner(signing_secret=_SECRET.encode(), ttl_seconds=900)
    grant = signer.issue(asset, now=1_000)

    signer.verify(
        asset,
        expires=grant.expires,
        signature=grant.signature,
        now=1_001,
    )
    with pytest.raises(VisualAssetGrantInvalid):
        signer.verify(
            asset,
            expires=grant.expires + 1,
            signature=grant.signature,
            now=1_001,
        )
    with pytest.raises(VisualAssetGrantExpired):
        signer.verify(
            asset,
            expires=grant.expires,
            signature=grant.signature,
            now=grant.expires,
        )
    assert _SECRET not in repr(signer)


def test_transport_reuses_a_stable_url_until_the_grant_nears_expiry(
    tmp_path: Path,
) -> None:
    image = tmp_path / "slide.png"
    image.write_bytes(PNG_1X1)
    config = VisualAssetTransportConfig.from_environment(
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com",
            "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
            "PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS": "900",
        }
    )
    transport = SignedUrlVisualAssetTransport(
        config=config,
        catalog=VisualAssetCatalog({VisualAssetVariant.SLIDE: (tmp_path,)}),
    )

    first = transport.publish(image, variant=VisualAssetVariant.SLIDE, now=1_000)
    reused = transport.publish(image, variant=VisualAssetVariant.SLIDE, now=1_100)
    refreshed = transport.publish(image, variant=VisualAssetVariant.SLIDE, now=1_700)

    assert reused == first
    assert refreshed != first


def test_caller_slide_image_is_copied_to_controlled_cas_before_publish(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external" / "caller-render.bin"
    external.parent.mkdir()
    external.write_bytes(PNG_1X1)
    render_root = tmp_path / "artifacts" / "slide-renders"
    config = VisualAssetTransportConfig.from_environment(
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com",
            "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
        }
    )
    transport = SignedUrlVisualAssetTransport(
        config=config,
        catalog=VisualAssetCatalog({VisualAssetVariant.SLIDE: (render_root,)}),
        content_store=VisualAssetCAS(render_root / "visual-cas"),
    )

    normalized = transport.prepare_slide_images((external,))
    imported = Path(normalized[0].uri)

    assert normalized[0].page_number == 1
    assert normalized[0].media_type == "image/png"
    assert imported.read_bytes() == PNG_1X1
    assert imported.is_relative_to((render_root / "visual-cas").resolve())
    assert imported.name == f"{normalized[0].sha256}.png"
    with pytest.raises(VisualAssetAccessError):
        transport.publish(external, variant=VisualAssetVariant.SLIDE)
    signed_url = transport.publish(
        imported,
        variant=VisualAssetVariant.SLIDE,
        expected_sha256=normalized[0].sha256,
    )
    assert f"/slide/{normalized[0].sha256}?" in signed_url

    external.write_bytes(b"the caller may mutate its own file later")
    assert imported.read_bytes() == PNG_1X1


def test_visual_cas_binds_declared_digest_and_media_type(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.png"
    external.write_bytes(PNG_1X1)
    render_root = tmp_path / "slide-renders"
    config = VisualAssetTransportConfig.from_environment(
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com",
            "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
        }
    )
    transport = SignedUrlVisualAssetTransport(
        config=config,
        catalog=VisualAssetCatalog({VisualAssetVariant.SLIDE: (render_root,)}),
        content_store=VisualAssetCAS(render_root / "visual-cas"),
    )

    wrong_digest = ModelImageInput(
        page_number=1,
        uri=str(external),
        media_type="image/png",
        sha256="0" * 64,
    )
    wrong_media = ModelImageInput(
        page_number=1,
        uri=str(external),
        media_type="image/jpeg",
        sha256=ModelImageInput.from_path(external, page_number=1).sha256,
    )
    with pytest.raises(VisualAssetAccessError, match="integrity"):
        transport.prepare_slide_images((wrong_digest,))
    with pytest.raises(VisualAssetAccessError, match="media type"):
        transport.prepare_slide_images((wrong_media,))


def test_visual_cas_deduplicates_content_and_fails_closed_if_entry_changes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(PNG_1X1)
    second.write_bytes(PNG_1X1)
    content_store = VisualAssetCAS(tmp_path / "visual-cas")

    first_asset = content_store.import_image(
        first,
        variant=VisualAssetVariant.SLIDE,
    )
    second_asset = content_store.import_image(
        second,
        variant=VisualAssetVariant.SLIDE,
    )
    assert second_asset.path == first_asset.path

    first_asset.path.write_bytes(PNG_1X1 + b"tampered")
    with pytest.raises(VisualAssetAccessError, match="size validation"):
        content_store.import_image(second, variant=VisualAssetVariant.SLIDE)


def test_profile84_model_image_cas_strips_metadata_and_pptx_trailer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "render-with-hidden-content.png"
    encoded = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "must-not-leave-the-host")
    Image.new("RGB", (24, 12), "#3366CC").save(
        encoded,
        format="PNG",
        pnginfo=metadata,
    )
    hidden_pptx = build_pptx(tmp_path / "hidden-source.pptx").read_bytes()
    source.write_bytes(encoded.getvalue() + hidden_pptx)

    store = CanonicalModelImageCAS(tmp_path / "model-image-cas")
    normalized = store.prepare_slide_images((source,), expected_page_count=1)
    output = Path(normalized[0].uri)
    output_bytes = output.read_bytes()

    assert normalized[0].media_type == "image/png"
    assert not output_bytes.endswith(hidden_pptx)
    assert b"must-not-leave-the-host" not in output_bytes
    verified_raster_image(
        output,
        expected_sha256=normalized[0].sha256,
        expected_media_type="image/png",
        require_exact_container=True,
    )
    with Image.open(output) as decoded:
        assert decoded.size == (24, 12)
        assert "private-note" not in decoded.info


def test_profile84_canonicalizes_renderer_output_before_model_use(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "renderer-source.pptx")
    hidden_pptx = build_pptx(tmp_path / "renderer-hidden.pptx").read_bytes()
    renderer_payload = PNG_1X1 + hidden_pptx
    runtime = LocalEvaluationRuntime(
        tmp_path / "var",
        slide_renderer=_TrailerRenderer(renderer_payload),
        review_rendering=True,
    )

    artifacts, versions = runtime._prepare_model_artifacts(
        EvalCase(
            case_id="renderer-normalization",
            scene=SceneType.READY_MADE,
            pptx_path=str(deck),
        ),
        default_profile(SceneType.READY_MADE),
        None,
    )

    model_images = artifacts["slide_images"]
    assert isinstance(model_images, tuple)
    assert isinstance(model_images[0], ModelImageInput)
    assert not Path(model_images[0].uri).read_bytes().endswith(hidden_pptx)
    assert artifacts["render_result"].slide_images[0].read_bytes() == renderer_payload
    assert artifacts["model_audit_rendering"]["rendered_page_set_sha256"]
    assert versions == {
        "model_audit_slides/trailer-renderer": "1.0",
        "model_audit_slides/canonical-model-image-cas": "1.0.0",
    }


@pytest.mark.parametrize("source_format", ["PNG", "JPEG", "WEBP"])
def test_profile84_model_image_cas_accepts_supported_single_frame_formats(
    tmp_path: Path,
    source_format: str,
) -> None:
    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[source_format]
    source = tmp_path / f"source{suffix}"
    Image.new("RGB", (32, 18), "#DB2777").save(source, format=source_format)

    normalized = CanonicalModelImageCAS(tmp_path / "cas").prepare_slide_images(
        (source,),
        expected_page_count=1,
    )

    assert normalized[0].media_type == "image/png"
    with Image.open(normalized[0].uri) as image:
        assert image.format == "PNG"
        assert image.size == (32, 18)


def test_profile84_model_image_cas_enforces_decoded_pixel_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "four-pixels.png"
    Image.new("RGB", (2, 2), "white").save(source, format="PNG")

    with pytest.raises(VisualAssetAccessError, match="dimensions"):
        CanonicalModelImageCAS(
            tmp_path / "cas",
            max_pixels=3,
        ).prepare_slide_images((source,), expected_page_count=1)


def test_profile84_normalizes_external_images_for_base64_and_signed_transport(
    tmp_path: Path,
) -> None:
    deck = build_pptx(tmp_path / "deck.pptx")
    external = tmp_path / "outside-render-cache.png"
    external.write_bytes(PNG_1X1)
    case = EvalCase(
        case_id="caller-render",
        scene=SceneType.READY_MADE,
        pptx_path=str(deck),
    )
    profile = default_profile(SceneType.READY_MADE)

    base64_root = tmp_path / "base64-var"
    base64_runtime = LocalEvaluationRuntime(base64_root)
    normalized_base64, unchanged_versions = base64_runtime._prepare_model_artifacts(
        case,
        profile,
        {"slide_images": (external,)},
    )
    base64_images = normalized_base64["slide_images"]
    assert isinstance(base64_images, tuple)
    assert isinstance(base64_images[0], ModelImageInput)
    assert Path(base64_images[0].uri).is_relative_to(
        (base64_root / "artifacts" / "slide-renders" / "model-image-cas").resolve()
    )
    assert unchanged_versions == {
        "model_audit_slides/canonical-model-image-cas": "1.0.0"
    }

    legacy, legacy_versions = base64_runtime._prepare_model_artifacts(
        case,
        profile_for_version(SceneType.READY_MADE, "8.3"),
        {"slide_images": (external,)},
    )
    assert legacy["slide_images"] == (external,)
    assert legacy_versions == {}

    signed_root = tmp_path / "signed-var"
    render_root = signed_root / "artifacts" / "slide-renders"
    config = VisualAssetTransportConfig.from_environment(
        {
            "PPT_EVAL_VISUAL_ASSET_TRANSPORT": "signed-url",
            "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL": "https://assets.example.com",
            "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET": _SECRET,
        }
    )
    transport = SignedUrlVisualAssetTransport(
        config=config,
        catalog=VisualAssetCatalog({VisualAssetVariant.SLIDE: (render_root,)}),
        content_store=VisualAssetCAS(render_root / "visual-cas"),
    )
    signed_runtime = LocalEvaluationRuntime(
        signed_root,
        visual_asset_transport=transport,
    )
    normalized, normalized_versions = signed_runtime._prepare_model_artifacts(
        case,
        profile,
        {
            "render_result": RenderResult(
                "caller-renderer",
                "1.0",
                (external,),
            )
        },
    )

    normalized_images = normalized["slide_images"]
    assert isinstance(normalized_images, tuple)
    assert len(normalized_images) == 1
    assert isinstance(normalized_images[0], ModelImageInput)
    assert Path(normalized_images[0].uri).is_relative_to(
        (render_root / "model-image-cas").resolve()
    )
    assert normalized_versions == {
        "model_audit_slides/canonical-model-image-cas": "1.0.0"
    }


def test_signed_asset_endpoint_serves_only_registered_untampered_visuals(
    tmp_path: Path,
) -> None:
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    with patch.dict(os.environ, _signed_url_environment()):
        client = make_test_client(lambda: create_app(runtime))
    transport = client.app.state.visual_asset_transport
    assert isinstance(transport, SignedUrlVisualAssetTransport)

    slide_root = runtime.paths.render_cache / "fixture-cache"
    slide_root.mkdir(parents=True)
    image = slide_root / "Slide1.png"
    image.write_bytes(PNG_1X1)
    public_url = transport.publish(image, variant=VisualAssetVariant.SLIDE)
    parsed = urlsplit(public_url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "model-assets.example.com"
    assert parsed.path.startswith("/eval/v1/model-assets/slide/")
    response = client.get(f"{parsed.path.removeprefix('/eval')}?{parsed.query}")
    assert response.status_code == 200
    assert response.content == PNG_1X1
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"].startswith("public, max-age=")

    invalid_signature = parsed.query[:-1] + ("0" if parsed.query[-1] != "0" else "1")
    rejected = client.get(
        f"{parsed.path.removeprefix('/eval')}?{invalid_signature}"
    )
    assert rejected.status_code == 403

    image.write_bytes(PNG_1X1 + b"changed")
    changed = client.get(f"{parsed.path.removeprefix('/eval')}?{parsed.query}")
    assert changed.status_code == 404


def test_signed_asset_endpoint_never_streams_path_replaced_after_validation(
    tmp_path: Path,
) -> None:
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    with patch.dict(os.environ, _signed_url_environment()):
        client = make_test_client(lambda: create_app(runtime))
    transport = client.app.state.visual_asset_transport

    slide_root = runtime.paths.render_cache / "fixture-cache"
    slide_root.mkdir(parents=True)
    image = slide_root / "Slide1.png"
    image.write_bytes(PNG_1X1)
    public_url = transport.publish(image, variant=VisualAssetVariant.SLIDE)
    parsed = urlsplit(public_url)
    replacement_pptx = build_pptx(tmp_path / "must-not-leak.pptx").read_bytes()
    original_resolve = transport.catalog.resolve

    def resolve_then_replace(**kwargs):
        asset = original_resolve(**kwargs)
        image.write_bytes(replacement_pptx)
        return asset

    with patch.object(transport.catalog, "resolve", side_effect=resolve_then_replace):
        response = client.get(f"{parsed.path.removeprefix('/eval')}?{parsed.query}")

    assert response.status_code == 404
    assert replacement_pptx not in response.content


def test_signed_asset_endpoint_rejects_expired_and_unknown_variants(
    tmp_path: Path,
) -> None:
    runtime = LocalEvaluationRuntime(tmp_path / "var")
    with patch.dict(os.environ, _signed_url_environment()):
        client = make_test_client(lambda: create_app(runtime))
    transport = client.app.state.visual_asset_transport

    slide_root = runtime.paths.render_cache / "fixture-cache"
    slide_root.mkdir(parents=True)
    image = slide_root / "Slide1.png"
    image.write_bytes(PNG_1X1)
    public_url = transport.publish(
        image,
        variant=VisualAssetVariant.SLIDE,
        now=int(time.time()) - DEFAULT_VISUAL_ASSET_TTL_SECONDS - 1,
    )
    parsed = urlsplit(public_url)
    expired = client.get(f"{parsed.path.removeprefix('/eval')}?{parsed.query}")
    assert expired.status_code == 410

    unknown = client.get(
        f"/v1/model-assets/source_pptx/{'0' * 64}?expires=1&signature={'0' * 64}"
    )
    assert unknown.status_code == 404


def test_create_app_rejects_partial_signed_url_environment(
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("python_multipart")
    environment = _visual_environment(
        PPT_EVAL_VISUAL_ASSET_TRANSPORT="signed-url",
        PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL="https://model-assets.example.com",
    )
    with patch.dict(os.environ, environment):
        with pytest.raises(VisualAssetConfigurationError):
            create_app(LocalEvaluationRuntime(tmp_path / "var"))


def test_environment_runtime_and_api_share_the_same_signed_asset_catalog(
    tmp_path: Path,
) -> None:
    environment = {
        **_signed_url_environment(),
        "PPT_EVAL_QWEN_AUDIT_ENABLED": "false",
        "PPT_EVAL_ZHIPU_AUDIT_ENABLED": "false",
    }
    runtime = build_runtime_from_environment(
        tmp_path / "var",
        environment=environment,
        workspace_root=tmp_path,
    )
    transport = runtime.visual_asset_transport
    assert isinstance(transport, SignedUrlVisualAssetTransport)

    with patch.dict(os.environ, _visual_environment()):
        client = make_test_client(lambda: create_app(runtime))
    assert client.app.state.visual_asset_transport is transport
    assert client.app.state.visual_asset_transport_config["mode"] == "signed-url"
