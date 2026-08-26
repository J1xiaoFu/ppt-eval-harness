# 01 调研

本阶段回答三个问题：**评什么、为什么这样评、数据能否合法复现**。所有实验结果先写入
`05_experiment_ledger.md`，再更新阶段门禁；没有证据的指标不得进入生产 Profile。

统一汇报结构：目标与问题 → 执行过程 → 证据与结果 → 决策与取舍 → 门禁与风险。

本轮新增真实证据：

- `06_baseline_reproduction.md`：PPTEval 与 AutoPresent/SlidesBench 的固定版本、真实 smoke、失败边界和接入决策。
- `07_public_dataset_catalog.md`：公开 PPT/PDF/图像/编辑/偏好数据的规模、revision、许可、下载实测和优先级。
- `evidence/public_dataset_catalog.json`：供审计站点和后续数据注册器读取的机器清单。
