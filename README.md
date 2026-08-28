# PPT Eval Harness

面向生成式与存量 PowerPoint 的证据优先评测系统。项目把 PPTX 解析、确定性规则、局部
LLM/VLM 审计、原子观察、Reducer、PPT-PDMS 评分、训练准入和人工复核组织成一条可回放、
可解释、可追加审计的运行链。

当前发布版是经过完整测试的**单机审计系统**：CLI、FastAPI、React 审计台、LibreOffice/
PowerPoint 渲染、本地内容寻址存储与哈希链均可直接使用。默认 v8.3 Profile 仍标记为
`PRE_RESEARCH`，表示权重和阈值尚未通过大规模人类金标完成生产校准，不应把当前分数解释为
行业标准。

### 版本分层

| 层级 | 当前值 | 何时变更 |
|---|---|---|
| 产品/软件发布 | `0.8.6` | 服务、CLI、UI 或运维能力发布 |
| Evaluation Profile | `8.3` / `PRE_RESEARCH` | 评分公式、权重、DAG 或准入语义改变 |
| EvalReport / Audit schema | `1.0` | 持久化 JSON 出现不兼容变更 |
| HTTP API namespace | `/v1` | 接口合同出现破坏性变更 |

产品版本与评测 Profile 是两个独立版本轴。产品升到 `0.8.6` 不改变 Profile、
Oracle/Prompt 版本、schema 或 `/v1`，历史 `8.3`/`1.0` run 仍可读取。本仓库在此前使用过
`0.1.0` 打包占位值；`0.8.4` 开始统一产品版本出口，`0.8.5` 新增正式批处理 API，
`0.8.6` 收敛跨 Composite 审计去重与冷构建锁定。

## 1. 能做什么

- 评测四类任务：`text_to_ppt`、`project_summary`、`multimodal`、`ready_made`。
- 对 PPTX 做 ZIP 安全预检、OOXML/`python-pptx` 解析和整页渲染。
- 以 package、object、page、slide-pair、claim、requirement、asset、chart-series、deck
  为作用域保存 `AtomicObservation` 与完整 Evidence。
- 使用规则负责可验证事实与 cap，使用 `qwen3.8-flash` 负责局部视觉/语义审计；同构念冲突、
  低置信或非法响应时才升级到 `glm-5.3-flash`。
- 将所有同构念规则 `CRITICAL` 页加入 VLM 兜底，不让抽样上限漏掉硬门候选。
- 通过版本化 Reducer 生成 content、composition、typography、palette、visual communication、
  visual system 和 authorship specificity 构念；模型不能直接提交整份 PPT 总分。
- 独立输出 visual、layout、content、full-deck 四条训练准入轨。
- 在审计台中查看风险页、bbox、规则/VLM 证据、Gate/Reducer lineage、模型路由、Manifest，
  并追加不可变的人工 ReviewEvent。
- 保存原 PPTX、完整 Observation、页图 Render Manifest 及逐图 SHA-256；运行事件写入哈希链。

## 2. 一分钟启动完整平台

要求：Docker Desktop 已启动。完整 Web 平台以 Docker 为推荐入口；容器只绑定本机
`127.0.0.1`。

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

打开：

- 审计平台：<http://127.0.0.1:8000/review/>
- 交互式接口浏览：<http://127.0.0.1:8000/docs>
- 权威静态接口合同：[`docs/openapi.yaml`](docs/openapi.yaml)
- 健康检查：<http://127.0.0.1:8000/healthz>

没有模型 Key 时，确定性 Oracle、评分、制品存储和审计平台仍可运行；模型相关 required
metric 会按合同返回 N/A，并把 Coverage 降级为 REVIEW，而不是伪造零分。

### 从浏览器完成一次评测与人审

推荐工作流是在审计平台点击“新建评测”，选择 `.pptx`、场景和可选上下文后提交。页面会
创建进程内 Job，轮询显示 `PENDING → RUNNING → COMPLETED/FAILED`；成功后按
`review_url` 进入该 run 的 Attention 审计页，最后提交不可变 `ReviewEvent`。

同一路径可用 multipart API 调用：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/v1/evaluations/upload?async=true" `
  -F "presentation=@C:\path\to\sample.pptx;type=application/vnd.openxmlformats-officedocument.presentationml.presentation" `
  -F "case_id=sample-001" `
  -F "scene=ready_made" `
  -F "request=请评估这份市场调研汇报"
```

