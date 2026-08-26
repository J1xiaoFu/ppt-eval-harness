"""Fetch a small, pinned CC-BY-4.0 SlideAudit slice with ground truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/zhuohaouw/SlideAudit"
REVISION = "642d490b7c1d2e78a50a631bfd359433397f3ecf"
RAW = f"https://raw.githubusercontent.com/zhuohaouw/SlideAudit/{REVISION}"
SLIDE_IDS = tuple(f"slide_{index:04d}" for index in range(1, 13))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(relative: str, destination: Path) -> dict[str, object]:
    url = f"{RAW}/{relative}"
    request = urllib.request.Request(url, headers={"User-Agent": "ppt-eval-dataset-fetch/1.0"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    destination.write_bytes(payload)
    return {
        "path": relative,
        "source_path": relative,
        "source_url": url,
        "bytes": len(payload),
        "sha256": sha256(destination),
    }


def fetch(output: Path) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for relative in ("LICENSE", "README.md", "data/metadata.csv"):
        records.append(download(relative, output / relative))
    for slide_id in SLIDE_IDS:
        for relative in (
            f"data/images/{slide_id}.png",
            f"data/annotations/{slide_id}.json",
            f"data/descriptions/{slide_id}.json",
        ):
            records.append(download(relative, output / relative))
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_id": "slideaudit_starter_v1",
        "repository": REPOSITORY,
        "revision": REVISION,
        "license": "CC-BY-4.0",
        "allowed_use": ["research", "development", "frozen_eval"],
        "selection": list(SLIDE_IDS),
        "ground_truth": [
            "design_deficiency_category",
            "design_deficiency",
            "response",
            "has_strong_agreement",
            "bounding_boxes",
            "object_descriptions"
        ],
        "files": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "var" / "datasets" / "slideaudit_starter_v1"
    )
    args = parser.parse_args()
    manifest = fetch(args.output.resolve())
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "slides": len(manifest["selection"]),
                "files": len(manifest["files"]),
            }
        )
    )


if __name__ == "__main__":
    main()
