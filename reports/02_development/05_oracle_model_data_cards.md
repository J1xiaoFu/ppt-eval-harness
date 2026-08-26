# Oracle、模型与数据卡

## Oracle Card 模板

| 字段 | 内容 |
|---|---|
| `oracle_id/version/owner` | 唯一 ID、语义版本、责任人 |
| 判定问题 | 一个原子问题，禁止复合打分 |
| 输入/输出 | 必需证据与 `OracleResult` 字段 |
| 支持范围 | 场景、格式、语言、页数上限 |
| 失败/降级 | 错误码、重试、替代路径、人审条件 |
| 校准 | 金标版本、指标、阈值和 ECE/Brier |
| 限制 | 已知盲区和禁止用途 |
| 安全 | 注入、隐私、供应商保留策略 |

## ORC-BASE-001 BaselinePptQualityOracle

- 组合内容清晰、叙事、视觉、技术、可编辑性、兼容性、可访问性 Leaf Oracle。
- 所有场景、所有 Profile 强制执行；PPTX 最完整，PDF/图片结构指标 `N/A`。
- 它不读取专项请求来抬高分，也不负责 `S_full`。
- 任何 Leaf `ERROR` 降低覆盖率并触发复核，不按零分处理。

## ORC-AGG-001 PptPdmsAggregator

- 纯函数，无模型/网络/存储依赖。
- 输入为校准后的原子结果和已验证 Profile；输出基础/完整分与贡献分解。
- 拒绝重复 `defect_id`、非法乘子、负权重、无适用加项、`ERROR` 混入数值。

## Model Card 模板

记录 `provider/model/version/region/data_retention/prompt_version/temperature/seed`、任务、语言、校准集、
排序/校准/稳定性指标、已知偏差、注入测试、成本、撤回条件。不得仅写营销型号。

示例 Adapter `adapter/demo` 仅用于合成流程演示，不能作为质量结论来源。

## Data Card 模板

记录来源、许可证据、授权用途、PII、脱敏、规模、场景/语言/模板分布、父子血缘、切分哈希、
标注协议、一致性、污染风险、保留与删除方式。冻结集必须有只读权限和独立哈希清单。

