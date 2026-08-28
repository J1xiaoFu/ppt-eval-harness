# 服务端人工审计平台

## 产品边界

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

## 人审工作台

桌面工作台采用三栏结构：

1. 左侧队列：P0–P3、case/run、机器 Decision、Coverage、首要疑点、疑点数和审计状态；
2. 中间幻灯片：当前页、风险页 filmstrip、bbox overlay、规则/VLM 原始文字证据；
3. 右侧 Attention：按 primary owner、metric、page 聚合后的问题，以及
   `CONFIRMED / FALSE_POSITIVE / INSUFFICIENT_EVIDENCE` 判断；
4. 底部 Review Composer：确认系统、覆盖结论、请求补证和不可变提交；
5. 按需抽屉：完整 Matrix、Gate/Reducer lineage、模型路由、Manifest、制品和人审历史。

PASS 不会从系统中消失。“审计队列”默认隐藏无疑点 P3 PASS，但“全部运行”始终保留入口，
主动抽样或存在疑点的 PASS 仍会进入 P2。

## 优先级

`audit-attention@1.0.0` 不读取任何 GT 或人类标签。当前顺序为：

```text
Coverage 非 FULL / Harness ERROR / 未解决 required metric
→ 已确认或未解决硬门
→ 未恢复 Provider 错误
→ 规则与模型冲突
→ CRITICAL / MAJOR 原子证据
→ 非 TRAIN 训练轨与主动 PASS 抽查
```

同级只使用审计状态、系统优先级、创建时间和 run ID，禁止按 human rank 或模型分数优化队列。

## 制品与安全

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
- Demo 模式只用于前端视觉开发，需显式设置 `VITE_DEMO_MODE=true`，生产构建不会回退到假数据。

## 尚未实现的生产控制面

本轮完成单机闭环合同与前端。多审计员 claim/release lease、认证/RBAC、cursor pagination、
PostgreSQL ReviewRepository、S3/MinIO 预签名下载、Celery 持久 Job、审计链周期校验与
Transactional Outbox 仍是部署到多人生产环境前的必要工作；界面已为这些状态保留结构，
但不会伪装为已完成。
