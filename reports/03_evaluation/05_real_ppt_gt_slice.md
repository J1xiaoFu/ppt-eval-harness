# Slides-Align 真实 PPT 人评对照切片

评测日期：2026-08-26。该实验用于验证真实生成 PPT 上的方向性，不替代正式全量元评测。

## 数据与协议

- 数据：`Yqy6/Slides-Align`，固定 revision
  `2f50ac6674a506acb245275e58c8a452c00e6a14`。
- 主题：`topic_introduction / market_analysis`。
- GT：同一 topic 内的逐 deck 人类相对排名，数值越小越好。
- 样本：Skywork-Banana、Quake、Gamma 三份真实 PPTX，共 48 页。
- 完整性：3 个 PPTX 和 48 个上游渲染 PNG 均与 LFS SHA-256 一致；PPTX 页数等于渲染数。
- Harness：`finished-deck-v2`；三个模型 Shadow 审计未配置 Provider，返回 N/A 且不参与公式。
- 视觉代理：按 Profile 原权重重新归一化组合 `visual_hierarchy`、`layout`、`typography`、
  `style_consistency`、`multimedia_quality`。

## 结果

| 产品 | 人评 rank / 8 | Baseline | 视觉代理 | Decision / Coverage | 三例内顺序 |
|---|---:|---:|---:|---|---:|
| Skywork-Banana | 2 | 88.918371 | 96.64 | PASS / FULL | 1 |
| Quake | 3 | 86.130769 | 85.95 | PASS / FULL | 2 |
| Gamma | 6 | 79.485972 | 76.47 | REVIEW / FULL | 3 |

- Baseline 对人评 Spearman：`1.00`。
- 视觉代理对人评 Spearman：`1.00`。
- 三个可比较 pair 的排序准确率：Baseline `100%`、视觉代理 `100%`。

## 为什么不能把 1.00 当成“系统已验证”

这只是一个 topic 的三个样本，且排序一致不代表判定理由一致：

1. **Skywork-Banana：人高、机高，但存在漏检。** 页面可见异常词间距和模板化表达，当前
   `typography=1.0`，因为 Oracle 主要检查显式字号；与此同时 `accessibility=0.4767`，主要处罚
   标题和 alt 覆盖，和人类审美排序并非同一构念。
2. **Quake：总体高分掩盖未完成内容。** `layout/style/multimedia/editability=1.0`，但结束页仍有
   日期、报告人等占位符。当前内容规则没有将 unresolved placeholder 识别为交付缺陷。
3. **Gamma：排序一致但可能因错误理由。** `layout=0.5019`，Evidence 集中在第 4/5 页的 overlap
   和 out-of-bounds；肉眼检查表明其中部分是时间线圆点、连线和背景装饰的意图内组合，存在结构
   Oracle 误报风险。

因此，本切片证明的是“当前分数在这三个样本上方向一致”，同时暴露了三类重要缺口：像素级排版
漏检、交付占位符漏检、意图内重叠误报。

## 可复现产物

- 数据与 hash manifest：`var/datasets/slides_align_sample/manifest.json`。
- GT 子集：`var/datasets/slides_align_sample/rankings/market_analysis.json`。
- Harness 报告与比较统计：`var/datasets/slides_align_sample/report/`。
- 可视化入口：`var/datasets/slides_align_sample/report/index.html`。
- 构建命令：`python scripts/benchmarks/evaluate_slides_align_sample.py`。

## 下一步

1. 按 topic 聚类抽取更多 PPTX，计算 macro Spearman、Kendall tau-b、pairwise accuracy 和 topic
   bootstrap 置信区间；不能将 deck 当独立样本 bootstrap。
2. 增加 placeholder/unresolved-template 确定性 Oracle。
3. 对 layout overlap 加入对象类型、层级和视觉渲染复核，区分意图内组合与真实遮挡。
4. 接入 VLM Shadow 后，在 SlideAudit 2,400 页 taxonomy/bbox GT 上做分类和定位评测，再决定是否
   发布计分 Profile。
