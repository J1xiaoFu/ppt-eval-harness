# 单机发布最终验证

日期：2026-08-28

## 验证对象

- v8.3 四场景 bundled Profile
- CRITICAL 规则页预算外 VLM 兜底
- Attention-first 服务端审计投影
- 服务端审计队列、页图、artifact、完整审计与 ReviewEvent
- React/Vite 审计台
- 精简后的单服务 Docker 镜像
- `archive/v8.3-pre-release` 历史 worktree 可检出性

## 自动门禁（既有发布检查点）

下表是此前单机发布检查点的已存记录，不是本轮 demo/上传与冷启动修改后的最终数字。
本轮最终结果待合并后全量门禁确认，不在此预写。

| 门禁 | 结果 |
|---|---|
| 全量 pytest | 157 passed |
| dependency-free runner | 157 passed |
| Ruff（src/tests/scripts） | passed |
| 全部 41 个 source module strict mypy | passed |
| React TypeScript + Vite build | passed |
| OpenAPI YAML parse | passed |
| `docker compose config --quiet` | passed |
| `git diff --check` | passed |

FastAPI TestClient 产生一条上游 `StarletteDeprecationWarning`，不影响功能或测试结论；项目代码
无 warning/error。

## 本轮冷启动增量（最终门禁）

本轮从远端 `main` 创建隔离 clone，分开核对 Git 完整性、README 冷启动合同、Compose/OpenAPI、
默认无 Key 降级与 demo 可移植性。本轮增量门禁要求：

- 四份 tracked manifest 使用相对 JSON 所在目录的 `./...` 路径，在任意 clone 位置都能直接加载并运行；
- 默认 Demo generator 只向 ignored `var/demo-generated/` 写入，执行前后 tracked demo hash 和
  `git status` 不变；
- 个性化 host-path 扫描覆盖示例、研发文档与外部基线文本制品；本项目生成的路径必须相对化/
  脱敏，只允许精确标注的 vendored upstream 硬编码路径作为不可复现证据保留。

最终结果：

| 门禁 | 结果 |
|---|---|
| 全量 pytest | `177 passed`；仅一条既有 Starlette/httpx2 迁移提示 |
| dependency-free runner | `177 passed, 0 failed` |
| 四份 tracked case | 全部从 manifest-relative 路径加载并完成运行 |
| Demo generator | 只允许 ignored `var/` 子目录；外部绝对路径与 `../` 逃逸被拒绝 |
| 路径卫生 | 个性化用户目录、开发机根、编码本地 URL 残留为 0 |
| 制品格式 | 4 份 Demo JSON 与 4 份 reproduction JSON 解析通过；SlidesBench PowerShell 语法通过 |
| 代码与 UI | Ruff、触及文件 strict mypy、TypeScript/Vite、Compose 与 diff check 通过 |

另从仓库外工作目录分别运行 tracked `case_ready_made.json` 与 generator 产出的相同 case，
两者均得到合法 `REVIEW/DEGRADED` 结果；生成 JSON 保持 `./aurora_demo.pptx` 和
`./source.txt`，执行前后 tracked Git 状态不变。测试数据随后从精确的 ignored 目录清理。

Docker `--pull` 尝试曾被外部 registry DNS/代理可达性阻断；该结果只证明当时无法刷新远程
基础层，不证明项目代码构建失败。随后使用已解析的固定 digest 基础层完成 no-cache 构建，
说明 Dockerfile/依赖和 UI 构建链在可用基础层上成立。隔离容器随后完成健康检查、浏览器上传、
4 页渲染、57 条 AtomicObservation、完整制品 hash、`REQUEST_MORE_EVIDENCE` 人审事件与重启恢复；
进程内 Job 在重启后返回 404，符合已声明的非持久 Job 合同。

## 包与容器

- wheel 已验证包含四份 `ppt_eval/profiles/*_v8.json`。
- 从仓库外目录直接从 wheel 导入，`ready_made` 返回 `finished-deck-v8 / 8.3`。
- Docker 多阶段构建成功，前端 production bundle 不包含 demo 或 benchmark 字段。
- Compose 只启动 `api`，旧 Redis/PostgreSQL/MinIO orphan 已移除。
- 容器健康状态为 `healthy`；`/healthz`、`/review/` 和静态 JS 均返回 200。
- 容器内旧整体 Oracle 与 Qwen 3.7/Plus 符号均不存在；v7 Profile 与旧 `APPROVE`
  写入探针均返回 422。

## 端到端烟测

在不配置任何远程模型 Key 的条件下，对容器内四页示例 PPT 运行一次完整评测：

- 生成 EvalReport、RunManifest 和审计链事件；
- 保存 source PPTX、AtomicObservation 和 render manifest；
- render manifest 为 1.1，逐页 SHA-256 校验通过；
- 审计详情按需返回页图与 11 个系统 AttentionIssue；
- 详情首屏不含全量 Matrix，也不泄露主机或容器绝对路径；
- 相同 Idempotency-Key 重试两次只产生一条 ReviewEvent；
- 请求补证后任务状态为 `NEEDS_EVIDENCE`。

该 smoke run 只验证运行合同，不用于指标校准或性能结论。
