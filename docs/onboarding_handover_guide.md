# PPT Eval Harness 新人交接与维护教程

本文面向第一次接触本项目、PPT 评测和 Agentic Harness 的开发者。目标不是让读者记住每个类名，
而是让其能够独立完成以下工作：

1. 在本地运行四种场景并解释结果。
2. 沿一次 Run 找到 Profile、DAG、Oracle、分数、证据和审计记录。
3. 正确区分质量失败、执行错误、证据不足和降级。
4. 新增或替换一个原子 Oracle，而不破坏上层 Harness。
5. 修改 Profile、验证候选、维护数据和处理线上异常。
6. 知道哪些能力已经实现，哪些只是架构目标，避免在交接和汇报中夸大成熟度。

适用版本：package `0.1.0`、API `v1`、Oracle/Profile/Audit Schema `1.0`。

## 1. 先建立正确的心智模型

不要把 Harness 理解成一个会自由规划的 LLM Agent。当前系统更像一家有严格 SOP 的检测中心。

| 项目概念 | 初学者类比 | 它负责回答的问题 |
|---|---|---|
| `EvalCase` | 送检单 | 评哪份 PPT，属于什么场景，附带了哪些上下文 |
| `EvalProfile` | 检测标准版本 | 启用哪些指标、权重多少、什么情况必须人审 |
| `RunSupervisor` | 值班经理 | 当前处于哪个阶段，下一步允许去哪里 |
| `EvaluationDag` | 检测工序单 | 哪个节点先执行，哪些节点依赖前序节点 |
| `OracleRegistry` | 检测能力目录 | 给定 Oracle ID 后，实际实现在哪里 |
| Composite Oracle | 检测套餐 | 按固定顺序组合一组原子检测 |
| Atomic Oracle | 单项检测员 | 只判断一个指标并提交结构化证据 |
| `PptxAdapter` | 检测仪器 | 安全地把 PPTX 翻译成统一对象模型 |
| `PptPdmsAggregator` | 计分器 | 如何把原子结果组合成基础分和完整分 |
| `DecisionPolicy` | 放行标准 | 最终是 PASS、REVIEW、FAIL 还是 ERROR |
| `RunManifest` | 黑匣子清单 | 这次结果由什么代码、字体和 Profile 产生 |
| `AuditEvent` | 不可涂改日志 | 谁在什么时候做了什么，前一条记录是什么 |

四类责任必须分开：

```text
Harness          决定按什么顺序调用谁
Oracle           决定单个指标怎样测
Aggregator       决定怎样算分
DecisionPolicy   决定业务上怎样处置
```

“Agentic”体现在固定的 `OBSERVE -> PLAN -> ACT -> VERIFY -> FINALIZE/REVIEW` 循环、工具化 Oracle
和证据回流，而不是让模型动态修改 DAG、权重或发布规则。

## 2. 五条系统不变量

1. 所有场景必须评 PPT 本体。`ProfileCompiler` 无条件注入唯一的 `baseline_ppt_quality`。
2. 执行错误不等于质量差。`ERROR` 不能自动变成质量零分。
3. Oracle 只交付原子判断，不决定全局路由、最终总分或生产发布。
4. 同一 `metric_id` 不能同时进入乘算和加算，也不能返回两个计分结果。
5. 专项缺失或失败时，基础分和基础证据不能被吞掉，`full_score` 必须为空。

对应源码：

- `src/ppt_eval/application/profile.py`：强制基础子图。
- `src/ppt_eval/domain/models.py`：Case、Profile、Result、Manifest 等冻结契约。
- `src/ppt_eval/application/supervisor.py`：状态机和最终兜底。
- `src/ppt_eval/scoring/pdms.py`：覆盖状态和分数聚合。
- `tests/test_harness.py`、`tests/test_scoring.py`：不变量的可执行说明书。

## 3. 先区分已实现、部分实现和目标设计

交接时最容易犯的错误，是把系统设计图上的目标能力都当成已接通能力。

| 能力 | 当前状态 | 准确说明 |
|---|---|---|
| CLI 与同步评测 | 已实现 | 使用本地 JSON/JSONL 持久化，可完整运行四场景 |
| 强制 Baseline、DAG、状态机、PDMS | 已实现 | 当前核心控制链已经落地并有测试 |
| 30 个原子 Oracle | 已实现 | 均为确定性 PPTX 对象树或词法启发式 v1 |
| FastAPI 同步接口 | 已实现 | 使用 `LocalEvaluationRuntime` |
| FastAPI 异步接口 | 部分实现 | 使用进程内线程池，不是 Celery；服务重启后 Job 状态丢失 |
| React 人工复核台 | 已实现 MVP | 支持列表、原子结果和 APPROVE/REJECT，无鉴权、分配和分页 |
| Celery Worker | 部分实现 | Task 已定义，但 API 不会向 Celery 投递 |
| PostgreSQL、S3/MinIO | 部分实现 | Adapter 已写，默认 composition root 仍使用本地文件 |
| Docker Compose | 部署骨架 | Redis/Postgres/MinIO 会启动，但当前 API 业务路径不会使用它们 |
| PowerPoint/LibreOffice Renderer | Adapter 已实现 | 默认 Oracle 评分不消费像素结果；LibreOffice 当前只导出 PDF |
| OCR、VLM、LLM Judge | 未接入 | 默认 Registry 中没有这些模型 Oracle |
| Calibrator | 未实现 | Leaf 启发式分直接进入 PDMS |
| 参数候选治理 | 已实现 MVP | 可到 `RELEASE_CANDIDATE`，故意没有自动 production apply |
| Shadow 与灰度路由 | 协议已设计 | 自动路由、统计和一键回滚尚未实现 |
| Transactional Outbox | 目标设计 | 当前运行日志是本地追加式 JSONL |

因此当前准确定位是：**可运行、可降级、可审计的 deterministic baseline v1，而不是已经完成工业验收的最终 Judge。**

