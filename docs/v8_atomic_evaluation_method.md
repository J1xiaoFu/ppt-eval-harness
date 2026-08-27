# v8 原子评测、Reducer 与训练准入方法

## 目标

v8 面向可编辑 PPTX 训练数据筛选。它禁止模型直接评价整份 PPT 并提交总分，改为：

```text
PPTX / rendered pages / source / request
→ scoped AtomicObservation
→ deterministic defect caps and deduplication
→ quality-attribute Reducer
→ PPT-PDMS quality decision
→ visual / layout / content / full-deck eligibility
```

旧 v1–v7 Profile 保留显式回放能力，四场景默认文件映射切到 v8。

## 原子作用域和完整审计

`EvaluationScope` 支持 package、object、page、slide-pair、claim、requirement、asset、
chart-series 和 deck。Observation 带稳定 ID、unit key、局部分、置信度、严重性、
importance、critical/key-unit 标记以及完整 Evidence。

所有 observations 以规范 JSON 写入 content-addressed artifact，SHA-256 进入 Run Manifest。
报告层只输出统计摘要和 artifact 引用，不用截断后的展示 Evidence 代替训练/审计数据。

## Reducer

- page quality：`0.60 mean + 0.25 nearest-rank p20 + 0.15 key-page minimum`；没有关键页时对
  mean/p20 的 0.85 权重重归一化。
- slide-pair quality：`0.70 mean + 0.30 p20`。
- requirement/claim/asset/chart：importance-weighted coverage。
- required evidence observability 低于 0.60 时返回 N/A，不填 0 或中性分。
- CRITICAL 且属于 critical/key unit 时，输出封顶 0.34；v8 functional gate 直接拒绝关键
  CRITICAL，MAJOR prevalence ≥20% 时降为 0.5 gate。

## 构念融合

Profile 不再按 deterministic/VLM 分组，而按质量属性计分。确定性规则负责事实、绝对缺陷
和 cap；VLM 负责语义与正向审美信号：

- content structure：25%
- composition craft：20%
- typography craft：10%
- palette craft：8%
- visual communication：15%
- visual system/sequence：10%
- authorship specificity：12%

六个视觉 criterion 是独立 DAG 节点。Flash 未解决、单维置信不足或同构念规则冲突时，
只升级该 criterion 到 qwen3.8-flash。模型分不能高于确定性缺陷推导出的 cap。

`authorship_specificity` 使用视觉与文案的低置信代理检查机械卡片化、组件重复、空泛标题和
套路句式。它只能作为加分构念与 REVIEW 信号；极简、黑白、暗色和无装饰本身不扣分。

## 四轨训练准入

每次 v8 Run 独立输出 visual、layout、content、full-deck 的 TRAIN/REVIEW/REJECT：

- 分数 ≥80 且相关硬门通过：TRAIN；
- 60–79.999 或证据缺失：REVIEW；
- <60 或关键 CRITICAL：REJECT；
- content 没有 request/source/GT 时不得自动 TRAIN；
- raster-only 只允许 visual 轨，其余轨 REJECT。

训练准入与用户侧 PASS/REVIEW/FAIL 是不同合同，不能互相替代。

## 已知边界

当前已提供 rendered text-region 对比度和 embedded/display pixel ratio 的确定性代理；真实
OCR 文本裁切、语义裁切和复杂图像含义仍主要依赖 VLM/renderer evidence。v8 是
预研默认，不代表已完成人类金标校准。现有真实切片只能用于合同与排序 sanity check，不能
反向拟合本方法中的权重和阈值。
