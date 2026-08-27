# finished-deck v6 六维结构化视觉实验方法

## 预注册范围

`finished_deck_v6_structured_visual_dimensions_candidate.json` 将 v5 的单一结构化视觉总分
拆为六个独立 `BASE_ADDITIVE` metric，使不同视觉维度能够分别归因、设置预算和触发
复核。六项仍来自一次 VLM 请求，不是六份独立证据。

权重在查看新一轮重放排名之前固结，选择依据仅是：

1. 与现有确定性 Oracle 的构念重叠程度；
2. 像素证据相对对象树证据的增量覆盖；
3. 六项共用模型、prompt、页图和一次请求所带来的相关性；
4. 预研阶段明确把像素审计作为强视觉证据，并将调用成本与证据权重解耦。

本文档与 Profile 的 `rank_fit_used=false` 一起构成预注册记录。现有 Slides-Align
排名不参与 v6 权重选择。

## 版本隔离

v6 只启用：

- `baseline_ppt_quality`；
- `structured_dimensions.model_audits`。

v6 不启用 `high_cost.model_audits`、v5 的 `structured.model_audits` 或曾用过的
`structured_visual.model_audits`。新 Composite 使用
`ppt-vlm-structured-visual-dimensions-audit@1.2.0`，在一次成功请求中返回六项。
输入、Provider 或整体响应契约失败时，六项同时 N/A 或 ERROR；某一维
`criterion_observability=INSUFFICIENT` 或低于 Profile 置信底线时，只有该维 N/A，
但因它是 required metric，整体 coverage 仍会降级并进入 REVIEW。成本在六项间分摊，
不伪造成六次调用。

v5 的 `structured_vlm_visual_audit` 保留于旧 Composite，用于版本化回放；v6 既不计入也
不 required 这个聚合 metric。默认生产路径仍是 `finished-deck-v3`。

## 构念重叠审计

| 六维 metric | 现有确定性代理 | 重叠 | VLM 内部预算 | 依据 |
|---|---|---|---:|---|
| `structured_vlm_composition_layout` | `visual_hierarchy`, `layout`, `style_consistency` | 高 | 10% | 对象树已检查标题锚点、边界和重叠；VLM 只补整体视觉平衡 |
| `structured_vlm_typography_legibility` | `typography`, `visual_hierarchy` | 高 | 10% | 显式字号与字号层次已可观测；VLM 补像素可读性 |
| `structured_vlm_color_contrast` | 无直接指标 | 低 | 20% | 现有 style 指标未读取颜色或像素对比度 |
| `structured_vlm_imagery_data_visualization` | `multimedia_quality` | 低—中 | 25% | 确定性代理主要检查媒体载荷可读和尺寸，不判断语义质量 |
| `structured_vlm_cross_slide_consistency` | `style_consistency` | 高 | 10% | 字体家族和标题位置已有确定性代理；VLM 补像素级视觉语法 |
| `structured_vlm_render_integrity` | `layout`, `multimedia_quality`, visibility gate | 低—中 | 25% | VLM 能看到缺字、导出错位、栅格化裁切等对象树盲区 |

前三个高重叠维度（composition/typography/consistency）合计占 VLM 内部预算 30%；
三个主要增量维度（color/imagery/render）合计 70%。这是基于代理互补性的工程
先验，不是从小样本排名拟合出的系数。

## 总 VLM 预算

v6 保留合计的顶层构念预算：content `26.5625%`、视觉 `50%`、delivery
`17.1875%`、handoff `6.25%`。为了让 20% 是真正的上限，Profile 把视觉拆成两个顶层
子构念：

- `visual_deterministic = 40%`：对象树与文件证据；
- `visual_vlm = 10%`：六个结构化 VLM 维度。

两者合计仍为总分的 50%视觉预算，所以 `visual_vlm` 始终占合并视觉的 20%、
占总分 10%。六项的 `base_weights` 只表示 `visual_vlm` 内部的
`10/10/20/25/10/25`分配，不会把一次相关的模型调用放大成六份独立证据。
具体分配为：

| metric | visual 组内份额 | 总分份额 |
|---|---:|---:|
| composition/layout | 2.0% | 1.00% |
| typography/legibility | 2.0% | 1.00% |
| color/contrast | 4.0% | 2.00% |
| imagery/data visualization | 5.0% | 2.50% |
| cross-slide consistency | 2.0% | 1.00% |
| render integrity | 5.0% | 2.50% |

20% 是在查看本轮 7 份结果前预注册的强视觉证据先验：像素观测能直接看到对象树
无法表达的配色、图像编码、跨页视觉系统和导出完整性。调用成本只决定路由和调用
频率，不降低证据权重。但六项仍来自一次共享调用，因此未扩到 25% 或更高，
且当前 Profile 仍标记为 `EXPERIMENTAL/UNVALIDATED`。硬隔离还保证：当可选的
`multimedia_quality` 因无媒体而 N/A 时，只会在 `visual_deterministic` 内重归一化，
不会把 VLM 的有效份额推高到 20% 以上。

