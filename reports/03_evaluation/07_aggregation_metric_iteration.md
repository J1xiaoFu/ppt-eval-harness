# 聚合层审计与指标迭代（7 份真实 PPT）

## 结论

当前聚合层确实有迭代空间，但问题不等于“VLM 权重太大”。

- `finished-deck-v3` 权重合计 `1.28`，VLM `.12` 的实际总分份额为 `9.375%`。
- 视觉相关指标块整体约占 `50%`，但这个份额是由平坦指标数量/权重隐式形成，
  不是显式的“视觉构念预算”。
- 原三例 `Gamma > Quake` 不是 VLM 造成：两者 VLM 均为 `.85`。主要差值来自
  `template_residue -4.844`、Flash LLM `-1.375` 和 `visual_hierarchy -1.159`。
- 数据扩到 7 份后，当前确定性基线与人评的 Spearman 仅 `.107`；加入 Flash 后为
  `-.321`。聚合候选未解决排序，说明主要瓶颈已经是“标签构念 + 指标可观测性”，
  而不是单一权重。

## 聚合层现状

当前加法项为动态归一平均：

\[
A=\frac{\sum_{i\in applicable} w_i s_i}{\sum_{i\in applicable}w_i}
\]

Ready-made 总分为 `100 * base_multiplier * A`。这有三个重要后果：

1. 每新增一个同构代理指标，都会改变整个构念的总份额。
2. optional NA 会被从分子/分母同时删除；required NA 也是算术中性，但会使 Coverage 降级。
3. `confidence` 完全不参与加法项的分数；模型自报高置信也不等于重复运行稳定。

当前有效构念预算：

| 观测构念 | 有效份额 |
|---|---:|
| 表层内容结构 | 20.31% |
| 语义内容模型 | 6.25% |
| 视觉几何与文字 | 34.38% |
| VLM 视觉感知 | 9.38% |
| 素材完整性 | 6.25% |
| 可编辑性与兼容性 | 12.50% |
| 无障碍元数据 | 4.69% |
| 交付清理 | 6.25% |

`content_clarity` 实际只检测空白/字数/文本框数，`narrative` 主要检测标题覆盖；
它们不能被解读为真正的语义内容质量。

## 数据扩充

Slides-Align `market_analysis` 从 3 份扩到 7 份完整 PPTX/PNG 配对：

- human rank：`1/2/3/4/5/6/8`
- 130 页，126,002,030 bytes
- 140/140 个 manifest 文件的 size / LFS SHA-256 / PPTX CRC / 页数校验通过
- rank 7 Zhipu 只有 9 张 PNG，无 PPTX，因此不纳入异构比较
- 用途仍为 `research_quarantine`，禁止当作生产/商业/训练数据

## 7 份真实 PPT 结果

| Product | Human rank | 当前确定性分 | Flash v3 | Flash LLM | Flash VLM | 系统顺序 |
|---|---:|---:|---:|---:|---:|---:|
| Kimi-Banana | 1 | 59.60 | 61.72 | .45* | .65 | 7 |
| Skywork-Banana | 2 | 88.92 | 84.00 | .65 | .45 | 3 |
| Quake | 3 | 86.13 | 77.91 | .20 | .85 | 6 |
| Kimi-Smart | 4 | 87.27 | 88.15 | .92 | .85 | 2 |
| Kimi-Standard | 5 | 87.94 | 88.42 | .88 | .85 | 1 |
| Gamma | 6 | 85.46 | 83.61 | .42 | .85 | 4 |
| Skywork | 8 | 79.17 | 82.01 | .95 | .85 | 5 |

`*` Kimi-Banana 没有可提取文本层，`.45` 来自新的渲染语义回退。

| 方案 | Spearman | Pairwise |
|---|---:|---:|
| 当前确定性基线（同代码快照） | .107 | 12/21 |
| 平坦 Flash v3 | -.321 | 8/21 |
| 构念封顶两层算术 | -.321 | 8/21 |
| 去除 handoff 的独立质量通道 | -.179 | 9/21 |

