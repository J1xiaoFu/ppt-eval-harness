# 版本矩阵与 Changelog

## 版本矩阵

| 组件 | 当前设计版本 | 兼容策略 |
|---|---|---|
| Audit schema | 1.0 | 同主版本向后兼容；未知字段显式处理 |
| Eval API | v1 | 新字段可选；删除/改义升 v2 |
| Oracle protocol | 1.0 result / v8 AtomicObservation | 原子观察与Reducer结果分离；legacy结果继续兼容 |
| PPT-PDMS Profile | v8.3 默认 / v8.2 authorship+language / v7 原子视觉 / v6 六维视觉 / v5 汇总视觉 / v4.1 construct / v3.1及更早历史 | 新行为提升 Profile/Oracle 版本；完整历史回放同时固定 Git SHA |
| Rubric | rubric-v1 | 报告携带版本 |
| Dataset | local-synthetic-v1 / external starter manifests v1 | 每次切分固定 source revision、逐文件 hash、许可和用途分区 |
| Runnable package | 0.1.0 | 领域契约同主版本兼容；行为变化发布新 Profile/Oracle 版本 |

## Changelog

### 2026-08-28

- v8.3 将可争议硬门的 MAJOR/CRITICAL 候选页强制纳入同构念 VLM 样本，保留封面、结尾和
  canonical 探索页；候选页已被观察后才能确认或否决，未观察不再被误作审计完成。
- 新增仅对全栅格 deck 生效的 `raster_content_structure` 与
  `raster_language_consistency` 页级 VLM/OCR observations。Reducer 仅在原规则 N/A 时接管，
  可编辑 deck 不调用，模型费用只在诊断结果中计一次。
- 修正训练准入优先级：已知 `<60` 分先 REJECT，`content_evidence:missing` 不再把明确低分
  提升为 REVIEW。

### 2026-08-27

- v8.2 新增 4% `language_consistency`（从 content structure 划转）和第七个 v8-only
  authorship VLM 原子节点；`authorship_specificity_v2` 以规则 30% / 语义 VLM 70% 在同一
  12% 构念内融合，旧字段只作不计分别名。新增跨页模板轮廓、机械卡片/图标、占位案例、
  公式化文案和风险页采样；语言/authorship 明确排除在 functional gate 之外，防止双罚。
- v8 成为四场景默认：新增九种 observation scope、完整 observation artifact、三类 Reducer、
  多阶段固定 DAG 和 visual/layout/content/full-deck 四轨训练准入。
- 重写/去重 page/object/pair/claim/requirement/asset/chart-series 原子能力；critical requirement、
  required asset 和 chart expectation 只观察一次，由 criticality 决定 gate 或 additive reducer。
- 六个视觉维度成为独立 DAG 节点；v8.1 以 qwen3.8-flash 为主线，未解决、低置信或与
  规则冲突时，只对同一 criterion 通过独立 BigModel Provider 调用 glm-5.3-flash。
  两次 attempt 的身份、Evidence、usage/cost 均保留；v8 公式不包含整体 LLM/VLM Judge 分数。
- v8 基础构念按质量属性而非传感器组织：content structure 25%、composition 20%、
  typography 10%、palette 8%、visual communication 15%、visual system 10%、
  authorship specificity 12%。规则提供cap，模型提供正向信号，避免重复计权。
- 新增 v6 六维视觉候选：一次 VLM 调用返回六个独立 metric，Prompt/Oracle `1.2.0`
  要求单维 score/confidence/observability；低置信或不可观测转 N/A/REVIEW。
- v6 将 `visual_deterministic=40%` 与 `visual_vlm=10%` 硬隔离；VLM 占合并视觉
  20%、总分 10%，不会因可选确定性视觉指标 N/A 被重归一化扩权。
- 现行 v3 Profile 升至 `3.1`、v4 候选升至 `4.1`：基线仍为 `qwen3.7-flash`，
  Advanced 角色默认迁移至 `qwen3.8-flash`。新 `PPT_EVAL_QWEN_ADVANCED_*` 环境变量优先，
  旧 `PLUS_*` 仅作无冲突兼容入口；历史 v3.0 精确回放需固定旧 Git ref。
- v6 当前仍为 Flash-only；`qwen3.8-flash` 尚未作为六维复核结果接入，后续必须另发
  criterion-isomorphic Advanced reviewer/Profile，不得使用旧标量视觉 Oracle。