## 4. 目录地图和推荐阅读顺序

```text
src/ppt_eval/
  domain/                 稳定枚举和冻结数据契约
  application/            Supervisor、DAG 编译、调度、Oracle 端口
  adapters/               PPTX、事实快照、PowerPoint/LibreOffice
  oracles/                30 个原子指标和 4 个 Composite
  scoring/                PPT-PDMS 与 DecisionPolicy
  infrastructure/         本地和生产候选存储/队列 Adapter
  runtime.py              本地 composition root，所有部件从这里接起来
  cli.py / api.py          CLI 和 HTTP 交付层
  worker.py                Celery task 入口
  flywheel.py              反馈、主动采样、参数候选治理

configs/profiles/          四场景版本化 Profile
examples/demo/             四场景最小可运行输入
tests/                     当前行为的可执行规范
ui/                        React 复核台
audit/                     项目级审计 Schema 和示例事件
reports/                   调研、开发、评测三阶段证据
scripts/reporting/         审计验证和汇报生成器
third_party/               PPTEval、SlidesBench 复现，不属于默认评分链
var/                       本地运行数据，已被 gitignore
```

推荐阅读顺序：

```text
README.md
-> domain/enums.py
-> domain/models.py
-> runtime.py
-> application/supervisor.py
-> application/profile.py
-> application/scheduler.py
-> application/oracle.py
-> oracles/base.py
-> oracles/baseline.py / scenarios.py
-> scoring/pdms.py / policy.py
-> tests/
```

不要一上来从某个评分公式开始。先理解数据契约和一次 Run 如何穿过系统，再深入具体指标。

关键源码入口：

| 入口 | 用途 |
|---|---|
| [runtime.py](../src/ppt_eval/runtime.py) | 本地 composition root，查看部件怎样装配 |
| [supervisor.py](../src/ppt_eval/application/supervisor.py) | 状态机、报告和异常兜底 |
| [profile.py](../src/ppt_eval/application/profile.py) | Profile 到强制 Baseline DAG 的编译 |
| [scheduler.py](../src/ppt_eval/application/scheduler.py) | 拓扑执行、Registry、预算和重试 |
| [oracle.py](../src/ppt_eval/application/oracle.py) | Oracle 端口、Descriptor、Context 和 Registry |
| [oracles/base.py](../src/ppt_eval/oracles/base.py) | Atomic/Composite 模板、解析缓存和证据函数 |
| [baseline.py](../src/ppt_eval/oracles/baseline.py) | 13 个本体 Leaf |
| [scenarios.py](../src/ppt_eval/oracles/scenarios.py) | 17 个场景 Leaf 和 3 个场景 Composite |
| [pdms.py](../src/ppt_eval/scoring/pdms.py) | 外乘内加、Coverage 和重复指标保护 |
| [policy.py](../src/ppt_eval/scoring/policy.py) | PASS/REVIEW/FAIL 决策顺序 |
| [flywheel.py](../src/ppt_eval/flywheel.py) | 反馈、主动采样和参数候选治理 |
| [tests](../tests) | 当前行为的可执行规范 |

## 5. 环境安装与第一次运行

### 5.1 标准环境

项目要求 Python `>=3.11`。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,api,worker,storage]"
python examples/generate_demo.py
```

仓库移动后必须重跑 `examples/generate_demo.py`，因为 Demo Case 会记录当前机器上的 PPTX 绝对路径。

UI 使用 Node.js 和 pnpm：

```powershell
Set-Location ui
pnpm install --frozen-lockfile
pnpm build
Set-Location ..
```

### 5.2 当前工作站的 bundled Python

如果不想修改本机环境，可以使用当前已经验证过的 bundled Python：

```powershell
$Py = 'C:\Users\DiegoWang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = 'src'
& $Py scripts/run_tests.py
& $Py -m ppt_eval.cli --data-dir var/onboarding run examples/demo/case_ready_made.json
```

`--data-dir` 是全局参数，必须写在子命令前面。

### 5.3 四场景第一次运行

```powershell
ppt-eval --data-dir var/onboarding run examples/demo/case_ready_made.json
ppt-eval --data-dir var/onboarding run examples/demo/case_text_to_ppt.json
ppt-eval --data-dir var/onboarding run examples/demo/case_project_summary.json
ppt-eval --data-dir var/onboarding run examples/demo/case_multimodal.json
```

直接传 PPTX 时，CLI 会自动构造 `ready_made` Case：

```powershell
ppt-eval --data-dir var/onboarding run examples/demo/aurora_demo.pptx
```

运行后重点观察：

- `coverage`：证据是否完整。
- `decision`：业务处置结论。
- `base_score`：PPT 本体分。
- `full_score`：完整场景分，降级时必须为 `null`。
- `review_reasons`：为什么需要人审。
- `results`：每个原子 Oracle 的值和证据。
- `manifest`：输入、代码、Profile 和结果指纹。

## 6. EvalCase：怎样描述一次送检

最小 Case：

```json
{
  "case_id": "case-ready-001",
  "scene": "ready_made",
  "pptx_path": "examples/demo/aurora_demo.pptx"
}
```

完整 Case 可包含：

| 字段 | 用途 |
|---|---|
| `case_id` | 调用方稳定业务 ID |
| `scene` | `text_to_ppt/project_summary/multimodal/ready_made` |
| `pptx_path` | 服务器本地 PPTX 路径，当前始终必需 |
| `request` | 生成指令或总结要求 |
| `audience` | 目标受众 |
| `source_materials` | 项目总结来源文件或文本 |
| `assets` | 多模态候选素材清单 |
| `metadata` | 关键事实、关键素材、图表真值和额外版本指纹 |

常用 `metadata` 键：

| 键 | 使用者 |
|---|---|
| `critical_consistency_keys` | 本体内部数据一致性硬门 |
| `fact_verification` / `fact_verification_path` | 文字场景可信事实 Oracle |
| `verified_facts` | 文字场景离线事实兜底 |
| `critical_facts` | 项目总结关键来源硬门 |
| `key_points` | 项目要点召回 |
| `required_assets` | 多模态必选素材硬门 |
| `critical_chart_values` | 关键图表数据硬门 |
| `chart_expectations` | 图表性能分 |
| `artifact_hashes` | 调用方预先登记的制品哈希 |

当前 `source_materials` 读取器只适合字符串和 UTF-8 文本文件，最多合计读取 2 MB。它不会正确解析
PDF、DOCX 或扫描件。正式接入这些格式前必须新增对应 Adapter，不能把二进制乱码当作来源文本。

## 7. EvalProfile：怎样描述一个评分标准版本

Profile 控制：

- 基础和场景加法权重。
- 基础和场景硬乘子指标。
- 哪些指标是 required。
- 启用哪些 Composite Oracle。
- `lambda_base`、硬门置信度阈值。
- PASS/REVIEW 阈值。
- 最大重试次数、成本预算和超时字段。

四个默认文件位于 `configs/profiles/`。Profile 是版本化检测标准，不是 Case 的一部分。

注意以下实现细节：

1. JSON 中的 `required_oracles` 实际会被解析成 `enabled_oracle_ids`。
2. `optional_oracles` 当前只写入 metadata，不会自动加入 DAG。
3. `required_metric_ids` 未显式提供时，所有配置中的加项和乘子默认都 required；成品场景仅特殊排除
   `multimedia_quality`。
4. 空的 `enabled_oracle_ids` 会被配置加载器回退成默认值。事故降级时可显式提供非空的
   `required_oracles: ["baseline_ppt_quality"]`，让编译器只生成基础节点。
5. 不同 Profile 的绝对总分不可直接横向比较。

## 8. 一次 Run 的完整调用链

```text
CLI/API
  -> case_from_mapping / load_case
  -> LocalEvaluationRuntime.evaluate
  -> EvaluationService
  -> RunSupervisor.OBSERVE
  -> ProfileCompiler.compile
  -> DagScheduler.execute
  -> Composite Oracle
  -> Atomic Oracle
  -> PptPdmsAggregator
  -> DecisionPolicy
  -> EvalReport + RunManifest
  -> JsonRunRepository + JsonlAuditLog