## 复核线

未校准阶段只为两个相对可验证、且低分更接近交付缺陷的维度设置 `0.70`
复核线：

- `structured_vlm_typography_legibility`；
- `structured_vlm_render_integrity`。

composition、color、imagery 和 consistency 在当前更接近主观偏好，只进入加权分，不因单项
低于任意阈值就独立触发 REVIEW。确定性硬门仍不能被 Flash 或未来的 Advanced
复核覆盖。

每维同时返回自己的 `criterion_confidence` 和 `criterion_observability`。Profile 将
`vlm_dimension_min_confidence` 预注册为 `0.60`；低于该值或不可观测的维度不带分进入
聚合，而是 N/A 并转人工 REVIEW。`PARTIAL` 仍可计分，但必须保留该维的较低置信度以供后续采样。

## 高级复核路由

v6 预注册为 `STRUCTURED_DIMENSIONS_FLASH_ONLY`，且
`advanced_routing_status=PENDING_DIMENSION_ISOMORPHIC_ADVANCED_VLM`。当前旧 Advanced Oracle 只返回
标量视觉结果，与 v6 六个 metric 不同构；用它复核某个六维底线会丢失构念
对应关系。因此 Supervisor 不得在 v6 重放中调用旧标量 Advanced reviewer。
`qwen3.8-flash` 只被配置为未来同构 Advanced 角色；尚无数据证明它比基线更强或更贵。

当 `structured_vlm_typography_legibility` 或 `structured_vlm_render_integrity` 低于 `0.70`，
或六维因 N/A/ERROR 造成覆盖不完整时，当前路径是直接进入人工 `REVIEW`。这不是
临时故障降级，而是避免跨构念复核的版本化安全边界。

只有在新的六维同构 Advanced Oracle 满足以下条件后，才能在后续新 Profile 中开启高级路由：

- 使用与 Flash 完全相同的六个 `metric_id`、criterion 定义和证据契约；
- 只复核触发问题的同维度，不把“一维差、另一维好”当作 judge disagreement；
- Advanced 只提供复核证据与置信度，不重算 v6 总分，不覆盖确定性硬门；
- 对“不调用旧标量 reviewer”、同维度路由、仍困惑转人工和成本归属有固定回归测试。

## 20% 候选的验证与继续扩权门槛

当前 20% 是未查看新一轮结果前固结的实验候选，不是生产批准。生产升级或继续扩权
不能依据当前 7 份同主题排名，也不能因为单次 Spearman 上升就触发。应使用与
delivery/handoff 标签分离的视觉维度金标，并保持候选权重在冻结测试集解盲前已注册。
Slides-Align 当前只提供同主题整体可见偏好排名，不是六维分项金标；本轮只能诊断合并
视觉构念的排序一致性，不能用它证明单维权重已被验证。

### 20% 实验候选 → 生产审批

至少同时满足：

- 第一个冻结真实集至少 200 份 deck、10 个 topic，且栅格/可编辑、中/英文与页数分层
  有明确覆盖；
- 六维分开标注，每个关键缺陷维度至少 30 个阳性例，双人标注与仲裁后
  Krippendorff's alpha ≥ `0.67`；
- finding 人工 precision ≥ `0.80`，页码定位 recall ≥ `0.85`，契约成功/完整覆盖率
  ≥ `0.98`；
- 同一 deck 至少三次重复运行，六维各自的分数标准差 ≤ `0.05`；
- 在按 topic 隔离的冻结测试集上，预注册的 20% 候选相比 10% 基线，视觉标签
  pairwise accuracy 的 bootstrap 95% 置信区间下界不小于 `0`，点估计至少提升 `3`
  个百分点，且任一预注册分层不下降超过 `3` 个百分点。

达到这些条件只允许让当前 20% `EXPERIMENTAL` Profile 进入生产审批，不代表自动批准。

### 20% → 25% 实验候选

除上述门槛外，还必须：

- 在第二个独立冻结真实集上复现，该集至少 200 份 deck，与第一个集无模板、
  topic 或派生关系泄漏；
- 相比 20% 候选，25% 候选的视觉标签 pairwise accuracy 仍达到上述非劣界，且
  typography/render 绝对缺陷的 false-negative rate 不上升超过 `2` 个百分点；
- 在至少两个独立模型/prompt patch 上保持预算方向和非劣结论；
- 页图失败、任一维度缺失或 contract ERROR 都保持降级/人审语义，不以其他五维重新
  归一化伪造 FULL coverage。

即使 25% 实验候选通过这些门槛，也仍需独立的 Profile 版本、决策审计和生产批准；
不得原地修改 v6，不得自动替换 v3。
