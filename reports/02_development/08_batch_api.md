# 0.8.5 正式批处理 API

日期：2026-08-28

## 目标

为文件夹型存量 PPT 评测提供正式 HTTP 入口，同时保持单机版的单进程写入、有界并发、
输入安全、独立 EvalReport 和现有审计队列合同。本轮不改变 Evaluation Profile 8.3、评分、
Oracle、Attention 投影或持久化 schema。

## HTTP 合同

- `POST /v1/evaluation-batches/upload`：接收 1–16 个有序 `presentations` 和等长 `case_ids`，
  始终异步返回 202。
- `GET /v1/evaluation-batches/{batch_id}`：返回批次聚合状态、五类计数和按提交顺序稳定的
  item 快照。
- 批量上传只接受 `ready_made`；source/assets 会显式返回 422，不从文件名或数组位置
  猜测附件归属。
- 单 PPTX 继续使用 100 MiB 限制，整个 multipart 请求继续使用 202 MiB ASGI
  前置限制。

## 调度与失败语义

1. 字段、数量、case ID 唯一性与所有 PPTX 安全预检全部成功后，才允许创建 Batch。
2. `LocalJobManager` 在同一锁内非阻塞预留 N 个 active slot。容量不足时回滚预留、清理
   全部 workspace 并返回 429，不会产生半批接收。
3. 准入后每项进入现有共享 executor，真实并发仍由 `workers` 限制。单项异常只产生
   `EVALUATION_FAILED`，不中断其他项。
4. 批次使用 `COMPLETED / PARTIALLY_FAILED / FAILED` 区分全成、部分失败和全失败。
5. Batch 保存 item 终态快照，不依赖可能被独立淘汰的 Job 记录；终态 Batch 也有独立
   有界保留。

## 幂等与持久化

`Idempotency-Key` 绑定有序 case ID、安全原始文件名、内容 hash、文件大小和共享字段。完全相同的
重试返回原 Batch 及原 child Job ID；任一内容、顺序或元数据变更返回 409。Batch 和 Job 状态
仅属于当前 API 进程；重启后查询 404，但已完成的 Run、Observation、Manifest 和 ReviewEvent 仍持久化。
