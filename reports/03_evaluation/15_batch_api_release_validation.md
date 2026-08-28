# 0.8.5 批处理 API 发布验证

日期：2026-08-28

## 验证结论

`POST /v1/evaluation-batches/upload` 与 `GET /v1/evaluation-batches/{batch_id}` 已通过单机正式入口验证。
本轮仅发布产品版本 `0.8.5`；Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为
`8.3.0`，EvalReport/Audit schema 仍为 `1.0`，Attention 策略仍为 `audit-attention@0.8.4`。

## 批处理专项

- 两份有效 PPT 按 1-based 提交顺序保留，分别产生独立 Job、Run 和 review URL。
- 单项运行失败不中断其他项；一成一败为 `PARTIALLY_FAILED`，全败为 `FAILED`。
- 第 N 个 PPTX 预检失败时整批 422，无 Job、无 Batch、无 workspace 残留。
- 已占用队列容量时，超容批次原子 429，未出现前半批已接收的状态。
- 同一幂等键与完全相同有序输入复用原 Batch 和 child Job ID；调换顺序返回 409。
- 0 项、case 数量不匹配、重复 case ID、17 项、非 `ready_made` 以及附件字段均返回 422。
- 64-byte 请求体探针在 multipart 处理前返回 413；现有 Origin/CORS 与单文件上传回归通过。
- `max_terminal_jobs=1` 时，两项批次仍保留完整终态快照，不依赖被淘汰的子 Job。
- 新 API 进程重启后 Batch 查询返回 404，但已完成 Run 与审计任务仍可读取。
- 动态 FastAPI 文档与静态 OpenAPI 均声明 1–16 项、202/Location 和完整错误响应。
- 隔离 Docker 容器中实际上传两份 PPT：`0.8.5` 健康检查与审计链正常，Batch 终态为
  `COMPLETED`，summary 为 `total=2, pending=0, running=0, completed=2, failed=0`，两项均生成
  独立 run 和可读 review task；容器随后销毁。

## 发布门禁

| 门禁 | 结果 |
|---|---|
| 全量 pytest | `202 passed`；仅一条既有 Starlette/httpx2 迁移提示 |
| dependency-free runner | `202 passed, 0 failed` |
| 项目 Ruff | passed |
| 触及源码 strict mypy | passed |
| TypeScript / Vite | passed |
| OpenAPI YAML parse | passed |
| Docker Compose config | passed |
| Git diff check | passed |

所有批次、item 失败、幂等冲突和容量拒绝响应均未暴露 `pptx_path`、workspace、本机绝对路径或原始异常文本。