```

### 8.1 OBSERVE

Supervisor 创建 `run_id` 和 `report_id`，计算输入哈希和 Profile 指纹，并记录第一条状态事件。
输入哈希同时包含 Case 内容和 PPTX 文件字节。

### 8.2 PLAN

`ProfileCompiler` 首先创建唯一且 mandatory 的 `baseline_ppt_quality`。除成品场景外，再根据
`enabled_oracle_ids` 创建场景 Composite 节点。所有场景节点依赖基础节点。

```text
baseline_ppt_quality
        |
        +--> scene:scenario.instruction_alignment
        +--> scene:scenario.source_faithfulness
        +--> scene:scenario.asset_compliance
```

一份具体 DAG 只会包含与当前场景对应的一个场景节点。

### 8.3 ACT

Scheduler 做确定性拓扑排序，从 Registry 根据 ID 取 Oracle，依次调用：

```text
describe()   声明版本和会产生哪些 metric
supports()   当前 Case 是否适用
evaluate()   返回一个或多个 OracleResult
```

默认 DAG 节点粒度是 Composite，不是 30 个 Leaf。Composite 内部按固定顺序执行 Atomic Oracle。
第一次 Leaf 解析 PPTX 后，结果缓存进 `EvaluationContext.memo`，其他 Leaf 不会重复解压同一文件。

### 8.4 VERIFY

Aggregator 只做纯算术，不调用模型。DecisionPolicy 只做业务决策，不修改原始分数。

### 8.5 FINALIZE / REVIEW

PASS 和质量 FAIL 进入 `FINALIZE`；REVIEW 和 Harness ERROR 进入 `REVIEW` 状态。最终报告与 Manifest
写入本地仓库，状态事件追加到运行级哈希链。

## 9. Oracle 框架和 30 个原子指标

默认 Registry 顶层注册四个 Composite：

| Composite ID | 原子数 | 适用场景 |
|---|---:|---|
| `baseline_ppt_quality` | 13 | 所有场景，编译期强制注入 |
| `scenario.instruction_alignment` | 4 | 文字生成 |
| `scenario.source_faithfulness` | 6 | 项目总结 |
| `scenario.asset_compliance` | 7 | 多模态 |

三个 ID 不要混淆：

- `oracle_id`：实现/执行单元 ID，例如 `layout_oracle`。
- `metric_id`：评分合同中的稳定指标 ID，例如 `layout`。
- Composite ID：Profile 启用的节点 ID，例如 `scenario.asset_compliance`。

### 9.1 本体 13 项

| metric_id | 角色/权重 | v1 实现原理 |
|---|---:|---|
| `file_deliverability` | 基础乘子 | 安全预检或解析失败为0，成功为1 |
| `critical_content_visibility` | 基础乘子 | 全空为0，非空页率低于50%为.5，否则1 |
| `internal_data_consistency` | 基础乘子 | 仅检查声明的关键“标签:数值”；冲突为.5，否则1 |
| `content_clarity` | 基础加法 .14 | 空白页、超700字符页、超18文本框页按比例扣分 |
| `narrative` | 基础加法 .12 | 标题覆盖、首页标题和重复标题率 |
| `visual_hierarchy` | 基础加法 .12 | 标题最大字号相对全页文字中位字号 |
| `layout` | 基础加法 .12 | 越界对象和重叠超过较小对象35%的对象对 |
| `typography` | 基础加法 .10 | 显式小于14pt的文本对象比例 |
| `style_consistency` | 基础加法 .10 | 超过3种字体与跨页标题纵坐标离散度 |
| `multimedia_quality` | 基础加法 .08 | 损坏媒体率和过小媒体率；无媒体当前返回1 |
| `editability` | 基础加法 .08 | 可编辑对象比例与整页栅格化页比例 |
| `compatibility` | 基础加法 .08 | 宏、外链、未知/OLE 对象风险扣分 |
| `accessibility` | 基础加法 .06 | 标题覆盖55% + 有效 alt text 覆盖45% |

### 9.2 文字生成 4 项

| metric_id | 角色/权重 | v1 实现原理 |
|---|---:|---|
| `critical_instruction_compliance` | 场景乘子 | 提取“必须/禁止”等硬要求，按词法覆盖的违反率映射到1/.5/0 |
| `instruction_coverage` | 场景加法 .45 | 请求拆成原子要求，逐条 token recall 后取平均 |
| `audience_fit` | 场景加法 .25 | 受众名称召回和领导/技术/客户固定词汇线索 |
| `fact_quality` | 场景加法 .30 | 可信事实 bundle 中支持=1、证据不足=.25、矛盾=0；无可信事实 N/A |

### 9.3 项目总结 6 项

| metric_id | 角色/权重 | v1 实现原理 |
|---|---:|---|
| `critical_source_consistency` | 场景乘子 | 关键事实召回缺失率映射1/.5/0；否则同标签数字冲突为.5 |
| `source_faithfulness` | 场景加法 .30 | PPT 唯一 token 中能在来源 token 集找到的比例 |
| `key_point_recall` | 场景加法 .25 | 配置要点或来源前20个合格句子的平均召回 |
| `numeric_accuracy` | 场景加法 .20 | PPT 数字字符串能在来源数字集合找到的比例 |
| `compression_quality` | 场景加法 .15 | PPT/来源归一化字符比例的分段函数 |
| `traceability` | 场景加法 .10 | 来源文件名或路径是否在页面文本/备注中出现 |

### 9.4 多模态 7 项

| metric_id | 角色/权重 | v1 实现原理 |
|---|---:|---|
| `required_asset_compliance` | 场景乘子 | SHA-256 或 OOXML part 文件名匹配必选素材，缺失率映射1/.5/0 |
| `critical_chart_data_accuracy` | 场景乘子 | 关键图表值对整份 PPT 文本/图表缓存值做召回 |
| `asset_compliance` | 场景加法 .30 | 候选素材成功匹配比例 |
| `asset_presentation` | 场景加法 .25 | 匹配素材占页面面积从1%到9%线性映射到0至1 |
| `crop_clarity` | 场景加法 .15 | crop 总量、过小面积和极端长宽比扣分 |
| `chart_data_accuracy` | 场景加法 .20 | 有可编辑 chart 后，对预期值做平均词法召回 |
| `media_availability` | 场景加法 .10 | 有可读取内嵌 payload 的媒体对象比例 |

### 9.5 OracleResult 的严格语义

| 情况 | execution_status | metric_status | 数值语义 |
|---|---|---|---|
| 加法正常评分 | `SUCCESS` | `SCORED` | `normalized_score` 在0至1 |
| 硬门通过/失败 | `SUCCESS` | `PASS/FAIL` | `multiplier` 为1/.5/0 |
| 不适用或证据不足 | `SUCCESS/SKIPPED` | `NA` | 从算术移除；required 时降级 |
| Oracle 自身异常 | `ERROR` | `ERROR` | 绝不当成质量0分 |
| 文件打不开 | `SUCCESS` | `FAIL` | `file_deliverability=0`，是明确质量结论 |

每条 Evidence 应回答四个问题：在哪里、发现了什么、依据是什么、由哪个版本产生。证据 ID 由
metric/key/page/object/source 确定性哈希生成，因此 Evidence key 不能使用随机 UUID。

## 10. PPT-PDMS 评分与决策

适用加法项先重新归一化权重：

```text
A_base  = sum(w_i * s_i) / sum(w_i)
A_scene = sum(w_j * s_j) / sum(w_j)
```

最终公式：

```text
S_base = 100 * product(M_base) * A_base

