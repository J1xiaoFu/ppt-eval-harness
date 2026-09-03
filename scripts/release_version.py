"""Check or synchronize the repository's independent version axes.

``release/version-matrix.json`` is the machine-readable release declaration.
The default command is deliberately read-only::

    python scripts/release_version.py --check

After changing ``product.version`` and writing the corresponding changelog
entry, ``--write`` updates only the mechanical product-version surfaces.  It
never changes Profile, Composite, Oracle, prompt, selection, schema, API, or
Attention-policy versions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def load_version_matrix(root: Path = REPOSITORY_ROOT) -> Mapping[str, Any]:
    path = root / "release" / "version-matrix.json"
    matrix = _mapping(_read_json(path), "version matrix")
    required = {
        "schema_version",
        "product",
        "evaluation",
        "contracts",
        "infrastructure",
    }
    missing = required - set(matrix)
    if missing:
        raise ValueError(f"version matrix is missing {sorted(missing)}")
    product = _mapping(matrix["product"], "product")
    version = product.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ValueError("product.version must be a SemVer release without a prefix")
    return matrix


def _constant(path: Path, name: str) -> str | None:
    match = re.search(
        rf'(?m)^{re.escape(name)}\s*=\s*["\']([^"\']+)["\']\s*;?\s*$',
        path.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def _record(issues: list[str], label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        issues.append(f"{label}: expected {expected!r}, found {actual!r}")


def _regex_value(path: Path, pattern: str) -> str | None:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def check_repository(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Return human-readable drift findings without changing the repository."""

    matrix = load_version_matrix(root)
    product = _mapping(matrix["product"], "product")
    evaluation = _mapping(matrix["evaluation"], "evaluation")
    contracts = _mapping(matrix["contracts"], "contracts")
    infrastructure = _mapping(matrix["infrastructure"], "infrastructure")
    product_version = product["version"]
    issues: list[str] = []

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    ui_package = _mapping(_read_json(root / "ui" / "package.json"), "ui/package.json")
    _record(issues, "pyproject product version", project["project"]["version"], product_version)
    _record(issues, "Python product version", _constant(root / "src/ppt_eval/version.py", "__version__"), product_version)
    _record(issues, "UI package version", ui_package.get("version"), product_version)
    _record(issues, "release-test product version", _constant(root / "tests/test_release_version.py", "PRODUCT_VERSION"), product_version)

    openapi_path = root / "docs" / "openapi.yaml"
    openapi = openapi_path.read_text(encoding="utf-8")
    info = openapi.split("paths:", 1)[0]
    _record(issues, "OpenAPI info version", _regex_value(openapi_path, r"^  version: ([^\s]+)$"), product_version)
    _record(issues, "OpenAPI product description", _regex_value(openapi_path, r"Product release ([0-9]+\.[0-9]+\.[0-9]+)"), product_version)
    _record(issues, "OpenAPI service version", _regex_value(openapi_path, r'service_version: \{ const: "([^"]+)" \}'), product_version)
    if f"version: {product_version}" not in info:
        issues.append("OpenAPI product version must be declared in the info block")

    readme_path = root / "README.md"
    _record(issues, "README product version", _regex_value(readme_path, r"^\| \u4ea7\u54c1/\u8f6f\u4ef6\u53d1\u5e03 \| `([^`]+)`"), product_version)
    _record(issues, "README current-release prose", _regex_value(readme_path, r"\u4ea7\u54c1\u5347\u5230 `([^`]+)`"), product_version)
    _record(issues, "review-platform product version", _regex_value(root / "docs/review_platform.md", r"\u4ea7\u54c1\u53d1\u5e03 `([^`]+)`"), product_version)
    _record(issues, "audit README product version", _regex_value(root / "audit/README.md", r"\u4ea7\u54c1\u53d1\u5e03 `([^`]+)`"), product_version)
    _record(issues, "latest changelog release", _regex_value(root / "CHANGELOG.md", r"^## ([0-9]+\.[0-9]+\.[0-9]+)\s+-"), product_version)

    demo = (root / "ui/src/demo.ts").read_text(encoding="utf-8")
    demo_versions = re.findall(r'service_version:\s*"([^"]+)"', demo)
    if not demo_versions:
        issues.append("UI demo declares no service_version")
    for index, actual in enumerate(demo_versions, start=1):
        _record(issues, f"UI demo service version #{index}", actual, product_version)

    profile_version = evaluation["default_profile_version"]
    lifecycle = evaluation["profile_lifecycle"]
    profile_paths = sorted((root / "src/ppt_eval/profiles").glob("*_v8.json"))
    if len(profile_paths) != 4:
        issues.append(f"expected four default v8 Profiles, found {len(profile_paths)}")
    for path in profile_paths:
        profile = _mapping(_read_json(path), str(path.relative_to(root)))
        _record(issues, f"{path.name} Profile version", profile.get("version"), profile_version)
        metadata = _mapping(profile.get("metadata"), f"{path.name} metadata")
        _record(issues, f"{path.name} lifecycle", metadata.get("lifecycle"), lifecycle)

    _record(issues, "UI Profile version", _constant(root / "ui/src/version.ts", "export const PROFILE_VERSION"), profile_version)
    _record(issues, "Composite version", _constant(root / "src/ppt_eval/oracles/v8_composites.py", "V8_QUALITY_VERSION"), evaluation["composite_version"])
    _record(issues, "Atomic Observation version", _constant(root / "src/ppt_eval/oracles/v8_atomic.py", "V8_ATOMIC_VERSION"), evaluation["atomic_observation_version"])

    grounded = _mapping(evaluation["grounded_vlm_versions"], "grounded_vlm_versions")
    model_audits = root / "src/ppt_eval/oracles/model_audits.py"
    _record(issues, "grounded visual Oracle/prompt version", _constant(model_audits, "V8_GROUNDED_VLM_ORACLE_VERSION"), grounded["visual"])
    _record(issues, "authorship Oracle/prompt version", _constant(model_audits, "V8_AUTHORSHIP_VLM_ORACLE_VERSION"), grounded["authorship"])
    _record(issues, "raster-text Oracle/prompt version", _constant(model_audits, "V8_RASTER_TEXT_VLM_ORACLE_VERSION"), grounded["raster_text"])
    _record(issues, "selection-policy version", _constant(model_audits, "_GROUNDED_PAGE_SELECTION_STRATEGY_VERSION"), evaluation["selection_policy_version"])

    schema_version = contracts["eval_report_schema_version"]
    _record(issues, "EvalReport schema version", _constant(root / "src/ppt_eval/domain/models.py", "SCHEMA_VERSION"), schema_version)
    _record(issues, "project-audit schema version", _regex_value(root / "audit/schema/project_audit.schema.json", r'"schema_version"\s*:\s*\{\s*"const"\s*:\s*"([^"]+)"'), contracts["audit_schema_version"])
    _record(issues, "runtime-audit schema version", _regex_value(root / "audit/schema/audit_event.schema.json", r'"schema_version"\s*:\s*\{\s*"const"\s*:\s*"([^"]+)"'), contracts["audit_schema_version"])
    _record(issues, "model-audit schema version", _constant(root / "src/ppt_eval/adapters/model_audits.py", "MODEL_AUDIT_SCHEMA_VERSION"), contracts["model_audit_schema_version"])
    _record(issues, "Attention policy version", _constant(root / "src/ppt_eval/application/audit_projection.py", "TRIAGE_POLICY_VERSION"), contracts["attention_policy_version"])
    _record(
        issues,
        "render-manifest schema version",
        _constant(root / "src/ppt_eval/runtime.py", "_RENDER_MANIFEST_SCHEMA_VERSION"),
        infrastructure["render_manifest_schema_version"],
    )
    _record(
        issues,
        "visual-asset transport version",
        _constant(
            root / "src/ppt_eval/infrastructure/visual_assets.py",
            "VISUAL_ASSET_TRANSPORT_VERSION",
        ),
        infrastructure["visual_asset_transport_version"],
    )
    _record(
        issues,
        "Qwen context-cache wire version",
        _constant(
            root / "src/ppt_eval/infrastructure/qwen_model_audits.py",
            "QWEN_CONTEXT_CACHE_WIRE_VERSION",
        ),
        infrastructure["qwen_context_cache_wire_version"],
    )

    namespace = contracts["http_api_namespace"]
    declared_api_paths = re.findall(r"^  (/v\d+[^:]*):", openapi, re.MULTILINE)
    if not declared_api_paths:
        issues.append("OpenAPI declares no versioned API paths")
    for path in declared_api_paths:
        if path != namespace and not path.startswith(f"{namespace}/"):
            issues.append(f"OpenAPI path {path!r} is outside namespace {namespace!r}")
    return issues


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    source = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"could not find exactly one product-version surface in {path}")
    path.write_text(updated, encoding="utf-8")


