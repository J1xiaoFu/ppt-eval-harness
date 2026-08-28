# MVP 验证报告

## 目标与问题

验证当前实现是否真实满足三项最小承诺：基础质量 Oracle 永不被场景绕过；PPT-PDMS 不混淆 `NA/ERROR/FAIL`；所有运行都能回溯到原子证据和不可变审计事件。本报告不替代 400 例金标、人工一致性或生产 Shadow。

## 执行过程

- `TST-MVP-001`：使用 `scripts/run_tests.py` 执行 7 个测试文件中的 42 个 plain-assert 测试。
- `EXP-MVP-FOUR-SCENES-001`：对同一份可编辑 `aurora_demo.pptx` 执行四类 Profile，避免文件质量差异掩盖场景降级逻辑。
- `EXP-AUDIT-MVP-001`：执行人工复核写入、Run Markdown/HTML 导出和 JSONL 哈希链验证。
- `REPORT-MVP-001`：校验三阶段审计快照，生成只读 HTML、九页面试 deck-data 和构建 manifest。

## 证据与结果

| 证据 ID | 结果 |
|---|---|
| `TST-MVP-001` | 42 passed，0 failed；覆盖 Harness、评分属性、PPTX 安全、联网事实快照、数据飞轮治理、Oracle、降级、持久化和审计 |
| `RUN-MVP-READY-001` | `FULL/PASS`，`S_base=S_full=95.455769` |
| `RUN-MVP-TEXT-001` | 缺可信事实证据时 `DEGRADED/REVIEW`，保留 `S_base=95.455769`，不产生 `S_full` |
| `RUN-MVP-PROJECT-001` | 来源专项完整执行，`FULL/FAIL`，`S_full=51.406008` |
| `RUN-MVP-MULTIMODAL-001` | 缺素材/图表证据时 `DEGRADED/REVIEW`，保留本体分，不产生完整分 |
| `EXP-AUDIT-MVP-001` | 复核事件写入后哈希链仍为 `valid=true`；Run 成功导出 Markdown 与无脚本 HTML |
| `REPORT-MVP-001` | 3 phases、9 slides、13 example events；HTML 无 JavaScript 与表单 |

## 决策与取舍

- `fact_quality` 没有可信快照时是 required `NA`，因此触发降级而不是乐观给分；联网检索必须由后续受控 Provider 生成可审计快照。
- 当前视觉指标使用 OOXML 几何与字体证据，尚未将 VLM 像素 Judge 作为生产评分信号；这避免用未校准审美模型制造虚假可信度。
- 测试环境不能安装 pytest，因此保留 pytest 兼容测试函数，并增加零依赖函数运行器；这不是降低测试断言数量。

## 阶段门禁与遗留风险

- MVP 工程门禁通过；生产统计门禁未通过且保持 `pending`。
- 尚缺 400 个基础金标、2,000 个受控缺陷、人工 α/ICC、pairwise、误放行置信区间和 1,000 份 Shadow。
- FastAPI API、异步 Job 和 React 复核台已完成本地构建与交互 Smoke；Celery/PostgreSQL/S3 适配和 Compose 配置已提供，但仍未做真实集群故障演练。
- PowerPoint Renderer Adapter 已通过 16.0.20326.20100 实测；最终面试 PPT 另经 artifact-tool 与 presentation helper 渲染复核。LibreOffice 差分仍留待容器环境补测。
- 历史面试 PPT、渲染图与 Node 构建阻塞记录已迁入 `archive/v8.3-pre-release`，不属于当前发布制品。
