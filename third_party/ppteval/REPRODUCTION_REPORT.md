# PPTAgent / PPTEval 复现报告

## 1. 结论与复现状态

本次复现将“论文基线”和持续演进的仓库 `main` 分开处理。论文最终版本为 EMNLP 2025
论文 `2025.emnlp-main.728`；评测实验代码固定为官方 `experiment` 标签对应的提交
`88c29f045ab5b7db331bd8b76cf6efc5f9ea7eee`。该提交中的 16 个关键源码、Prompt、依赖清单和
100 条实验数据清单已经逐一用 Git blob hash 校验，结果为 `16/16` 一致。

| 项目 | 状态 | 可验证证据 |
|---|---|---|
| 论文与公开结果 | 已复核 | EMNLP 正式 PDF、DOI、arXiv v3 均已留存并计算 SHA-256 |
| 官方实验源码 | 已固定 | `paper_experiment/`，Git blob `16/16` 匹配固定提交 |
| 官方示例输入 | 已预检 | 10 页、10 张官方渲染图、42 次预期模型调用，`READY` |
| 本项目 Aurora 输入 | 已预检 | PowerPoint 原生渲染 4/4 页、18 次预期模型调用，`READY` |
| GPT-4o 实际评分 | 凭证边界 | `OPENAI_API_KEY` 未配置，结果明确为 `BLOCKED_CREDENTIAL`，没有生成伪分数 |
| 全论文 500 任务重跑 | 资源边界 | 需要论文约 500 GPU 小时、私有 Qwen 服务替代部署及全部输入预处理 |

可执行适配器为 `run_ppteval.py`。它保留官方 Prompt、`gpt-4o-2024-08-06`、消息拆分、五次重试、
1-5 分验证、缓存和聚合；唯一有意差异是将论文代码的 OpenAI Batch 传输改为同步公共 Chat
Completions，便于单份 PPT 可复现。这个差异已写入每个结果的 provenance，不能与“零差异运行”混称。

## 2. 官方方法与结果

PPTEval 对每页先分别生成内容描述和设计描述，再让 GPT-4o 对描述评分；Content 与 Design 在页级
取均值。Coherence 先从整份 PPT 文本提取逐页用途和背景信息，再进行整份 1-5 分评分。最终 Avg.
为三个维度的算术平均，不含置信度、硬门禁或业务任务遵循。

正式论文 Table 5 报告如下：

| 方法 | 配置 | SR | PPL | ROUGE-L | FID | Content | Design | Coherence | Avg. |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DocPres | GPT-4o LM | - | 76.42 | 13.28 | - | 2.98 | 2.33 | 3.24 | 2.85 |
| DocPres | Qwen2.5 LM | - | 100.4 | 13.09 | - | 2.96 | 2.37 | 3.28 | 2.87 |
| KCTV | GPT-4o LM | 80.0% | 68.48 | 10.27 | - | 2.49 | 2.94 | 3.57 | 3.00 |
| KCTV | Qwen2.5 LM | 88.0% | 41.41 | 16.76 | - | 2.55 | 2.95 | 3.36 | 2.95 |
| PPTAgent | GPT-4o LM + GPT-4o VM | 97.8% | 721.54 | 10.17 | 7.48 | 3.25 | 3.24 | 4.39 | 3.62 |
| PPTAgent | Qwen2-VL LM + VM | 43.0% | 265.08 | 13.03 | 7.32 | 3.13 | 3.34 | 4.07 | 3.51 |
| PPTAgent | Qwen2.5 LM + Qwen2-VL VM | 95.0% | 496.62 | 14.25 | 6.20 | 3.28 | 3.27 | 4.48 | 3.67 |

论文的人评验证覆盖 250 份 PPT、4 名研究生。Fleiss' Kappa 总均值为 `0.59`，Content/Design/
Coherence 分别为 `0.61/0.61/0.54`；自动分与人工分 Pearson 为 `0.70/0.90/0.55`，均值 `0.71`，
Spearman 为 `0.73/0.88/0.57`，均值 `0.74`。这说明设计维度相关性较强，但 Coherence 仍明显偏弱，
不能把论文整体均值误读成每个维度都达到 0.71。

## 3. 实际执行记录

官方示例 `build_effective_agents.pptx`：PPTX SHA-256 为
`217bd88104b68f927901ac29e2a61ab057fd2b820d96b842f222bb1d3a7eb382`，识别到 10 页与 10 张官方
JPG，提取文本 2,771 字符，六个 Prompt 均通过占位符检查，预期调用 42 次，预检耗时 0.214 秒。
完整证据在 `reproduction/official-example-preflight.json`。

本项目 `aurora_demo.pptx`：SHA-256 为
`d9cc4b42e750532a5c86893b98c8369b1c629887069685f9f07d61cc5e5af1b5`。PowerPoint 原生导出 4 张
1600×900 PNG，4/4 页匹配，提取文本 482 字符，预期调用 18 次，预检耗时 0.194 秒。无凭证正式入口
运行耗时 0.201 秒并以退出码 `2`、状态 `BLOCKED_CREDENTIAL` 终止；证据在
`reproduction/aurora-ppteval.json`。

