"""Verify downloaded dataset files against a preparation manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(manifest_path: Path) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for record in manifest.get("files", []):
        relative = str(record["path"])
        path = manifest_path.parent / relative
        if not path.is_file():
            failures.append(f"missing file: {relative}")
            continue
        expected_bytes = int(record["bytes"])
        if path.stat().st_size != expected_bytes:
            failures.append(
                f"size mismatch for {relative}: expected {expected_bytes}, got {path.stat().st_size}"
            )
        expected_hash = str(record["sha256"])
        actual_hash = sha256(path)
        if actual_hash != expected_hash:
            failures.append(
                f"sha256 mismatch for {relative}: expected {expected_hash}, got {actual_hash}"
            )
        upstream_lfs_hash = record.get("upstream_lfs_sha256")
        if upstream_lfs_hash is not None and actual_hash != str(upstream_lfs_hash):
            failures.append(
                f"upstream LFS sha256 mismatch for {relative}: "
                f"expected {upstream_lfs_hash}, got {actual_hash}"
            )
    return {
        "dataset_id": manifest.get("dataset_id"),
        "valid": not failures,
        "file_count": len(manifest.get("files", [])),
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    result = verify(args.manifest.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
