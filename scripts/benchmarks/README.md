# Profile 8.4 offline acceptance benchmark

Run the model-free long-deck routing and visual-input cost preflight from the
repository root:

```powershell
python scripts/benchmarks/profile84_long_deck_acceptance.py `
  --output var/benchmarks/profile84-long-deck.json
```

The benchmark materializes deterministic 20-, 50-, and 100-page renders and
real 4x4 Atlas files in a temporary directory. It runs the production
`VisualPageIndex` and `VisualSelectionPlan` code, then uses a frozen local Scout
and exact criterion-scoped audit fixture. No API key or network connection is
required.

The JSON output records every challenge defect, criterion-specific page sample,
classification count, cache/Token estimate, acceptance threshold, and the
versions of all benchmark assumptions. It deliberately includes one benign
Scout route per deck to verify that a suspicion remains non-scoring until the
independent high-resolution audit confirms it.

This is a routing/cache regression gate, not a claim about live VLM quality.
Provider response validity, real token billing, and model precision/recall must
still be measured on the fixed live-provider challenge set before release.

## Pinned Slides-Align live replay

The live runner validates every pinned manifest byte, anonymizes PPTX and render
paths before they reach the evaluation runtime, and keeps human rank outside the
Oracle context. It writes resumable per-case checkpoints plus topic-local
Spearman/pairwise diagnostics and an HTML audit report:

```powershell
$env:PPT_EVAL_QWEN_AUDIT_ENABLED = "true"
$env:PPT_EVAL_DASHSCOPE_API_KEY_FILE = "api/qwen3.7_flash_api.txt"
$env:PPT_EVAL_ZHIPU_AUDIT_ENABLED = "true"
$env:PPT_EVAL_ZHIPU_API_KEY_FILE = "api/glm5.3_flash_api.txt"
python scripts/benchmarks/evaluate_slides_align_profile84.py `
  --suite-root var/datasets/slides_align_three_topics `
  --output-dir var/benchmarks/slides-align-profile84-live `
  --workers 3 --resume
```

Use `--verify-only` before a paid run. A live run requires a clean Git checkout.
Resume is bound to the dataset hashes,
Profile fingerprint, evaluation Git SHA, persisted report hash, visual contract
hashes, and audit chain. Rank correlations and Composite variance are diagnostic
only: they are never sent to Oracles, used to fit Profile 8.4, or shown in the
production review queue.

“模型合法响应率”按一次逻辑审计在 Qwen/GLM 回退后的最终结构化合同统计；Provider
内部 HTTP 重试次数单独保留在 `visual_usage.request_count`，不会把已成功恢复的传输
重试重复算成多个最终响应。
