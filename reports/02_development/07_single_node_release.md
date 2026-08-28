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

## 冷启动与路径可移植性补充

本轮从远端 `main` 建立隔离 clone 做静态发布审查，不依赖原工作区的 Git 状态。检查暴露出
tracked demo manifest 保存旧机器绝对路径的可移植性问题，因此将发布合同收紧为：

- 四份 tracked case 使用 manifest-relative `./...` 路径，clone 后可直接加载/运行；
- Demo generator 只生成 ignored `var/` 下的变体，不覆盖 `examples/demo/`，也不应改变
  tracked 文件 hash 或 Git 状态；
- 发布路径扫描拒绝项目自有代码、文档和新 provenance 中的个性化宿主机路径；只对精确标注的
  vendored upstream 硬编码路径放行，以保留“上游为何不可复现”的原始证据。

对应专项验证已纳入发布门禁；最终工作区的 pytest 与 dependency-free runner 均为
`177 passed`，路径卫生、生成器 containment、JSON、PowerShell、Ruff 与 strict mypy 检查均通过。

Docker 冷启动审计也区分了代码失败与外部环境：要求重新拉取的 `--pull` 流程曾受 registry
DNS/代理可达性影响，该现象记为外部依赖问题，不伪装成代码编译失败。使用已解析的固定
digest 基础层进行 no-cache 构建已成功，并完成容器健康、浏览器上传、审计事件和重启持久性验证。

## 明确不声称的能力

- v8.3 权重仍处于 PRE_RESEARCH，不代表生产校准完成。
- 当前已有本机浏览器上传闭环，但没有认证、RBAC、多审计员 lease、持久 Job 或远端对象存储。
- 进程内异步 Job 在 API 重启后不会恢复。
- 完整 Web UI 以 Docker 为交付入口；普通 wheel 保证 CLI/API 与 bundled Profile。
