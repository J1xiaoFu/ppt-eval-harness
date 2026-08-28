# 0.8.6 预发布稳定性验证

日期：2026-08-28

## 结论

初次 0.8.5 冷克隆已证明 GitHub 仓库可在新目录完成构建、启动、批次评测、制品审计、人审幂等与
重启持久性。本轮将实验中暴露的跨 Composite 重复卡、plain runner 假 PASS/加载失败与依赖漂移
收敛到 0.8.6。

## 专项验证

- 冷实验原始 Run 重放：两张 `COLOR_CONTRAST` 主卡合并为一张，总主卡由 4 降为 3。
- 合并卡 owner 为 `VISUAL_LAYOUT_READABILITY`，`detail_count=2`，consensus 仍为
  `INSUFFICIENT`，sources 仍为 `[REDUCER, RULE]`，两 family 与两 metric lineage 均保留。
- 反转 result 顺序后完整 projection 不变；前六页相同但第七页不同的 issue ID 不再碰撞。
- 完整开发环境中 pytest 与 plain runner 均为 `207 passed`。
- 无 pytest/httpx 的精简镜像中，plain runner 为 `186 passed, 21 skipped, 0 failed`；可选 HTTP 测试和
  需要 Git metadata 的仓库扫描以 SKIP 呈现，不再中止或冒充 PASS。
- `pnpm@11.24.0` 与精确直接依赖通过 frozen lock 安装和 UI build。
- 固定 digest 的双阶段 Docker 无缓存构建通过，constraints 全部命中预期版本，`pip check`
  返回 `No broken requirements found`。
- Dockerfile check、Compose config、OpenAPI YAML、Ruff、strict mypy、Vite 与 Git diff check 均通过。

Evaluation Profile 仍为 `8.3 / PRE_RESEARCH`，Composite 仍为 `8.3.0`，EvalReport/Audit schema 仍为 `1.0`，
HTTP namespace 仍为 `/v1`。
