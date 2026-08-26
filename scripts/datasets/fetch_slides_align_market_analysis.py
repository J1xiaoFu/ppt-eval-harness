"""Fetch the largest paired PPTX/PNG slice for one pinned Slides-Align topic.

The source dataset is intentionally kept in ``research_quarantine``.  Public
availability and the dataset-card license do not clear the generated decks for
production, commercial, or training use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://huggingface.co/datasets/Yqy6/Slides-Align"
REVISION = "2f50ac6674a506acb245275e58c8a452c00e6a14"
RESOLVE_BASE = f"{REPOSITORY}/resolve/{REVISION}"
TREE_API_BASE = f"https://huggingface.co/api/datasets/Yqy6/Slides-Align/tree/{REVISION}"
ALLOWED_USE = ("research_quarantine",)
DIFFICULTY = "topic_introduction"
TOPIC = "market_analysis"

DATASET_CARD_SHA256 = "2222cff56ba10030b254e8263d93ac17e32fe0e1d074cd4614c153703fb91604"
DATASET_CARD_GIT_OID = "8cf1ec7711cc6fa820d9c25b4c6004b4eaec72ae"
RANKING_SHA256 = "5a61f25ede8eb1b2d050b7b40d72cd1428e3d8fa83a5461a85025c6d17059669"
RANKING_GIT_OID = "2a92bf71f87c473ea7cdb9255c856f6401904571"

# Human labels differ from repository directory names for the two Skywork products.
PRODUCTS: Mapping[str, tuple[str, str]] = {
    "Kimi-Banana": ("Kimi-Banana", "kimi_banana"),
    "Skywork-Banana": ("Skyworks-Banana", "skyworks_banana"),
    "Quake": ("Quake", "quake"),
    "Kimi-Smart": ("Kimi-Smart", "kimi_smart"),
    "Kimi-Standard": ("Kimi-Standard", "kimi_standard"),
    "Gamma": ("Gamma", "gamma"),
    "Zhipu": ("Zhipu", "zhipu"),
    "Skywork": ("Skyworks", "skyworks"),
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SLIDE_XML_RE = re.compile(r"ppt/slides/slide\d+\.xml")
_SLIDE_PNG_RE = re.compile(r"slide_(\d{4})\.png")
_USER_AGENT = "ppt-eval-harness-dataset-prep/1.0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_matches(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    """Return whether a local file matches an explicit byte/hash contract."""

    return (
        path.is_file()
        and path.stat().st_size == expected_bytes
        and sha256(path) == expected_sha256
    )


def validate_rankings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select and validate the complete human ranking for the pinned topic."""

    rows = [
        dict(item)
        for item in payload.get("results", [])
        if item.get("difficulty") == DIFFICULTY and item.get("topic") == TOPIC
    ]
    if len(rows) != len(PRODUCTS):
        raise ValueError(
            f"expected {len(PRODUCTS)} ranked products for {DIFFICULTY}/{TOPIC}, got {len(rows)}"
        )
    labels = {str(item.get("product")) for item in rows}
    if labels != set(PRODUCTS):
        raise ValueError(
            f"unexpected product labels: missing={sorted(set(PRODUCTS) - labels)}, "
            f"extra={sorted(labels - set(PRODUCTS))}"
        )
    ranks = [int(item["rank"]) for item in rows]
    expected_ranks = list(range(1, len(PRODUCTS) + 1))
    if sorted(ranks) != expected_ranks:
        raise ValueError(f"expected unique ranks {expected_ranks}, got {sorted(ranks)}")
    return sorted(rows, key=lambda item: int(item["rank"]))


def pptx_slide_count(path: Path) -> int:
    """Validate the ZIP package CRCs and return its OOXML slide count."""

    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"PPTX CRC failure in {bad_member}")
            slides = [name for name in archive.namelist() if _SLIDE_XML_RE.fullmatch(name)]
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid PPTX ZIP package: {path}") from exc
    if not slides:
        raise ValueError(f"PPTX has no slide XML parts: {path}")
    return len(slides)