异步提交返回 HTTP `202` 和 `job_id`。用下列接口轮询；`COMPLETED` 结果会同时给出
`run_id` 与 `review_url`：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/v1/jobs/job-..."
```

对文件夹内的多份存量 PPT，可使用正式批处理入口一次提交 1–16 份文件：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/v1/evaluation-batches/upload" `
  -H "Idempotency-Key: market-folder-001" `
  -F "presentations=@C:\path\to\deck-a.pptx" `
  -F "presentations=@C:\path\to\deck-b.pptx" `
  -F "case_ids=deck-a" `
  -F "case_ids=deck-b" `
  -F "scene=ready_made"
```

`presentations` 和 `case_ids` 按提交顺序一一对应。返回的 `Location` 指向
`GET /v1/evaluation-batches/{batch_id}`；每个 item 仍是独立 Job，可独立成功或失败，成功项会
给出自己的 `run_id` 和 `review_url`。批次在入队前会完整预检并原子预留队列容量；
任一文件不安全或容量不足时整批拒绝，不会只接收前半批。

当前批量上传仅支持 `ready_made` PPTX，不接受 `source_materials` 或 `assets`；需要逐项
来源/素材的场景应继续使用单任务入口。单个 PPTX 仍限 100 MiB，批次整个 multipart
请求与其他写请求共享 202 MiB 前置上限；超出时请拆成多个批次。

上传只接受有限大小的 `.pptx`。文件名、MIME 和 multipart 字段都是不可信输入；服务端使用
不可猜测的本地名称、ZIP/OOXML 安全预检与原子工作区。所有写请求在 multipart 解析/落 spool
前有 202 MiB 总 body 硬上限，同时保留逐文件的二次流式限制。路径穿越、加密条目、重复条目、
ZIP bomb、DTD/entity 或缺少必要 OOXML part 的包会被拒绝。`source_materials` 和
`assets` 是可选的真实文件字段，不是服务端文件路径；多份文件使用重复的 multipart 字段提交，
并与主 PPTX 一样接受文件名、大小、落盘和清理约束。

单份 PPTX 上限为 100 MiB；每份 source/asset 上限为 25 MiB，附件合计上限为 100 MiB，
`source_materials` 最多 16 份、`assets` 最多 32 份。文件名最长 120 个字符，不得包含路径分隔符、
控制字符或 Windows 保留名；主文件只允许 `.pptx`。当前 source 仅接受可直接提取的文本后缀
`.txt/.md/.csv/.tsv/.json/.yaml/.yml`；PDF、DOCX 和 XLSX 需要专用解析器，本版不会假装已解析。
asset 按图像、视频、PDF 或表格后缀白名单验证。

对带 `Origin` 的浏览器写请求，服务端只允许 `localhost` 或 `127.0.0.1` 的同机 UI；来自其他
网站的上传/审计写入返回 `403 ORIGIN_FORBIDDEN`。本机 CLI/curl 通常不带 Origin，仍可调用。

`POST /v1/evaluations` 的 JSON `pptx_path` 仍保留给受信本机管理员和自动化，调用方负责
提供服务端可见路径；该接口不得向不可信用户暴露，也不是浏览器或远程用户的推荐入口。

停止服务：

```powershell
docker compose down
```

`var/` 中已完成的运行、审计和制品不会因容器停止而删除。Job 状态仅存在于当前 API
进程：重启后未完成的 `PENDING/RUNNING` Job 不会自动续跑；终态 Job 也只保留最近的有界数量。
`GET /v1/jobs/{job_id}` 返回 404 时，浏览器会清除失效 Job、停止轮询，并提示到“全部运行”确认
是否已产生 run。

正常完成、失败或取消的工作区会精确清理；进程/主机崩溃可能在 `var/uploads/work/` 留下
`upload-*` 或隐藏 draft。为避免新进程误删另一实例仍在使用的文件，启动时不跨进程自动清理；
单机运维应在停止 API 后核对并清理孤儿工作区。自动 TTL/lease 清理属于后续生产控制面。

## 3. 启用模型审计

`.env.example` 默认关闭远程模型，防止空 Key 或误调用。需要时在本地 `.env` 中设置：

```dotenv
PPT_EVAL_QWEN_AUDIT_ENABLED=true
DASHSCOPE_API_KEY=sk-...
PPT_EVAL_QWEN_FLASH_MODEL=qwen3.8-flash

PPT_EVAL_ZHIPU_AUDIT_ENABLED=true
ZAI_API_KEY=...
PPT_EVAL_ZHIPU_MODEL=glm-5.3-flash
```

