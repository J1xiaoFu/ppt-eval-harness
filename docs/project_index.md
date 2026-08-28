# PPT Eval 文档索引

## 从这里开始

- [项目 README](../README.md)：安装、启动、提交 PPT、CLI、测试与当前边界。
- [v8 原子评测方法](v8_atomic_evaluation_method.md)：Observation、Reducer、评分和训练准入。
- [服务端人工审计平台](review_platform.md)：浏览器上传、Job 进度、AttentionIssue 和 ReviewEvent 闭环。
- [OpenAPI](openapi.yaml)：multipart 评测、进程内 Job、精简审计 DTO 与人审写入合同。

## 运行与审计

- [模型审计 Provider 合同](model_audit_provider_contract.md)
- [数据许可计划](../reports/01_research/04_data_license_plan.md)

## 系统设计

- [SRS](../reports/02_development/00_srs.md)
- [系统设计](../reports/02_development/01_system_design.md)
- [架构决策](../reports/02_development/02_architecture_decisions.md)
- [威胁模型、FMEA 与 SLO](../reports/02_development/03_threat_model_fmea_slo.md)
- [需求追踪矩阵](../reports/02_development/04_traceability_matrix.md)
- [Oracle、模型与数据卡](../reports/02_development/05_oracle_model_data_cards.md)
- [单机发布收敛记录](../reports/02_development/07_single_node_release.md)

## 可审计研发过程

- [调研阶段索引](../reports/01_research/README.md)
- [开发阶段索引](../reports/02_development/README.md)
- [评测阶段索引](../reports/03_evaluation/README.md)
- [实验预注册](../reports/03_evaluation/00_preregistration.md)
- [测试计划](../reports/03_evaluation/01_test_plan.md)
- [MVP 验证](../reports/03_evaluation/04_mvp_verification.md)
- [v8.3 三主题真实切片](../reports/03_evaluation/13_v83_three_topic_replay.md)
- [单机发布最终验证](../reports/03_evaluation/14_single_node_release_validation.md)

## 历史复现

v1–v7 Profile、旧 Oracle 入口和同期方法文档保存在不可变 Git tag
`archive/v8.3-pre-release`。需要复现实验时请在独立 worktree checkout 该 tag，不要把旧合同
重新合入当前 `main`。

外部基线的代码、许可证与 provenance 位于 `third_party/`，不参与默认运行时。
