#!/usr/bin/env python3
"""Validate the dependency-free audit snapshot and append-only event stream."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ID_CHAIN = re.compile(r"^(REQ|ADR|ORC|TST|EXP|RUN|REL)-")
PHASES = ["调研", "开发", "评测"]


class AuditValidationError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditValidationError(message)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    require(isinstance(value, dict), f"{path}: root must be an object")
    return value


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditValidationError(f"{path}:{line_number}: {exc}") from exc
            require(isinstance(event, dict), f"{path}:{line_number}: event must be an object")
            events.append(event)
    return events


def validate_snapshot(data: dict[str, Any]) -> None:
    require(data.get("schema_version") == "1.0", "unsupported schema_version")
    project = data.get("project")
    require(isinstance(project, dict), "project must be an object")
    for field in ("id", "name", "status", "as_of"):
        require(bool(project.get(field)), f"project.{field} is required")
    datetime.fromisoformat(str(project["as_of"]))

    phases = data.get("phases")
    require(isinstance(phases, list) and len(phases) == 3, "exactly three phases are required")
    require([phase.get("name") for phase in phases] == PHASES, "phases must be 调研, 开发, 评测")
    for phase in phases:
        for field in ("id", "objective", "process", "evidence", "decisions", "gate", "risks"):
            require(field in phase, f"phase {phase.get('name')}: missing {field}")

    slides = data.get("deck", {}).get("slides")
    require(isinstance(slides, list) and len(slides) == 9, "deck must contain exactly nine slides")
    expected_chapters = [name for name in PHASES for _ in range(3)]
    require([slide.get("chapter") for slide in slides] == expected_chapters, "deck must contain three slides per phase")
    for index, slide in enumerate(slides, 1):
        for field in ("chapter", "title", "claim", "points", "source"):
            require(slide.get(field), f"slide {index}: missing {field}")
        require(isinstance(slide["points"], list) and 2 <= len(slide["points"]) <= 4, f"slide {index}: points must contain 2-4 items")

    traceability = data.get("traceability")
    require(isinstance(traceability, list) and traceability, "traceability must not be empty")
    for row in traceability:
        for field in ("requirement", "decision", "oracle", "test", "experiment", "release"):
            value = row.get(field)
            require(isinstance(value, str) and (value == "ORC-N/A" or ID_CHAIN.match(value)), f"traceability: invalid {field}={value!r}")

    manifest = data.get("run_manifest_example", {})
    for field in ("run_id", "case_id", "input_sha256", "git_sha", "container_digest", "font_bundle", "models", "prompt_profile", "eval_profile", "random_seed", "cost_cny", "output_sha256"):
        require(field in manifest, f"run_manifest_example.{field} is required")
    for field in ("input_sha256", "output_sha256"):
        require(bool(re.fullmatch(r"[0-9a-f]{64}", str(manifest[field]))), f"{field} must be lowercase sha256")

    targets = data.get("acceptance_targets", [])
    require(targets, "acceptance_targets must not be empty")
    for target in targets:
        require(target.get("status") in {"planned", "observed"}, "acceptance target status must be planned or observed")
        if target.get("status") == "planned":
            require("observed" not in target, "planned target cannot contain an observed value")


def validate_events(events: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    previous_time: datetime | None = None
    for index, event in enumerate(events, 1):
        for field in ("event_id", "event_type", "occurred_at", "actor", "subject_id", "payload"):
            require(field in event, f"event {index}: missing {field}")
        event_id = event["event_id"]
        require(isinstance(event_id, str) and event_id.startswith("EVT-"), f"event {index}: invalid event_id")
        require(event_id not in seen, f"event {index}: duplicate {event_id}")
        occurred_at = datetime.fromisoformat(str(event["occurred_at"]))
        require(previous_time is None or occurred_at >= previous_time, f"event {index}: timestamps must be non-decreasing")
        supersedes = event.get("supersedes")
        require(supersedes is None or supersedes in seen, f"event {index}: supersedes must reference an earlier event")
        require(isinstance(event["payload"], dict), f"event {index}: payload must be an object")
        seen.add(event_id)
        previous_time = occurred_at


def validate_files(snapshot: Path, data: dict[str, Any]) -> None:
    root = snapshot.resolve().parents[2]
    evidence_paths = [path for phase in data["phases"] for path in phase["evidence"]]
    evidence_paths.extend(slide["source"] for slide in data["deck"]["slides"])
    for relative in sorted(set(evidence_paths)):
        candidate = (root / relative).resolve()
        require(root == candidate or root in candidate.parents, f"path escapes repository: {relative}")
        require(candidate.is_file(), f"referenced evidence does not exist: {relative}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: verify_audit.py PROJECT_AUDIT.json EVENTS.jsonl", file=sys.stderr)
        return 2
    snapshot, events_path = map(Path, argv[1:])
    try:
        data = load_json(snapshot)
        events = load_events(events_path)
        validate_snapshot(data)
        validate_events(events)
        validate_files(snapshot, data)
    except (AuditValidationError, OSError, ValueError) as exc:
        print(f"audit validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"audit validation passed: 3 phases, 9 slides, {len(events)} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

