# 03 评测

本阶段同时评价“PPT 产物质量”和“评测器是否可信”。所有阈值、样本排除、统计方法在查看冻结集
前预注册；`planned target` 与 `observed result` 必须分栏，避免面试或发布汇报误报。

当前新增的真实样本方向性实验见 `05_real_ppt_gt_slice.md`。其 `n=3` 结果只用于发现误判模式，
不得表述为全量人评相关性或生产门禁通过。

Qwen v3 的真实 Flash -> Plus -> Human 对照见 `06_qwen_v3_real_ppt_gt_slice.md`。该轮的
Spearman 为 `0.50`，低于历史 v2 切片的 `1.00`：当前模型分、权重和复核线尚未校准。

扩充到 7 份真实 PPT 后的聚合层审计、构念候选与栅格内容回退见
`07_aggregation_metric_iteration.md`。该结果证明单纯调低 VLM 权重不能修复排序，主要瓶颈是
构念标签不一致、文本可观测性和模型单一黑盒分。

`08_structured_visual_oracle_profile.md` 记录了仅 Oracle/Profile 的下一轮：六维结构化视觉
Oracle、v5 实验 Profile、标题空壳页诊断，以及 Skywork/Kimi 的真实 API 契约冒烟。

`09_v6_visual_dimensions_replay.md` 记录了 7/7 真实 deck 的 v6 六维重放：VLM 占 visual
20% 的预注册主方案、10/15/25% 敏感性、可证伪的 VLM 误判、以及
`qwen3.8-flash` Advanced 影子复核的契约阻塞。

`10_qwen38_glm53_route_smoke.md` 记录 v8.1 固定 Prompt 模型切换烟测：
`qwen3.8-flash` 主线在一份真实 deck 上六维全合法，`glm-5.3-flash` 使用同一原子 Prompt
完成独立多模态合同验证；该单例不用于相关性结论或参数拟合。

`11_kimi_smart_authorship_language_iteration.md` 记录 v8.2 对 partial localization 与系统性
卡片/图标/模板 formulaicity 的原子评测改造，以及 Kimi-Smart 的真实 Qwen3.8 重放；旧六维
合同保持可回放，新 authorship 只进入公式一次。

`12_v83_gate_raster_validation.md` 记录 v8.3 对硬门候选页定向采样、同页同缺陷确认、
栅格文字原子观察和训练准入优先级的真实双例验证。

`13_v83_three_topic_replay.md` 记录 3 个 topic-introduction 主题、22 份 PPTX、420 页的
完整真实回放、主题内偏好相关性、训练准入、产品稳定性与审计/成本信息。
