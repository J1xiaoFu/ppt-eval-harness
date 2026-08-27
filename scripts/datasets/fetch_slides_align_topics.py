"""Fetch multiple pinned Slides-Align topics as self-contained eval slices.

The suite remains research-quarantined.  Every selected deck must have one
PPTX and a contiguous upstream ``slide_images`` sequence; missing ranked
products remain explicit exclusions and ranks are never renumbered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.datasets.fetch_slides_align_market_analysis import (  # noqa: E402
    ALLOWED_USE,
    DATASET_CARD_GIT_OID,
    DATASET_CARD_SHA256,
    PRODUCTS,
    RANKING_GIT_OID,
    RANKING_SHA256,
    REPOSITORY,
    RESOLVE_BASE,
    REVISION,
    TREE_API_BASE,
    _download_lfs_record,
    _download_metadata,
    _source_url,
    _tree,
    _validate_slide_images,
    pptx_slide_count,
    sha256,
)

DEFAULT_TOPICS = (
    "Chinese_New_Year",
    "stock_market",
    "modern_architecture",
)
DEFAULT_DIFFICULTY = "topic_introduction"
_SAFE_SLUG = re.compile(r"[^a-z0-9]+")


def topic_slug(topic: str) -> str:
    slug = _SAFE_SLUG.sub("_", topic.casefold()).strip("_")
    if not slug:
        raise ValueError("topic must contain at least one alphanumeric character")
    return slug


def validate_topic_rankings(
    payload: Mapping[str, Any],
    *,
    difficulty: str,
    topic: str,
) -> tuple[dict[str, Any], ...]:
    rows = [
        dict(item)
        for item in payload.get("results", ())
        if item.get("difficulty") == difficulty and item.get("topic") == topic
    ]
    if not rows:
        raise ValueError(f"no human rankings for {difficulty}/{topic}")
    ranks = [int(item["rank"]) for item in rows]
    if len(ranks) != len(set(ranks)) or sorted(ranks) != list(
        range(1, len(rows) + 1)
    ):
        raise ValueError(f"ranking for {difficulty}/{topic} is not complete: {ranks}")
    labels = [str(item.get("product") or "") for item in rows]
    if len(labels) != len(set(labels)):
        raise ValueError(f"ranking for {difficulty}/{topic} repeats a product")
    unsupported = set(labels) - set(PRODUCTS) - {"NotebookLM"}
    if unsupported:
        raise ValueError(f"unsupported ranked products: {sorted(unsupported)}")
    return tuple(sorted(rows, key=lambda item: int(item["rank"])))


def _metadata_record(
    *, source_path: str, local_path: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    lfs = metadata.get("lfs")
    if not isinstance(lfs, Mapping):
        raise ValueError(f"missing LFS contract for {source_path}")
    return {
        "path": local_path,
        "source_path": source_path,
        "source_url": _source_url(source_path),
        "bytes": int(lfs["size"]),
        "sha256": str(lfs["oid"]),
        "upstream_lfs_sha256": str(lfs["oid"]),
        "upstream_git_oid": str(metadata["oid"]),
    }


def _prefixed(record: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {**dict(record), "path": f"{prefix}/{record['path']}"}


def _prepare_topic(
    *,
    suite_root: Path,
    ranking_payload: Mapping[str, Any],
    difficulty: str,
    topic: str,
    minimum_paired: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    slug = topic_slug(topic)
    topic_root = suite_root / "topics" / slug
    topic_root.mkdir(parents=True, exist_ok=True)
    ranking_rows = validate_topic_rankings(
        ranking_payload,
        difficulty=difficulty,
        topic=topic,
    )
    artifacts: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    subset_rows: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for row in ranking_rows:
        product = str(row["product"])
        rank = int(row["rank"])
        if product == "NotebookLM":
            source_directory = f"NotebookLM/{difficulty}/{topic}"
            entries = _tree(source_directory)
            image_entries = _validate_slide_images(
                source_directory,
                _tree(f"{source_directory}/slide_images"),
            )
            pptx_entries = [
                item
                for item in entries
                if item.get("type") == "file"
                and str(item.get("path", "")).endswith(".pptx")
            ]
            if pptx_entries:
                raise ValueError(
                    f"NotebookLM unexpectedly contains PPTX for {difficulty}/{topic}"
                )
            subset_rows.append(
                {
                    **row,
                    "selected": False,
                    "availability": "pdf_png_only_no_pptx",
                }
            )
            unavailable.append(
                {
                    "product_label": product,
                    "human_rank": rank,
                    "source_directory": source_directory,
                    "reason": "no_pptx_at_pinned_revision",
                    "upstream_slide_png_count": len(image_entries),
                    "upstream_slide_png_bytes": sum(
                        int(item["size"]) for item in image_entries
                    ),
                }
            )
            continue

        directory_product, product_slug = PRODUCTS[product]
        source_directory = f"{directory_product}/{difficulty}/{topic}"
        entries = _tree(source_directory)
        image_entries = _validate_slide_images(
            source_directory,
            _tree(f"{source_directory}/slide_images"),
        )
        pptx_entries = [
            dict(item)
            for item in entries
            if item.get("type") == "file"
            and str(item.get("path", "")).endswith(".pptx")
        ]
        if len(pptx_entries) > 1:
            raise ValueError(f"multiple PPTX files for {source_directory}")
        if not pptx_entries:
            subset_rows.append(
                {
                    **row,
                    "selected": False,
                    "availability": "png_only_no_pptx",
                }
            )
            unavailable.append(
                {
                    "product_label": product,
                    "human_rank": rank,
                    "source_directory": source_directory,
                    "reason": "no_pptx_at_pinned_revision",
                    "upstream_slide_png_count": len(image_entries),
                    "upstream_slide_png_bytes": sum(
                        int(item["size"]) for item in image_entries
                    ),
                }
            )
            continue

        deck_local = f"decks/{product_slug}_rank_{rank}.pptx"
        pptx_record = _download_lfs_record(
            output=topic_root,
            local_path=deck_local,
            metadata=pptx_entries[0],
        )
        render_directory = f"renders/{product_slug}_rank_{rank}"
        render_records = [
            _download_lfs_record(
                output=topic_root,
                local_path=(
                    f"{render_directory}/{PurePosixPath(str(item['path'])).name}"
                ),
                metadata=item,
            )
            for item in image_entries
        ]
        slide_count = pptx_slide_count(topic_root / deck_local)
        if slide_count != len(render_records):
            raise ValueError(
                f"PPTX/render mismatch for {topic}/{product}: "
                f"{slide_count} vs {len(render_records)}"
            )
        files.append(dict(pptx_record))
        files.extend(dict(item) for item in render_records)
        artifact_bytes = int(pptx_record["bytes"]) + sum(
            int(item["bytes"]) for item in render_records
        )
        artifacts.append(
            {
                "product_label": product,
                "directory_product": directory_product,
                "human_rank": rank,
                "source_directory": source_directory,
                "artifact_bytes": artifact_bytes,
                "pptx": {
                    "source_path": pptx_record["source_path"],
                    "source_url": pptx_record["source_url"],
                    "local_path": pptx_record["path"],
                    "bytes": pptx_record["bytes"],
                    "sha256": pptx_record["sha256"],
                    "upstream_lfs_sha256": pptx_record[
                        "upstream_lfs_sha256"
                    ],
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
                            "upstream_lfs_sha256": item[
                                "upstream_lfs_sha256"
                            ],
                            "upstream_git_oid": item["upstream_git_oid"],
                        }
                        for item in render_records
                    ],
                },
            }
        )
        subset_rows.append(
            {
                **row,
                "selected": True,
                "availability": "paired_pptx_png",
            }
        )

    artifacts.sort(key=lambda item: int(item["human_rank"]))
    if len(artifacts) < minimum_paired:
        raise ValueError(
            f"{difficulty}/{topic} has only {len(artifacts)} paired decks"
        )
    subset = {
        "schema_version": "1.0",
        "source_dataset": "Yqy6/Slides-Align",
        "source_revision": REVISION,
        "source_file": "Slides-Align.json",
        "semantics": (
            "Per-topic human ordinal ranking; lower rank is preferred and ranks "
            "are not interval-scale scores."
        ),
        "difficulty": difficulty,
        "topic": topic,
        "ranked_product_count": len(ranking_rows),
        "paired_product_count": len(artifacts),
        "results": subset_rows,
    }
    subset_path = topic_root / "rankings" / f"{slug}.json"
    subset_path.parent.mkdir(parents=True, exist_ok=True)
    subset_path.write_text(
        json.dumps(subset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    subset_record = {
        "path": f"rankings/{slug}.json",
        "bytes": subset_path.stat().st_size,
        "sha256": sha256(subset_path),
        "derived_from": "Slides-Align.json",
    }
    files.append(subset_record)
    total_bytes = sum(int(item["artifact_bytes"]) for item in artifacts)
    total_slides = sum(
        int(item["rendered_slides"]["count"]) for item in artifacts
    )
    available_ranks = [int(item["human_rank"]) for item in artifacts]
    missing_ranks = [int(item["human_rank"]) for item in unavailable]
    manifest = {
        "schema_version": "1.2",
        "dataset_id": f"slides_align_{slug}_sample",
        "prepared_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "source": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "revision_pinned": True,
            "resolve_base": RESOLVE_BASE,
            "tree_api_base": TREE_API_BASE,
        },
        "license": {
            "declared": "MIT",
            "evidence": "suite-root/source/README.source.md front matter",
            "license_file_at_revision": False,
            "allowed_use": list(ALLOWED_USE),
            "caveat": (
                "Generated presentations may remain subject to product terms. "
                "Research quarantine only; no training or commercial release."
            ),
        },
        "selection": {
            "difficulty": difficulty,
            "topic": topic,
            "human_ranking_source_path": "Slides-Align.json",
            "human_ranking_source_url": _source_url("Slides-Align.json"),
            "human_ranking_local_path": "../../rankings/Slides-Align.json",
            "human_ranking_subset": subset_record["path"],
            "human_ranking_sha256": RANKING_SHA256,
            "human_ranking_upstream_git_oid": RANKING_GIT_OID,
            "human_ranked_products": len(ranking_rows),
            "selected_products": len(artifacts),
            "available_human_ranks": available_ranks,
            "unavailable_human_ranks": missing_ranks,
            "selection_reason": (
                "Complete same-topic ordinal ranking restricted to products with "
                "one PPTX and a contiguous upstream slide PNG sequence."
            ),
            "ranking_semantics": (
                "Ordinal relative preference within this topic; missing upstream "
                "PPTX ranks are retained as gaps and never renumbered."
            ),
        },
        "artifacts": artifacts,
        "unavailable_products": unavailable,
        "files": files,
        "excluded_upstream_assets": ["detection", "slide_contents"],
        "integrity": {
            "hash_algorithm": "sha256",
            "manifest_files": len(files),
            "pptx_matches_upstream_lfs": True,
            "all_render_files_hashed": True,
            "all_render_files_match_upstream_lfs": True,
            "pptx_slide_counts_match_render_sequences": True,
            "paired_product_count": len(artifacts),
            "rendered_slide_count": total_slides,
            "artifact_bytes": total_bytes,
        },
    }
    (topic_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    prefixed_files = [
        _prefixed(record, f"topics/{slug}") for record in files
    ]
    return manifest, prefixed_files


def fetch_suite(
    output: Path,
    *,
    difficulty: str,
    topics: Sequence[str],
    minimum_total: int = 20,
    maximum_total: int = 30,
    minimum_paired_per_topic: int = 6,
) -> dict[str, Any]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    root_items = {
        str(item["path"]): item
        for item in _tree()
        if item.get("type") == "file"
    }
    dataset_card = _download_metadata(
        output=output,
        source_path="README.md",
        local_path="source/README.source.md",
        metadata=root_items["README.md"],
        expected_sha256=DATASET_CARD_SHA256,
        expected_git_oid=DATASET_CARD_GIT_OID,
    )
    ranking = _download_metadata(
        output=output,
        source_path="Slides-Align.json",
        local_path="rankings/Slides-Align.json",
        metadata=root_items["Slides-Align.json"],
        expected_sha256=RANKING_SHA256,
        expected_git_oid=RANKING_GIT_OID,
    )
    ranking_payload = json.loads(
        (output / "rankings" / "Slides-Align.json").read_text(encoding="utf-8")
    )
    topic_entries: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = [dict(dataset_card), dict(ranking)]
    for topic in topics:
        manifest, topic_files = _prepare_topic(
            suite_root=output,
            ranking_payload=ranking_payload,
            difficulty=difficulty,
            topic=topic,
            minimum_paired=minimum_paired_per_topic,
        )
        slug = topic_slug(topic)
        topic_entries.append(
            {
                "topic": topic,
                "slug": slug,
                "local_directory": f"topics/{slug}",
                "manifest_path": f"topics/{slug}/manifest.json",
                "paired_products": manifest["selection"]["selected_products"],
                "available_human_ranks": manifest["selection"][
                    "available_human_ranks"
                ],
                "unavailable_human_ranks": manifest["selection"][
                    "unavailable_human_ranks"
                ],
                "rendered_slides": manifest["integrity"][
                    "rendered_slide_count"
                ],
                "artifact_bytes": manifest["integrity"]["artifact_bytes"],
            }
        )
        files.extend(topic_files)
    total_decks = sum(int(item["paired_products"]) for item in topic_entries)
    if not minimum_total <= total_decks <= maximum_total:
        raise ValueError(
            f"suite has {total_decks} decks; expected {minimum_total}..{maximum_total}"
        )
    manifest = {
        "schema_version": "1.0",
        "dataset_id": "slides_align_three_topic_suite_v1",
        "prepared_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "source": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "revision_pinned": True,
            "dataset_card_sha256": DATASET_CARD_SHA256,
            "ranking_sha256": RANKING_SHA256,
        },
        "license": {
            "declared": "MIT",
            "allowed_use": list(ALLOWED_USE),
            "caveat": "Research quarantine only; product terms may still apply.",
        },
        "selection": {
            "difficulty": difficulty,
            "topics": list(topics),
            "topic_count": len(topic_entries),
            "paired_deck_count": total_decks,
            "selection_rule": (
                "Three fixed topic-introduction rankings; identical PPTX-capable "
                "product panel where available; ranks are never pooled across topics."
            ),
        },
        "topics": topic_entries,
        "files": files,
        "integrity": {
            "hash_algorithm": "sha256",
            "manifest_files": len(files),
            "paired_deck_count": total_decks,
            "rendered_slide_count": sum(
                int(item["rendered_slides"]) for item in topic_entries
            ),
            "artifact_bytes": sum(
                int(item["artifact_bytes"]) for item in topic_entries
            ),
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
        default=ROOT / "var" / "datasets" / "slides_align_three_topics",
    )
    parser.add_argument("--difficulty", default=DEFAULT_DIFFICULTY)
    parser.add_argument("--topic", action="append", dest="topics")
    parser.add_argument("--minimum-total", type=int, default=20)
    parser.add_argument("--maximum-total", type=int, default=30)
    args = parser.parse_args()
    topics = tuple(args.topics or DEFAULT_TOPICS)
    manifest = fetch_suite(
        args.output,
        difficulty=args.difficulty,
        topics=topics,
        minimum_total=args.minimum_total,
        maximum_total=args.maximum_total,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "revision": manifest["source"]["revision"],
                "topics": manifest["selection"]["topics"],
                "paired_decks": manifest["selection"]["paired_deck_count"],
                "rendered_slides": manifest["integrity"][
                    "rendered_slide_count"
                ],
                "artifact_bytes": manifest["integrity"]["artifact_bytes"],
                "allowed_use": manifest["license"]["allowed_use"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
