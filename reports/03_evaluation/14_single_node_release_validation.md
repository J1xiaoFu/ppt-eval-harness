# 单机发布最终验证

日期：2026-08-28

## 验证对象

- v8.3 四场景 bundled Profile
- CRITICAL 规则页预算外 VLM 兜底
- Attention-first 服务端审计投影
- 服务端审计队列、页图、artifact、完整审计与 ReviewEvent
- React/Vite 审计台
- 精简后的单服务 Docker 镜像
- `archive/v8.3-pre-release` 历史 worktree 可检出性

## 自动门禁

| 门禁 | 结果 |
|---|---|
| 全量 pytest | 157 passed |
| dependency-free runner | 157 passed |
| Ruff（src/tests/scripts） | passed |
| 全部 41 个 source module strict mypy | passed |
| React TypeScript + Vite build | passed |
| OpenAPI YAML parse | passed |
| `docker compose config --quiet` | passed |
| `git diff --check` | passed |

FastAPI TestClient 产生一条上游 `StarletteDeprecationWarning`，不影响功能或测试结论；项目代码
无 warning/error。

## 包与容器

- wheel 已验证包含四份 `ppt_eval/profiles/*_v8.json`。
- 从仓库外目录直接从 wheel 导入，`ready_made` 返回 `finished-deck-v8 / 8.3`。
- Docker 多阶段构建成功，前端 production bundle 不包含 demo 或 benchmark 字段。
- Compose 只启动 `api`，旧 Redis/PostgreSQL/MinIO orphan 已移除。
- 容器健康状态为 `healthy`；`/healthz`、`/review/` 和静态 JS 均返回 200。
- 容器内旧整体 Oracle 与 Qwen 3.7/Plus 符号均不存在；v7 Profile 与旧 `APPROVE`
  写入探针均返回 422。

## 端到端烟测

在不配置任何远程模型 Key 的条件下，对容器内四页示例 PPT 运行一次完整评测：

- 生成 EvalReport、RunManifest 和审计链事件；
- 保存 source PPTX、AtomicObservation 和 render manifest；
- render manifest 为 1.1，逐页 SHA-256 校验通过；
- 审计详情按需返回页图与 11 个系统 AttentionIssue；
- 详情首屏不含全量 Matrix，也不泄露主机或容器绝对路径；
- 相同 Idempotency-Key 重试两次只产生一条 ReviewEvent；
- 请求补证后任务状态为 `NEEDS_EVIDENCE`。

该 smoke run 只验证运行合同，不用于指标校准或性能结论。
