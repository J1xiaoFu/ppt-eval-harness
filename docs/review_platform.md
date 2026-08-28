# 服务端人工审计平台

## 产品边界

当前审计平台属于 PPT Eval Harness 产品发布 `0.8.6`，但它消费的默认评测 Profile 仍为
`8.3` / `PRE_RESEARCH`，持久化 EvalReport/Audit schema 仍为 `1.0`，HTTP 命名空间仍为 `/v1`。
产品版本只表达软件能力发布，不得被当作 Profile 或报告 schema 版本。

审计平台是数据闭环的操作面，不是指标研究报告。Slides-Align 人评排名、Spearman、
pairwise、历史 Profile 对比等研发协议已迁入 `archive/v8.3-pre-release`，不得进入生产队列、
优先级或人工结论。

生产事实源固定为：

```text
EvalReport + RunManifest + AtomicObservation artifact + render manifest
    → ReviewTriagePolicy（版本化、纯系统事实）
    → ReviewTask / AttentionIssue
    → append-only ReviewEvent
```

机器报告永不被人工结果覆盖。人工确认、推翻或补证请求都会形成新事件，并记录 report hash、
observation hash、triage policy 版本和问题级判断。

## 推荐用户闭环

```text
新建评测（multipart presentation + 可选 source/assets 文件）
  → 202 Job
  → PENDING / RUNNING 进度
  → COMPLETED + run_id + review_url
  → Attention 页人工核对
  → 幂等追加 ReviewEvent
```

浏览器“新建评测”与 `POST /v1/evaluations/upload` 是普通用户的主入口。主 PPTX 字段为
`presentation`；多份 `source_materials` 和 `assets` 以重复的 multipart 文件字段提交。
`POST /v1/evaluations` 中的 `pptx_path` 仅是受信本机管理员/自动化兼容入口；调用方负责
服务端可见路径，不得将该入口暴露给不可信用户。

`POST /v1/evaluation-batches/upload` 是文件夹型存量 PPT 的正式批处理入口。它接受 1–16 个
重复 `presentations` 字段与等长、同序的 `case_ids`，整批只支持 `ready_made`。所有文件
先完成安全预检，随后在同一 `LocalJobManager` 内原子预留 N 个 active slot；任一预检失败或
容量不足时整批拒绝并清理 workspace。202 接收后，每项的运行失败互不影响，批次可进入
`COMPLETED / PARTIALLY_FAILED / FAILED`。当前批量入口显式拒绝 source/assets；需要逐项附件时
应拆分单任务，不根据文件名或数组位置猜测归属。

异步 Job 与 Batch 都是进程内状态，不是持久任务队列。API 进程重启后，未完成 Job 不续跑；终态 Job
也只保留最近的有界数量。轮询返回 404 时，前端清除 sessionStorage 中的失效 Job、停止重试，
并提示用户到“全部运行”确认是否已产生 run。Batch 也使用独立有界终态快照；重启或淘汰后
`GET /v1/evaluation-batches/{batch_id}` 返回 404，但已持久的 run、制品、审计链和 ReviewEvent 不受影响。

## 人审工作台

桌面工作台采用三栏结构：

1. 左侧队列：P0–P3、case/run、机器 Decision、Coverage、首要疑点、疑点数和审计状态；
2. 中间幻灯片：当前页、关联页 filmstrip、bbox overlay 和当前语义判断；
3. 右侧 Attention：最多 8 个 Composite/多模态语义问题，以中文标题、证据共识、
   聚焦页和折叠判断依据呈现，并接受 `CONFIRMED / FALSE_POSITIVE /
   INSUFFICIENT_EVIDENCE` 判断；
4. 底部 Review Composer：确认系统、覆盖结论、请求补证和不可变提交；
5. 按需抽屉：完整 Matrix、Gate/Reducer lineage、模型路由、Manifest、制品和人审历史。

`GET /v1/review/tasks/{run_id}` 是首屏精简 DTO，只返回 Attention、页图链接、训练轨、制品链接和
人审历史；不在此处携带 `results`、`gate_results`、`model_routes` 或 Manifest。用户打开
“完整审计事实”时，前端才按 `audit_url` 请求 `/v1/review/tasks/{run_id}/audit`。
精简 DTO 还会给出 run-bound `inputs`；有效 source/asset 通过
`/v1/review/tasks/{run_id}/inputs/{role}/{index}` 下载，每次读取都复核 Manifest 中的角色、序号和 SHA-256。

PASS 不会从系统中消失。“审计队列”默认隐藏无疑点 P3 PASS，但“全部运行”始终保留入口，
主动抽样或存在疑点的 PASS 仍会进入 P2。

## 优先级

`audit-attention@0.8.6` 不读取任何 GT 或人类标签。主 Attention 首先按以下信号形成
语义候选，再合并到 8 个稳定质量族：