def _source_url(source_path: str) -> str:
    encoded = urllib.parse.quote(source_path, safe="/")
    return f"{RESOLVE_BASE}/{encoded}"


def _next_link(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="?next"?', part)
        if match:
            candidate = match.group(1)
            parsed = urllib.parse.urlparse(candidate)
            if parsed.scheme != "https" or parsed.netloc != "huggingface.co":
                raise ValueError(f"refusing unexpected pagination host: {candidate}")
            if not parsed.path.startswith(
                f"/api/datasets/Yqy6/Slides-Align/tree/{REVISION}"
            ):
                raise ValueError(f"refusing pagination outside pinned tree: {candidate}")
            return candidate
    return None


def _tree(path: str = "") -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(path, safe="/")
    suffix = f"/{encoded}" if encoded else ""
    url: str | None = f"{TREE_API_BASE}{suffix}?recursive=false&expand=false"
    records: list[dict[str, Any]] = []
    while url:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                    link = response.headers.get("Link")
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if not isinstance(payload, list):
            raise ValueError(f"unexpected tree response for pinned path {path!r}")
        records.extend(dict(item) for item in payload)
        url = _next_link(link)
    return records


def _lfs_contract(metadata: Mapping[str, Any]) -> tuple[int, str]:
    lfs = metadata.get("lfs")
    if not isinstance(lfs, Mapping):
        raise ValueError(f"upstream file lacks LFS hash: {metadata.get('path')}")
    expected_bytes = int(lfs["size"])
    expected_hash = str(lfs["oid"]).lower()
    if not _SHA256_RE.fullmatch(expected_hash):
        raise ValueError(f"invalid upstream LFS SHA-256: {metadata.get('path')}")
    if int(metadata["size"]) != expected_bytes:
        raise ValueError(f"upstream size disagreement: {metadata.get('path')}")
    return expected_bytes, expected_hash


def _download_verified(
    *,
    source_path: str,
    destination: Path,
    expected_bytes: int,
    expected_sha256: str,
) -> bool:
    """Download atomically, retaining a valid existing file when possible."""

    if file_matches(destination, expected_bytes, expected_sha256):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = _source_url(source_path)
    last_error: Exception | None = None
    for attempt in range(3):
        temporary: Path | None = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                with urllib.request.urlopen(request, timeout=180) as response:
                    shutil.copyfileobj(response, stream, length=1024 * 1024)
            if not file_matches(temporary, expected_bytes, expected_sha256):
                actual_bytes = temporary.stat().st_size
                actual_hash = sha256(temporary)
                raise ValueError(
                    f"upstream integrity mismatch for {source_path}: "
                    f"expected {expected_bytes}/{expected_sha256}, "
                    f"got {actual_bytes}/{actual_hash}"
                )
            temporary.replace(destination)
            return True
        except Exception as exc:
            last_error = exc
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _local_file_record(
    *,
    source_path: str,
    local_path: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    expected_bytes, expected_hash = _lfs_contract(metadata)
    return {
        "path": local_path,
        "source_path": source_path,
        "source_url": _source_url(source_path),
        "bytes": expected_bytes,
        "sha256": expected_hash,
        "upstream_lfs_sha256": expected_hash,
        "upstream_git_oid": str(metadata["oid"]),
    }


def _download_lfs_record(
    *,
    output: Path,
    local_path: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    source_path = str(metadata["path"])
    record = _local_file_record(
        source_path=source_path,
        local_path=local_path,
        metadata=metadata,
    )
    _download_verified(
        source_path=source_path,
        destination=output / PurePosixPath(local_path),
        expected_bytes=int(record["bytes"]),
        expected_sha256=str(record["sha256"]),
    )
    return record


def _download_metadata(
    *,
    output: Path,
    source_path: str,
    local_path: str,
    metadata: Mapping[str, Any],
    expected_sha256: str,
    expected_git_oid: str,
) -> dict[str, Any]:
    if str(metadata.get("oid")) != expected_git_oid:
        raise ValueError(f"unexpected Git object for {source_path} at pinned revision")
    expected_bytes = int(metadata["size"])
    _download_verified(
        source_path=source_path,
        destination=output / PurePosixPath(local_path),
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
    )
    return {
        "path": local_path,
        "source_path": source_path,
        "source_url": _source_url(source_path),
        "bytes": expected_bytes,
        "sha256": expected_sha256,
        "upstream_git_oid": expected_git_oid,
    }


def _validate_slide_images(
    source_directory: str, records: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    prefix = f"{source_directory}/slide_images/"
    images = [
        dict(item)
        for item in records
        if item.get("type") == "file"
        and str(item.get("path", "")).startswith(prefix)
        and str(item.get("path", "")).endswith(".png")
    ]
    images.sort(key=lambda item: str(item["path"]))
    if not images:
        raise ValueError(f"no upstream slide PNGs for {source_directory}")
    indices: list[int] = []
    for item in images:
        name = PurePosixPath(str(item["path"])).name
        match = _SLIDE_PNG_RE.fullmatch(name)
        if not match:
            raise ValueError(f"unexpected slide image name: {item['path']}")
        _lfs_contract(item)
        indices.append(int(match.group(1)))
    expected = list(range(1, len(images) + 1))
    if indices != expected:
        raise ValueError(f"non-contiguous slide images for {source_directory}: {indices}")
    return images


def fetch(output: Path) -> dict[str, Any]:
    """Prepare the pinned paired slice and return its integrity manifest."""

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    root_files = {str(item["path"]): item for item in _tree() if item.get("type") == "file"}
    dataset_card = _download_metadata(
        output=output,
        source_path="README.md",
        local_path="source/README.source.md",
        metadata=root_files["README.md"],
        expected_sha256=DATASET_CARD_SHA256,
        expected_git_oid=DATASET_CARD_GIT_OID,
    )
    ranking_file = _download_metadata(
        output=output,
        source_path="Slides-Align.json",
        local_path="rankings/Slides-Align.json",
        metadata=root_files["Slides-Align.json"],
        expected_sha256=RANKING_SHA256,
        expected_git_oid=RANKING_GIT_OID,
    )
    ranking_payload = json.loads(
        (output / "rankings" / "Slides-Align.json").read_text(encoding="utf-8")
    )
    ranking_rows = validate_rankings(ranking_payload)

    flat_files: list[dict[str, Any]] = [dataset_card, ranking_file]
    artifacts: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    subset_rows: list[dict[str, Any]] = []
    total_artifact_bytes = 0
    total_rendered_slides = 0

    for row in ranking_rows:
        product_label = str(row["product"])
        human_rank = int(row["rank"])
        directory_product, slug = PRODUCTS[product_label]
        source_directory = f"{directory_product}/{DIFFICULTY}/{TOPIC}"
        topic_entries = _tree(source_directory)
        image_entries = _validate_slide_images(
            source_directory,
            _tree(f"{source_directory}/slide_images"),
        )
        pptx_entries = [
            dict(item)
            for item in topic_entries
            if item.get("type") == "file" and str(item.get("path", "")).endswith(".pptx")
        ]
        if len(pptx_entries) > 1:
            raise ValueError(f"multiple PPTX files for {source_directory}")
        selected = len(pptx_entries) == 1
        subset_rows.append(
            {
                "product": product_label,
                "difficulty": DIFFICULTY,
                "topic": TOPIC,
                "rank": human_rank,
                "selected": selected,
                "availability": "paired_pptx_png" if selected else "png_only_no_pptx",
            }
        )
        if not selected:
            image_directory = next(
                (
                    item
                    for item in topic_entries
                    if item.get("type") == "directory"
                    and str(item.get("path")) == f"{source_directory}/slide_images"
                ),
                None,
            )
            unavailable.append(
                {
                    "product_label": product_label,
                    "directory_product": directory_product,
                    "human_rank": human_rank,
                    "source_directory": source_directory,
                    "reason": "no_pptx_at_pinned_revision",
                    "upstream_slide_png_count": len(image_entries),
                    "upstream_slide_png_bytes": sum(int(item["size"]) for item in image_entries),
                    "upstream_image_directory_git_oid": (
                        str(image_directory["oid"]) if image_directory else None
                    ),
                }
            )
            continue

        pptx_meta = pptx_entries[0]
        deck_local = f"decks/{slug}_rank_{human_rank}.pptx"
        pptx_record = _download_lfs_record(
            output=output,
            local_path=deck_local,
            metadata=pptx_meta,
        )
        render_directory = f"renders/{slug}_rank_{human_rank}"
        render_records: list[dict[str, Any]] = []
        for image_meta in image_entries:
            name = PurePosixPath(str(image_meta["path"])).name
            render_records.append(
                _download_lfs_record(
                    output=output,
                    local_path=f"{render_directory}/{name}",
                    metadata=image_meta,
                )
            )
        deck_path = output / PurePosixPath(deck_local)
        slide_count = pptx_slide_count(deck_path)
        if slide_count != len(render_records):
            raise ValueError(
                f"PPTX/render count mismatch for {product_label}: "
                f"{slide_count} slides vs {len(render_records)} PNGs"
            )
        flat_files.append(pptx_record)
        flat_files.extend(render_records)
        artifact_bytes = int(pptx_record["bytes"]) + sum(
            int(item["bytes"]) for item in render_records
        )
        total_artifact_bytes += artifact_bytes
        total_rendered_slides += len(render_records)
        artifacts.append(
            {
                "product_label": product_label,
                "directory_product": directory_product,
                "human_rank": human_rank,
                "source_directory": source_directory,
                "artifact_bytes": artifact_bytes,
                "pptx": {
                    "source_path": pptx_record["source_path"],
                    "source_url": pptx_record["source_url"],
                    "local_path": pptx_record["path"],
                    "bytes": pptx_record["bytes"],
                    "sha256": pptx_record["sha256"],
                    "upstream_lfs_sha256": pptx_record["upstream_lfs_sha256"],
                    "upstream_git_oid": pptx_record["upstream_git_oid"],
                },
                "rendered_slides": {
                    "count": len(render_records),
                    "local_directory": render_directory,
                    "files": [
                        {
                            "source_path": item["source_path"],
                            "local_path": item["path"],
                            "bytes": item["bytes"],
                            "sha256": item["sha256"],
                            "upstream_lfs_sha256": item["upstream_lfs_sha256"],
                            "upstream_git_oid": item["upstream_git_oid"],
                        }
                        for item in render_records
                    ],
                },
            }
        )

    subset = {
        "schema_version": "1.1",
        "source_dataset": "Yqy6/Slides-Align",
        "source_revision": REVISION,
        "source_file": "Slides-Align.json",
        "source_sha256": RANKING_SHA256,
        "semantics": (
            "Per-topic human relative ranking; lower rank is preferred. Rank is ordinal, "
            "not an absolute quality score."
        ),
        "difficulty": DIFFICULTY,
        "topic": TOPIC,
        "ranked_product_count": len(ranking_rows),
        "paired_product_count": len(artifacts),
        "results": subset_rows,
    }
    subset_path = output / "rankings" / "market_analysis.json"
    subset_path.write_text(
        json.dumps(subset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    subset_record = {
        "path": "rankings/market_analysis.json",
        "bytes": subset_path.stat().st_size,
        "sha256": sha256(subset_path),
        "derived_from": "rankings/Slides-Align.json",
    }
    flat_files.append(subset_record)

    available_ranks = [int(item["human_rank"]) for item in artifacts]
    missing_ranks = [int(item["human_rank"]) for item in unavailable]
    manifest: dict[str, Any] = {
        "schema_version": "1.1",
        "dataset_id": "slides_align_market_analysis_sample",
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "revision_pinned": True,
            "resolve_base": RESOLVE_BASE,
            "tree_api_base": TREE_API_BASE,
            "dataset_card": {
                "source_path": dataset_card["source_path"],
                "local_path": dataset_card["path"],
                "sha256": dataset_card["sha256"],
                "upstream_git_oid": dataset_card["upstream_git_oid"],
            },
        },
        "license": {
            "declared": "MIT",
            "evidence": "source/README.source.md front matter",
            "license_file_at_revision": False,
            "allowed_use": list(ALLOWED_USE),
            "caveat": (
                "The dataset card states that generated presentations may remain subject to "
                "the respective AI products terms of service. Do not use this slice for "
                "production, commercial, training, or data-flywheel ingestion."
            ),
        },
        "selection": {
            "difficulty": DIFFICULTY,
            "topic": TOPIC,
            "human_ranking_source_path": "Slides-Align.json",
            "human_ranking_source_url": _source_url("Slides-Align.json"),
            "human_ranking_local_path": "rankings/Slides-Align.json",
            "human_ranking_subset": "rankings/market_analysis.json",
            "human_ranking_sha256": RANKING_SHA256,
            "human_ranking_upstream_git_oid": RANKING_GIT_OID,
            "human_ranked_products": len(ranking_rows),
            "selected_products": len(artifacts),
            "available_human_ranks": available_ranks,
            "unavailable_human_ranks": missing_ranks,
            "selection_reason": (
                "Largest complete same-topic slice at the pinned revision where both an "
                "original PPTX and the complete upstream slide PNG sequence are available."
            ),
            "ranking_semantics": (
                "Ordinal relative preference within this topic; lower is preferred. "
                "Differences between rank numbers are not interval-scale score gaps."
            ),
        },
        "artifacts": artifacts,
        "unavailable_products": unavailable,
        "files": flat_files,
        "excluded_upstream_assets": [
            "detection",
            "slide_contents",
            "all non-market_analysis topics",
            "Zhipu rank-7 slide PNGs because no paired PPTX exists at the pinned revision",
            "local _partial_extra files because they are incomplete and do not match the pinned topic",
        ],
        "known_mapping_caveats": [
            "Ranking label Skywork-Banana maps to repository directory Skyworks-Banana.",
            "Ranking label Skywork maps to repository directory Skyworks.",
            "Zhipu rank 7 has nine upstream PNGs but no PPTX at the pinned revision.",
        ],
        "integrity": {
            "hash_algorithm": "sha256",
            "upstream_files_verified": len(flat_files) - 1,
            "manifest_files": len(flat_files),
            "pptx_matches_upstream_lfs": True,
            "all_render_files_hashed": True,
            "all_render_files_match_upstream_lfs": True,
            "pptx_slide_counts_match_render_sequences": True,
            "paired_product_count": len(artifacts),
            "rendered_slide_count": total_rendered_slides,
            "artifact_bytes": total_artifact_bytes,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "var" / "datasets" / "slides_align_sample",
    )
    args = parser.parse_args()
    manifest = fetch(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "revision": manifest["source"]["revision"],
                "allowed_use": manifest["license"]["allowed_use"],
                "paired_products": manifest["selection"]["selected_products"],
                "available_human_ranks": manifest["selection"]["available_human_ranks"],
                "unavailable_human_ranks": manifest["selection"][
                    "unavailable_human_ranks"
                ],
                "rendered_slides": manifest["integrity"]["rendered_slide_count"],
                "artifact_bytes": manifest["integrity"]["artifact_bytes"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
