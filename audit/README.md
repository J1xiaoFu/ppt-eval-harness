# 审计 schema

本目录只保存当前机器可读运行审计 schema。产品发布 `0.8.5` 仍使用 Evaluation Profile
`8.3` 和 Audit/EvalReport schema `1.0`；三者是独立版本轴。实际运行数据写入用户指定 data-dir：

```text
var/runs/
var/runs/reviews.jsonl
var/audit/events.jsonl
var/artifacts/
```

运行事件与 ReviewEvent 都是 append-only；任何修订都必须创建新事件，不能覆盖 EvalReport 或
旧事件。产品版本更新不会要求历史 schema `1.0` run 凭空出现新的必填字段。开发过程由
Git 提交、`reports/` 与归档 tag 追踪，不再混入运行时 Event schema。旧三阶段项目快照、
面试汇报生成物和 v1–v7 事件示例已迁入
`archive/v8.3-pre-release`，不属于当前运行协议。
