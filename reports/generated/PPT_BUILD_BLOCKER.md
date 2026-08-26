# 九页面试 PPT 构建状态

## 状态

`resolved`。此前 bundled Node.js 在 JavaScript 初始化阶段触发的 `ncrypto::CSPRNG` 问题已由用户修复。
保留本文件路径是为了让历史 `build.blocked` 事件可被审计回放；新的完成事件通过 `supersedes`
指向旧事件，不覆盖历史。

## 最终产物

- `ppt-eval-interview-v3.pptx`：最终三章九页版本，调研、开发、评测各三页；已纳入基线复现与公开数据集调研结论。
- `ppt-eval-interview-v2.pptx`：审计事件 `EVT-000014` 对应的研究更新快照，保留用于历史回放。
- `deck-preview/`：9 张 artifact-tool PNG、9 个 layout JSON 和 montage。
- `ppt-eval-interview-v3/`：从最终 PPTX 重新渲染的 9 张逐页 PNG。
- `ppt-eval-interview-v3-montage.png`：最终 PPTX 渲染总览。
- `ppt-eval-interview-v3-powerpoint-montage.png`：PowerPoint 16.0.20326.20100 原生渲染总览。
- `interview-deck-v3-eval.json`：评测系统对最终面试 PPT 的完整质量报告。

## 验证结果

- artifact-tool 导出成功，最终 PPTX SHA-256：`d4afb4aca8497428c3aefbb30048ac0b5c46091099f14496b25ee65e0cc23add`。
- 9/9 张幻灯片完成 artifact-tool、容器渲染与 PowerPoint 原生渲染检查。
- `slides_test.py`：`Test passed. No overflow detected.`
- 逐页原图检查已修复编号拆行、公式符号、审计链节点、指标标签和第 2 页基线状态口径问题。
- 系统自评：`FULL / PASS`，`base_score = full_score = 87.382170`。
- 9/9 页均包含 `[Sources]` speaker notes。

## 重建命令

按 Presentations skill 设置 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_BIN_DIR` 和 `SKILL_DIR` 后运行：

```powershell
pwsh scripts/reporting/build_all.ps1 `
  -Python $env:RUNTIME_PYTHON `
  -Node $env:RUNTIME_NODE `
  -NodeModules $env:RUNTIME_NODE_MODULES `
  -RuntimeBinDir $env:RUNTIME_BIN_DIR `
  -SkillDir $env:SKILL_DIR
```
