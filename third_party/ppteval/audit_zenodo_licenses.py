"""Audit per-record licenses for the PPTAgent paper's Zenodo manifest."""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from run_ppteval import portable_path, sha256

RECORD_PATTERN = re.compile(r"/records/(\d+)")


def fetch_record(record_id: str) -> dict[str, str | int | None]:
    url = f"https://zenodo.org/api/records/{record_id}"
    request = urllib.request.Request(url, headers={"User-Agent": "ppt-eval-license-audit/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"record_id": record_id, "url": url, "status": 0, "license": None, "error": str(exc)}
    metadata = payload.get("metadata", {})
    license_value = metadata.get("license")
    if isinstance(license_value, dict):
        license_value = license_value.get("id")
    return {
        "record_id": record_id,
        "url": url,
        "status": status,
        "license": license_value,
        "doi": metadata.get("doi"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    record_ids = sorted(
        {
            match.group(1)
            for row in rows
            if (match := RECORD_PATTERN.search(row["url"])) is not None
        },
        key=int,
    )
    with ThreadPoolExecutor(max_workers=12) as executor:
        records = list(executor.map(fetch_record, record_ids))
    output = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": portable_path(args.manifest),
        "manifest_sha256": sha256(args.manifest),
        "manifest_files": len(rows),
        "unique_zenodo_records": len(records),
        "http_status_counts": dict(sorted(Counter(record["status"] for record in records).items())),
        "license_counts": dict(
            sorted(Counter(record["license"] or "UNKNOWN" for record in records).items())
        ),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in output.items() if key != "records"}, indent=2))
    return 0 if output["http_status_counts"] == {200: len(records)} else 1


if __name__ == "__main__":
    raise SystemExit(main())
