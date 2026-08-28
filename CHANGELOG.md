# Changelog

本文档只记录产品/软件发布版本。Evaluation Profile、Oracle/Prompt 和持久化 schema
有独立版本，不会随产品版本自动变更。

## 0.8.5 - 2026-08-28

- 新增 `POST /v1/evaluation-batches/upload` 与 `GET /v1/evaluation-batches/{batch_id}`。
- 一个批次可原子接收 1–16 份 `ready_made` PPTX，各项在同一进程内 JobManager
  中独立执行并使用现有并发上限。
- 新增批次级有序幂等、队列容量原子预留、单项失败隔离、终态快照与有界保留。
- 批量入口复用单任务的文件名、MIME、ZIP/OOXML、Origin、请求体与工作区清理边界。

本次仅升级产品版本。Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为
`8.3.0`，EvalReport/Audit schema 仍为 `1.0`，Attention 投影策略仍为 `audit-attention@0.8.4`。

## 0.8.4 - 2026-08-28

产品从内部 `0.8.3` 预研基线进入 `0.8.4`，并将此前的 `0.1.0` 打包占位值收敛为统一
产品版本出口。

- 将人审主区从 Oracle/硬门/原子规则列表收敛为最多 8 个 Composite/多模态语义问题。
- 增加中文问题标题、多源共识、聚焦页跳转、判断依据折叠与分状态空结果。
- 已恢复的 Provider 重试和未升级的原子规则只进入完整审计，不占用人审主注意力。
- 完整 Observation、Gate、Reducer、模型路由和 Manifest 仍保留 hash 校验与下载入口。
- 保留浏览器上传、进程内 Job、Attention-first 人审与不可变 ReviewEvent 闭环。
- 加强 multipart/Origin、OOXML、CAS、run-bound 输入制品与路径卫生边界。
- 使 tracked Demo 和外部基线 provenance 可移植，并完成隔离 clone 冷启动验证。

兼容性承诺：

- 默认四场景 Evaluation Profile 仍为 `8.3`，生命周期仍为 `PRE_RESEARCH`。
- `V8_QUALITY_VERSION` 仍为 `8.3.0`，Atomic/VLM/Prompt 版本不因本次发布自动变更。
- EvalReport、RunManifest 与 AuditEvent schema 仍为 `1.0`，HTTP 命名空间仍为 `/v1`。
- 不要求历史 run 具有新的必填版本字段；`profile_version=8.3` / `schema_version=1.0`
  的已存报告继续可读。
