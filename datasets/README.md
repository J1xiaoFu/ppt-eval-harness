# PPT evaluation datasets

This directory contains dataset manifests and preparation instructions, not bulk third-party
assets. Generated and downloaded files live under `var/datasets/`, which is ignored by Git.

Publicly downloadable does not mean production-licensed. Every external sample must retain its
source URL, immutable revision, content hash, license, attribution, and allowed-use partition.

## Prepared benchmark layers

### 1. Local synthetic PPTX gold set

This is the first mandatory regression layer. The decks are generated from repository-owned
fixtures and contain known defects such as small text, overlap, out-of-bounds objects, blank
slides, rasterization, active content, external relationships, and an unreadable package.

```powershell
& .\.venv\Scripts\python.exe scripts\datasets\build_local_gold.py
& .\.venv\Scripts\python.exe scripts\datasets\verify_local_gold.py
```

Outputs:

- `var/datasets/local_gold_v1/files/*.pptx`
- `var/datasets/local_gold_v1/manifest.json`
- `var/datasets/local_gold_v1/verification.json`

The manifest records metric-level predicates rather than brittle whole-report snapshots. This
lets it verify monotonicity and evidence localization while profiles and optional model audits
evolve.

### 2. SlideAudit starter slice

SlideAudit provides slide images, defect taxonomy labels, object descriptions, and bounding-box
ground truth. The full repository is about 1.2 GB, so the starter command fetches three four-slide
groups (12 images) from a pinned commit, plus annotations, descriptions, metadata, license, and
citation files.

```powershell
& .\.venv\Scripts\python.exe scripts\datasets\fetch_slideaudit_starter.py
& .\.venv\Scripts\python.exe scripts\datasets\verify_download_manifest.py `
  var\datasets\slideaudit_starter_v1\manifest.json
```

Output: `var/datasets/slideaudit_starter_v1/`.

This slice is for VLM visual-audit contract tests and evaluator meta-evaluation. It is image-level
ground truth and cannot validate PPTX editability or OOXML structure.

### 3. PresentBench rubric starter slice

PresentBench contains 238 source-grounded cases with an average of 54.1 manually designed binary
checklist items. The starter command downloads the dataset metadata and one rubric/instruction set
from each of five domains, but deliberately excludes the background materials because their source
licenses vary. The rubrics are CC-BY-NC-4.0 and remain research-only.

```powershell
& .\.venv\Scripts\python.exe scripts\datasets\fetch_presentbench_rubrics.py
& .\.venv\Scripts\python.exe scripts\datasets\verify_download_manifest.py `
  var\datasets\presentbench_rubrics_v1\manifest.json
```

Output: `var/datasets/presentbench_rubrics_v1/`.

### 4. Slides-Align real same-topic paired slice

This preparation script expands the `topic_introduction / market_analysis` human-ranking slice
to every product for which the pinned source revision contains both an original PPTX and its
complete upstream per-slide PNG sequence. Every downloaded binary is checked against its upstream
Git LFS SHA-256 before it replaces a local file, and PPTX slide counts must match the PNG sequence.

```powershell
& .\.venv\Scripts\python.exe scripts\datasets\fetch_slides_align_market_analysis.py
& .\.venv\Scripts\python.exe scripts\datasets\verify_download_manifest.py `
  var\datasets\slides_align_sample\manifest.json
```

Output: `var/datasets/slides_align_sample/`.

At revision `2f50ac6674a506acb245275e58c8a452c00e6a14`, ranks 1, 2, 3, 4, 5, 6,
and 8 form complete PPTX/PNG pairs. Rank 7 (Zhipu) has nine PNGs but no PPTX, so it is recorded as
unavailable rather than silently evaluated on a different input contract. This whole slice remains
restricted to `research_quarantine`; it is not admitted to training, production, or commercial use.

## Larger candidate sets

The curated admission plan is in [benchmark_plan.json](benchmark_plan.json). The comprehensive
research audit remains in `reports/01_research/07_public_dataset_catalog.md` and its machine-readable
evidence file. Large, gated, non-commercial, or per-record-licensed datasets must stay in
`research_quarantine` until their listed admission checks pass.

## Split policy

- Split by parent deck, source record, template, and generation system, never by individual page.
- Keep all controlled defect variants with their parent in the same split.
- Do not let frozen evaluation samples enter prompt tuning, model training, or candidate generation.
- A new Oracle/Profile release must report results on `local_synthetic`, a licensed real-PPTX slice,
  a visual-defect slice, and scenario-grounded tasks.
