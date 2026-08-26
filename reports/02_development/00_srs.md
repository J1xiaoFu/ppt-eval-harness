# 软件需求规格说明（SRS）

## 1. 业务目标

用统一 Agentic Harness 接受四类 PPT 评测任务，稳定地产生硬门禁、分维度分数、证据、置信度、
覆盖率、成本与复核建议，并将可治理反馈写入数据飞轮。

## 2. 参与者

- 生成服务：提交 `EvalCase`，异步获取报告。
- 评测运营：查看证据、复核/仲裁、提出参数候选。
- Profile 管理员：审批权重、阈值和模型路由版本。
- 数据管理员：管理许可、脱敏、保留和删除血缘。
- 审计员/面试汇报者：按 Run ID 回放并导出报告。

## 3. 功能需求

| ID | 需求 | 验收摘要 |
|---|---|---|
| REQ-BASE-001 | 所有场景强制执行本体质量评测 | DAG 中必含 `BaselinePptQualityOracle`，Profile 不可删除 |
| REQ-SCENE-001 | 支持文字/总结/多模态/成品四场景 | 选择确定性的场景 Profile |
| REQ-DEGRADE-001 | 专项缺失/失败仍输出本体结果 | `BASE_ONLY/DEGRADED + REVIEW`，`full_score=null` |
| REQ-SCORE-001 | 外乘内加 PPT-PDMS | 无双罚、N/A 重归一、ERROR 隔离 |
| REQ-ORACLE-001 | Oracle 可替换且契约稳定 | `describe/supports/evaluate`，严格 Schema |
| REQ-AUDIT-001 | 全链路可回放 | Manifest 与 append-only 事件齐全 |
| REQ-REVIEW-001 | 支持人工复核和仲裁 | 不覆盖机器结果，生成独立事件 |
| REQ-FLYWHEEL-001 | 生成参数候选但禁止自动发布 | 候选必须冻结回放、审批、Shadow |
| REQ-API-001 | 异步 API、CLI、批量与审计导出 | 幂等提交、状态查询、JSON/HTML/PPT 导出 |

## 4. 输入输出契约

`EvalCase`：`case_id/scenario/request/audience/source_refs/assets/presentation/privacy/profile_id`。

`OracleResult`：`oracle_id/execution_status/metric_status/raw/calibrated/confidence/severity/evidence/
defect_id/version/latency_ms/cost`。

`EvalReport`：`run_id/completeness/decision/base_score/full_score/dimensions/coverage/gates/results/
review_reasons/manifest`。

约束：`ERROR` 只用于执行失败；质量失败使用 `FAIL`。`BASE_ONLY` 时 `full_score` 必须为空。

## 5. 状态语义

| 维度 | 枚举 |
|---|---|
| Supervisor | `OBSERVE/PLAN/ACT/VERIFY/FINALIZE/REVIEW/FAILED` |
| 完整度 | `FULL/DEGRADED/BASE_ONLY/UNASSESSABLE` |
| 决策 | `PASS/FAIL/REVIEW/ERROR` |
| Oracle 执行 | `SUCCESS/ERROR/TIMEOUT/SKIPPED` |
| 指标 | `PASS/FAIL/SCORED/NA` |

若文件不可解析，技术 Oracle 记录可交付性失败；其他指标不可计算，完整度为 `UNASSESSABLE`。
若基础完成但任一所需专项错误，完整度至少 `DEGRADED`，严重缺证据时为 `BASE_ONLY`，决策 `REVIEW`。

## 6. 非功能需求

- 可靠性：任务幂等；重试不重复写最终事件；Outbox 保证元数据与事件一致。
- 性能：20 页快速层 P95≤30 秒，完整层 P95≤5 分钟（目标，待压测）。
- 安全：沙箱解包、大小/关系/宏/外链限制，页面内容永不进入系统指令。
- 可解释：每个处罚可定位到页/对象/bbox/来源，且携带版本与置信度。
- 可维护：领域层不依赖 FastAPI/Celery/模型 SDK；Adapter 可替换。
- 可观测：trace/run/node 三层 ID，结构化日志、指标、成本和状态时长。

## 7. 不做什么

首期不自动修改 PPT，不自动发布 Profile，不用联网失败判低分，不保证 PDF 输入的结构/可编辑性指标，
不同 Profile 总分不作排名。

