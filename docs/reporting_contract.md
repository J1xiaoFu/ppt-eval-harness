# 汇报生成契约

## 输入

- `--audit`：符合 `audit/schema/project_audit.schema.json` 的 JSON 快照。
- `--events`：只追加 JSONL；事件 ID 唯一，`supersedes` 只能指向更早事件。
- `--docs-root`：Markdown 根目录，默认项目根；JSON 中的 evidence/source 必须留在根目录内。

## 输出

| 文件 | 用途 |
|---|---|
| `project-audit.html` | 无 JavaScript 的只读三阶段项目站点 |
| `deck-data.json` | PPT 的唯一中间数据，含输入哈希和九页内容 |
| `build-manifest.json` | 输入/输出哈希、生成时间、Schema/生成器版本 |
| `ppt-eval-interview.pptx` | 三章九页面试汇报 |

生成器拒绝：非三阶段、非九页、路径逃逸、重复 ID、非单调事件时间、非法 supersedes、把 planned
验收项标成 observed。HTML 中所有输入均先转义；Markdown 只支持受限标题、段落、列表、表格和代码块。

## 同源原则

结构化状态/门槛/公式来自审计 JSON；长说明来自 Markdown；生成物显示各自 SHA-256。面试 PPT 的
speaker notes 每页包含 `[Sources]` 和对应 Markdown 路径。不得手工编辑生成物后作为正式证据。

