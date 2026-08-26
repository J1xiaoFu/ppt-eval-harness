"""Run the Harness against the local synthetic gold manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ppt_eval.config import default_profile  # noqa: E402
from ppt_eval.domain import EvalCase, SceneType  # noqa: E402
from ppt_eval.runtime import LocalEvaluationRuntime  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_number(actual: float | None, specification: Mapping[str, object]) -> bool:
    if actual is None:
        return False
    value = float(actual)
    if "eq" in specification and abs(value - float(specification["eq"])) > 1e-9:
        return False
    if "lt" in specification and not value < float(specification["lt"]):
        return False
    if "lte" in specification and not value <= float(specification["lte"]):
        return False
    if "gt" in specification and not value > float(specification["gt"]):
        return False
    if "gte" in specification and not value >= float(specification["gte"]):
        return False
    return True


def check_metric(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in ("execution_status", "metric_status", "multiplier"):
        if field in expected and actual.get(field) != expected[field]:
            failures.append(f"{field}: expected {expected[field]!r}, got {actual.get(field)!r}")
    if "normalized_score" in expected and not compare_number(
        actual.get("normalized_score"), expected["normalized_score"]
    ):
        failures.append(
            f"normalized_score: expected {expected['normalized_score']!r}, "
            f"got {actual.get('normalized_score')!r}"
        )
    evidence_kinds = {item.get("kind") for item in actual.get("evidence", [])}
    for kind in expected.get("evidence_kinds", []):
        if kind not in evidence_kinds:
            failures.append(f"missing evidence kind {kind!r}; observed {sorted(evidence_kinds)!r}")
    metadata = actual.get("metadata", {})
    for key, value in expected.get("metadata", {}).items():
        if metadata.get(key) != value:
            failures.append(f"metadata.{key}: expected {value!r}, got {metadata.get(key)!r}")
    payloads = [item.get("payload", {}) for item in actual.get("evidence", [])]
    for key, value in expected.get("evidence_payload", {}).items():
        if not any(payload.get(key) == value for payload in payloads):
            failures.append(
                f"evidence payload {key}: expected {value!r}; "
                f"observed {[payload.get(key) for payload in payloads]!r}"
            )
    return failures


def verify(manifest_path: Path, runtime_root: Path) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = LocalEvaluationRuntime(runtime_root)
    outcomes: list[dict[str, object]] = []
    all_valid = True
    for entry in manifest["cases"]:
        pptx = (manifest_path.parent / entry["pptx"]).resolve()
        failures: list[str] = []
        if sha256(pptx) != entry["sha256"]:
            failures.append("input sha256 does not match manifest")
        scene = SceneType(entry["scene"])
        report = runtime.evaluate(
            EvalCase(case_id=entry["case_id"], scene=scene, pptx_path=str(pptx)),
            default_profile(scene),
        )
        expected = entry["expected"]
        if "decision" in expected and report["decision"] != expected["decision"]:
            failures.append(
                f"decision: expected {expected['decision']!r}, got {report['decision']!r}"
            )
        indexed = {item["metric_id"]: item for item in report["results"]}
        for metric_id, metric_expected in expected.get("metrics", {}).items():
            if metric_id not in indexed:
                failures.append(f"missing metric {metric_id!r}")
                continue
            failures.extend(
                f"{metric_id}: {failure}"
                for failure in check_metric(indexed[metric_id], metric_expected)
            )
        all_valid = all_valid and not failures
        outcomes.append(
            {
                "case_id": entry["case_id"],
                "run_id": report["run_id"],
                "decision": report["decision"],
                "coverage": report["coverage"],
                "valid": not failures,
                "failures": failures,
            }
        )
    result: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_id": manifest["dataset_id"],
        "valid": all_valid,
        "case_count": len(outcomes),
        "outcomes": outcomes,
    }
    output = manifest_path.parent / "verification.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "var" / "datasets" / "local_gold_v1" / "manifest.json",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=ROOT / "var" / "datasets" / "local_gold_v1" / "runtime",
    )
    args = parser.parse_args()
    result = verify(args.manifest.resolve(), args.runtime_root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