```powershell
$Python = 'C:\Users\DiegoWang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python third_party/ppteval/run_ppteval.py `
  --pptx examples/demo/aurora_demo.pptx `
  --slides third_party/ppteval/reproduction/aurora_slides `
  --output third_party/ppteval/reproduction/aurora-ppteval.json
```

适配器通过 `py_compile`、Ruff 和 JSON/消息契约 smoke。没有 API 凭证时仍可运行 `--dry-run`，校验
输入、Prompt hash、页数、渲染图和预期调用数。

## 4. 依赖与不可直接复现项

- 论文评测模型固定为 `gpt-4o-2024-08-06`；Qwen 实验服务在源码中硬编码为私网地址，不是公开服务。
- PPL 使用源码中的绝对私有路径 `/141nfs/zhenghao2022/PPTAgent/Llama-3-8B`，需要改成公开
  `Meta-Llama-3-8B` 等价权重并记录新 hash 后才可重跑，不能声称当前已复现论文 PPL。
- FID 子模块固定为 `jaywu109/faster-pytorch-fid@6e6bd16...`，使用 64 维 Inception 输出并需要 GPU。
- 依赖清单多数没有版本锁；论文环境要求 Python 3.11+、LibreOffice、Poppler、Node.js 和 GPU。
- DocPres、KCTV 在该仓库中是作者为统一实验环境编写的复现脚本，不是原项目的锁定容器；KCTV 还
  依赖 `pdflatex`，DocPres 依赖 CLIP 和 CUDA。因此论文数值属于“作者复现口径”，应保留此标签。
- 当前 `main` 已演进为 DeepPresenter；其 PPTEval 包导入了新的实验依赖，且 coherence 路径存在异步
  调用未等待的漂移风险。因此生产基线不得追踪 `main`，必须使用固定 Profile/Prompt/模型版本。

## 5. Zenodo10K 可用性与合规

[Zenodo10K](https://huggingface.co/datasets/Forceless/Zenodo10K) 公开且不 gated，当前 revision 为
`e59bf3ec11f7518a6c84dc145d83c0675d412522`，索引包含 10,448 条 PPTX 元数据和原始文件入口。
数据卡没有一个覆盖全部文件的统一许可证；许可证在每条记录中。因此“公开可下载”不等于“可统一商用”。

论文固定的 100 条实验清单由五个领域各 10 个 PDF 与 10 个 PPTX 组成，覆盖 90 个 Zenodo record。
2026-08-26 对 90 个 Zenodo API 元数据逐项复核全部返回 200：87 个 `CC-BY-4.0`、2 个
`CC-BY-SA-4.0`、1 个 `CC-BY`。正式数据管线必须按条保存 DOI、license、作者与 attribution；SA/NC
等条款单独路由，`notspecified` 不进入工业训练或冻结评测集。90 条逐项响应和汇总保存在
`reproduction/zenodo-license-audit.json`，可由 `audit_zenodo_licenses.py` 重新生成。

## 6. 纳入本系统的建议

- 将 PPTEval 作为三个可替换的软分 Oracle：`content_quality`、`visual_design`、`deck_coherence`；
  使用 `(score-1)/4` 映射到 `[0,1]`，只进入 PPT-PDMS 内层加算，绝不触发外层硬乘子。
- 四场景都可运行这三个 Oracle，因为它只要求 PPT 本体与渲染图；若模型失败，仍保留确定性本体
  Oracle，状态为 `DEGRADED/BASE_ONLY + REVIEW`，不得把 PPTEval ERROR 当零分。
- Content rubric 同时包含文字与图像，Design rubric也包含可读性，接入时须与本系统的字体、溢出、
  素材相关性指标做缺陷去重，避免同一问题在乘算与加算中重复处罚。
- 不纳入 PPTEval 的硬门禁能力：来源忠实性、事实正确性、指令遵循、必选素材、图表数据、文件安全、
  可编辑性和兼容性，继续由现有专项/确定性 Oracle 负责。
- 上生产前在中文企业 PPT 金标集重新校准。至少报告重复运行方差、Judge 版本漂移、中文/英文切片、
  模板切片和与人工分的分维度相关性；Coherence 因论文 Pearson 仅 0.55，应优先人审和二次验证。

## 7. 来源

- EMNLP 正式论文与 DOI：<https://aclanthology.org/2025.emnlp-main.728/>
- arXiv v3：<https://arxiv.org/abs/2501.03936v3>
- 官方仓库：<https://github.com/icip-cas/PPTAgent>
- 固定实验提交：<https://github.com/icip-cas/PPTAgent/commit/88c29f045ab5b7db331bd8b76cf6efc5f9ea7eee>
- Zenodo10K：<https://huggingface.co/datasets/Forceless/Zenodo10K>
- 结构化来源、revision 与 hash：`sources.json`