默认兼容端点：

- DashScope：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- BigModel：`https://open.bigmodel.cn/api/paas/v4`

不要提交 `.env`、`api/` 或任何真实凭证。当前远程模型请求只包含渲染页像素、有限的幻灯片
对象上下文和有长度上限的 request/audience 文本；不会把原 PPTX 或服务端路径发给模型。
但上传的 PPTX、页图和证据会落在本机 `var/`，启用模型时被选中页的像素会发给所配置 Provider；
不应上传未获授权的敏感材料。

## 4. 运行架构

```text
PPTX / request / source / assets
  │
  ├─ package preflight + parser + renderer
  ▼
baseline_ppt_quality
  ▼
OBSERVE ── scoped AtomicObservation + Evidence
  ▼
AUDIT   ── criterion-specific Qwen → conditional GLM fallback
  ▼
FUSE    ── deterministic facts/caps + model positive signals
  ▼
REDUCE  ── mean / p20 / key-page minimum / importance coverage
  ▼
SCORE   ── PPT-PDMS + Coverage + Decision + training eligibility
  ▼
REVIEW  ── AttentionIssue → append-only ReviewEvent
```

核心边界：

- **Harness** 负责任务状态、DAG、重试、超时、缓存、Evidence 与 Manifest。
- **Oracle** 只输出原子事实或单构念审计，不直接评价整份 PPT。
- **Reducer/Composite** 负责去重、聚合、cap 和训练轨映射。
- **审计平台** 只消费持久化机器事实，绝不读取 benchmark human rank、Spearman 或
  `comparison.json`。
- **人工 Review** 不覆盖机器报告，而是追加带 report/observation hash 的新事件。

## 5. 评分、Coverage 与训练准入

```text
S_base = 100 × product(base multipliers) × A_base
S_full = 100 × product(base multipliers) × product(scene multipliers)
         × (lambda × A_base + (1-lambda) × A_scene)
```

- hard multiplier 只能是 `1`、`0.5` 或 `0`。
- required N/A 不参与算术、不扩大其他权重，但会降低 Coverage 并转 REVIEW。
- runtime ERROR 与质量零分严格分离。
- 可争议的 geometry、typography、contrast、resolution 硬门必须有同页、同构念模型证据。

| Coverage | 含义 | 决策约束 |
|---|---|---|
| `FULL` | required 证据完整 | 可 PASS / REVIEW / FAIL |
| `DEGRADED` | required 证据未解决 | 强制 REVIEW |
| `BASE_ONLY` | 场景子图不可用 | 只发布本体分，强制 REVIEW |
| `UNASSESSABLE` | 无法产生本体结论 | ERROR / REVIEW |

四条训练轨独立输出 `TRAIN / REVIEW / REJECT`。机器训练准入与用户侧 Decision 是不同合同，
不能互相替代。

## 6. 审计平台工作流

真实用户闭环为：

```text
新建评测（multipart PPTX）
→ Job 进度
→ EvalReport / Observation / Render Manifest 落盘
→ review_url 进入 Attention 审计
→ 问题级 resolution
→ 幂等追加 ReviewEvent
```

队列按系统事实产生 P0–P3，不使用人类标签。主 Attention 最多展示 8 个
Composite/多模态语义问题：

```text
Harness ERROR / required metric 未解决 / 未恢复 Provider 错误
→ 同页 VLM 确认或仍有冲突的严重候选
→ 规则与模型的同构念冲突
→ VLM MAJOR/CRITICAL 语义缺陷与低于关注线的 Composite/Reducer
```

Coverage 非 FULL 只会提升队列优先级，不会凭空生成局部疑点。原子规则、已恢复的
Provider 尝试、metric/Oracle/observation ID 和完整 Gate/Reducer lineage 仅保留在
“完整审计事实”与 Observation 制品中。

问题级判断：

- `CONFIRMED`
- `FALSE_POSITIVE`
- `INSUFFICIENT_EVIDENCE`

运行级判断：

- `CONFIRM_SYSTEM_DECISION`
- `OVERRIDE_DECISION`
- `REQUEST_MORE_EVIDENCE`

确认或覆盖最终结论前，所有 P0/P1 问题必须有问题级判断。`Idempotency-Key` 防止网络重试重复
写入。历史 `APPROVE/REJECT` 事件仍可读取，但新写入只接受上述三种运行级 verdict。
上传的 source/asset 与 run Manifest 绑定后，会在完整审计抽屉中作为可下载的输入制品，
人审无需依赖服务端路径即可对照来源和指定素材。

