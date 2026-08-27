# v8.3 定向硬门与栅格文字观察验证

## 变更范围

- 可争议硬门的 MAJOR/CRITICAL 候选页优先进入对应 VLM criterion 的四页样本。
- 硬门确认要求同一候选页出现同构 defect code；页码与缺陷代码不能跨 evidence 拼接。
- 全栅格 deck 新增 `raster_content_structure`（最多 4 页）和
  `raster_language_consistency`（最多 8 页）两个页级 observation-only VLM 节点。
- 未抽样页显式记录 N/A；Reducer 同时报告整 deck 页覆盖率与完整 bounded-sample 下限。
- 已知训练轨分数 `<60` 先 REJECT，再处理 content evidence missing。
- 无文字的栅格页对不再由 lexical duplicate/transition proxy 评分。

## 真实回放

| Case | v8.2 | v8.3 | 关键证据 |
|---|---|---|---|
| Kimi-Banana | 79.235 / REVIEW / DEGRADED | 84.940 / PASS / FULL | content structure 0.959（4/15 页，importance observability 0.279）；language 0.850（8/15 页，0.541）；visual track 80.60 / TRAIN |
| Kimi-Standard | 70.941 / REVIEW / DEGRADED | 69.355 / REVIEW / FULL | composition 样本 `[1,18,21,26]`；page 21 规则 CRITICAL 未获同页同缺陷确认，gate verdict `REJECTED`，functional gate PASS |

Kimi-Banana 最终 Run：`run-29ac7a2d-fdf4-48d9-b6d9-bd2e11019a8c`，Git
`00c5dbe069d49692699f3d78bb55873c5d12a543`。Kimi-Standard 定向 Run：
`run-fa70da12-2bf1-4d3d-a2c9-7c432da5f163`，Git
`49e7f572ee2716efcd9c210261769e2d4dbf0752`；其后的提交只影响 raster-only
missingness、文档和栅格 lexical proxy，不改变该 editable case 的 gate 路径。

两条运行审计链均有效；完整 observation artifact 的实际 SHA、Report 引用与 Manifest
`artifact_hashes.atomic_observations` 三方一致。模型 usage 分别为 249,249 与 161,475 tokens；
Provider 未返回可验证货币费用，因此 `cost_known=false`，不解释为免费。

## 回归门禁

- pytest：293 passed。
- dependency-free runner：279 passed。
- 项目 Ruff：通过。
- 触及源文件 strict mypy：通过。
- Docker Compose config 与 `git diff --check`：通过。

本验证只证明三条异常链已闭合，不使用两例结果拟合权重或阈值。
