# 需求追踪矩阵

| Requirement | ADR | 实现/Oracle | Test | Experiment | Release |
|---|---|---|---|---|---|
| REQ-BASE-001 | ADR-002 | ORC-BASE-001 / ProfileCompiler | `test_profile_compiler_*`, `test_all_four_scenes_*` | EXP-MVP-FOUR-SCENES-001 | REL-PENDING |
| REQ-DEGRADE-001 | ADR-003 | RunSupervisor / DecisionPolicy | `test_runtime_scene_degradation_*` | EXP-MVP-FOUR-SCENES-001 | REL-PENDING |
| REQ-SCORE-001 | ADR-004 | ORC-AGG-001 | TST-PROP-001..005 | EXP-PDMS-ABLATION | REL-PENDING |
| REQ-ORACLE-001 | ADR-007 | Oracle Protocol / Registry | TST-CONTRACT-001 | EXP-ADAPTER-REPLAY | REL-PENDING |
| REQ-AUDIT-001 | ADR-006 | AuditLog / Outbox / Manifest | `test_review_and_run_export_*` | AUDIT-MVP-001 | REL-PENDING |
| REQ-REVIEW-001 | ADR-006 | ReviewService | `test_review_and_run_export_*` | EXP-HUMAN-001 | REL-PENDING |
| REQ-FLYWHEEL-001 | ADR-008 | CandidatePipeline | `test_flywheel.py` | EXP-SHADOW-001 | REL-PENDING |
| REQ-API-001 | ADR-010 | EvaluationService/API/CLI | TST-API-001 / TST-API-BATCH-001 | EXP-LOAD-001 | REL-0.8.5 |

## 完整性规则

1. 每个生产 Requirement 必须至少关联一个 ADR、一个测试和一个发布条目。
2. `REL-PENDING` 允许在开发期存在，但发布门禁不允许。
3. 任何失败测试不得从矩阵删除；通过新的 Test ID 或 superseding event 记录修复。
4. 自动生成的 HTML/PPT 只读取本矩阵与审计 JSON，不手抄状态数字。