## 7. CLI 与本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,api]"

ppt-eval run examples/demo/case_ready_made.json
ppt-eval audit verify
ppt-eval serve --host 127.0.0.1 --port 8000
```

`examples/demo/` 是已跟踪、可移植的烟测输入；case 内的 `./...` 路径相对各自 JSON
解析，新人 clone 后可直接运行，不需要先重新生成。如需制作一份可修改变体，可选执行：

```powershell
python examples/generate_demo.py
```

生成器默认只写入被忽略的 `var/demo-generated/`；即使显式指定输出，项目约定也只允许
`var/` 下的目录。它不覆盖 tracked demo，正常使用后 `git status` 应保持干净。

默认四场景 v8.3 Profile 已作为 package data 打进 wheel，不依赖当前工作目录。历史 v1–v7
Profile、旧 Oracle 入口和同期方法文档保存在不可变 tag `archive/v8.3-pre-release`。需要复现时
使用独立 worktree，不要在当前 main 混用合同：

```powershell
git worktree add ..\ppt-eval-legacy archive/v8.3-pre-release
```

前端开发：

```powershell
cd ui
pnpm install --frozen-lockfile
pnpm dev -- --port 5173
```

Vite 会把 `/v1` 代理到 `127.0.0.1:8000`。`VITE_DEMO_MODE=true` 只用于显式视觉开发，生产
bundle 不包含 demo 数据。

## 8. API 入口

| Endpoint | 用途 |
|---|---|
| `POST /v1/evaluations/upload` | 推荐：multipart 上传 PPTX，创建同步或进程内异步评测 |
| `POST /v1/evaluation-batches/upload` | 1–16 份 `ready_made` PPTX 原子入队，每项独立执行 |
| `GET /v1/evaluation-batches/{batch_id}` | 读取进程内批次进度、计数和各项 review URL |
| `GET /v1/jobs/{job_id}` | 读取进度；完成后返回 `run_id` 与 `review_url` |
| `POST /v1/evaluations` | 受信本机兼容：管理员/自动化提供服务端可见 PPTX 路径 |
| `GET /v1/review/tasks` | 读取精简审计队列 |
| `GET /v1/review/tasks/{run_id}` | Attention、页图和训练轨详情 |
| `GET /v1/review/tasks/{run_id}/audit` | 按需加载 Matrix、路由与 Manifest |
| `GET /v1/review/tasks/{run_id}/slides/{page}` | 读取校验后的页图 |
| `GET /v1/review/tasks/{run_id}/artifacts/{role}` | 读取 run 绑定制品 |
| `GET /v1/review/tasks/{run_id}/inputs/{role}/{index}` | 下载 Manifest 绑定的 source/asset 输入 |
| `POST /v1/reviews` | 幂等追加人工 ReviewEvent |
| `GET /healthz` | 服务与哈希链状态 |

完整 schema 见 [`docs/openapi.yaml`](docs/openapi.yaml)。

## 9. 数据与审计目录

```text
var/
├─ uploads/work/                 进程内上传工作区（崩溃孤儿需停服务后人工清理）
├─ runs/                         EvalReport、RunManifest、reviews.jsonl
├─ audit/events.jsonl            全局 append-only SHA-256 链
├─ artifacts/                    内容寻址 PPTX / Observation / render manifest
├─ artifacts/slide-renders/      输入哈希键控的逐页渲染缓存
├─ feedback/                     下游接受与 edit diff
└─ proposals/                    参数提案事件
```

不要手工编辑 JSONL，也不要在 Job 运行期间删除 `uploads/work/` 工作文件。报告中的本机 URI 不会
发送给审计前端；artifact 下载必须同时匹配 run、固定 role 与 Manifest hash。原 PPTX 作为
内容寻址制品按审计保留；source/asset 也只在 run 产生后进入 CAS 并写入 Manifest。
上传工作副本不是归档。

## 10. 开发过程审计与历史复现

生产审计与开发审计是两套不同事实链：

- `audit/schema/`：当前 v8.3 运行审计事件 schema。
- `reports/01_research/`：调研、基线和数据许可。
- `reports/02_development/`：SRS、ADR、威胁模型、追踪矩阵和版本卡。
- `reports/03_evaluation/`：预注册、测试计划与真实切片实验记录。
- `third_party/`：带 provenance/license 的外部基线复现快照。

查看当前开发链或复现归档版本：

```powershell
git log --oneline --decorate
git worktree add ..\ppt-eval-legacy archive/v8.3-pre-release
```

历史实现采用 tag 而不是常驻 `legacy` 分支：tag 精确锁定一次提交，旧代码不会继续漂移，也
不会被误认为仍受支持的第二条产品线；需要修复历史版本时，从 tag 临时创建 `codex/...`
分支并在独立 worktree 中工作，完成后再用新的归档 tag 固化。`main` 因而始终代表唯一当前
写合同，而 Git 历史与 tag 继续承担完整追溯。

路径卫生也是发布门禁：主线自有示例和新生成 provenance 只能保存 manifest-relative/
repo-relative 路径、内容 hash 或 opaque ID，禁止提交开发机盘符、用户名和组织目录。唯一类精确例外是
vendored 上游源码本身硬编码的私有路径：它必须被明确标注为 upstream 不可复现证据，不得出现在
本机运行命令或新的输出制品中。

## 11. 发布验证

```powershell
python -m pytest -q
python scripts/run_tests.py
ruff check src tests scripts
pnpm --dir ui build
docker compose config --quiet
docker compose build api
```

第二条 runner 会在自身进程内提供仅支持当前 `raises / approx / importorskip` 用法的严格
pytest facade。因此精简生产镜像没有 pytest 时也能运行 plain-assert 测试；缺失 `httpx`
等可选测试传输时会显式记为 `SKIP`，不会以早退冒充 `PASS`。

测试覆盖 Harness、当前 Profile/Oracle/Reducer、PPTX 安全、模型合同、artifact 完整性、审计
队列、Review 幂等性和 UI 构建。旧合同的原始测试随 archive tag 保存。

## 12. 项目结构

```text
src/ppt_eval/
├─ domain/             Report、Observation、Manifest 等领域合同
├─ application/        DAG、Supervisor、调度和审计投影
├─ adapters/           PPTX、渲染器和模型中立接口
├─ oracles/            baseline、场景、v8 原子规则与局部模型审计
├─ scoring/            PPT-PDMS 与 Reducer
├─ profiles/           wheel 内置的四份当前 v8 默认 Profile
├─ infrastructure/     单机 JSON/CAS/哈希链实现
├─ runtime.py          单机 composition root
├─ api.py              FastAPI 与静态审计前端
└─ cli.py              命令行入口

