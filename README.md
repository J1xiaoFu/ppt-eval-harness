# PPT Eval Harness

面向生成式与存量 PowerPoint 的证据优先评测系统。项目把 PPTX 解析、确定性规则、局部
LLM/VLM 审计、原子观察、Reducer、PPT-PDMS 评分、训练准入和人工复核组织成一条可回放、
可解释、可追加审计的运行链。

当前发布版是经过完整测试的**单机审计系统**：CLI、FastAPI、React 审计台、LibreOffice/
PowerPoint 渲染、本地内容寻址存储与哈希链均可直接使用。默认 v8.3 Profile 仍标记为
`PRE_RESEARCH`，表示权重和阈值尚未通过大规模人类金标完成生产校准，不应把当前分数解释为
行业标准。

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
- OpenAPI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/healthz>

没有模型 Key 时，确定性 Oracle、评分、制品存储和审计平台仍可运行；模型相关 required
metric 会按合同返回 N/A，并把 Coverage 降级为 REVIEW，而不是伪造零分。

### 提交一个 PPTX

当前 API 接受的是**服务端可见路径**，不是浏览器上传。Compose 只挂载宿主机 `var/` 到容器
`/var/lib/ppt-eval/`，因此先把文件放进 `var/`：

```powershell
New-Item -ItemType Directory -Force var/inbox | Out-Null
Copy-Item C:\path\to\sample.pptx var/inbox/sample.pptx

$body = @{
  case = @{
    case_id = "sample-001"
    scene = "ready_made"
    pptx_path = "/var/lib/ppt-eval/inbox/sample.pptx"
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/evaluations?async=false" `
  -ContentType "application/json" `
  -Body $body
```

刷新审计平台即可看到新任务。停止服务：

```powershell
docker compose down
```

`var/` 中的运行、审计和制品不会因容器停止而删除。

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
对象上下文和有长度上限的 request/audience 文本；`source_materials` 中的本地文件路径不会被
模型审计链打开或上传。

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

队列按系统事实产生 P0–P3，不使用人类标签：

```text
Coverage/审计链异常
→ required metric 未解决或硬门 CONFIRMED/UNRESOLVED
→ Provider 错误或规则/VLM 冲突
→ CRITICAL/MAJOR 原子证据
→ 主动 PASS 抽查
```

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

## 7. CLI 与本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,api]"
python examples/generate_demo.py

ppt-eval run examples/demo/case_ready_made.json
ppt-eval audit verify
ppt-eval serve --host 127.0.0.1 --port 8000
```

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
| `POST /v1/evaluations` | 创建同步或进程内异步评测 |
| `GET /v1/review/tasks` | 读取精简审计队列 |
| `GET /v1/review/tasks/{run_id}` | Attention、页图和训练轨详情 |
| `GET /v1/review/tasks/{run_id}/audit` | 按需加载 Matrix、路由与 Manifest |
| `GET /v1/review/tasks/{run_id}/slides/{page}` | 读取校验后的页图 |
| `GET /v1/review/tasks/{run_id}/artifacts/{role}` | 读取 run 绑定制品 |
| `POST /v1/reviews` | 幂等追加人工 ReviewEvent |
| `GET /healthz` | 服务与哈希链状态 |

完整 schema 见 [`docs/openapi.yaml`](docs/openapi.yaml)。

## 9. 数据与审计目录

```text
var/
├─ runs/                         EvalReport、RunManifest、reviews.jsonl
├─ audit/events.jsonl            全局 append-only SHA-256 链
├─ artifacts/                    内容寻址 PPTX / Observation / render manifest
├─ artifacts/slide-renders/      输入哈希键控的逐页渲染缓存
├─ feedback/                     下游接受与 edit diff
└─ proposals/                    参数提案事件
```

不要手工编辑 JSONL。报告中的本机 URI 不会发送给审计前端；artifact 下载必须同时匹配 run、
固定 role 与 Manifest hash。

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

## 11. 发布验证

```powershell
python -m pytest -q
python scripts/run_tests.py
ruff check src tests scripts
pnpm --dir ui build
docker compose config --quiet
docker compose build api
```

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
- 当前发布版是单机运行时；异步 Job 在 API 进程内，服务重启后未完成 Job 不保留。
- 尚无浏览器上传、认证、RBAC、多审计员 claim lease 或远端对象存储。
- 审计平台应保持绑定 localhost；部署给团队前必须补身份与权限控制。
- Docker 包含完整审计 UI；普通 wheel 从仓库外运行 `ppt-eval serve` 只保证 API，除非另行提供
  `PPT_EVAL_UI_DIR` 或使用 Docker 镜像。
- benchmark 结果只用于 Oracle/Profile 研发，不能作为生产人工标签或队列排序依据。

进一步阅读：

- [v8 原子评测方法](docs/v8_atomic_evaluation_method.md)
- [服务端人工审计平台](docs/review_platform.md)
- [模型审计 Provider 合同](docs/model_audit_provider_contract.md)
- [项目文档索引](docs/project_index.md)

## License

Proprietary。第三方基线及数据的许可证和 provenance 见各自 `third_party/*/LICENSE`、
`PROVENANCE.json` 与数据清单。
