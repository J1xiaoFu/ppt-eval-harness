# Changelog

本文档只记录产品/软件发布版本。Evaluation Profile、Oracle/Prompt 和持久化 schema
有独立版本，不会随产品版本自动变更。

## 0.8.7 - 2026-09-03

- 新增 `release/version-matrix.json` 作为产品与评测合同版本的机器可读声明。
- 新增默认只读的 `scripts/release_version.py --check`，统一检查 Python、UI、
  OpenAPI、README、四份默认 Profile 与测试版本出口；`--write` 仅同步产品版本。
- 统一 CLI、FastAPI、审计台和 OpenAPI 对外显示的产品版本为 `0.8.7`。
- 渲染缓存改为由 PPTX 内容、渲染器、字体指纹和渲染策略共同寻址；同一文件换路径或
  case ID 后不再重复渲染，旧缓存只供原运行显式读取。
- 模型 usage 新增图片、缓存创建/命中、请求字节与费用可观测性，并贯穿重试和跨模型路由。
- 新增默认关闭的 Qwen 视觉前缀缓存 wire contract；关闭时保持 Profile 8.3 请求不变。
- Base64 仍是零配置默认；显式配置公网 HTTPS 与 HMAC 密钥后，可让 Qwen/GLM 通过
  短期签名 URL 复用已注册页图，原始 PPTX 永不进入该路由。

Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为 `8.3.0`，Atomic Observation
仍为 `2.1.0`，Grounded VLM/Selection 合同仍为 `2.x`，EvalReport/Audit schema
仍为 `1.0`，HTTP namespace 仍为 `/v1`，Attention policy 仍为
`audit-attention@0.8.6`。

## 0.8.6 - 2026-08-28

- 按“具体 `semantic_code` + 完整受影响页”合并跨 Composite 重复主卡，同一页的
  `COLOR_CONTRAST` 不再同时以交付完整性和视觉可读性各呈现一次。
- Attention issue ID 改为绑定完整页集与 primary owner，策略版本升为
  `audit-attention@0.8.6`，完整 lineage 仍保留所有贡献 family/metric/candidate。
- `scripts/run_tests.py` 增加严格的内建 pytest facade，在没有 pytest 的精简环境也能执行
  plain-assert 测试，并将缺失的可选 HTTP 测试依赖明确记为 SKIP。
- Docker 基础镜像锁定 digest，pnpm 与所有 Node 直接依赖锁定精确版本，Linux/Python 3.11
  运行依赖由 `constraints/docker-py311-linux.txt` 锁定并通过 `pip check`。

Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为 `8.3.0`，EvalReport/Audit schema 仍为
`1.0`，HTTP namespace 仍为 `/v1`。

## 0.8.5 - 2026-08-28

- 新增 `POST /v1/evaluation-batches/upload` 与 `GET /v1/evaluation-batches/{batch_id}`。
- 一个批次可原子接收 1–16 份 `ready_made` PPTX，各项在同一进程内 JobManager
  中独立执行并使用现有并发上限。
- 新增批次级有序幂等、队列容量原子预留、单项失败隔离、终态快照与有界保留。
- 批量入口复用单任务的文件名、MIME、ZIP/OOXML、Origin、请求体与工作区清理边界。

本次仅升级产品版本。Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为
`8.3.0`，EvalReport/Audit schema 仍为 `1.0`，Attention 投影策略仍为 `audit-attention@0.8.4`。

## 0.8.4 - 2026-08-28

产品从内部 `0.8.3` 预研基线进入 `0.8.4`，并将此前的 `0.1.0` 打包占位值收敛为统一
产品版本出口。

- 将人审主区从 Oracle/硬门/原子规则列表收敛为最多 8 个 Composite/多模态语义问题。
- 增加中文问题标题、多源共识、聚焦页跳转、判断依据折叠与分状态空结果。
- 已恢复的 Provider 重试和未升级的原子规则只进入完整审计，不占用人审主注意力。
- 完整 Observation、Gate、Reducer、模型路由和 Manifest 仍保留 hash 校验与下载入口。
- 保留浏览器上传、进程内 Job、Attention-first 人审与不可变 ReviewEvent 闭环。
- 加强 multipart/Origin、OOXML、CAS、run-bound 输入制品与路径卫生边界。
- 使 tracked Demo 和外部基线 provenance 可移植，并完成隔离 clone 冷启动验证。

兼容性承诺：

- 默认四场景 Evaluation Profile 仍为 `8.3`，生命周期仍为 `PRE_RESEARCH`。
- `V8_QUALITY_VERSION` 仍为 `8.3.0`，Atomic/VLM/Prompt 版本不因本次发布自动变更。
- EvalReport、RunManifest 与 AuditEvent schema 仍为 `1.0`，HTTP 命名空间仍为 `/v1`。
- 不要求历史 run 具有新的必填版本字段；`profile_version=8.3` / `schema_version=1.0`
  的已存报告继续可读。