ui/                    React 审计工作台
configs/profiles/      当前 Profile 位置与 archive tag 说明
docs/                  当前方法、平台和 API 文档
reports/               可审计的研发过程记录
tests/                 单元、属性、集成和端到端测试
```

## 13. 当前边界

- 默认 v8.3 权重仍处于预研阶段，尚未通过大规模人类金标完成生产校准。
- 当前发布版是单机运行时；异步 Job 在 API 进程内，不是持久任务队列。进程重启后旧
  `job_id` 与未完成 Job 不保留，已落盘 run 和 ReviewEvent 保留。
- 上传工作区没有跨进程 lease/TTL；崩溃孤儿需单机运维清理，服务不会在启动时自动删除。
- 已有浏览器上传的单机闭环，但尚无认证、RBAC、多审计员 claim lease、持久 Job 或远端
  对象存储。
- 审计平台应保持绑定 localhost；部署给团队前必须补身份与权限控制。
- Docker 包含完整审计 UI；普通 wheel 从仓库外运行 `ppt-eval serve` 只保证 API，除非另行提供
  `PPT_EVAL_UI_DIR` 或使用 Docker 镜像。
- Docker 基础镜像、Node/pnpm 与 Linux/Python 运行依赖已锁定；Debian apt 仍使用移动安全仓库，
  因此属于功能可复现，尚不是字节级 hermetic build。首次获取缺失的基础镜像仍需 Docker Hub 可达。
- benchmark 结果只用于 Oracle/Profile 研发，不能作为生产人工标签或队列排序依据。

进一步阅读：

- [v8 原子评测方法](docs/v8_atomic_evaluation_method.md)
- [服务端人工审计平台](docs/review_platform.md)
- [模型审计 Provider 合同](docs/model_audit_provider_contract.md)
- [项目文档索引](docs/project_index.md)

## License

Proprietary。第三方基线及数据的许可证和 provenance 见各自 `third_party/*/LICENSE`、
`PROVENANCE.json` 与数据清单。
