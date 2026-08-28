# 0.8.6 预发布稳定性收敛

日期：2026-08-28

## 触发背景

0.8.5 从 GitHub `main` 冷克隆后完成了无 Key Docker 启动、两项 Batch、Run/制品、浏览器审计、
幂等 ReviewEvent 和重启持久性验证。运行时发现同一页的 `COLOR_CONTRAST` 分别由
`v8_functional_integrity` 和 `slide_pixel_contrast` 生成两张主卡；同时严格冷构建暴露了
基础镜像/依赖漂移和精简镜像无法加载 pytest 测试模块的边界。

## 收敛决策

- 仅对 `_DEFECT_SEMANTICS` 中的具体语义码，按 `(semantic_code, complete affected pages)` 做精确
  跨 family 合并；不使用页面交集，不合并 generic family 或 evidence-integrity 卡。
- 合并时联合原 candidates 后重新调用同一 `_semantic_issue` Reducer；主卡只剩一个 primary owner，
  lineage 保留所有 contributing family、metric、raw issue 和 candidate。
- issue ID 绑定完整页集而不是最多六页的 UI 摘要，并将 Attention 策略升为
  `audit-attention@0.8.6`。
- plain runner 在自身进程内提供严格 pytest facade；只实现当前已使用的 `raises / approx /
  importorskip`，其他 pytest 特性继续显式失败。可选 HTTP 传输不存在时记录 SKIP，不以早退计 PASS。
- Docker `FROM` 锁定现有多架构 digest，pnpm/Node 直接依赖与 Linux/Python 3.11 运行依赖
  均锁定精确版本；Docker 安装使用 constraints、`--no-build-isolation` 和 `pip check`。

## 仍保留的边界

Docker Hub 首次获取缺失基础镜像仍依赖外部网络。Debian apt 仍使用移动仓库，本轮因此提供
“可重复解析的应用层构建”，不声称字节级 hermetic image。完全锁定 apt 需要带日期的
Debian snapshot 与独立安全更新流程，不应仅写一组将来会消失的 `pkg=version`。
