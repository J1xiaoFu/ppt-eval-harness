# 调研阶段门禁

| 门禁 | 证据 | 状态 | 负责人 |
|---|---|---|---|
| 至少两个公开 baseline 可复现 | `EXP-BASELINE-*` 运行清单 | partial：均跑到公开产物/凭证边界，未完成论文全量数值 | Research Lead |
| 失败分类经产品/设计/工程评审 | 评审事件与 Rubric 版本 | pending | Evaluation Lead |
| 每个生产指标有证据和锚点 | Oracle 卡+受控缺陷实验 | pending | Metric Owner |
| 正式数据许可与隐私通过 | Data Card+法务记录 | catalog-complete / legal-pending | Data Steward |
| 评测门槛已预注册 | `03_evaluation/00_preregistration.md` | ready | Evaluation Lead |

**阶段结论：部分通过，尚未达到生产研究门禁。** 两个既定 baseline 已锁定并跑到可验证边界，
公开数据目录及样本可用性已审计；但 PPTEval/reference-free 的真实模型分、AutoPresent 全量论文数据、
中文人工金标和法务签字仍缺失，因此不能表述为 exact reproduction 或生产数据验收通过。