- 新增四个 `3.0` Profile：`qwen3.7-flash` 全量进入 base/scene 公式，单项复核线、
  Flash 分歧/困惑触发 `qwen3.7-plus`，Plus 仍不确定时转人工。
- 新增 `template_residue` 指标，Layout 升至 `1.1.0`，降低真实卡片/时间线/背景装饰误报。
- 接入 DashScope OpenAI-compatible Provider：结构化 JSON、thinking、模型层级验证、token/
  prompt/model 版本审计、确定性参数和传输 timeout。
- CLI/API/worker 按环境注入 Flash/Plus；Windows PowerPoint 与容器 LibreOffice+Poppler 可自动
  生成按输入哈希缓存的页图，长 deck 最多确定性采样 12 页。
- 收紧 secret 边界：`api/` 和 `.env*` 不进入 Git/Docker context，模型本地来源文件
  默认禁读，只允许显式 source root，远端仅收到 opaque ID，渲染子进程不继承密钥。
- 在 Slides-Align 同主题三份真实 deck 上执行实际 Flash/Plus；v3 对人评的描述性
  Spearman 为 `0.50`、两两一致为 `2/3`，低于历史 v2 切片，因此保持
  `PRE_RESEARCH`，不作生产声明。
- Slides-Align 同主题扩充至 7 份完整 PPTX/PNG 配对、130 页；140/140 个文件的
  LFS SHA-256/size/CRC/页数校验通过。当前确定性/Flash Spearman 为 `.107/-.321`。
- 新增 `CONSTRUCT_WEIGHTED_MEAN` 实验聚合契约、构念分报和
  `finished_deck_v4_construct_candidate.json`；v3 默认不变。
- 内容审计在文本页低于 25% 时新增渲染语义回退，避免将栅格 deck 的“无可提取文本”
  伪造为高置信内容 0 分。
- 新增 v5 结构化视觉 Oracle/Profile：单次 VLM 返回 6 个固定 criterion，Harness 严格校验并
  重算分数，模型全局分只留 metadata；旧标量 Plus 在 v5 中禁用。
- Baseline 升至 `2.1.0`，新增不计分的 `body_completeness` 诊断 Oracle，栅格-only deck 为 N/A。

### 2026-08-26

- 建立三阶段审计骨架与机器可读 Schema。
- 将基础质量子图定义为编译期不变量。
- 固定 PPT-PDMS 外乘内加、无双罚、N/A/ERROR 语义。
- 建立九页面试汇报与只读 HTML 的同源生成流程。
- 实现确定性 RunSupervisor、Oracle Registry/Composite、四场景降级和本地/API/Worker 接入层。
- 实现 PPTX 安全预检、对象树/OOXML 解析、本体指标及三类专项 Oracle；后续新增模板残留后本体 Leaf 增至14项。
- 实现可信事实快照、反馈/edit-diff、主动采样与双人审批参数候选；首期无自动发布入口。
- 42项测试与四场景 Smoke 通过，生产人工金标和 Shadow 门槛保持 pending。
- Node 运行环境恢复后，React 复核台、FastAPI 同步/异步 API 与九页面试 PPT 均完成构建和本地验收。
- 新增严格、供应商无关的 LLM/VLM Provider 合同，以及内容、视觉、场景合规三个 Shadow 审计；
  未配置 Provider 时为可审计 N/A，v2 默认不改变分数、Coverage 或 Decision。
- 保留四个 v1 Profile 供历史回放，新增四个 v2 Shadow Profile 和一个明确标记未校准的实验计分 Profile。
- `compression_quality` Oracle 升至 1.1.0，以连续分段函数修复 `.03/.45` 边界跳变，并补方向性测试。
- 建立 9 例本地合成 PPTX 金标、12 页 SlideAudit 缺陷切片和 5 个 PresentBench 领域的 292 条
  rubric 切片；所有外部文件记录固定 revision、许可、SHA-256 和用途边界。
- 全套 pytest 与无第三方测试器均为 62 passed；Ruff、定向 mypy 和 `git diff --check` 通过。

> 当前为可运行 MVP `0.1.0`，尚无生产 Release ID；不得将 MVP 测试通过表述为生产质量门禁通过。
