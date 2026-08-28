# PPTEval baseline reproduction

This directory pins and audits the official PPTEval implementation described in
PPTAgent, rather than treating the moving `main` branch as the paper baseline.

## Pinned sources

- Paper: [EMNLP 2025 / DOI 10.18653/v1/2025.emnlp-main.728](https://aclanthology.org/2025.emnlp-main.728/), with [arXiv:2501.03936v3](https://arxiv.org/abs/2501.03936v3) retained for version comparison.
- Repository: [icip-cas/PPTAgent](https://github.com/icip-cas/PPTAgent), MIT.
- Paper experiment tag/commit: `experiment` / `88c29f045ab5b7db331bd8b76cf6efc5f9ea7eee` (2025-03-13).
- Current upstream snapshot: `upstream/`, commit `2419d30b134a71486523e95ded60b32489fd3c61` (2026-06-28).
- Paper experiment snapshot: `paper_experiment/`, downloaded byte-for-byte from the pinned commit.
- Public metadata corpus: [Forceless/Zenodo10K](https://huggingface.co/datasets/Forceless/Zenodo10K), 10,448 records. Licensing is per Zenodo item, not one blanket dataset license.

Reproduction JSON stores repository inputs as repo-relative POSIX paths and external inputs as
`<external>/basename`; hashes, byte counts, commits and timestamps remain the authoritative
provenance. Host usernames and checkout roots are deliberately not persisted.

The published evaluator uses `gpt-4o-2024-08-06` for both slide description and
judging. Content and design are scored per slide on a 1-5 scale and averaged;
coherence is scored once for the whole deck; the reported overall score is the
arithmetic mean of the three dimensions.

## Reproduction commands

Use a Python environment containing `python-pptx` (the Codex bundled Python in
this workspace already contains it). First run the credential-free preflight:

```powershell
$Python = (Get-Command python).Source
& $Python third_party/ppteval/run_ppteval.py `
  --pptx third_party/ppteval/upstream/resource/build_effective_agents/build_effective_agents.pptx `
  --slides third_party/ppteval/upstream/resource/build_effective_agents `
  --output third_party/ppteval/reproduction/official-example-preflight.json `
  --dry-run
```

To obtain actual judge scores, set a valid API credential and remove
`--dry-run`. The command writes after every slide and resumes from the output
cache:

```powershell
$env:OPENAI_API_KEY = '<key>'
& $Python third_party/ppteval/run_ppteval.py `
  --pptx examples/demo/aurora_demo.pptx `
  --slides third_party/ppteval/reproduction/aurora_slides `
  --output third_party/ppteval/reproduction/aurora-ppteval.json
```

Recheck every Zenodo record used by the paper manifest instead of trusting a
blanket dataset-license claim:

```powershell
& $Python third_party/ppteval/audit_zenodo_licenses.py `
  --manifest third_party/ppteval/paper_experiment/resource/dataset.jsonl `
  --output third_party/ppteval/reproduction/zenodo-license-audit.json
```

## Fidelity and known boundary

`run_ppteval.py` preserves the official prompts, GPT-4o snapshot, message split,
five-attempt retry policy, 1-5 validation, and aggregation. It deliberately uses
synchronous public Chat Completions instead of the paper code's Batch API so a
single deck can be reproduced interactively. The untouched experiment runner
also hard-codes two private Qwen endpoints and imports the full generation stack;
those endpoints are not required for the published GPT-4o judging baseline.

No score is fabricated when credentials are absent. The result is explicitly
`BLOCKED_CREDENTIAL`, while prompt, deck, slide image, call-count, and hash
preflight evidence remains reproducible.
