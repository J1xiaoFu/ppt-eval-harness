# finished-deck v5 结构化视觉实验方法

## 目标与边界

`finished_deck_v5_structured_visual_candidate.json` 是下一轮 Oracle/Profile 预研候选，
用结构化视觉审计替换 v3/v4 的单一黑盒指标 `vlm_visual_quality_audit`。它不是
生产标准：Profile 固定标记为 `EXPERIMENTAL / UNVALIDATED / production_approved=false`，
默认 `READY_MADE` Profile 仍是 `finished-deck-v3`。

本轮只迭代 Oracle 观测方式和 Profile 选择/权重，不改变
`CONSTRUCT_WEIGHTED_MEAN` 聚合器的公式。

## Oracle 替换约束

v5 只启用两个 Composite：

- `baseline_ppt_quality`：原有确定性 PPT 本体指标与硬门。
- `structured.model_audits`：完整的模型审计替代组，产出现有
  `llm_content_quality_audit` 和新的 `structured_vlm_visual_audit`。

v5 不启用 `high_cost.model_audits` 或早期的
`structured_visual.model_audits`。这是硬性约束，用于防止同一份幻灯片同时调用旧/新
VLM，以及两个视觉结果重复计分。

结构化 Oracle 在一次请求中检查固定视觉维度，并将 typed findings 折算为唯一的
`BASE_ADDITIVE` deck-level metric `structured_vlm_visual_audit`。当前固定维度和 Oracle 内部
权重为：

| criterion_id | Oracle 内部权重 |
|---|---:|
| `composition_layout` | 25% |
| `typography_legibility` | 20% |
| `color_contrast` | 15% |
| `imagery_data_visualization` | 20% |
| `cross_slide_consistency` | 10% |
| `render_integrity` | 10% |

模型输出是有类型的观测与证据，不拥有 Profile 权重或最终决策权。六个维度的
归一化和 deck score 计算属于 Oracle 的版本化契约；Profile 只聚合该 Oracle 提交的
唯一原子指标。

## 构念预算与视觉代理上限

v5 完全保留 v4 的顶层构念预算：

| 构念 | 总分份额 |
|---|---:|
| content | 26.5625% |
| visual | 50.0000% |
| delivery | 17.1875% |
| handoff | 6.2500% |

视觉组内的五个确定性指标原始权重之和为 `0.52`。为使结构化 VLM 只占
视觉组的 10%，其原始权重设为：

\[
w_{svlm}=\frac{0.10}{0.90}\times0.52=0.0577777778
\]

因此它的顶层总分份额为 `50% × 10% = 5%`。这个上限表示 VLM 只是视觉质量的
部分代理，不会因新增模型维度而挤占 content、delivery 或 handoff 的预算。

Baseline `2.1.0` 还会输出 `body_completeness` 诊断指标，用于检测文本可观测 deck 中的
标题空壳页，但不参与 v5 分数。封面、常规结尾页和有大型图表/图像主体的页不会被当作
空壳页；栅格-only deck 返回 N/A，由渲染语义内容 Oracle 负责。

## 覆盖、复核与降级

- `structured_vlm_visual_audit` 是 required metric。页图缺失、传输错误或不完整覆盖不得
  伪造为零分，应当保留 N/A/ERROR 并使 Coverage 降级。
- 结构化视觉分低于 `0.70` 时进入 REVIEW。当前 v5 的
  `model_audit_routing=STRUCTURED_FLASH_ONLY`，不调用旧的标量 Plus VLM；在实现同样六维契约的
  Plus Oracle 前，低分/困惑保留人工复核。
- 确定性硬门不能被 VLM 或 Plus 覆盖。视觉分好也不能抵消文件不可交付、关键
  内容不可见或内部数据冲突。

## 评测与晋级条件

v5 应在现有真实 PPT 金标切片上与 v3/v4 并行重放，且使用相同的页图、模型
版本、prompt 版本和随机性设置。至少记录：

- 与人评顺序的 Spearman 和 pairwise accuracy；
- 每个固定维度的证据可定位率、覆盖率和人工精度；
- 同一 deck 重复运行的维度分和总分波动；
- Flash→Plus→Human 的触发率、解决率、token 和货币成本；
- 按 topic、页数、文本/栅格 deck 和缺陷类型分层的失败模式。

在扩大并冻结金标集、完成重复性试验、确认证据契约稳定，且相对已注册基线
有明确改善之前，不得将 `production_approved` 改为 `true`，也不得替换 v3 默认路径。
