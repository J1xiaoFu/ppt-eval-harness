# v5 结构化视觉 Oracle / Profile 预研

## 目标

本轮只修改 Oracle 观测契约与 Profile 选择，不再调整聚合器公式。目标是把
`vlm_visual_quality_audit` 的单一黑盒分替换为固定视觉维度，并让 Harness 而不是模型计算
deck-level 视觉分。

## 新 Oracle 契约

- Leaf：`structured_vlm_visual_audit_oracle`
- Metric：`structured_vlm_visual_audit`
- 完整替代 Composite：`structured.model_audits`
- Oracle version：`1.0.0`
- 请求次数：一次 VLM，不为每个维度分别调用

固定 criterion 与 Oracle 内部权重：

| criterion_id | 权重 |
|---|---:|
| `composition_layout` | .25 |
| `typography_legibility` | .20 |
| `color_contrast` | .15 |
| `imagery_data_visualization` | .20 |
| `cross_slide_consistency` | .10 |
| `render_integrity` | .10 |

模型必须为每个 criterion 返回唯一 `criterion_summary` evidence，其 payload 包含
`criterion_id` 和 `[0,1]` 内有限数 `criterion_score`。缺失、重复、未知 ID、bool/NaN/越界分
全部拒绝。响应的模型全局 score 仅保存在 `model_global_score`，
`model_global_score_used=false`。

## v5 实验 Profile

`finished_deck_v5_structured_visual_candidate.json` 只启用：

- `baseline_ppt_quality`
- `structured.model_audits`

它不启用 `high_cost.model_audits`，因此不会双调 legacy VLM。它保留 v4 的
content/visual/delivery/handoff 预算，结构化 VLM 只占 visual 组 10%，即总分 5%。

Profile 明确标记：

- `EXPERIMENTAL`
- `UNVALIDATED`
- `production_approved=false`
- `model_audit_routing=STRUCTURED_FLASH_ONLY`
- `advanced_routing_status=PENDING_CRITERION_ISOMORPHIC_PLUS`

由于当前 Plus 视觉 Oracle 仍是旧标量契约，v5 不允许它“解决”六维 Flash 结果。
低分或困惑直接保留人工 REVIEW，直到存在 criterion-isomorphic Plus Oracle。

## 真实 API 冒烟

### Skywork-Banana

- Coverage / Decision：`FULL / REVIEW`
- v5 总分：`84.695385`
- 模型全局视觉分：`.45`（未使用）
- Harness 六维重算分：`.425`
- Content：`.42`
- Visual construct：`.912241`
- `body_completeness=1.0`
- 本次内容+视觉 tokens：`64,874`

| criterion | score |
|---|---:|
| composition/layout | .40 |
| typography/legibility | .25 |
| color/contrast | .70 |
| imagery/data visualization | .60 |
| cross-slide consistency | .30 |
| render integrity | .20 |

### Kimi-Banana（栅格-only）

- Coverage / Decision：`FULL / REVIEW`
- v5 总分：`61.333173`
- 模型全局视觉分：`.45`（未使用）
- Harness 六维重算分：`.445`
- Content：`.45`，`content_input_mode=RENDERED_SEMANTIC_FALLBACK`
- Visual construct：`.761038`
- `body_completeness=N/A / TEXT_OBSERVABILITY_INSUFFICIENT`
- 本次内容+视觉 tokens：`68,242`

| criterion | score |
|---|---:|
| composition/layout | .50 |
| typography/legibility | .30 |
| color/contrast | .60 |
| imagery/data visualization | .50 |
| cross-slide consistency | .40 |
| render integrity | .30 |

这个结果演示了构念与模态分离：Kimi 的内容分使用 VLM 能力从像素恢复，但它仍进入
content 构念；结构化视觉分只是 visual 构念的小部分。

## 新确定性诊断 Oracle

Baseline `2.1.0` 新增 `body_completeness`（`DIAGNOSTIC`）：

- 检测只有标题/短标签、没有正文或大型语义视觉主体的页面
- 封面和常规结尾页中性
- 大型图表/图像可作为有效 body signal
- 文本页比例低于 25% 时 N/A，不将栅格 deck 记 0
- 当前不计分，待拥有正文完整性标签后再考虑进入 content 构念

## 局限与下一步

1. 目前只完成两份真实 deck 的契约冒烟，不宣称排序提升。
2. 需在 7 份固定集上重复运行，分别评估 criterion 稳定性和人工证据精度。
3. 需建立六维人工标签；Slides-Align 的单一整体 rank 无法校准每个 criterion。
4. 实现同契约 Plus Oracle 后，再启用定向 Flash -> Plus -> Human 路由。
5. 栅格 deck 的内容/视觉目前各上传一次同样页图；后续可合并为一次多任务响应以降低 token。

## 产物

- Profile：`configs/profiles/finished_deck_v5_structured_visual_candidate.json`
- 方法：`docs/structured_visual_profile_method.md`
- Skywork 报告：`var/datasets/slides_align_sample/report_qwen_v5_structured_smoke/`
- Kimi 报告：`var/datasets/slides_align_sample/report_qwen_v5_structured_kimi_smoke/`
