# 公开基线复现报告

审计日期：2026-08-26。本文区分“代码/输入已复现”“评分已复现”和“论文全量数值已复现”。
没有模型凭证、缺失数据或上游实现错误时，不以论文数字或人工补分冒充本机结果。

## 复现结论

| 实验 | 固定版本 | 本机实际完成 | 未完成边界 | 结论 |
|---|---|---|---|---|
| `EXP-BASELINE-PPTAGENT` | PPTAgent `experiment@88c29f045ab5b7db331bd8b76cf6efc5f9ea7eee` | 16/16 关键 blob 校验；官方 10 页样例和 Aurora 4 页 PPTEval 预检；渲染、Prompt、调用图与退出语义通过 | 无 `OPENAI_API_KEY`，不能执行固定 `gpt-4o-2024-08-06` 的真实评分；私有 Qwen/PPL 路径和全论文算力不可得 | `PARTIAL_REPRODUCTION / BLOCKED_CREDENTIAL` |
| `EXP-BASELINE-AUTOPRESENT` | AutoPresent `98e0c012e89469863d9c3c8bc87eac967d82b2e6` | 单页 reference-based 指标、媒体抽取和 canonical code 生成真实运行 | Reference-free 无 API key；公开训练集缺 `code`，测试 reference 仅 1/10，AutoPresent LoRA 依赖 gated Llama 与 Linux/GPU | `PARTIAL_REPRODUCTION / RELEASE_INCOMPLETE` |

这两个实验均达到“入口、版本、依赖、输入、失败边界可复现”，但没有达到“论文全量数值可独立重算”。
因此研究门禁从 `planned` 提升为 `partial`，不能标记为论文 exact reproduction。

## PPTEval 实际记录

- 官方 10 页 `build_effective_agents.pptx`：文本 2,771 字符，预期 42 次固定模型调用，预检耗时 0.222 秒。
- 本项目 `aurora_demo.pptx`：PowerPoint 原生渲染 4/4 页，文本 482 字符，预期 18 次调用，预检耗时 0.206 秒。
- 无凭证正式入口：退出码 `2`，状态 `BLOCKED_CREDENTIAL`，没有生成 Content/Design/Coherence 分数。
- 论文人工相关：Content/Design/Coherence Pearson 分别为 `0.70/0.90/0.55`；Coherence 不应成为硬门禁。
- 可重跑适配器：`third_party/ppteval/run_ppteval.py`；完整来源、Prompt hash 与证据见 `third_party/ppteval/`。

```powershell
$Python = "C:\Users\DiegoWang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $Python third_party/ppteval/run_ppteval.py `
  --pptx examples/demo/aurora_demo.pptx `
  --slides third_party/ppteval/reproduction/aurora_slides `
  --dry-run `
  --output third_party/ppteval/reproduction/aurora-preflight.json
```

## SlidesBench / AutoPresent 实际记录

| Smoke | match | text | color | position | 观察 |
|---|---:|---:|---:|---:|---|
| 官方 `food.pptx` 第 1 页与自身比较 | 100 | 100 | 25 | 100 | identity test 的颜色不为 100 |
| `aurora_demo.pptx` 第 1 页与自身比较 | 100 | 100 | 50 | 100 | 同样暴露颜色指标伪差异 |
| 官方空白第 2 页与自身比较 | - | - | - | - | `ZeroDivisionError` |

- `parse_media.py`：32 个页面目录、36 个图像、0.687 秒；实际为 7 JPEG、27 PNG、2 GIF，却全部命名为 `.jpg`。
- `reproduce_code.py`：生成 32 个程序和 53 个图像、0.850 秒；执行被未声明的 `chromedriver_py` 和不存在的 `mysearchlib` 阻断。
- 三份公开训练 CSV 为 7,622/7,045/7,045 行，只有 `instruction,slide_folder`；`train.py` 必需的 `code` 列不存在。
- 585 个测试 instruction 已公开，但 reference PPTX 仅 `1/10`，页面媒体为 `0/195`，README 引用的三个测试 CSV 不存在。
- 可重跑脚本和完整日志：`third_party/slidesbench/run_smoke.ps1`、`reproduction-manifest.json` 与 `evidence/`。

## 接入决策

- PPTEval 的 Content、Design、Coherence 只能作为 PPT-PDMS 内层软 Oracle；模型失败时触发降级，不能计零或触发硬乘子。
- SlidesBench 的 element/content/color/position 只作为 reference profile 候选指标。进入主系统前必须先修复 identity、空页、类型、MIME 和跨平台问题。
- 两个基线均不覆盖来源忠实、关键数字、必选素材、文件安全、可编辑性和兼容性；这些能力继续由现有确定性/专项 Oracle 负责。
- 论文报告数值保留 `paper-reported` 标签；本机真实输出保留 Run/log/hash，二者不得混表。

## 官方来源

- PPTAgent/PPTEval：<https://aclanthology.org/2025.emnlp-main.728/>、<https://github.com/icip-cas/PPTAgent>
- AutoPresent/SlidesBench：<https://arxiv.org/abs/2501.00912>、<https://github.com/para-lost/AutoPresent>
- 详细复现报告：`third_party/ppteval/REPRODUCTION_REPORT.md`、`third_party/slidesbench/README.md`