S_full = 100 * product(M_base) * product(M_scene)
         * [lambda * A_base + (1-lambda) * A_scene]
```

默认 `lambda`：文字生成 `.55`、项目总结 `.40`、多模态 `.45`、成品 PPT `1.0`。

原子状态到算术的转换：

- `PASS -> 1`
- `FAIL -> 0`
- `SCORED -> normalized_score`
- 非 required `NA` 从分子和分母一起移除
- required `NA`、缺结果或 `ERROR` 不填零，但标记 unresolved
- 负向硬乘子置信度低于 `.90` 时恢复为1，并转人审
- 同一 `metric_id` 返回两个计分结果时直接报错

决策顺序：

1. 高置信公共零乘子直接 FAIL。
2. Coverage 不完整、存在 unresolved 或低置信硬门时先 REVIEW。
3. Coverage 完整后，高置信场景零乘子直接 FAIL。
4. 否则 `>=80 PASS`、`60-80 REVIEW`、`<60 FAIL`。

不要只读取 `overall_score`。当前降级时它会回退显示 `base_score`，调用方必须同时检查 `coverage`、
`full_score` 和 `decision`，否则会把本体分误称为完整场景分。

## 11. Coverage 和降级

| Coverage | 含义 | 可发布分数 |
|---|---|---|
| `FULL` | 本体和 required 场景证据完整 | `base_score` 与 `full_score` |
| `DEGRADED` | 有部分场景信号，但 required 指标不完整 | 只展示 `base_score`，进入 REVIEW |
| `BASE_ONLY` | 场景子图没有任何可用信号 | 只展示 `base_score`，进入 REVIEW |
| `UNASSESSABLE` | 无法产生基础质量结论或 Harness 自身失败 | 技术证据和 ERROR/REVIEW |

多模态没有素材时经常是 `DEGRADED` 而不是 `BASE_ONLY`：未声明关键必选素材和关键图表值时，两个
场景硬门仍会返回中性乘子1，因而存在部分场景信号；其他 required 加项 N/A 使场景不完整。

## 12. 异常、重试和安全处理

| 异常 | 当前处理 |
|---|---|
| PPTX 损坏或安全预检拒绝 | 文件 Oracle 成功返回高置信乘子0；其余 Leaf 返回 ERROR；最终硬门 FAIL |
| Leaf 抛异常 | Atomic 模板包装为 `OracleResult.error` |
| Composite 出现任一 ERROR | Scheduler 最多重试 `max_retries+1` 次，当前会重跑整个 Composite |
| Oracle 未注册 | 返回 `ORACLE_NOT_REGISTERED` ERROR，不吞掉已完成结果 |
| 非 mandatory 节点预算耗尽 | 返回 `COST_BUDGET_EXHAUSTED` ERROR |
| `supports()` 为 false | 返回 `SKIPPED/NA` |
| Case 与 Profile 场景不匹配 | Supervisor 捕获为 `UNASSESSABLE/ERROR` 并审计 |
| DAG 环或聚合重复指标 | Supervisor 捕获为 Harness ERROR |

`oracle_timeout_seconds` 当前只存在于 Profile 并校验正数，Scheduler 尚未强制执行。Renderer 有自己的
120 秒超时，Celery task 有 soft 540 秒/hard 600 秒限制，但它们不是 Leaf 级超时。

对于不可信 PPT，顺序必须是：隔离上传 -> ZIP/OOXML preflight -> 解析 -> 必要时再调用 Office 渲染。
不要先让 PowerPoint 打开未知文件。

## 13. CLI 使用手册

```powershell
# 单份场景评测
ppt-eval --data-dir var/dev run examples/demo/case_project_summary.json `
  --output var/dev/project-summary-output.json

# 混合四场景批处理，不要传统一 Profile
ppt-eval --data-dir var/batch batch `
  examples/demo/case_ready_made.json `
  examples/demo/case_text_to_ppt.json `
  examples/demo/case_project_summary.json `
  examples/demo/case_multimodal.json `
  --output var/batch-results.json

