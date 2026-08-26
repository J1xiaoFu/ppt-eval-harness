from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.datasets.build_local_gold import build
from scripts.datasets.fetch_slides_align_market_analysis import (
    ALLOWED_USE,
    PRODUCTS,
    REVISION,
    file_matches,
    validate_rankings,
)
from scripts.datasets.verify_download_manifest import verify as verify_download_manifest
from scripts.datasets.verify_local_gold import verify

ROOT = Path(__file__).resolve().parents[1]


def test_local_synthetic_gold_set_builds_and_verifies(tmp_path) -> None:
    dataset_root = tmp_path / "local_gold"
    manifest = build(dataset_root)

    assert manifest["dataset_id"] == "local_synthetic_v1"
    assert manifest["case_count"] == 9
    assert len(list((dataset_root / "files").glob("*.pptx"))) == 9

    result = verify(dataset_root / "manifest.json", dataset_root / "runtime")

    assert result["valid"] is True
    assert result["case_count"] == 9
    assert all(outcome["valid"] for outcome in result["outcomes"])
    persisted = json.loads((dataset_root / "verification.json").read_text(encoding="utf-8"))
    assert persisted["valid"] is True


def test_benchmark_plan_records_ground_truth_license_and_revision() -> None:
    plan = json.loads((ROOT / "datasets" / "benchmark_plan.json").read_text(encoding="utf-8"))
    partitions = {item["id"]: item for item in plan["partitions"]}

    assert {"local_synthetic_v1", "slideaudit_starter_v1", "presentbench_rubrics_v1"} <= set(
        partitions
    )
    for item in partitions.values():
        assert item["ground_truth"]
        assert item["license"]
        if item.get("source"):
            assert item.get("revision")

    slides_align = partitions["slides_align_human_preference"]
    assert slides_align["allowed_use"] == ["research_quarantine"]
    assert slides_align["revision"] == REVISION
    assert slides_align["prepared_slice"]["available_human_ranks"] == [1, 2, 3, 4, 5, 6, 8]
    assert slides_align["prepared_slice"]["unavailable_human_ranks"] == [7]


def test_download_manifest_detects_tampering(tmp_path) -> None:
    payload = b"ground-truth annotation\n"
    sample = tmp_path / "sample.json"
    sample.write_bytes(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "fixture",
                "files": [
                    {
                        "path": sample.name,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert verify_download_manifest(manifest)["valid"] is True
    sample.write_bytes(b"tampered")
    result = verify_download_manifest(manifest)
    assert result["valid"] is False
    assert result["failures"]


def test_download_manifest_checks_upstream_lfs_hash(tmp_path) -> None:
    payload = b"locally self-consistent but not upstream\n"
    sample = tmp_path / "sample.png"
    sample.write_bytes(payload)
    local_hash = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "fixture",
                "files": [
                    {
                        "path": sample.name,
                        "bytes": len(payload),
                        "sha256": local_hash,
                        "upstream_lfs_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = verify_download_manifest(manifest)

    assert result["valid"] is False
    assert any("upstream LFS sha256 mismatch" in item for item in result["failures"])


def test_slides_align_topic_selection_keeps_complete_human_rank_order() -> None:
    rows = [
        {
            "product": product,
            "difficulty": "topic_introduction",
            "topic": "market_analysis",
            "rank": rank,
        }
        for rank, product in enumerate(reversed(PRODUCTS), start=1)
    ]
    rows.append(
        {
            "product": "Gamma",
            "difficulty": "topic_introduction",
            "topic": "another_topic",
            "rank": 1,
        }
    )

    selected = validate_rankings({"results": list(reversed(rows))})

    assert [item["rank"] for item in selected] == list(range(1, 9))
    assert {item["product"] for item in selected} == set(PRODUCTS)
    assert REVISION == "2f50ac6674a506acb245275e58c8a452c00e6a14"
    assert ALLOWED_USE == ("research_quarantine",)


def test_slides_align_file_match_requires_both_size_and_sha256(tmp_path) -> None:
    payload = b"pinned artifact"
    path = tmp_path / "artifact.pptx"
    path.write_bytes(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()

    assert file_matches(path, len(payload), expected_hash) is True
    assert file_matches(path, len(payload) + 1, expected_hash) is False
    assert file_matches(path, len(payload), "0" * 64) is False
