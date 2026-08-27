# qwen3.8 主线 / GLM-5.3-Flash fallback 固定 Prompt 烟测

日期：2026-08-27

## 实验约束

- 数据：Slides-Align `market_analysis` 真实切片中的 `Kimi-Banana`，15 页。
- Profile：`finished-deck-v8@8.1`。
- 主模型：DashScope `qwen3.8-flash`。
- fallback：BigModel `glm-5.3-flash`。
- 六个视觉 criterion 的 PromptSpec、权重、Reducer、页面采样和升级阈值均未修改。
- 本轮不使用结果拟合 Prompt、权重或阈值。

## 端到端主线结果

`qwen3.8-flash` 的六个独立 criterion 均返回合法 `SCORED`，没有 ERROR：

| criterion | score | token |
|---|---:|---:|
| composition/layout | 0.80 | 14,236 |
| typography/legibility | 0.96 | 14,352 |
| color/contrast | 0.93 | 13,116 |
| imagery/data visualization | 0.86 | 28,749 |
| cross-slide consistency | 0.72 | 26,256 |
| render integrity | 0.98 | 13,971 |

合计 110,680 token。Harness base score 为 `40.300817`，Decision 为 `REVIEW`，Coverage
为 `DEGRADED`。该 deck 是整页栅格化交付，低分/降级仍主要来自可编辑性和内容结构证据，
不能把六个视觉分直接解释为整份 PPT 的交付总质量。

这次主线结果没有低于置信阈值，也没有触发同构念规则冲突，因此端到端 run 没有自然调用
fallback。这是正确的路由结果，不能据此声称 GLM 路径已经由该 run 覆盖。

与本地旧 v8 七份报告中的同一 deck 作方向性对照：composition `0.83→0.80`、typography
`0.85→0.96`、color `0.90→0.93`、imagery `0.86→0.86`、cross-slide
`0.65→0.72`、render `1.00→0.98`；base score `39.729389→40.300817`。这个对照不是严格
A/B：旧报告早于最后两项确定性规则补丁，而且旧 cross-slide 已由 qwen3.8 fallback 产生。
同为 qwen3.8 的 cross-slide 两次仍相差 0.07，也直接说明单次模型波动不能解释为能力提升。

## GLM fallback 合同烟测

为单独验证 fallback 的真实多模态合同，使用同一 deck、同一个现有
`ppt-vlm-grounded-composition-layout-audit@2.0.0` Prompt 直接执行一个 GLM 节点：

- Prompt SHA-256：`6cca0faef22f5a99954e1593e5afbcc346163d96ff92b3e152cfe0481d58e682`
- actual provider：`zhipu-bigmodel-openai-compatible`
- actual model：`glm-5.3-flash`
- status：`SCORED`
- score：`0.8825`
- confidence：`0.85`
- grounded evidence：4 条
- usage：28,307 input / 3,689 output / 31,996 total token

这次真实响应证明 GLM-5.3-Flash 接受当前的多图 Data URL、system/user messages、强制
thinking 和 `response_format={"type":"json_object"}` 组合。Provider 未返回货币费用，
因此 Harness 中的 `cost=0.0` 只能解释为“费用未知/未返回”，不能解释为免费。

## 审计产物

- HTML：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smoke/index.html`
- 比较 JSON：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smoke/comparison.json`
- 完整 run：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smoke/kimi_banana_rank_1.report.json`
- run id：`run-4659c605-8270-482f-9cd3-d5a1287f6cef`
- 运行级哈希链：有效

## 限制

- 只有一个 deck，不能计算 Spearman 或 pairwise accuracy。
- GLM 是同 Prompt 单节点合同烟测，不是自然触发的 Qwen → GLM fallback 对照。
- 运行时工作区基于 `a8065fc` 加本轮未提交修改；在代码提交后应重放一次，才能形成由最终
  Git SHA 完整标识的不可变实验证据。
- 尚未测量两模型重复运行方差，也没有比较 criterion 级人类金标。

官方接口依据：

- [GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)
- [OpenAI API 兼容](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)
- [Chat Completion API](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8)
