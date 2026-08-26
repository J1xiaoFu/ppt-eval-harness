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
