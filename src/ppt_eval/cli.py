from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from ppt_eval.adapters import LibreOfficeRenderer, PowerPointRenderer
from ppt_eval.config import default_profile, load_case, load_profile
from ppt_eval.domain import EvalCase, SceneType
from ppt_eval.infrastructure import to_primitive
from ppt_eval.runtime import build_runtime_from_environment


def _json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, indent=2)


def _case_from_argument(path: str) -> EvalCase:
    candidate = Path(path)
    if candidate.suffix.lower() == ".pptx":
        return EvalCase(case_id=candidate.stem, scene=SceneType.READY_MADE, pptx_path=str(candidate.resolve()))
    return load_case(candidate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ppt-eval", description="Deterministic PPT evaluation harness")
    parser.add_argument("--data-dir", default="var", help="local run/audit/artifact directory")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="evaluate one PPTX or EvalCase JSON")
    run.add_argument("case")
    run.add_argument("--profile")
    run.add_argument("--output")

    batch = commands.add_parser("batch", help="evaluate EvalCase JSON files")
    batch.add_argument("cases", nargs="+")
    batch.add_argument("--profile")
    batch.add_argument("--output", default="var/batch-results.json")

    review = commands.add_parser("review", help="record an immutable human review")
    review.add_argument("run_id")
    review.add_argument(
        "verdict",
        choices=(
            "CONFIRM_SYSTEM_DECISION",
            "OVERRIDE_DECISION",
            "REQUEST_MORE_EVIDENCE",
        ),
    )
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", default="")
    review.add_argument("--target-decision", choices=("PASS", "REVIEW", "FAIL", "ERROR"))
    review.add_argument(
        "--resolve",
        action="append",
        default=[],
        metavar="ISSUE_ID=RESOLUTION",
        help="repeat for each P0/P1 issue",
    )
    review.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="TRACK=STATUS",
        help="optional visual/layout/content/full_deck disposition",
    )
    review.add_argument("--client-request-id")

    feedback = commands.add_parser("feedback", help="record downstream acceptance and edit feedback")
    feedback.add_argument("run_id")
    feedback.add_argument("case_id")
    feedback.add_argument("--accepted", choices=("yes", "no", "unknown"), default="unknown")
    feedback.add_argument("--abandoned", action="store_true")
    feedback.add_argument("--label")
    feedback.add_argument("--modification-seconds", type=float)

    proposal = commands.add_parser("proposal", help="create and govern parameter candidates")
    proposal_commands = proposal.add_subparsers(dest="proposal_command", required=True)
    create_proposal = proposal_commands.add_parser("create")
    create_proposal.add_argument("profile_id")
    create_proposal.add_argument("base_version")
    create_proposal.add_argument("--changes", required=True, help="JSON object or path to a JSON file")
    create_proposal.add_argument("--rationale", required=True)
    create_proposal.add_argument("--evidence", nargs="+", required=True)
    validate_proposal = proposal_commands.add_parser("validate")
    validate_proposal.add_argument("proposal_id")
    validate_proposal.add_argument("--report", required=True, help="validation JSON object or file")
    approve_proposal = proposal_commands.add_parser("approve")
    approve_proposal.add_argument("proposal_id")
    approve_proposal.add_argument("approver")

    audit = commands.add_parser("audit", help="verify or export audit evidence")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_commands.add_parser("verify")
    export = audit_commands.add_parser("export")
    export.add_argument("run_id")
    export.add_argument("--output", default="var/exports")

    serve = commands.add_parser("serve", help="start the optional FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)

    render = commands.add_parser("render", help="render PPTX through PowerPoint or LibreOffice")
    render.add_argument("pptx")
    render.add_argument("--renderer", choices=("powerpoint", "libreoffice"), default="powerpoint")
    render.add_argument("--output", default="var/rendered")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = None
    if args.command in {"run", "batch", "review", "feedback", "proposal", "audit"}:
        runtime = build_runtime_from_environment(args.data_dir)

    if args.command == "run":
        assert runtime is not None
        case = _case_from_argument(args.case)
        profile = load_profile(args.profile) if args.profile else default_profile(case.scene)
        payload = runtime.evaluate(case, profile)
        text = _json(payload)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        print(text)
        return 0 if payload["decision"] != "ERROR" else 2

    if args.command == "batch":
        assert runtime is not None
        override = load_profile(args.profile) if args.profile else None
        reports = []
        for path in args.cases:
            case = _case_from_argument(path)
            reports.append(runtime.evaluate(case, override or default_profile(case.scene)))
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_json(reports), encoding="utf-8")
        print(target.resolve())
        return 0

    if args.command == "review":
        assert runtime is not None
        issue_resolutions = [
            {"issue_id": key, "resolution": value}
            for key, value in (_assignment(item) for item in args.resolve)
        ]
        track_resolutions = dict(_assignment(item) for item in args.track)
        print(_json(runtime.review({
            "run_id": args.run_id,
            "verdict": args.verdict,
            "reviewer_id": args.reviewer,
            "note": args.note,
            "target_decision": args.target_decision,
            "issue_resolutions": issue_resolutions,
            "track_resolutions": track_resolutions,
            "client_request_id": args.client_request_id or f"cli-{uuid.uuid4().hex}",
        })))
        return 0

    if args.command == "feedback":
        assert runtime is not None
        accepted = {"yes": True, "no": False, "unknown": None}[args.accepted]
        print(_json(runtime.add_feedback({
            "run_id": args.run_id,
            "case_id": args.case_id,
            "accepted": accepted,
            "abandoned": args.abandoned,
            "human_label": args.label,
            "modification_seconds": args.modification_seconds,
        })))
        return 0

    if args.command == "proposal":
        assert runtime is not None
        if args.proposal_command == "create":
            changes = _json_argument(args.changes)
            result = runtime.proposals.create(
                profile_id=args.profile_id,
                base_version=args.base_version,
                proposed_changes=changes,
                rationale=args.rationale,
                evidence_run_ids=args.evidence,
            )
        elif args.proposal_command == "validate":
            result = runtime.proposals.validate(args.proposal_id, _json_argument(args.report))
        else:
            result = runtime.proposals.approve(args.proposal_id, args.approver)
        print(_json(result))
        return 0

    if args.command == "audit":
        assert runtime is not None
        if args.audit_command == "verify":
            valid, event = runtime.audit_log.verify()
            print(_json({"valid": valid, "broken_event": event}))
            return 0 if valid else 3
        paths = runtime.export(args.run_id, args.output)
        print("\n".join(str(path.resolve()) for path in paths))
        return 0

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError:
            print("Install the API extra: pip install -e '.[api]'", file=sys.stderr)
            return 2
        os.environ["PPT_EVAL_DATA_DIR"] = args.data_dir
        uvicorn.run("ppt_eval.api:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.command == "render":
        renderer = PowerPointRenderer() if args.renderer == "powerpoint" else LibreOfficeRenderer()
        render_result = renderer.render(args.pptx, args.output)
        print(_json(render_result))
        return 0
    return 2


def _json_argument(value: str) -> dict[str, Any]:
    path = Path(value)
    text = path.read_text(encoding="utf-8") if path.is_file() else value
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _assignment(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key.strip() or not item.strip():
        raise ValueError(f"expected KEY=VALUE, got {value!r}")
    return key.strip(), item.strip().upper()


if __name__ == "__main__":
    raise SystemExit(main())
