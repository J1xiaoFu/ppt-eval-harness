# PPT Eval Harness

An evidence-first, deterministic evaluation harness for generated and existing PowerPoint decks. It combines an always-on intrinsic PPT quality baseline, scenario-specific Oracles, PDMS-style scoring, explicit degradation, human review, and append-only audit evidence.

New maintainers should start with the Chinese [onboarding and handover guide](docs/onboarding_handover_guide.md). It explains the architecture, every component's responsibility, the versioned deterministic atomic metrics, the tiered model audits, scoring and degradation, local operations, extension patterns, release governance, troubleshooting, and current implementation gaps.

## What works

- Four scenarios: `text_to_ppt`, `project_summary`, `multimodal`, and `ready_made`.
- The `baseline_ppt_quality` composite is injected into every execution DAG and cannot be removed by configuration.
- Safe PPTX ZIP preflight, `python-pptx` parsing, and an OOXML fallback parser.
- Fifteen intrinsic metrics plus scenario metrics with page/object/bbox evidence.
- Deterministic `OBSERVE -> PLAN -> ACT -> VERIFY -> FINALIZE/REVIEW` supervisor.
- PPT-PDMS aggregation, high-confidence hard multipliers, required/optional `NA`, and isolated runtime `ERROR`.
- Strict vendor-neutral LLM/VLM contracts. The default v8 profiles run deterministic scoped
  observations first, then criterion-specific `qwen3.8-flash` audits and same-criterion
  `glm-5.3-flash` escalation only when the primary result is unresolved, uncertain, or conflicts
  with rules. The two providers use independent credentials, endpoints, and audit lineage.
  Historical v1-v7 Profiles remain explicitly loadable; bit-identical replay also requires the
  corresponding historical Git SHA or container image.
- Optional construct-aware aggregation fixes content/visual/delivery/handoff budgets and reports
  construct scores; it is exposed only through an unvalidated v4 candidate. Raster-only decks use
  rendered semantic content recovery instead of treating an empty object-tree text layer as zero.
- The v5 historical experimental Profile replaces the scalar visual judge with six fixed visual criteria;
  Harness validates every criterion and recomputes the visual score, while the model's global score
  is retained only as metadata. The v6 candidate exposes the six criteria as independent metrics,
  hard-caps VLM at 20% of visual, and keeps scalar Advanced routing disabled until a dimension-
  isomorphic reviewer is released.
- The v7 grounded visual candidate replaces the unstable six-dimensions-in-one-call judge with six
  independent criterion calls over bounded, explicitly labelled page samples. It adds SlideAudit-
  derived defect codes, deterministic score caps, positive aesthetic anchors, Harness-owned
  observability, and a 5% VLM-internal render-integrity budget. It remains experimental and does
  not replace the historical v6 contract.
- The default v8 path stores complete object/page/pair/claim/requirement/asset observations in a
  content-addressed audit artifact, reduces them with versioned lower-tail-aware policies, fuses
  deterministic caps with model positive signals, and reports independent visual/layout/content/
  full-deck training eligibility. v8.2 adds an independently owned language-consistency metric and
  a seventh, cross-page authorship-specificity VLM audit for systemic card/icon/template formulaicity;
  the authorship construct still enters the formula only once. No holistic LLM/VLM score enters it.
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

The environment-aware CLI/API runtime reads the primary DashScope key from
`DASHSCOPE_API_KEY`, or from the ignored local file `api/qwen3.7_flash_api.txt`.
The independent fallback reads `ZAI_API_KEY`, or `api/glm5.3_flash_api.txt`.
The endpoints are `https://dashscope.aliyuncs.com/compatible-mode/v1` and
`https://open.bigmodel.cn/api/paas/v4`; both models run with thinking enabled.
Do not put a real key in `.env.example` or commit the `api/` directory.

HTTP ceilings default to 120 seconds for the Qwen primary and 300 seconds for
the GLM fallback (`PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS` and
`PPT_EVAL_ZHIPU_HTTP_TIMEOUT_SECONDS`). Legacy
`PPT_EVAL_QWEN_PLUS_*` names remain accepted when the new names are unset. These are transport limits; the
Profile-level `oracle_timeout_seconds` is not yet enforced by the scheduler.

Local files named in `EvalCase.source_materials` are fail-closed for remote
model audits. Inline source text remains available, but a local file is read
only when it is beneath an explicitly approved `PPT_EVAL_MODEL_SOURCE_ROOTS`
directory (use `;` between roots on Windows and `:` on Linux/macOS). Absolute
paths are replaced by opaque source IDs before transmission. Credential paths,
`.env`, `.git`, `api/`, and operating-system secret locations remain blocked
even when a broader root was configured.

```powershell
$env:DASHSCOPE_API_KEY = "sk-..."
ppt-eval run examples/demo/case_ready_made.json
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

PowerPoint and LibreOffice renderer adapters are included behind stable ports. When the Qwen Flash VLM is enabled and callers did not supply `slide_images`, the environment-aware local runtime attempts a hash-keyed render cache automatically. Windows prefers native PowerPoint; LibreOffice exports PDF and, when `pdftoppm` is available, rasterizes it to per-slide PNGs (the Docker image includes this dependency). A render failure is recorded as missing evidence and degrades v3 coverage instead of crashing the run.

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

The first real Qwen v3 check is intentionally reported without spin:
on three same-topic Slides-Align decks, descriptive Spearman versus human
ranking was `0.50` (two of three pairwise orders correct), below the historical
v2 slice's `1.00`. See
[`06_qwen_v3_real_ppt_gt_slice.md`](reports/03_evaluation/06_qwen_v3_real_ppt_gt_slice.md).
This small slice is diagnostic evidence that the current prompts, weights, and
review thresholds still need calibration—not a production quality claim.

The expanded same-topic set now contains seven complete PPTX/render pairs
(130 slides). On this larger diagnostic slice, the current-code deterministic
baseline has Spearman `0.107`, while Flash v3 has `-0.321`; construct-capped
aggregation alone does not repair the ordering. The audit and the experimental
v4 construct Profile are documented in
[`07_aggregation_metric_iteration.md`](reports/03_evaluation/07_aggregation_metric_iteration.md).

The next Oracle/Profile-only iteration is documented in
[`08_structured_visual_oracle_profile.md`](reports/03_evaluation/08_structured_visual_oracle_profile.md):
six fixed VLM criteria, Harness-side score recomputation, raster semantic fallback,
and a diagnostic body-completeness Oracle.
