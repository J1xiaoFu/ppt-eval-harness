# 架构决策记录（ADR 摘要）

## ADR-001 模块化单体，accepted

**上下文：** 首期团队和吞吐不足以证明微服务收益。**决定：** 领域模块边界清晰，进程可分 Worker，
元数据保持单库。**后果：** 部署简单；以后按渲染/Judge 资源形态拆分而非按名词拆分。

## ADR-002 强制注入基础 Oracle，accepted

**决定：** `ProfileCompiler` 在任何场景 DAG 中首先注入唯一的 `BaselinePptQualityOracle`；配置不能禁用。
**理由：** 把兜底从操作约定升级为可测试的不变量。

## ADR-003 ERROR 与 FAIL 分离，accepted

执行错误不得变成质量零分。重试耗尽触发降级或人审；只有具有质量反证的 Oracle 返回 `FAIL`。

## ADR-004 外乘内加聚合，accepted

不可补偿、高置信缺陷用 `{0,0.5,1}` 乘子；可补偿性能加权。Profile 编译时验证无重复 `defect_id`。

## ADR-005 双渲染器与 Adapter，accepted

PowerPoint 为主要视觉基准，LibreOffice 用于兼容性差分。二者版本/字体均写 Manifest，不能互为无声替代。

## ADR-006 追加式审计与 Transactional Outbox，accepted

机器结果、人工复核和参数候选分别成事件；修订用 `supersedes`，不更新旧记录。

## ADR-007 模型只存在于 Oracle 内部，accepted

模型不得生成执行 DAG、修改权重、决定发布或绕过门禁。总控完全确定性并可由序列化 DAG 回放。

## ADR-008 参数候选人工发布，accepted

飞轮可排序候选，但发布必须冻结集回放、审批、Shadow 和 champion/challenger 对比。

## ADR-009 证据优先于自然语言理由，accepted

自然语言解释是派生视图；处罚必须先有结构化定位证据、版本和置信度。

## ADR-010 幂等异步 Job API，accepted

`Idempotency-Key + input/profile hash` 唯一确定 Job；重复提交复用结果或返回冲突，不重复计费。

