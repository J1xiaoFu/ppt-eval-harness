# Profile 8.4 自适应视觉审计

Profile 8.4 不让 VLM 一次性给整份 PPT 打总分。它先低成本覆盖全页，再把高清
像素证据送给相互独立的局部 criterion，最后仍由确定性 Reducer 和 PPT-PDMS
聚合。

```text
BASELINE
→ OBSERVE: deterministic AtomicObservation
→ OBSERVE: VisualPageIndex
→ AUDIT: 4×4 Atlas Scout
→ SELECT: VisualSelectionPlan
→ AUDIT: every criterion's shared-cohort initial seed
→ AUDIT: raster text recovery when applicable
→ AUDIT: independent criterion refinement
→ FUSE: VisualCoverageCertificate
→ REDUCE: Composite 8.4
→ PDMS / Decision / Training Eligibility
```

## 1. 全页低成本观测

`VisualPageIndex@1.0.0` 遍历每一页，记录页面角色、文字量、对象密度、图片面积、
页图/素材感知 hash、版式 silhouette、色彩直方图、图像熵、边缘密度、raster-only、
caption/alt/OCR 缺口及规则风险。一个极保守的对象树—像素一致性代理只在对象树含有
大量可见文本、而渲染图近乎无信息时标记异常；它只路由 `render_integrity`，不计分。

页面同时进入两类确定性聚类：

- layout/style cluster：发现版式代表页和单页风格离群；
- asset/content cluster：发现重复素材、素材槽变体和内容离群。

cluster 只用于选页和不确定区间，不会把 medoid 分数复制给未审页。每类 cluster
只有一个覆盖 owner：layout/style 由 `composition_layout` 负责，asset/content 由
`imagery_data_visualization` 负责；cross-slide/authorship 不会为了同一个 medoid
重复制造覆盖门槛。

## 2. Atlas Scout

`Atlas Scout@1.0.0` 把每 16 页缩略图排成 4×4 contact sheet，每格显示清晰的原始页码。
一次请求最多 12 张 Atlas，即 192 页；更长 deck 自动分批。Scout 的合法输出只有：

- 原始页码；
- 风险码；
- 置信度；
- 建议的后续 criterion。

风险码包括占位视觉、图库水印、语义错配、重复库图、图内文字密集、图解文字
不可读和渲染异常。Scout 不输出分数、严重度或 PASS/FAIL。Qwen 3.8 为主；仅
在传输或严格响应合同失败时，使用同一请求的 GLM 5.3 复核一次。

## 3. 统一选页

`VisualSelectionPlan@3.0.0` 按固定优先级定义所有 criterion 共用的页面集：

1. P0：规则 CRITICAL 和未解硬门；
2. P1：Scout 置信度至少 0.70 的占位、水印、错配、不可观测关键页，或高置信
   对象树—像素矛盾页；
3. P2：规则 MAJOR、cluster 离群、图片主导页和有意义的 OCR 缺口；
4. P3：cluster medoid、封面、数据页、结尾和 deck-hash 探索页。

普通高清预算是 `Bmax=min(N,16,4+ceil(sqrt(N)))`。P0 不占普通预算，但仍受运行总
成本与超时限制。小装饰 icon 和全局共享 logo 不会因为没有 OCR/alt text 泛滥为 P2。

## 4. 渐进式高清复核与输入复用

- page-local criteria 使用同一个 4 页稳定前缀；
- cross-slide/authorship 使用同一个最多 8 页前缀；
- criterion 专属风险页在缓存边界后传输；
- 每次只新增 2 个唯一页，失败的页不计为已完成高清审计；
- 仅在新发现 MAJOR/CRITICAL、低置信、规则冲突、criterion-owned cluster medoid 未覆盖、
  素材上下文未完整或 Profile 权重的乐观/悲观 PDMS 区间跨越 60/80 时继续。

执行分两相：全部 criterion 先各自审计公共 cohort，raster-only 的文字恢复也在此时
完成；只有在完整 provisional 构念向量就绪后，criterion 才进入独立 refinement。
PDMS 区间对上下界分别重跑 Reducer 和 `PptPdmsAggregator`，包含 Profile 权重、场景
lambda 与已知硬门；未知构念只使用明确的 0/1 数学边界，不用中性分，也不重归一化。

高清审计首次发现占位图、水印、语义错配或不可读图内文字时，会惰性加入同 hash、
相邻页和 asset-cluster medoid。该替换不能移除 P0/P1、公共前缀、已审页或任何 cluster
medoid，普通页总量仍不超过 Bmax；空间不足时保留 unresolved context 并转 REVIEW。

Qwen 请求顺序固定为：公共系统合同 → 页码标签与公共图片 → `cache_control` →
criterion rubric → criterion 风险页。是否真正命中缓存只看 Provider 返回的
`cached_tokens`；未确认的 GLM 视觉缓存不进成本估算。

规则触发的页面还会携带同构念 `rule_hypotheses`（metric、severity、page、object、
bbox、defect 与有界摘要）。Prompt 明确声明这些内容是不可信、可能错误的定位线索；
VLM 必须依据已上传像素独立确认或否定，不能因为规则提出假设就直接判缺陷成立。

Profile 8.4 使用线程安全的全局请求账本。每次 Provider 调用在发出 HTTP 前按最大重试
上界原子预留，返回后只结算可证明的实际次数；超时后台线程的预留会一直保留到它真正
返回。因此 `settled + in-flight upper bound` 永远不超过 Profile 的 64 次硬上限。

## 5. 计分边界与审计链

Profile 8.4 不改变现有八项基础权重、四场景 lambda、PDMS 或 60/80 训练阈值。
`visual_asset_semantic_risk` 和 Scout 只路由页面；占位、水印、错配和图内文字缺陷最终仅
由 `visual_communication` 计分，不重复惩罚。`render_integrity` 没有诊断触发时返回诊断型
SKIPPED/N/A，不影响 Coverage 或总分。

每次运行持久化并在 Manifest 绑定：

- `VisualPageIndex`；
- `AtlasScoutResult`；
- `VisualSelectionPlan`；
- `VisualAuditRound[]`；
- `VisualCoverageCertificate`。

Coverage 不完整时强制 REVIEW。主审计页只呈现一条可读的“视觉证据未完整”问题；
页面索引、Scout、轮次、模型路由和 Token 遥测放在完整审计抽屉中。
`VisualAuditRound` 只记录真实发生的两页 refinement Provider 调用；公共 cohort 已足够
时 round count 合法地为 0，不会再按最终页面并集事后伪造轮次。

## 6. 兼容性

默认 Profile 为 8.4。四份 `*_v83.json` 保留为显式回放合同，可通过 CLI 的 `--profile`
或 Python 配置加载；当前 HTTP API 始终使用服务默认 Profile。EvalReport/Audit schema
仍为 `1.0`，HTTP 命名空间仍为 `/v1`。
