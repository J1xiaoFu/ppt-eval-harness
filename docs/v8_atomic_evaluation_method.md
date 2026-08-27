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
- CRITICAL 且属于 critical/key unit 时，输出封顶 0.34；v8 functional gate 只统计显式
  functional allowlist，并先按 primary owner/metric 计算 prevalence，禁止不同 scope 的原子数量
  互相稀释。geometry、typography、contrast、resolution 等可争议视觉候选必须由同构念 VLM
  在实际采样页确认；未覆盖或低置信时为 N/A/REVIEW，不由规则直接落锤。语言、authorship
  和审美 observation 不进入硬门，避免“加分项扣分 + 全局 multiplier”的双罚。

## 构念融合

Profile 不再按 deterministic/VLM 分组，而按质量属性计分。确定性规则负责事实、绝对缺陷
和 cap；VLM 负责语义与正向审美信号：

- content structure：21%
- language consistency：4%
- composition craft：20%
- typography craft：10%
- palette craft：8%
- visual communication：15%
- visual system/sequence：10%
- authorship specificity v2：12%

历史六个视觉 criterion 仍保持不变；v8.2 另加一个只服务 authorship 构念的跨页原子
criterion。七个 v8 criterion 都是独立 DAG 节点，以 `qwen3.8-flash` 为主线；结果未解决、
单维置信不足或同构念规则冲突时，只把该 criterion 升级到独立 BigModel Provider 的
`glm-5.3-flash`。Prompt、权重和 Reducer 不因 Provider 切换而改变。composition 等高置信
事实规则继续提供 cap；authorship 规则是低置信风险传感器，采用同构念 30/70 融合而不作
绝对 cap。两次调用的模型身份、fingerprint、Evidence、token 和报告成本
分别保留在 `routing_attempts`，总成本不会只计算最终被采用的结果。
若厂商 usage 不含货币费用，attempt 标记 `cost_known=false`；数值 `0.0` 不解释为免费，
也不能在尚无版本化价格表时宣称成本预算已经覆盖该调用。

`language_consistency` 是中英内部混用/部分本地化的唯一 owner；明确请求语言不符合仍由
scenario instruction 负责。专业缩写和声明的系统性双语不扣分。

`authorship_specificity_v2` 不输出“AI 生成概率”，只评可观察的 formulaicity/specificity：
规则提供跨页同构模板、机械卡片、图标仪式、占位案例和公式化文案事实，风险采样后的跨页
VLM 提供语义判断，二者只融合为一个 12% 公式入口。极简、黑白、暗色、统一品牌系统、
单个合理 taxonomy/checklist/process 布局和功能性图标本身不扣分。

## 四轨训练准入

每次 v8 Run 独立输出 visual、layout、content、full-deck 的 TRAIN/REVIEW/REJECT：

- 分数 ≥80 且相关硬门通过：TRAIN；
- 60–79.999 或证据缺失：REVIEW；
- <60 或关键 CRITICAL：REJECT；
- content 没有 request/source/GT 时不得自动 TRAIN；
- raster-only 只允许 visual 轨，其余轨 REJECT。

训练准入只消费 gate 的 `CONFIRMED` verdict，不再直接读取原始 CRITICAL observation；
contestable gate 为 UNRESOLVED 时相关 run 进入 REVIEW，而不是由规则直接 REJECT。

训练准入与用户侧 PASS/REVIEW/FAIL 是不同合同，不能互相替代。

## 已知边界

当前已提供 rendered text-region 对比度和 embedded/display pixel ratio 的确定性代理；真实
OCR 文本裁切、语义裁切和复杂图像含义仍主要依赖 VLM/renderer evidence。v8 是
预研默认，不代表已完成人类金标校准。现有真实切片只能用于合同与排序 sanity check，不能
反向拟合本方法中的权重和阈值。