这些是单一 topic 的描述值，不是可用于拟合权重的校准集。

## 为什么聚合改了也没解决

Kimi-Banana 是最清晰的反例：它在人评中为 rank 1，但 15 页全部是整页栅格图。

- 对象树看到：标题覆盖 0、文本可观测 0、editability 0。
- 像素看到：视觉完整，但第 4/5/9/10 页有真实拼写错误和幻觉乱码。
- 原 LLM 因“无文本”给 `0.0 / confidence=1.0`，这是输入不可观测，不是有效内容结论。

聚合函数无法修复缺失的文本/OCR 证据，也无法把 Slides-Align 的“可见偏好”标签变成
“可编辑、无障碍、可交付”绝对标签。

## 已实现的迭代

1. **版本化构念聚合接口**

   `EvalProfile` 新增 `aggregation_strategy`、metric-to-construct mapping 和 construct weights。
   `CONSTRUCT_WEIGHTED_MEAN` 先在组内归一，再按固定构念预算聚合；新增同类代理不会
   扩大该构念的顶层份额。`ScoreBreakdown` 会报告各构念分。

2. **v4 实验候选**

   `finished_deck_v4_construct_candidate.json` 固定 content/visual/delivery/handoff 预算，
   把 VLM 限制为 visual 组的 10%（总分上限约 5%）。该 Profile 明确标记
   `EXPERIMENTAL / UNVALIDATED / production_approved=false`，v3 默认不变。

3. **指标语义更名**

   HTML/JSON 中的 `visual_proxy` 新名为 `deterministic_visual_proxy`，明确它不包含 Qwen VLM。
   旧字段仅作兼容 alias。

4. **栅格 deck 的自适应内容输入**

   当有文本页比例低于 25% 时，内容 Oracle 不再用空对象树作质量判断；它使用已渲染
   页图和同一 Flash 模型完成语义审计，结果仍归入“内容构念”。Kimi-Banana：

   - 内容分 `.00 -> .45`
   - 该项对总分的可归因提升 `+2.8125`
   - 总分 `57.03 -> 61.72`，其中另有 `+1.875` 来自同次重跑的 VLM `.45 -> .65`漂移
   - 排序仍末位，证明输入修复有效，但不会伪造相关性改善

## 仍需要的下一迭代

1. 同一次 VLM 调用返回固定维度，而不是单一黑盒总分：
   `composition_layout / typography_legibility / color / imagery_visualization /
   render_integrity`。
2. LLM 输出固定语义维度：清晰度、连贯性、具体性、内部一致性、可行动性。
3. 每条 finding 使用 typed `criterion_id / polarity / severity / page / coverage`，由 Harness
   而非模型计算 deck score。
4. Plus 只复核触发问题的同一维度；“内容差、视觉好”是质量画像，不是 judge disagreement。
5. Slides-Align 只能检验可见相对偏好；editability / accessibility / compatibility /
   handoff 必须使用另外的绝对可交付标签校准。

## 费用与稳定性

7 份 Flash 内容+视觉共记录 `552,258` tokens。栅格内容回退不增加 API 调用次数，
但会把原文本请求变为图像请求，Kimi-Banana 单份 Flash token 从 `37,627`
增至 `67,448`。下一步应让一次多模态响应同时产生视觉维度和栅格内容维度，避免两次
上传同一组页图。

## 产物

- 数据 manifest：`var/datasets/slides_align_sample/manifest.json`
- 同代码确定性对照：`var/datasets/slides_align_sample/report_v2_current7/`
- 7 份 Flash 报告：`var/datasets/slides_align_sample/report_qwen_v3_adaptive7/`
- 构念候选：`aggregation_candidates.json/.md`
- 构念/逐指标审计工具：`scripts/benchmarks/analyze_aggregation_candidates.py`、
  `scripts/benchmarks/audit_metric_constructs.py`
