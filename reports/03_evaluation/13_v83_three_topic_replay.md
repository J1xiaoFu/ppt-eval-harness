# v8.3 Slides-Align 三主题真实回放

## 结论摘要

当前 v8.3 基线在 3 个 `topic_introduction` 主题、22 份真实 PPTX、420 页上的正式排名
统计被正确抑制：每个主题都至少包含一个 `DEGRADED` case，不能把不同缺失公式的分数当作
同口径排序。

仅用于诊断、不得门禁或拟合的结果为：

- 全部 22 份：Macro Spearman `0.107`，主题内 Micro Pairwise `37/70 = 52.9%`。
- 仅 18 份 FULL：Macro Spearman `0.333`，主题内 Micro Pairwise `28/45 = 62.2%`。

这比先前单一 `market_analysis` 切片更清楚地表明：当前 Harness 可以稳定给出合法、可审计的
原子结果，但其 Composite 尚不能稳定代表跨主题人类可见偏好。

## 数据合同

- Dataset：`Yqy6/Slides-Align`。
- Revision：`2f50ac6674a506acb245275e58c8a452c00e6a14`。
- Difficulty：`topic_introduction`。
- Profile：`finished-deck-v8@8.3`。
- Evaluation Git：`caf37275c1b8f50261731a8f1e1f0b2690e84f2c`。
- 数据用途：`research_quarantine`；数据卡声明 MIT，但产品生成物仍受各产品 ToS 约束。

下载后共有 22 份有效 PPTX、420 张完整官方渲染图、`500,149,008 bytes`。顶层与三个
topic manifest 共验证 447 个文件，实际 size/SHA-256/LFS SHA 全部一致；PPTX ZIP/CRC 与
slide XML 数量也经过验证。

NotebookLM 三主题都只有 PDF/PNG，没有 PPTX。另有两个真实上游异常被隔离：

- `Chinese_New_Year/Zhipu`：PPTX 18 页，官方 slide PNG 仅 10 张。
- `modern_architecture/Zhipu`：PPTX 17 页，官方 slide PNG 仅 10 张。

这些 rank 均保留为缺口，不重编号。

## 主题内结果

| 主题 | N | FULL | 正式 Spearman | 未设门诊断 Spearman | 未设门 Pairwise | FULL-only Spearman | FULL-only Pairwise |
|---|---:|---:|---:|---:|---:|---:|---:|
| Chinese_New_Year | 7 | 6 | N/A | -0.036 | 52.4% | -0.029 | 53.3% |
| stock_market | 8 | 6 | N/A | -0.071 | 46.4% | 0.543 | 66.7% |
| modern_architecture | 7 | 6 | N/A | 0.429 | 61.9% | 0.486 | 66.7% |

不得计算跨主题 global Spearman、global pairwise 或跨主题总顺序。上述 Macro 是三个主题
内部统计的等权汇总；Micro Pairwise 只按主题内可比较 pair 加权。

## 业务处置与训练轨

- Decision：`PASS 4 / REVIEW 15 / FAIL 3`。
- Coverage：`FULL 18 / DEGRADED 4`。
- visual：`TRAIN 2 / REVIEW 16 / REJECT 4`。
- layout：`TRAIN 5 / REVIEW 14 / REJECT 3`。
- content：`REVIEW 18 / REJECT 4`；该排名集没有 source/requirement/内容 GT。
- full_deck：`REVIEW 16 / REJECT 6`。

三个 FAIL 都由 VLM 确认的 functional defect prevalence 触发 `0.5` multiplier：

- Chinese_New_Year / Skywork-Banana：29.71，人评 rank 1；typography 缺陷获确认。
- modern_architecture / Skywork：35.35，人评 rank 4；geometry 缺陷获确认。
- modern_architecture / Kimi-Smart：33.22，人评 rank 7；geometry 缺陷获确认。

四个 DEGRADED 分别为：

- Chinese_New_Year / Skywork：`v8_functional_integrity` unresolved。
- stock_market / Skywork-Banana：`authorship_specificity_v2` unresolved。
- stock_market / Zhipu：`v8_functional_integrity` unresolved。
- modern_architecture / Gamma：`palette_craft` unresolved。

## 跨主题产品稳定性

| 产品 | 主题数 | 均分 | 最低 | 最高 | 极差 | Decision 分布 |
|---|---:|---:|---:|---:|---:|---|
| Kimi-Banana | 3 | 83.85 | 80.59 | 86.01 | 5.41 | PASS 3 |
| Quake | 3 | 72.38 | 67.57 | 75.72 | 8.15 | REVIEW 3 |
| Kimi-Standard | 3 | 72.00 | 66.67 | 75.02 | 8.36 | REVIEW 3 |
| Gamma | 3 | 68.78 | 60.19 | 75.45 | 15.26 | REVIEW 3 |
| Kimi-Smart | 3 | 62.57 | 33.22 | 82.36 | 49.14 | PASS/REVIEW/FAIL 各 1 |
| Skywork | 3 | 58.72 | 35.35 | 70.61 | 35.26 | REVIEW 2 / FAIL 1 |
| Skywork-Banana | 3 | 55.67 | 29.71 | 71.77 | 42.06 | REVIEW 2 / FAIL 1 |

Kimi-Banana 的跨主题稳定性显著高于其他系统，但它在人评 rank 为 3/4/3，而 Harness 三次均
PASS，说明 raster-text recovery 和 VLM 视觉信号可能形成产品特定优势。另一方面，Kimi-Smart
与两个 Skywork 系统的极差很大，表明规则硬门、主题模板和模型判断存在强交互。

## 构念相关性诊断

内部构念与人排的方向随主题明显变化。例如：

- visual communication Spearman：`0.541 / -0.383 / 0.750`。
- composition craft：`-0.198 / 0.619 / 0.000`。
- typography craft：`-0.571 / -0.371 / -0.036`。
- authorship specificity：`0.214 / -0.321 / 0.536`。

没有一个构念在三个主题上都表现为强、同方向的偏好代理；尤其 typography 在三个主题上均
非正相关。当前不能据此调权，因为 Slides-Align 只有整体相对 rank，没有构念级 GT；正确的
下一步是把这些反例交给人工做构念级标注。

## 审计与成本

- 22/22 append-only audit chain 有效。
- 22/22 完整 observation artifact 的实际 SHA、Report 与 Manifest 三方一致。
- 20/22 deck 的七个核心视觉模型指标全部 SCORED；另外两份为合法 N/A，无执行 ERROR。
- 37 次同构念 GLM fallback。
- 模型 usage：`5,052,068 tokens`。
- Provider 未返回可验证货币费用，因此 `cost_known=false`，不把 0 解释为免费。

每个主题的 HTML 都保留全部幻灯片、modal 翻页、证据页跳转、原始 PPTX、EvaluationReport、
Reducer lineage 和完整 observation JSON。Suite HTML 只做跨主题运行健康与 topic 内统计汇总。

本轮没有利用这些结果拟合权重、阈值或 Prompt。
