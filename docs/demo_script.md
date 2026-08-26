# 演示与面试讲解脚本

## 0. 开场（30 秒）

“我没有把 PPT 评测做成一次模型打分，而是做成确定性 Agentic Harness：总控负责编排，Oracle
封装具体判断，PPT-PDMS 区分不可补偿错误和可补偿性能，审计链把结果送入受控数据飞轮。”

## 1. 完整文字生成场景

提交包含 request/audience/PPTX 的 Case，展示 DAG 必含基础 Composite，专项 Oracle 成功时输出
`S_base + S_full + FULL`。展开一个基础指标和一个专项指标的页/对象/来源证据。

## 2. 移除上下文后的本体兜底

用相同 PPTX 移除 request，或注入 Scene Oracle 超时。展示：基础节点仍执行；`base_score` 和基础证据
不变；`full_score=null`；完整度 `BASE_ONLY`，决策 `REVIEW`。强调 ERROR 没有变成零分。

## 3. 一个硬乘子

注入“必选素材缺失”或“关键数字与来源矛盾”。展示高置信结构化证据使 `M_scene=0.5/0`；说明外层
乘算表达不可补偿性，而非所有难改问题都乘算，主观审美不会直接清零。

## 4. 一个 Additional 指标

改变非关键受众语气或次要版式，只影响内层加权项。对比分解表，确认 `defect_id` 未同时绑定乘子。

## 5. 人审回流

打开低置信/分歧 Case；人工选择结论、严重度和理由。展示自动结果仍在，新建 `review.recorded` 事件，
编辑 diff 进入工作池而非立即改变生产 Profile。

## 6. 参数候选审批

展示候选的来源样本、校准集变化、冻结集回放、挑战集与 Shadow 差异。拒绝“自动上线”；批准后产生
新不可变 Profile ID 和 ADR/Release 事件。

## 7. Run ID 回放

输入 `RUN-DEMO-0001`，展示 Manifest 的输入/输出 hash、Git/容器/字体/模型/Prompt/Profile/DAG、成本，
沿 `REQ-BASE-001 → ADR-002 → ORC-BASE-001 → TST-ARCH-001 → EXP-DEGRADE-001` 回放。

## 8. 收束（30 秒）

“系统的价值不是一个看似精确的总分，而是：专项失败时仍有本体结论；严重问题不被美观抵消；
每个判断能定位、能复现；数据飞轮能演进，但不能绕过人类治理。”

## 现场备用

- 外部模型不可用：切换合成 Adapter，明确只演示控制流，不宣称质量成绩。
- PowerPoint 渲染不可用：使用已哈希渲染产物与 LibreOffice 兼容证据。
- 网络不可用：事实检索返回 ERROR/REVIEW，不把联网失败解释为事实错误。

