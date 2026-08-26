# Qwen v3 真实 PPT 人评对照切片

## 结论

在 Slides-Align 固定 revision `2f50ac6674a506acb245275e58c8a452c00e6a14`
的同一 `market_analysis` 主题上，三份真实 PPT 的 Qwen v3 描述性结果为：

- Spearman（v3 总分 vs 人评顺序）：`0.50`
- 两两顺序准确率：`2/3 = 66.7%`
- 三例均进入 `REVIEW`，三例均触发 Plus，三例均因 Plus 低置信保留人工复核。

这个结果低于历史 v2 同切片的 `1.00 / 3/3`，不支持“加入 Qwen 就提高了与人评的一致性”。它说明当前
Flash Prompt、权重和复核线尚未校准，v3 仅适合 `PRE_RESEARCH`，不能当作生产质量结论。

## 方法

- 标签：Slides-Align 同主题人工相对排名，数字越小越好；这是序数标签，不是等距绝对分。
- 输入：固定哈希的上游 PPTX 和上游逐页 PNG，不使用本地合成 deck，也不重新渲染。
- Profile：`finished-deck-v3@3.0`。Flash 内容权重 `.08`，Flash 视觉权重 `.12`，
  `template_residue` 权重 `.08`。
- 路由：`qwen3.7-flash -> qwen3.7-plus -> human`。Plus 结果是 `DIAGNOSTIC`，不二次进入分数公式。
- VLM：每份 deck 最多上传 12 页，首尾必含、其余确定性均匀采样；完整对象树仍随请求提供。
- Wire：`enable_thinking=true`、`temperature=0`、`seed=0`、`max_tokens=4096`。
- 传输上限：Flash 120 秒，Plus 240 秒；Gamma Plus VLM 在 120 秒下曾超时，提高后成功。

## 结果

| 产品 | 人评 rank | v2 分 | v3 分 | Flash LLM | Flash VLM | Plus LLM | Plus VLM | v3 顺序 | 处置 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Skywork-Banana | 2/8 | 88.92 | 84.00 | .65 / .90 | .45 / .90 | .35 / .60 | .45 / .85 | 1/3 | HUMAN REVIEW |
| Quake | 3/8 | 86.13 | 77.91 | .20 / .90 | .85 / .90 | .20 / .60 | .62 / .78 | 3/3 | HUMAN REVIEW |
| Gamma | 6/8 | 79.49 | 83.61 | .42 / .88 | .85 / .90 | .85 / .75 | .82 / .88 | 2/3 | HUMAN REVIEW |

表中模型列为 `score / confidence`。三例合计记录 `316,432` tokens。DashScope
Chat Completions 响应未返回货币费用，因此 Manifest 的 `cost=0` 表示“费用未知”，
不表示免费。

## 关键观察

1. **Gamma 被系统排在 Quake 之前。** Flash VLM 给出 `.85`，而当前 VLM 权重高于
   Flash LLM。这表明当前方案可能过度奖励表面规整度，尚未对齐人类对 Quake/Gamma 的相对偏好。
2. **Quake 的模板缺陷被新 Oracle 捕捉。** `template_residue=.225`，对应日期和报告人占位符；
   这修正了旧基线容易被其它满分项补偿的问题。
3. **模型评分与路由都尚不稳定。** 所有样本都触发 Plus，但都无法以高置信一致解决，
   说明当前阈值会带来高复核率和高 token 成本。
4. **严格响应契约在真实流量中有价值。** Quake VLM 两次返回像素 bbox，而非
   `[0,1]` 归一化坐标。最终适配器仅删除这个无效的可选定位字段，并在 evidence
   中记录 `adapter_sanitized_fields=["bbox"]`；页码和其它契约仍严格校验。

## 下一步

1. 扩展到更多 topic 和更完整的人评 deck，不在 `n=3` 上直接调权。
2. 分开估计 Flash LLM、Flash VLM、确定性指标与人评的相关性、稳定性和校准误差。
3. 对 Prompt/模型做重复运行，报告均值、方差、排序翻转率和 token 用量。
4. 建立版本化价格表，用实际 input/output token 估算货币成本；在此之前不得将
   `cost_budget` 解读为真实费用门禁。

## 可复核产物

- 交互 HTML：`var/datasets/slides_align_sample/report_qwen_v3/index.html`
- 结构化比较：`var/datasets/slides_align_sample/report_qwen_v3/comparison.json`
- 逐份完整报告：`var/datasets/slides_align_sample/report_qwen_v3/*.report.json`
- 历史 v2 HTML 仍保留：`var/datasets/slides_align_sample/report/index.html`

限制：三份 deck 只是同主题教学切片；Slides-Align 标签是序数相对排名；上游卡片宣称
MIT，但生成产物仍可能受各 AI 产品条款限制，因此当前仅用于 research quarantine。
