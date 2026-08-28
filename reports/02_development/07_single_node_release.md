# 单机审计平台发布收敛记录

日期：2026-08-28

## 目标

把预研阶段累积的 Harness、v8 Oracle/Profile 和人工审计界面整理为一条可以直接运行、可以
解释开发过程、且不会用未接线基础设施冒充生产能力的发布路径。

## 运行路径收敛

发布默认只保留经过端到端验证的单机 composition root：

```text
CLI / FastAPI / React
→ LocalEvaluationRuntime
→ JsonRunRepository + LocalArtifactStore + JsonlAuditLog
→ bundled v8.3 Profile
```

删除了未被 API 或 Runtime 引用的 Celery worker、PostgreSQL repository、S3 adapter，以及
Compose 中未产生业务效果的 Redis/PostgreSQL/MinIO 服务。异步 API 明确采用进程内
`ThreadPoolExecutor`，不再暗示具备持久化多节点 Job。

## Profile 发布修复

四份默认 v8.3 Profile 从仓库相对路径迁入 `ppt_eval.profiles` package data，并由
`importlib.resources` 加载。wheel 在任意工作目录都能得到相同 v8.3 默认合同；显式 root
缺失会报错，不再静默回退旧权重。v1–v7、旧 Oracle 入口与过期协议由不可变 tag
`archive/v8.3-pre-release` 保存，不再进入 main 的可执行表面。

主线 Profile loader 与 `LocalEvaluationRuntime` 都只接受精确的 `8.3` 写合同和非空 DAG；旧
`required_oracles` / `optional_oracles` 协议会明确报错。Qwen 只保留 `qwen3.8-flash` 单一主
Provider，跨 Provider 复核由 `glm-5.3-flash` 承担，不再公开 qwen3.7/Plus 兼容符号。

## 审计平台

- 生产队列不读取 human rank、Spearman、pairwise 或 benchmark comparison。
- AttentionIssue 只由 EvalReport、Observation、Gate、模型路由和训练准入产生。
- 详情首屏保持精简；Matrix、模型路由和 Manifest 通过 `/audit` 按需加载。
- 原 PPTX、Observation、render manifest 与逐页 hash 都绑定到 RunManifest。
- 人工判断追加为幂等 ReviewEvent，不覆盖机器报告。
- 新写入只接受确认系统、覆盖结论、请求补证三种运行级 verdict；历史 APPROVE/REJECT 仅只读。
- Repository 存储边界再次校验当前 verdict，防止绕过 API/CLI 写入旧协议。

## 发布文档

README 已改写为用户与开发者的唯一入口，覆盖 Docker、容器路径、模型配置、架构、审计工作流、
CLI、API、数据目录、安全、历史回放和当前边界。过时的新员工交接手册与面试演示脚本已删除；
研发 provenance 和外部基线快照继续保留；可执行历史合同迁入 archive tag。

## 明确不声称的能力

- v8.3 权重仍处于 PRE_RESEARCH，不代表生产校准完成。
- 当前没有认证、RBAC、浏览器上传、多审计员 lease 或远端对象存储。
- 进程内异步 Job 在 API 重启后不会恢复。
- 完整 Web UI 以 Docker 为交付入口；普通 wheel 保证 CLI/API 与 bundled Profile。
