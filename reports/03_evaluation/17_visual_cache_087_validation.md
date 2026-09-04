# 0.8.7 视觉资产复用与可观测性验证

日期：2026-09-04

## 结论

0.8.7 只改变运行时基础设施，默认 Evaluation Profile 仍为 `8.3`。在
`Base64 + Qwen context cache=false` 的默认路径上，固定 Provider fixture 的请求结构与
JSON 字节与 0.8.6 一致；评分、Decision、Evidence 与主 Attention 语义不变。

新增的运行遥测会让完整 audit JSON 产生兼容扩展，因此不宣称完整审计文件
字节级不变。旧 `1.0` 报告仍可读。

## 专项验证

- 同一 PPTX 更换 case ID 或本地路径后命中同一渲染缓存。
- 缓存键绑定 PPTX SHA-256、renderer/version、font fingerprint 和 render policy。
- 渲染期间源 PPTX 变化会中止运行，不写错绑 cache；缓存命中返回前也再验证源文件。
- Linux/Docker 优先使用 `fc-list` 的排序清单生成字体指纹；不可观测时禁止跨运行复用。
- Signed URL 仅发布受控的 slide/atlas/crop 图片，不发布原始 PPTX。
- Signed URL 端点响应使用读取后再校验的有界字节快照，不在校验后延迟重开路径；
  校验与响应之间替换文件时 fail closed。
- 外部预渲染图在 Signed URL 模式下先按内容哈希导入受控 CAS；路径逃逸、
  symlink、伪装媒体类型、超限、声明 hash 不符和导入后篡改均 fail closed。
- 可选 usage 字段非法时 fail open，不使合法模型结果变为 ERROR；任一 attempt 缺失时
  不输出伪累加的 optional token 总量。
- Oracle 和 route attempt 完整审计保留实际 `image_transport_mode` 与
  `context_cache_enabled`，不新增主 Attention 卡。

## 门禁结果

- pytest：`245 passed`
- dependency-free runner（完整开发环境）：`245 passed, 0 skipped, 0 failed`
- Ruff：通过
- strict mypy（本轮触及源码）：通过
- UI TypeScript/Vite：通过
- 版本同步检查：通过
- `docker compose config --quiet`：通过
- `git diff --check`：通过
- 审计链、旧报告读取、签名 URL 和 CAS 安全专项：通过

## 隔离 GitHub clone 与 Docker 闭环

在一个全新临时目录中从 GitHub 直接 clone
`origin/codex/visual-cache-087`，未复用原工作区、虚拟环境、`api/`、`.env` 或
`var/` 数据。clone 后 HEAD 精确为
`4165d1e48d86c436c96128d43b7e7869c3d67c91`，且分支与远端跟踪关系正常。

按 README 从零完成了以下链路：

1. 新建 Python 3.11 虚拟环境并执行 `pip install -e ".[dev,api]"`；
2. 确认 distribution、Python module、CLI 和版本矩阵均为 `0.8.7`；
3. 执行全量 pytest、dependency-free runner、Ruff、UI 锁文件安装与 Vite 生产构建；
4. 使用无 Key、Base64、Qwen cache disabled 的跟踪配置校验 Compose；
5. 执行 `docker compose build api`，完成锁定基础镜像、Linux 依赖、UI、wheel 和
   `pip check` 的冷构建；
6. 将镜像映射到独立 localhost 端口，实际验证 `/healthz`、`/review/` 和
   `/docs` 均返回 HTTP 200，服务版本为 `0.8.7`，审计链有效；
7. 仅使用仓库跟踪的 demo PPTX 发起一次无 Key 评测，成功生成 4 页可渲染的
   Profile 8.3 审计任务。结果按合同为 `DEGRADED/REVIEW`，模型指标为 N/A，
   但不存在运行时 ERROR；审计任务、页图和完整链均可读；
8. 在精简生产镜像内再执行 dependency-free runner，得到
   `219 passed, 26 explicit skips, 0 failed`。skip 均由生产镜像未安装测试专用
   `httpx` 引起，与 README 声明的显式降级合同一致。

容器最终正常停止并返回退出码 0；隔离 clone、生成的本地审计制品、
镜像摘要和已停止容器保留用于发布审计。该验证证明 0.8.7 可以在不依赖
开发机私有数据或模型密钥的情况下，从 GitHub clone 冷启动到可审计结果。
