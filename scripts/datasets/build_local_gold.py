"""Build a small repository-owned PPTX gold set with controlled defects."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.pptx_factory import build_pptx  # noqa: E402

SLIDE_W = 12_192_000
SLIDE_H = 6_858_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text(
    value: str,
    *,
    x: int = 600_000,
    y: int = 400_000,
    w: int = 5_000_000,
    h: int = 800_000,
    font_pt: float = 20,
) -> dict[str, object]:
    return {"kind": "text", "text": value, "x": x, "y": y, "w": w, "h": h, "font_pt": font_pt}


def image(*, x: int = 0, y: int = 0, w: int = SLIDE_W, h: int = SLIDE_H) -> dict[str, object]:
    return {"kind": "image", "name": "Flattened slide", "x": x, "y": y, "w": w, "h": h}


def metric(**expected: object) -> dict[str, object]:
    return expected


def build(output: Path) -> dict[str, object]:
    files = output / "files"
    files.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, object]] = []

    def add(case_id: str, filename: str, expected: dict[str, object]) -> None:
        path = files / filename
        cases.append(
            {
                "case_id": case_id,
                "scene": "ready_made",
                "pptx": f"files/{filename}",
                "sha256": sha256(path),
                "expected": expected,
            }
        )

    clean = files / "clean_baseline.pptx"
    build_pptx(
        clean,
        slides=((text("Clean baseline", font_pt=28), text("Readable body", y=1_600_000, font_pt=20)),),
    )
    add(
        "clean-baseline",
        clean.name,
        {
            "metrics": {
                "file_deliverability": metric(metric_status="PASS", multiplier=1.0),
                "critical_content_visibility": metric(metric_status="PASS", multiplier=1.0),
                "layout": metric(normalized_score={"eq": 1.0}),
                "typography": metric(normalized_score={"eq": 1.0}),
            }
        },
    )

    small = files / "small_text.pptx"
    build_pptx(
        small,
        slides=((text("Small text", font_pt=28), text("This body is intentionally tiny", y=1_600_000, font_pt=8)),),
    )
    add(
        "small-text",
        small.name,
        {"metrics": {"typography": metric(normalized_score={"lt": 1.0}, evidence_kinds=["small_text"])}},
    )

    overlap = files / "overlap.pptx"
    build_pptx(
        overlap,
        slides=(
            (
                text("Overlap", font_pt=28),
                text("Object A", x=1_000_000, y=1_800_000, w=4_000_000, h=1_200_000),
                text("Object B", x=1_200_000, y=1_900_000, w=4_000_000, h=1_200_000),
            ),
        ),
    )
    add(
        "peer-overlap",
        overlap.name,
        {"metrics": {"layout": metric(normalized_score={"lt": 1.0}, evidence_kinds=["overlap"])}},
    )

    outside = files / "out_of_bounds.pptx"
    build_pptx(
        outside,
        slides=((text("Out of bounds", font_pt=28), text("Outside", x=11_500_000, y=2_000_000, w=2_000_000)),),
    )
    add(
        "out-of-bounds",
        outside.name,
        {"metrics": {"layout": metric(normalized_score={"lt": 1.0}, evidence_kinds=["out_of_bounds"])}},
    )

    blank = files / "blank_majority.pptx"
    build_pptx(blank, slides=((text("Only visible slide", font_pt=28),), (), ()))
    add(
        "blank-majority",
        blank.name,
        {
            "metrics": {
                "critical_content_visibility": metric(
                    metric_status="FAIL", multiplier=0.5, evidence_kinds=["blank_slide"]
                )
            }
        },
    )

    raster = files / "rasterized_slide.pptx"
    build_pptx(raster, slides=((image(),),))
    add(
        "rasterized-slide",
        raster.name,
        {"metrics": {"editability": metric(normalized_score={"lt": 0.5}, evidence_kinds=["rasterized_slide"])}},
    )

    external = files / "external_relationship.pptx"
    build_pptx(external, external_relationship=True)
    add(
        "external-relationship",
        external.name,
        {
            "metrics": {
                "file_deliverability": metric(
                    multiplier=1.0, evidence_payload={"has_external_relationships": True}
                )
            }
        },
    )

    active = files / "active_content.pptx"
    build_pptx(active, active_content=True)
    add(
        "active-content",
        active.name,
        {
            "metrics": {
                "file_deliverability": metric(multiplier=1.0, evidence_payload={"has_macros": True})
            }
        },
    )

    unreadable = files / "unreadable.pptx"
    unreadable.write_bytes(b"not a valid pptx package\n")
    add(
        "unreadable-package",
        unreadable.name,
        {
            "decision": "FAIL",
            "metrics": {
                "file_deliverability": metric(
                    execution_status="SUCCESS", metric_status="FAIL", multiplier=0.0
                )
            },
        },
    )

    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_id": "local_synthetic_v1",
        "license": "repository-owned synthetic fixtures",
        "split": "frozen_eval",
        "case_count": len(cases),
        "cases": cases,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "var" / "datasets" / "local_gold_v1")
    args = parser.parse_args()
    manifest = build(args.output.resolve())
    print(json.dumps({"output": str(args.output.resolve()), "case_count": manifest["case_count"]}))


if __name__ == "__main__":
    main()
