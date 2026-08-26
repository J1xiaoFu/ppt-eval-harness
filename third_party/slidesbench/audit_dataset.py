"""Create a static, network-free audit of the vendored SlidesBench snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

INSTRUCTION_FILES = (
    "instruction.txt",
    "instruction_no_image.txt",
    "instruction_high_level.txt",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_examples(examples_dir: Path) -> dict:
    domains = {}
    for domain_dir in sorted(path for path in examples_dir.iterdir() if path.is_dir()):
        slide_dirs = sorted(
            (path for path in domain_dir.glob("slide_*") if path.is_dir()),
            key=lambda path: int(path.name.rsplit("_", 1)[-1]),
        )
        missing = Counter()
        for slide_dir in slide_dirs:
            for filename in INSTRUCTION_FILES:
                if not (slide_dir / filename).is_file():
                    missing[filename] += 1
        reference_decks = sorted(path.name for path in domain_dir.glob("*.pptx"))
        media_dirs = sum((slide_dir / "media").is_dir() for slide_dir in slide_dirs)
        domains[domain_dir.name] = {
            "slide_count": len(slide_dirs),
            "instruction_task_count": len(slide_dirs) * len(INSTRUCTION_FILES),
            "missing_instruction_files": dict(missing),
            "reference_decks_in_repo": reference_decks,
            "slides_with_media_directory": media_dirs,
        }

    total_slides = sum(item["slide_count"] for item in domains.values())
    return {
        "domain_count": len(domains),
        "unique_test_slides": total_slides,
        "test_instruction_tasks": total_slides * len(INSTRUCTION_FILES),
        "reference_decks_in_repo": sum(
            len(item["reference_decks_in_repo"]) for item in domains.values()
        ),
        "domains": domains,
    }


def audit_csv(path: Path) -> dict:
    rows = 0
    nonempty = Counter()
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows += 1
            for field in fieldnames:
                if row.get(field, "").strip():
                    nonempty[field] += 1
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "columns": fieldnames,
        "row_count": rows,
        "nonempty_by_column": dict(nonempty),
        "has_training_code_column": "code" in fieldnames,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, default=Path("upstream"))
    parser.add_argument("--output", type=Path, default=Path("evidence/dataset-audit.json"))
    args = parser.parse_args()

    examples_dir = args.upstream / "slidesbench" / "examples"
    train_dir = args.upstream / "slidesbench" / "train_data"
    result = {
        "source": "https://github.com/para-lost/AutoPresent",
        "commit": "98e0c012e89469863d9c3c8bc87eac967d82b2e6",
        "examples": audit_examples(examples_dir),
        "training_csvs": {
            path.name: audit_csv(path) for path in sorted(train_dir.glob("*.csv"))
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