def sync_product_surfaces(root: Path, product_version: str) -> None:
    """Write only mechanical product-version surfaces from the version matrix."""

    if SEMVER.fullmatch(product_version) is None:
        raise ValueError("product_version must be SemVer")
    _replace_once(root / "pyproject.toml", r'^(version = ")[^"]+("\s*)$', rf"\g<1>{product_version}\g<2>")
    _replace_once(root / "src/ppt_eval/version.py", r'^(__version__ = ")[^"]+("\s*)$', rf"\g<1>{product_version}\g<2>")
    _replace_once(root / "tests/test_release_version.py", r'^(PRODUCT_VERSION = ")[^"]+("\s*)$', rf"\g<1>{product_version}\g<2>")

    package_path = root / "ui/package.json"
    package = _mapping(_read_json(package_path), "ui/package.json")
    updated_package = dict(package)
    updated_package["version"] = product_version
    package_path.write_text(json.dumps(updated_package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    openapi = root / "docs/openapi.yaml"
    _replace_once(openapi, r"^(  version: )[^\s]+$", rf"\g<1>{product_version}")
    _replace_once(openapi, r"(Product release )[0-9]+\.[0-9]+\.[0-9]+", rf"\g<1>{product_version}")
    _replace_once(openapi, r'(service_version: \{ const: ")[^"]+(" \})', rf"\g<1>{product_version}\g<2>")

    _replace_once(root / "README.md", r"^(\| \u4ea7\u54c1/\u8f6f\u4ef6\u53d1\u5e03 \| `)[^`]+(` \|)", rf"\g<1>{product_version}\g<2>")
    _replace_once(root / "README.md", r"(\u4ea7\u54c1\u5347\u5230 `)[^`]+(` \u4e0d\u6539\u53d8 Profile)", rf"\g<1>{product_version}\g<2>")
    _replace_once(root / "docs/review_platform.md", r"(\u4ea7\u54c1\u53d1\u5e03 `)[^`]+(`)", rf"\g<1>{product_version}\g<2>")
    _replace_once(root / "audit/README.md", r"(\u4ea7\u54c1\u53d1\u5e03 `)[^`]+(`)", rf"\g<1>{product_version}\g<2>")

    demo_path = root / "ui/src/demo.ts"
    demo = demo_path.read_text(encoding="utf-8")
    demo, count = re.subn(r'(service_version:\s*")[^"]+("\s*,)', rf"\g<1>{product_version}\g<2>", demo)
    if count < 1:
        raise ValueError("UI demo declares no service_version")
    demo_path.write_text(demo, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check for version drift (default)")
    mode.add_argument("--write", action="store_true", help="sync product-version surfaces from the matrix")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()

    try:
        matrix = load_version_matrix(root)
        if args.write:
            product = _mapping(matrix["product"], "product")
            sync_product_surfaces(root, str(product["version"]))
        issues = check_repository(root)
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"release-version check failed: {exc}", file=sys.stderr)
        return 2
    if issues:
        print("release-version drift detected:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"release versions are synchronized at product {matrix['product']['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
