# 审计数据目录

本目录保存评测系统的机器可读证据。`project_audit.json` 是项目级快照，
`events.jsonl` 是只追加事件流；任何更正都新增事件并填写 `supersedes`，不得覆盖原记录。

## 不变量

- ID 前缀固定为 `REQ/ADR/ORC/TST/EXP/RUN/REL`，全链路可追踪。
- 所有时间为带时区的 ISO-8601；示例使用 `+08:00`。
- 运行清单必须记录输入、代码、容器、字体、模型、Prompt、Profile 与输出哈希。
- 自动结果和人工复核分别写入事件；人工结论不得改写机器原始结果。
- `status=planned` 只表示设计或门槛，不能在汇报中表述为已达成结果。

## 验证与生成

```powershell
python scripts/reporting/verify_audit.py audit/example/project_audit.json audit/example/events.jsonl
python scripts/reporting/build_report.py --audit audit/example/project_audit.json --events audit/example/events.jsonl --output reports/generated
```