# 人工复核追加新事件，不覆盖机器报告
ppt-eval --data-dir var/dev review RUN_ID APPROVE `
  --reviewer reviewer-name --note '证据已人工复核'

# 反馈
ppt-eval --data-dir var/dev feedback RUN_ID CASE_ID `
  --accepted yes --modification-seconds 45 --label minor-edit

# 运行级审计和导出
ppt-eval --data-dir var/dev audit verify
ppt-eval --data-dir var/dev audit export RUN_ID --output var/dev/exports

# 渲染是独立工具，当前不进入默认评分
ppt-eval render deck.pptx --renderer powerpoint --output var/rendered/run-001
ppt-eval render deck.pptx --renderer libreoffice --output var/rendered/run-001-lo
```

CLI 的质量 FAIL 和 REVIEW 当前仍返回进程退出码0；只有 Harness `ERROR` 返回2。CI 门禁必须解析 JSON
中的 `decision` 和 `coverage`，不能只看进程退出码。

## 14. FastAPI 与 UI

启动 API：

```powershell
ppt-eval --data-dir var/api serve --host 127.0.0.1 --port 8000
```

同步提交：

```powershell
$case = Get-Content examples/demo/case_ready_made.json -Raw | ConvertFrom-Json
$body = @{ case = $case } | ConvertTo-Json -Depth 20
$report = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/v1/evaluations?async=false' `
  -ContentType 'application/json' -Body $body
```

默认 `async=true`，先得到 `job_id`，轮询 `/v1/jobs/{job_id}`，完成后再 GET
`/v1/evaluations/{run_id}`。Job 只存在内存，服务重启后 Job 查询会404，但已经保存的 Run 仍在磁盘。

启动 UI：

```powershell
Set-Location ui
$env:VITE_API_BASE = 'http://127.0.0.1:8000'
pnpm dev -- --port 5173
```

访问 `http://127.0.0.1:5173/`，API 文档位于 `http://127.0.0.1:8000/docs`。

当前 API 只有本地路径输入，没有上传、鉴权、租户隔离、限流、配额和幂等键。它只应绑定 loopback，
不能直接暴露公网。

## 15. Docker Compose 的真实边界

```powershell
docker compose config --quiet
docker compose up --build
docker compose down
```

当前工作站若只有 standalone Compose，则使用 `docker-compose`。

Compose 会启动 API、Celery Worker、Redis、PostgreSQL 和 MinIO，但 API 仍使用本地 JSON 仓库和进程内
JobManager。PostgreSQL、S3 和 Redis 尚未接入默认业务路径，UI 也不在 Compose 内。固定密码和
`change-me` 只能用于本地，不得用于共享环境。

生产接线至少还需：

1. 新的 production composition root。
2. 文件上传和内容寻址对象存储入口。
3. API 到 Celery 的真实投递和持久化 Job backend。
4. PostgreSQL migration、review repository 和 Transactional Outbox。
5. MinIO bucket 初始化、Secrets、鉴权和故障演练。

## 16. 运行数据、审计与回放

每个 `--data-dir ROOT` 是一套独立运行域：

```text
ROOT/runs/run-*.json          机器报告和 RunManifest
ROOT/runs/reviews.jsonl       人工复核，只追加
ROOT/audit/events.jsonl       运行级 SHA-256 哈希链，只追加
ROOT/feedback/records.jsonl   下游反馈和 edit diff，只追加
ROOT/proposals/events.jsonl   参数提案状态，只追加
ROOT/artifacts/               本地内容寻址对象目录
```

不要手工修改 JSONL。历史哈希链被修改后，继续追加新记录不能修复过去的断链，只能从可信备份恢复并
记录事故。

