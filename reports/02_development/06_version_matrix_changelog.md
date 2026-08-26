# 版本矩阵与 Changelog

## 版本矩阵

| 组件 | 当前设计版本 | 兼容策略 |
|---|---|---|
| Audit schema | 1.0 | 同主版本向后兼容；未知字段显式处理 |
| Eval API | v1 | 新字段可选；删除/改义升 v2 |
| Oracle protocol | 1.0 | 返回 Schema 严格校验 |
| PPT-PDMS Profile | profile-*-v1 | 不原地修改，发布新 ID |
| Rubric | rubric-v1 | 报告携带版本 |
| Dataset | pending | 每次切分生成 manifest hash |
| Runnable package | 0.1.0 | 领域契约同主版本兼容；行为变化发布新 Profile/Oracle 版本 |

## Changelog

### 2026-08-26

- 建立三阶段审计骨架与机器可读 Schema。
- 将基础质量子图定义为编译期不变量。
- 固定 PPT-PDMS 外乘内加、无双罚、N/A/ERROR 语义。
- 建立九页面试汇报与只读 HTML 的同源生成流程。
- 实现确定性 RunSupervisor、Oracle Registry/Composite、四场景降级和本地/API/Worker 接入层。
- 实现 PPTX 安全预检、对象树/OOXML 解析、13项本体指标及三类专项 Oracle。
- 实现可信事实快照、反馈/edit-diff、主动采样与双人审批参数候选；首期无自动发布入口。
- 42项测试与四场景 Smoke 通过，生产人工金标和 Shadow 门槛保持 pending。
- Node 运行环境恢复后，React 复核台、FastAPI 同步/异步 API 与九页面试 PPT 均完成构建和本地验收。

> 当前为可运行 MVP `0.1.0`，尚无生产 Release ID；不得将 MVP 测试通过表述为生产质量门禁通过。
