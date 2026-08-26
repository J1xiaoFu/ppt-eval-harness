"""Fetch a metadata-only PresentBench rubric slice without mixed-license source materials."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://huggingface.co/datasets/PresentBench/PresentBench"
REVISION = "31ec40084405c5f2e6b8b5adedf2999b6060e7e1"
RAW = f"{REPOSITORY}/resolve/{REVISION}"


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
    records = [
        download("README.md", output / "README.md"),
        download("hf/metadata.json", output / "hf" / "metadata.json"),
    ]
    metadata = json.loads((output / "hf" / "metadata.json").read_text(encoding="utf-8"))
    by_domain: dict[str, dict[str, object]] = {}
    for item in metadata:
        domain = str(item["domain"])
        by_domain.setdefault(domain, item)
    selected = [by_domain[key] for key in sorted(by_domain)]
    selected_cases: list[dict[str, object]] = []
    for item in selected:
        case_files = []
        for key in ("instructions_path", "judge_prompt_path", "statistics_path"):
            relative = str(item[key])
            record = download(relative, output / relative)
            records.append(record)
            case_files.append(relative)
        selected_cases.append(
            {
                "id": item["id"],
                "domain": item["domain"],
                "checklist_total_count": item["checklist_total_count"],
                "files": case_files,
                "materials_excluded": [material["path"] for material in item["materials"]],
            }
        )
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_id": "presentbench_rubrics_v1",
        "repository": REPOSITORY,
        "revision": REVISION,
        "license": "CC-BY-NC-4.0 for prompts and rubrics; source materials excluded",
        "allowed_use": ["research_quarantine"],
        "full_case_count": len(metadata),
        "selection": selected_cases,
        "files": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "var" / "datasets" / "presentbench_rubrics_v1"
    )
    args = parser.parse_args()
    manifest = fetch(args.output.resolve())
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "full_cases": manifest["full_case_count"],
                "selected_cases": len(manifest["selection"]),
                "files": len(manifest["files"]),
            }
        )
    )


if __name__ == "__main__":
    main()