`LocalArtifactStore` 虽然已经实例化，但当前 `evaluate()` 不会自动保存 PPT、source 和 assets。
备份必须同时保存原始输入、Case、Profile、Run、Audit、Review 和 Feedback，不能只备 `artifacts/`。

本项目存在两类审计：

| 类型 | 命令 | 用途 |
|---|---|---|
| 运行级哈希链 | `ppt-eval --data-dir ROOT audit verify` | 验证具体运行域是否被篡改 |
| 项目级三阶段审计 | `scripts/reporting/verify_audit.py` | 验证调研、开发、评测证据和九页汇报结构 |

项目级生成：

```powershell
python scripts/reporting/verify_audit.py `
  audit/example/project_audit.json audit/example/events.jsonl
python -m ppt_eval.cli project-report
```

生成物是派生视图，不要手工编辑后冒充正式源数据。修订应先更新 Markdown/审计事件，再重新生成。

## 17. 新增一个 Atomic Oracle

新增指标前先写 Oracle Card，回答：业务问题、输入证据、是否可补偿、公式方向、N/A 条件、FAIL 与
ERROR 边界、置信度来源、已知盲区、版本和 owner。

示例：

```python
class ActionabilityOracle(AtomicOracle):
    oracle_id = "actionability_oracle"
    metric_id = "actionability"
    score_role = ScoreRole.SCENE_ADDITIVE

    def supports(self, context: object) -> bool:
        case = getattr(context, "case", context)
        return super().supports(context) and case.scene == SceneType.TEXT_TO_PPT

    def _evaluate(self, context: object) -> OracleResult:
        case = getattr(context, "case", context)
        if not case.request:
            return self.not_applicable("No request was supplied.")
        presentation = self.presentation(context)
        score = ...  # 越好必须越接近 1
        findings = (
            evidence(
                self.metric_id,
                "stable-key",
                "action_gap",
                "Action item is missing an owner.",
                page_number=1,
                object_id="12",
                bbox=(0.1, 0.2, 0.3, 0.1),
                payload={"observed": "missing_owner"},
            ),
        )
        return self.scored(score, findings, raw_value=score, confidence=0.8)
