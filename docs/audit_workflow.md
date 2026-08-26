# 三阶段工作流与汇报审计

## 核心规则

项目只按 **调研 → 开发 → 评测** 三章汇报。每章统一回答：目标与问题、执行过程、证据与结果、
决策与取舍、阶段门禁与风险。任何数字都来自 `audit/example/project_audit.json` 或正式审计快照，
Markdown 只解释数字，不复制成第二份事实源。

## ID 链

```text
REQ → ADR → ORC/Implementation → TST → EXP/RUN → REL
```

- `REQ`：可验收需求；改变语义必须新 ID 或明确 supersede。
- `ADR`：架构取舍及后果。
- `ORC`：指标实现与版本卡。
- `TST`：自动测试或人工协议。
- `EXP/RUN`：预注册实验与具体运行。
- `REL`：签名的代码、Profile、数据和模型组合。

追踪矩阵缺任一关键列时不得发布。示例使用 `REL-PENDING` 明确未发布，避免用空白掩盖状态。

## Run Manifest

每次运行最低记录：`run_id/case_id/input_sha256/git_sha/container_digest/font_bundle/models/
prompt_profile/eval_profile/random_seed/cost/output_sha256`。实际生产还应加入渲染器、OCR、数据集、
地区、租户、DAG hash、开始/结束时间和证据 URI。

## Append-only 规则

`events.jsonl` 每行一个事件。原始机器结果不可被人工结论覆盖；人工复核是新 `review.recorded` 事件。
修订通过新事件的 `supersedes` 指向旧事件。数据库实现使用 Transactional Outbox；归档使用 WORM/
对象锁和周期 hash chain 校验。

## 阶段门禁

阶段文档可先完成，但门禁只能由实际证据事件更新。`planned/ready-for-review/in-progress/pending` 不得
在面试稿中改写为“已完成/已达标”。生成脚本会在 HTML 和 PPT 页脚显示快照时间与项目状态。

## 生成只读汇报

```powershell
python scripts/reporting/verify_audit.py audit/example/project_audit.json audit/example/events.jsonl
python scripts/reporting/build_report.py --audit audit/example/project_audit.json --events audit/example/events.jsonl --output reports/generated
node scripts/reporting/build_interview_deck.mjs --input reports/generated/deck-data.json --output reports/generated/ppt-eval-interview.pptx
```

正常情况下运行 `scripts/reporting/build_all.ps1`，它会读取 Codex bundled runtime 环境变量或显式参数。
HTML 使用 CSP、无脚本、无表单，是只读静态站点；PPT 恰好三章九页，每章三页。

