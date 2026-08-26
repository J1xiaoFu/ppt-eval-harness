# 调研实验台账

任何结果必须先登记再运行；完成后附运行 ID、原始输出位置和结论。不得删除失败实验。

| 实验 ID | 假设 | 数据版本 | 方法版本 | 状态 | 运行 ID | 结论 |
|---|---|---|---|---|---|---|
| EXP-BASELINE-PPTAGENT | 公开基线可复现 | official-100-list + aurora-v1 | experiment-88c29f | partial / credential-blocked | PPTEVAL-PREFLIGHT-OFFICIAL, PPTEVAL-PREFLIGHT-AURORA | 版本、Prompt、渲染、调用图可复现；真实 GPT-4o 分需凭证 |
| EXP-BASELINE-AUTOPRESENT | 基准可覆盖本体/reference 指标 | food-v1 + aurora-v1 | autopresent-98e0c012 | partial / release-incomplete | SLIDESBENCH-FOOD-SELF, SLIDESBENCH-AURORA-SELF | 可运行单页指标，但 identity color 非100、空页除零；完整论文数据缺失 |
| EXP-PUBLIC-DATA-AUDIT | 公开 PPT 数据可形成隔离研究池 | catalog-20260826 | dataset-audit-v1 | completed | DATASET-CATALOG-001 | 形成 A/B/C 目录；没有任何来源获得“公开即自动商用”结论 |
| EXP-RUBRIC-DIRECTION | 缺陷注入导致预期方向变化 | synth-v1 | rubric-v1 | planned | - | - |
| EXP-RUBRIC-LOCALITY | 单缺陷主要影响对应维度 | synth-v1 | rubric-v1 | planned | - | - |
| EXP-PDMS-ABLATION | 外乘内加降低严重误放行 | gold-v1 | profile-v1 | planned | - | - |

完成定义：审计事件存在、输入/输出哈希齐全、统计脚本锁定、异常样本有原因、结论区分“观察”与“解释”。
