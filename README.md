# PPT Eval Harness

An evidence-first, deterministic evaluation harness for generated and existing PowerPoint decks. It combines an always-on intrinsic PPT quality baseline, scenario-specific Oracles, PDMS-style scoring, explicit degradation, human review, and append-only audit evidence.

New maintainers should start with the Chinese [onboarding and handover guide](docs/onboarding_handover_guide.md). It explains the architecture, every component's responsibility, the 30 atomic metrics, scoring and degradation, local operations, extension patterns, release governance, troubleshooting, and current implementation gaps.

## What works

- Four scenarios: `text_to_ppt`, `project_summary`, `multimodal`, and `ready_made`.
- The `baseline_ppt_quality` composite is injected into every execution DAG and cannot be removed by configuration.
- Safe PPTX ZIP preflight, `python-pptx` parsing, and an OOXML fallback parser.
- Thirteen intrinsic metrics plus scenario metrics with page/object/bbox evidence.
- Deterministic `OBSERVE -> PLAN -> ACT -> VERIFY -> FINALIZE/REVIEW` supervisor.
- PPT-PDMS aggregation, high-confidence hard multipliers, required/optional `NA`, and isolated runtime `ERROR`.
- Local CLI/runtime, optional FastAPI/Celery/PostgreSQL/S3 adapters, review UI source, Docker Compose, run export, and hash-chained audit logs.
- Feedback/edit-diff ingestion, active-sampling priorities, and parameter proposals that require frozen/challenge/shadow validation plus two human approvals; v1 intentionally exposes no automatic production apply method.
- Three-part research/development/evaluation audit pack and a generated read-only HTML report.

## Quick start

```powershell
python -m pip install -e .
python examples/generate_demo.py
ppt-eval run examples/demo/case_ready_made.json
ppt-eval run examples/demo/case_text_to_ppt.json
ppt-eval audit verify
```

Governed flywheel signals are available from the same CLI:

```powershell
ppt-eval feedback RUN_ID CASE_ID --accepted yes --modification-seconds 30
ppt-eval proposal create PROFILE_ID 1.0 --changes '{"lambda_base":0.58}' --rationale "calibration" --evidence RUN_ID
```

Proposals can be validated and approved into `RELEASE_CANDIDATE`, but v1 intentionally has no command or API that applies them to production.

In this restricted workspace, use the bundled runtime without installing optional packages:

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\DiegoWang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/run_tests.py
& "C:\Users\DiegoWang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m ppt_eval.cli run examples/demo/case_ready_made.json
```

## Scoring and degradation

```text
S_base = 100 * product(base hard multipliers) * A_base
S_full = 100 * product(base multipliers) * product(scene multipliers)
         * (lambda * A_base + (1-lambda) * A_scene)
```

Hard multipliers are limited to deterministic or calibrated high-confidence, non-compensable defects and can only be `1`, `0.5`, or `0`. A metric cannot appear both inside and outside the formula. `NA` is removed from arithmetic; a required `NA` degrades coverage and routes review. Runtime `ERROR` never becomes a quality zero.

| Coverage | Meaning | Published score |
|---|---|---|
| `FULL` | Baseline and required scenario evidence complete | `S_base` and `S_full` |
| `DEGRADED` | Some required evidence unavailable | `S_base` only, decision `REVIEW` |
| `BASE_ONLY` | Scenario subgraph unavailable | `S_base` only, decision `REVIEW` |
| `UNASSESSABLE` | Intrinsic score cannot be produced | Technical evidence and `ERROR/REVIEW` |

## Service and workers

Install optional integrations or use the supplied containers:

```powershell
python -m pip install -e ".[api,worker,storage]"
ppt-eval serve --host 127.0.0.1 --port 8000
docker compose up --build
```

The API contract is in `docs/openapi.yaml`. `POST /v1/evaluations` is an asynchronous job endpoint by default; use `?async=false` for a local synchronous run. The React review client is under `ui/`.

```powershell
# terminal 1
ppt-eval serve --host 127.0.0.1 --port 8000

# terminal 2
cd ui
pnpm install
pnpm dev -- --port 5173
```

Open the reviewer at `http://127.0.0.1:5173/` and the interactive API schema at `http://127.0.0.1:8000/docs`.

PowerPoint and LibreOffice renderer adapters are included behind stable ports. The final interview deck was successfully rendered with PowerPoint 16, while the default MVP scoring chain still uses PPTX object-tree evidence and does not consume renderer pixels. The LibreOffice adapter currently exports PDF only and is not part of scoring.

## Audit and interview report

```powershell
$env:PYTHONPATH = "src"
python -m ppt_eval.cli project-report
python scripts/reporting/verify_audit.py audit/example/project_audit.json audit/example/events.jsonl
```

The source of truth is split into `reports/01_research/`, `reports/02_development/`, and `reports/03_evaluation/`. The generated audit site is `reports/generated/project-audit.html`.

The nine-slide interview deck is generated from the same audited JSON through `scripts/reporting/build_interview_deck.mjs`. The latest verified delivery is `reports/generated/ppt-eval-interview-v3.pptx`; it has been rendered through artifact-tool, the presentation helpers, and PowerPoint itself, and its overflow, speaker notes, and system self-evaluation all pass. The historical Node blocker and its superseding resolution remain documented in `reports/generated/PPT_BUILD_BLOCKER.md`.

## Verification

`scripts/run_tests.py` executes the plain-assert suite without third-party test dependencies. It covers the Harness, scoring properties, degradation, PPTX security/parser behavior, Oracle evidence, persistence, review, and audit export. Production release thresholds remain pre-registered targets rather than claimed results until the 400-case gold set and shadow traffic exist.