```

标准步骤：

1. 在 `baseline.py`、`scenarios.py` 或新的领域模块中实现 Leaf。
2. 把 Leaf 加进对应 Composite 的固定 children tuple。
3. 在 `oracles/__init__.py` 导出。默认 Registry 注册 Composite，Leaf 不必单独注册。
4. 在新 Profile 版本中增加加法权重或乘子 ID，不能两处同时出现。
5. 明确 `required_metric_ids`；真正 optional 的指标必须从 required 列表排除。
6. 算法、阈值或证据语义变化时提升 Oracle version 和 Profile version。
7. 增加正常、N/A、质量失败、技术异常、证据定位、聚合和降级测试。
8. 更新 Oracle Card、Changelog、实验记录和需求追踪矩阵。

如果新增一个 Leaf 需要让 Supervisor 识别该指标名字，说明分层被破坏了。正常情况下只改
Adapter/Leaf/Composite/Profile 和测试。

## 18. 新增一个场景

新增场景比新增 Leaf 影响面更大，至少需要修改：

1. `domain/enums.py` 的 `SceneType`。
2. `domain/models.py` 的默认权重、乘子和 `lambda` 映射。
3. 新的场景 Composite 和 Leaf。
4. `oracles/__init__.py` 的默认 Registry 与场景目录。
5. `config.py` 的别名和 Profile 路径映射。
6. 新的 `configs/profiles/*.json`。
7. OpenAPI scene enum、UI 类型和显示文本。
8. 四场景固定审计 Schema、汇报结构和所有架构测试。

当前系统和审计合同明确围绕四场景设计。没有新的业务证据和验收协议时，不要为了“扩展性”随意增加场景。

## 19. 测试策略

每个新 Leaf 最低需要：

- 正常样本：检查 metric、role、分数和 evidence。
- 缺证据：必须返回 N/A，而不是0。
- 明确坏质量：SUCCESS + SCORED/FAIL，而不是 ERROR。
- 技术异常：ERROR 且没有 normalized score。
- 证据定位：页码、对象、bbox、source URI 和 payload 可复核。
- 聚合：N/A 中性、required N/A 降级、低置信硬门不生效。
- 局部性：一个缺陷主要影响预期维度。
- 单调性：缺陷加重后对应分数和总分不能提高。
- Harness：专项失败时 base 保留，`full_score=null`，进入 REVIEW。

现有测试职责：

| 文件 | 覆盖内容 |
|---|---|
| `test_pptx_adapter.py` | OOXML、图表、隐藏文本和 ZIP 安全 |
| `test_oracles.py` | 本体/场景指标与对象级证据 |
| `test_scoring.py` | PDMS、N/A、ERROR、乘子、单调和无双罚 |
| `test_harness.py` | Baseline 不变量、状态机、重试和降级 |
| `test_runtime.py` | 本地持久化、复核和审计导出 |
| `test_fact_verification.py` | 可信事实快照和 HTTPS 约束 |
| `test_flywheel.py` | 主动采样和双人参数审批 |

普通改动的最低门禁：

```powershell
python -m pytest -q
python scripts/run_tests.py
python -m ruff check src tests scripts examples `
  third_party/ppteval/run_ppteval.py `
  third_party/ppteval/audit_zenodo_licenses.py `
  third_party/slidesbench/audit_dataset.py

Push-Location ui
pnpm build
Pop-Location

python scripts/reporting/verify_audit.py `
  audit/example/project_audit.json audit/example/events.jsonl
docker compose config --quiet
```

当前已验证为 42 tests。尚缺 API 契约测试、UI E2E、Celery/Postgres/S3 集成、并发压测、完整变形矩阵、
双渲染差分、400 例人工元评测和真实 Shadow。

## 20. 参数化、校准与自进化

当前已经 Profile 化的参数：权重、`lambda`、required metrics、硬门置信下限、PASS/REVIEW 阈值、
重试、成本预算和 timeout 字段。

当前仍硬编码在 Leaf 中的阈值：700 字符、18 文本框、14pt、35% overlap、字体族数量、素材面积、
crop/长宽比和多种 token recall 门槛。

推荐演进方式：

1. 给 `EvalProfile` 增加带 Schema 的 `metric_params`，按 `metric_id` 保存 Leaf 参数。
2. OracleResult metadata 回写本次实际使用的参数。
3. 新增纯函数 Calibrator，保留 raw value，再按 Oracle/Calibration version 产生 calibrated score。
4. 使用人工金标验证方向性、单调性、局部性、相关性、定位 F1 和 ECE/Brier。
5. 参数候选只生成新 Profile，不原地覆盖旧 Profile。

治理流程：

```text
生产反馈
-> 主动采样工作池
-> 人工金标
-> 校准集拟合
-> 冻结集回放
-> OOD/安全挑战集
-> Shadow
-> 两人审批
-> RELEASE_CANDIDATE
-> 由独立发布系统灰度
```

当前 `ParameterProposalService` 要求 frozen/challenge/shadow 三项验证均通过，并需要两个不同审批者；
但它不校验证据 Run 是否真实存在、不校验参数白名单，也没有身份签名和生产应用接口。因此发布委员会
必须人工核对 Profile diff、证据 Run 和审批身份。

## 21. 数据飞轮和数据许可

反馈至少记录：接受/拒绝、放弃、人工标签、修改时长、编辑操作、Oracle 分歧、不确定性、严重度、
业务价值和多样性 key。

主动采样优先级当前为：

```text
.28 * uncertainty
+.24 * oracle_disagreement
+.22 * severity
+.16 * business_value
+.10 * diversity_bonus
+ 少量确定性随机探索
```

数据不能因为“公开可下载”就进入工业训练池。每份输入最低登记：

```text
dataset/version/source_uri/license/license_evidence/allowed_use
pii_level/consent/retention/owner/sha256/parent_id
```

建议分区：

- `research_quarantine`：许可或来源仍需确认。
- `license_allowlist`：逐文件权利清楚，可按授权用途使用。
- `frozen_eval`：只读评测集，禁止训练污染。

派生缺陷必须跟随父 deck 分组切分，不能让同一 PPT 的页面、标准化副本或缺陷变体跨集合泄漏。

## 22. 日常维护清单

### 每日

- 检查 `/healthz`，并对所有活跃 data-dir 执行 `audit verify`。
- 统计 PASS/REVIEW/FAIL/ERROR 和 FULL/DEGRADED/BASE_ONLY。
- 查看 Oracle ERROR、N/A 激增、磁盘空间和 JSONL 增长。
- 抽查至少一份 PASS、REVIEW、FAIL 的对象级证据和原 PPT。
- 处理高严重度反馈，并核对 `run_id` 与 `case_id`。

### 每周

- 运行 42 tests、Ruff、UI build、项目审计和 Compose config。
- 用固定 smoke deck 做一次 PowerPoint 原生渲染并写入全新目录。
- 汇总人工推翻率、修改时长、Oracle 分歧和语言/模板/生成模型切片。
- 审核新增数据的许可、PII、用途、来源 URI 和 SHA-256。
- 验证备份可读，检查默认密钥和 API 网络暴露范围。

### 每次候选发布

- Champion/Challenger 在校准、冻结、OOD、安全和四场景降级集上全量回放。
- 检查人评一致性、pairwise、ECE/Brier、严重误放行和门禁重复性。
- Shadow 至少两周且至少1,000份，以较晚满足者为准。
- 依次执行5%、25%、100%灰度，并保留旧 Profile/Oracle/container 组合。
- 做备份恢复、Profile 切回和容器回滚演练。

## 23. 常见排障

| 现象 | 优先检查 | 解释 |
|---|---|---|
| `ModuleNotFoundError: ppt_eval` | Python版本、editable install、`PYTHONPATH=src` | 必须使用 Python 3.11+ |
| Demo 找不到 PPT | Case 中的绝对路径 | 仓库移动后重跑 `generate_demo.py` |
| 高 base 但 `DEGRADED/REVIEW` | `review_reasons` 和 required N/A/ERROR | 通常是缺事实、source、asset 或 chart 真值 |
| `full_score=null` | `coverage` 与专项结果 | 这是禁止伪造综合分的设计 |
| `UNASSESSABLE/ERROR` | `errors`、场景/Profile、DAG | 属于 Harness 或契约失败 |
| API Job 404 | API 是否重启 | 内存 JobManager 丢失；完成的 Run 可能仍在磁盘 |
| UI 请求失败 | `/healthz`、5173 CORS、`VITE_API_BASE` | CORS 当前只允许 localhost/127.0.0.1:5173 |
| 审计断链 | 是否手改 JSONL 或磁盘损坏 | 从可信备份恢复，不能靠追加修复 |
| PowerPoint RPC/超时 | Office路径、桌面会话、文件锁、输出目录 | COM 依赖 Windows Office 环境 |
| LibreOffice 没有 PNG | 输出目录中的 PDF | 当前 Adapter 只导出 PDF |
| Profile timeout 不生效 | Scheduler | Leaf timeout 尚未真正实现 |
| Compose 正常但 DB 无数据 | runtime composition | API 仍使用 JSON repository |
| 多个正常覆盖被判 overlap | `layout` evidence | 当前是轴对齐 bbox 启发式，存在合理叠放误报 |
| 来源忠实性异常低 | source 文件格式 | 当前只正确读取 UTF-8 文本，不解析 PDF/DOCX |
| 素材明明使用却未匹配 | 文件是否重编码、basename 是否变化 | 当前只做精确 SHA 或 part name 匹配 |

## 24. 版本与变更规则

| 变更 | 必须动作 |
|---|---|
| Oracle 公式、阈值、证据语义 | bump Oracle version，补局部性、单调性和金标回归 |
| 权重、lambda、required、决策阈值 | 新 Profile ID/version，走 proposal、冻结、Shadow |
| API 删除字段或改义 | 升 API major；新增可选字段可保留 v1 |
| Audit 字段改义 | 新 Schema major，并保留旧 reader |
| 数据集增删或重切分 | 新 dataset manifest/version/hash |
| 模型、Prompt、字体、Renderer | 固定 snapshot/hash，写入 Manifest 并做回放 |
| 第三方 baseline 更新 | 保留 pinned commit，另建 reproduction，不追 moving main |

当前仓库尚未建立首个正式 Git commit/tag，Manifest 会记录 `git_sha=uncommitted`。在建立经过负责人审核的
首个 commit、容器 digest 和 Release ID 前，不应宣称已经具备可靠代码回滚能力。

## 25. 当前技术债和建议优先级

1. 建立首个 Git commit/tag、Release manifest 和可执行回滚点。
2. 把 Leaf 阈值外置为 typed `metric_params`，修复 `compression_quality` 分段跳变。
3. 实现真正的 Calibrator，并冻结 calibration version。
4. 为每个 Leaf 增加强制 timeout、按错误类型 retry、Circuit Breaker 和独立成本计量。
5. 接入 PDF/DOCX/OCR、像素渲染、VLM 和逐 chart/series/category 结构核验。
6. 把 API 异步路径接到 Celery，把 repository/artifact port 接到 PostgreSQL/S3，并实现 Outbox。
7. 增加上传、鉴权、租户隔离、幂等、配额、限流和证据权限。
8. 为 feedback/proposal 增加查询、身份签名、参数白名单和 evidence Run 外键校验。
9. 统一 `application/oracle.py` 与 `oracles/base.py` 中迁移期重复的 Composite 实现。
10. 完成 API/UI E2E、故障注入、完整变形矩阵、400例金标、2,000缺陷变体和真实 Shadow。
11. 为30个 Leaf 分别完成 Oracle Card：owner、适用域、金标、ECE/Brier、限制和撤回条件。

已知需要优先修正的具体规则问题：

- `compression_quality` 在 `.03` 和 `.45` 分段边界存在分数跳变。
- 关键事实/图表硬门使用 `.65/.70`，通用 evidence 标签仍按 `.45` 写 match，可能出现证据文字与门禁不一致。
- `numeric_accuracy` 不理解单位换算、年份/页码和标签绑定。
- `chart_data_accuracy` 搜索整份 PPT 文本，没有严格绑定到具体 chart/series。
- `accessibility` 尚未覆盖颜色对比、阅读顺序、表头、字幕和屏幕阅读器验证。
- `compatibility` 是结构风险分，不是 PowerPoint 与 LibreOffice 双渲染差分。

## 26. 新人第一周练习

### 第一天：能运行和解释

1. 重建 Demo。
2. 跑四场景。
3. 找到 `runs/run-*.json` 和 `audit/events.jsonl`。
4. 解释 `coverage`、`decision`、`base_score`、`full_score` 的区别。

### 第二天：能沿代码追踪

1. 从 `runtime.py` 追到 Supervisor、Compiler、Scheduler。
2. 找出一次 ready-made Run 的 13 个 Leaf 结果。
3. 用一个 Evidence 的 page/object/bbox 回到 PPT 对象。

### 第三天：能制造降级

1. 删除文字场景的可信事实，观察 `fact_quality=N/A`。
2. 删除项目来源，观察 BASE_ONLY/DEGRADED。
3. 运行损坏 PPT 测试，理解质量 FAIL 与执行 ERROR。

### 第四天：能安全扩展

1. 写一个 `DIAGNOSTIC` 练习 Oracle，不参与评分。
2. 再写一个加法 Oracle，加入测试 Profile。
3. 验证 N/A 中性、ERROR 隔离和专项失败保留 base。

### 第五天：能维护和交付

1. 跑完整门禁。
2. 记录一条人工复核和反馈。
3. 创建、验证并双人批准一个测试参数提案。
4. 验证运行级和项目级两条审计链。

## 27. 最终交接检查表

### 代码和环境

- [ ] Python 3.11+ 环境和依赖可重建。
- [ ] Node/pnpm 与 UI lockfile 可重建。
- [ ] 已建立可信 Git commit/tag 和容器 digest。
- [ ] 四场景 Demo、42 tests、Ruff、UI build 均通过。

### 运行和审计

- [ ] 能根据 Run ID 找到 Report、Manifest、Audit、Review 和 Feedback。
- [ ] 所有活跃 data-dir 的 hash chain 均通过验证。
- [ ] 输入 PPT、source、assets 与 Profile 有备份和 hash。
- [ ] 没有人手工覆盖机器结果或历史 JSONL。

### Oracle 和评分

- [ ] 能解释30个 Leaf 的输入、N/A 条件、公式和限制。
- [ ] 明确哪些指标是乘子、哪些是加法以及是否 required。
- [ ] 修改指标时同步更新版本、Card、Profile、测试和实验记录。
- [ ] 消费方不会把降级 `overall_score` 当作 `full_score`。

### 数据和发布

- [ ] 数据许可、PII、用途、保留期和父子血缘可追踪。
- [ ] 冻结集不会进入训练或候选生成。
- [ ] 参数候选经过冻结、挑战、Shadow 和双人审批。
- [ ] 生产流量和回滚由独立发布系统控制，不由 Oracle 或模型决定。

## 28. 一句话维护原则

**让 Harness 永远只认识节点和契约，让 Oracle 只负责一个判断，让分数只由版本化 Profile 和纯函数
聚合器决定，让每一次修改都能由证据、测试、审计和旧版本回放。**