```text
Harness ERROR / 未解决 required metric / 未恢复 Provider 错误
→ 已经同页 VLM 确认或仍有冲突的硬门候选
→ 规则与模型的同构念冲突
→ VLM MAJOR/CRITICAL 语义缺陷与低于关注线的 Composite/Reducer
```

原子规则不直接生成主卡；已恢复的 Provider 尝试也只留在完整审计。页面布局与
文字可读性等同类问题合并后，主区仅展示语义标题、共识、聚焦页和最多三条判断依据。
同一具体 `semantic_code` 若在不同 Composite/family 中绑定完全相同的受影响页集，主区只保留
一张 primary-owner 卡；各条 metric、family、raw issue 和 candidate 仍全部进入 lineage，不通过去重删除事实。
原始 metric/Oracle/observation ID、Gate、Reducer 和全量 Observation 在“完整审计事实”中保留。

Coverage 非 FULL 会提升队列优先级，但不会凭空生成一张局部问题卡。同级只使用审计状态、
系统优先级、创建时间和 run ID，禁止按 human rank 或模型分数优化队列。

## 制品与安全

- 上传文件名和 MIME 只是不可信元数据；服务端使用 opaque 工作名、流式大小上限、ZIP/OOXML
  预检和原子落盘，不用原始文件名组合路径；
- 主 PPTX 上限 100 MiB；附件每份上限 25 MiB、合计上限 100 MiB，source 最多 16 份、
  asset 最多 32 份；
- 写请求在 multipart 解析前有 202 MiB ASGI 总 body 上限；带 Origin 的浏览器写请求只允许
  `localhost` / `127.0.0.1` 的同机 UI，非本机 Origin 返回 403；
- source 只接受 `.txt/.md/.csv/.tsv/.json/.yaml/.yml`，确保当前 Oracle 能提取文本；
- 路径穿越、加密/重复 ZIP 条目、异常压缩比、DTD/entity 或缺少必要 OOXML part 会在评测前拒绝；
- 上传先进入同文件系统工作区，run 产生后才把 PPTX/source/asset 原子写入 CAS 并绑定 Manifest；
- 正常失败/完成会清理工作区。进程崩溃留下的 `var/uploads/work/` 孤儿不在新进程启动时
  自动删除，避免误删另一实例正使用的文件；当前需停服务后人工核对/清理，未来由 TTL/lease 控制面接管；
- 原 PPTX、Observation 和 render manifest 进入 content-addressed store，SHA-256 写入 Manifest；
- render manifest `1.1` 记录每张页图的文件名、大小和 SHA-256；读取缓存时逐图复核；
- 下载端点按 run + 固定 role 授权，不提供任意文件路径或全局 SHA 下载；
- run ID 和 SHA 均采用白名单格式，并在 resolve 后校验 root containment；
- API DTO 移除本机 `uri`，本地路径不发送到浏览器；
- artifact hash 不一致时停止下载并返回完整性错误。

## ReviewEvent

运行级 verdict：

- `CONFIRM_SYSTEM_DECISION`
- `OVERRIDE_DECISION`
- `REQUEST_MORE_EVIDENCE`

问题级 resolution：

- `CONFIRMED`
- `FALSE_POSITIVE`
- `INSUFFICIENT_EVIDENCE`

覆盖系统或请求补证必须提供备注；误报/证据不足也必须留下理由。`Idempotency-Key` 防止网络
重试重复追加。当前本地实现使用 reviewer ID 输入框；正式多用户部署仍需把它替换为服务端
认证主体，并增加 claim lease、RBAC、事务性数据库与对象存储。

## 运行

- 开发：API 在 `127.0.0.1:8000`，Vite 在 `127.0.0.1:5173` 并代理 `/v1`；
- 构建：`ui` 先生成静态资源，FastAPI 在 `/review/` 同源服务；
- Docker：默认启用 `PPT_EVAL_REVIEW_RENDERING_ENABLED=true`，即使模型不需要图片也为人审
  保存完整页图；
- 无模型 Key：上传、确定性 Oracle、落盘与人审入口仍可用；模型 required metric 返回 N/A，
  Coverage 降级并进入 REVIEW，不伪造零分；
- Demo 模式只用于前端视觉开发，需显式设置 `VITE_DEMO_MODE=true`，生产构建不会回退到假数据。

## 尚未实现的生产控制面

本轮完成单机闭环合同与前端。多审计员 claim/release lease、认证/RBAC、cursor pagination、
PostgreSQL ReviewRepository、S3/MinIO 预签名下载、Celery 持久 Job、审计链周期校验与
Transactional Outbox 仍是部署到多人生产环境前的必要工作；界面已为这些状态保留结构，
但不会伪装为已完成。
