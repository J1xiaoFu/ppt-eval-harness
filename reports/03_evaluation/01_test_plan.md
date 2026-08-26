# 测试与验证计划

## 测试金字塔

| 层级 | 覆盖 | 代表 ID |
|---|---|---|
| 单元/属性 | 解析、状态、聚合、校准 | TST-PROP-001..005 |
| 契约 | Oracle、Adapter、API Schema | TST-CONTRACT-001 |
| 组件 | PPTX、渲染、OCR、素材、图表 | TST-COMP-* |
| 架构 | 依赖方向、基础节点不变量 | TST-ARCH-001 |
| 集成 | 队列、DB、对象存储、Outbox | TST-INT-* |
| 端到端 | 四场景 FULL/DEGRADED | TST-E2E-* |
| 元评测 | 人工一致、校准、切片 | EXP-HUMAN-* |

## 评分属性测试

1. 任一乘子从 1→0.5→0，总分不得上升。
2. `N/A` 被移除并重归一；加入 `N/A` 不改变已有分。
3. `ERROR` 不可进入数值；若必需专项错误，`full_score=null`。
4. 同一 `defect_id` 同时配置乘算/加算时 Profile 编译失败。
5. 权重非负且适用项和为 1；不同 Profile 报告带版本并禁止直接排名。

## 变形测试

| 变换 | 预期主影响 | 不应发生 |
|---|---|---|
| 改错一个关键数字 | 事实/来源分下降，可能触发乘子 | 视觉分大幅变化 |
| 页面乱序 | 结构/叙事下降 | 字体与媒体分变化 |
| 整页栅格化 | 可编辑性下降 | 视觉观感必然归零 |
| 添加隐藏关键词 | 覆盖不提升，安全告警 | 指令分上升 |
| 增加重叠强度 | 版式单调下降 | 内容忠实分无故变化 |
| 移除必选素材 | 多模态乘子下降 | 基础解析失败 |

## 四场景降级矩阵

| 场景 | 基础成功/专项成功 | 基础成功/专项部分错 | 基础成功/专项全错缺失 | 基础不可评 |
|---|---|---|---|---|
| 文字生成 | FULL | DEGRADED+REVIEW | BASE_ONLY+REVIEW | UNASSESSABLE |
| 项目总结 | FULL | DEGRADED+REVIEW | BASE_ONLY+REVIEW | UNASSESSABLE |
| 多模态 | FULL | DEGRADED+REVIEW | BASE_ONLY+REVIEW | UNASSESSABLE |
| 成品 PPT | FULL（base） | N/A | N/A | UNASSESSABLE |

断言：前三场景任何专项异常都保留 `base_score` 和基础证据，且不生成伪造 `full_score`。

## 鲁棒、安全和故障注入

- Judge 输入顺序互换、重复三次、模型/Prompt 小版本变化。
- 隐藏备注/透明文字/页外文本提示注入；严格 Schema 逸出。
- 压缩炸弹、损坏关系、宏、外链、OLE、大图、超长页数。
- Redis/DB/对象存储短暂故障、Worker 中断、Outbox 重放、幂等重复提交。
- OOD：中英混排、非 16:9、复杂图表、无常见字体、PDF/逐页图。

## 视觉验收

样例 PPTX 全页渲染；检查重叠、溢出、字体替换、连接线、媒体和图表数据。PowerPoint 与 LibreOffice
差分不能只看像素阈值，必须关联对象与字体证据。

