from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from ppt_eval.adapters import ModelImageInput, RenderResult
from ppt_eval.api import create_app
from ppt_eval.config import default_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure.visual_assets import (
    DEFAULT_VISUAL_ASSET_TTL_SECONDS,
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
)
from ppt_eval.runtime import LocalEvaluationRuntime, build_runtime_from_environment
from tests.fixtures.api_client import make_test_client
from tests.fixtures.pptx_factory import PNG_1X1, build_pptx

_ENVIRONMENT_KEYS = (
    "PPT_EVAL_VISUAL_ASSET_TRANSPORT",
    "PPT_EVAL_VISUAL_ASSET_PUBLIC_BASE_URL",
    "PPT_EVAL_VISUAL_ASSET_SIGNING_SECRET",
    "PPT_EVAL_VISUAL_ASSET_URL_TTL_SECONDS",
)
_SECRET = "test-only-signing-secret-with-at-least-32-bytes"


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


def test_signed_runtime_normalizes_external_images_while_base64_is_unchanged(
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
    unchanged, unchanged_versions = base64_runtime._prepare_model_artifacts(
        case,
        profile,
        {"slide_images": (external,)},
    )
    assert unchanged["slide_images"] == (external,)
    assert unchanged_versions == {}
    assert not (base64_root / "artifacts" / "slide-renders" / "visual-cas").exists()

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
        (render_root / "visual-cas").resolve()
    )
    assert normalized_versions == {}


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
