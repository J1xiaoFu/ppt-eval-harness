# 0.8.7 视觉资产复用与可观测性验证

日期：2026-09-03

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
- dependency-free runner：`220 passed, 25 skipped, 0 failed`
- Ruff：通过
- strict mypy（本轮触及源码）：通过
- UI TypeScript/Vite：通过
- 版本同步检查：通过
- `docker compose config --quiet`：通过
- `git diff --check`：通过
- 审计链、旧报告读取、签名 URL 和 CAS 安全专项：通过

## 宿主环境阻断

Docker CLI 已安装，但 Docker Desktop 引擎未能启动；宿主仍保留
`AppData/Local/Docker/run/sailor-ingest.sock` 的 0 字节陈旧重解析点。当前执行策略
拒绝清理该工作区外路径，因此 `docker compose build api` 和隔离 clone 容器冷启动尚未
重跑。在该宿主问题解决前，分支可作为候选检查点，不应标记正式 `v0.8.7`
发布标签。
