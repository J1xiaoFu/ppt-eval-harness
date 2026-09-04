# 0.9.0 / Profile 8.4 自适应视觉评测验证

日期：2026-09-04
状态：**候选实现与离线验收完成；真实运行稳定性门禁未通过，不发布 0.9.0**

## 结论摘要

Profile 8.4 已完成自适应视觉底座、两阶段原子审计、全局请求预算和完整审计链的
工程实现。冻结的离线长 deck 挑战集已超过本轮预注册的重大缺陷召回和视觉输入
降本目标；它证明选页与缓存策略在冻结 fixture 上有效，**不等价于真实 VLM
识别精度**。

22 份 Slides-Align 真实 PPT 最终均生成合法报告和有效审计链。余额耗尽造成的失败
checkpoint 已在充值后选择性重跑，不作为模型或 Profile 的失败基线。最终仍有两个长 deck
共 6 个原子视觉节点达到 600 秒上限；fallback 后逻辑审计合法率为
`212/218 = 97.2477%`，低于预注册的 98%。因此本轮只正式发布 0.8.7；Profile 8.4
继续保留在 `codex/adaptive-visual-090` 候选分支，不切换 `main` 默认 Profile。

## 验证对象与版本冻结

| 层级 | 冻结合同 |
|---|---|
| 产品 | `0.9.0` |
| 默认 Evaluation Profile | `8.4`（四场景同时切换） |
| 显式历史回放 | `8.3` |
| Composite | `8.4.0` |
| Atomic Observation | `2.2.0` |
| Grounded VLM | visual / authorship / raster text 均为 `3.0.0` |
| Selection / Index / Atlas | `3.0.0` / `1.0.0` / `1.0.0` |
| Visual round / Coverage certificate | `1.0.0` / `1.0.0` |
| Attention | `audit-attention@0.9.0` |
| EvalReport / Audit / Model audit schema | 均保持 `1.0` |
| HTTP API | 保持 `/v1` |
| 图片 CAS / 资产传输 / Qwen cache wire | `1.0.0` / `1.0.0` / `2.0.0` |
| 真实评测执行 Git SHA | `441d7f8abf120c868a80aa43874d9d6c78696934` |

机器可读版本源为 `release/version-matrix.json`。本报告不将产品版本、Profile 版本、
Prompt/Oracle 版本或 schema 版本混为同一概念。

## 本轮预注册目标

Profile 8.4 不调整八项基础权重、四场景 lambda、PPT-PDMS 公式或 60/80 决策阈值。
本轮只验证“更可靠地找到应看页面，同时降低重复视觉输入”：

| 指标 | 预注册门槛 | 用途 |
|---|---:|---|
| MAJOR/CRITICAL 页级召回提升 | 至少 `+10pp` | 离线挑战集主门槛 |
| precision 下降 | 不超过 `3pp` | 防止扩张式误报 |
| 已标注规则盲区 false PASS | `0` | 占位图/水印/图内文字等风险 |
| 平均未缓存视觉输入 Token | 下降至少 `25%` | 离线成本模型 |
| 版本化视觉输入费用估算 | 下降至少 `25%` | 非账单的比较性指标 |
| 真实切片模型响应合法率 | 不低于 `98%` | 真实运行门槛 |
| 真实切片报告与审计链 | 22/22 合法 | 真实运行门槛 |

Slides-Align 的 Spearman、pairwise 和 Composite 方差只报告诊断，不用于本轮拟合
权重/Prompt，也不是 0.9.0 的发布门槛。

## 被验证的执行链

```text
BASELINE
→ deterministic AtomicObservation
→ VisualPageIndex
→ 4×4 Atlas Scout
→ VisualSelectionPlan
→ every criterion's shared-cohort initial seed
→ raster text recovery when applicable
→ independent criterion refinement
→ VisualCoverageCertificate
→ Composite 8.4 / PPT-PDMS / Decision / Training Eligibility
```

核心约束如下：

- Scout 只输出页码、风险码、置信度和建议 criterion，不输出分数或 PASS/FAIL。
- 普通高清页预算为 `min(N, 16, 4 + ceil(sqrt(N)))`；规则 CRITICAL 页不占普通探索
  预算，但仍受全局请求、成本和超时限制。
- page-local criterion 共享稳定 4 页前缀；cross-slide/authorship 共享最多 8 页前缀。
- 所有 criterion 先完成 provisional seed，再根据完整构念向量的乐观/悲观 PDMS 区间
  判断是否追加两页一组的 refinement。
- 规则只提供与 criterion 同构念的 page/object/bbox/defect 定位假设；VLM 必须依据
  已上传像素独立确认或否定。
- Qwen `qwen3.8-flash` 为主线；仅在同构念低置信、冲突或响应失败时使用
  GLM `glm-5.3-flash`。
- 每次 Provider 调用在 HTTP 前原子预留最坏重试上界；`settled + in-flight upper bound`
  不得超过 Profile 的 `64` 次。超时后仍未返回的请求不释放额度。
- Profile 8.4 的原子节点超时为 600 秒。Scout 在外层超时前尝试持久化失败合同；
  Index/Scout/Plan 任一缺失时，普通视觉与 raster 恢复均不发送无计划模型请求。
- 原始 PPTX 永不发给模型供应商；模型只接收经解码、限幅、去元数据、确定性重编码
  和 hash 校验的页图/Atlas/crop。

## 离线长 deck 挑战集

可重现命令：

```powershell
python scripts/benchmarks/profile84_long_deck_acceptance.py `
  --output var/benchmarks/profile84-long-deck.json
```

固定挑战集由 20/50/100 页的 3 份 deck 组成，共 170 页、18 个冻结的重大缺陷。
缺陷包括占位图、图库水印、图内文字、语义错配、单页风格异常和规则已知的排版问题。

| 离线指标 | Profile 8.3 | Profile 8.4 | 变化 | 结论 |
|---|---:|---:|---:|---|
| MAJOR/CRITICAL 召回 | 16.67% | 100% | +83.33pp | 通过 |
| precision | 100% | 100% | 0pp | 通过 |
| 规则盲区 false PASS | 12 | 0 | -12 | 通过 |
| 平均未缓存视觉输入 Token | 45,056 | 22,741.33 | -49.53% | 通过 |
| 版本化视觉输入费用估算 | 0.0405504 | 0.02439936 | -39.83% | 通过 |

该成本估算只包含版本化的视觉输入假设，排除文本、输出、Provider 重试、fallback
和网络存储；它不是供应商账单。选页命中由冻结 Scout/审计 fixture 判定，不是对
Qwen/GLM 真实视觉能力的估计。

## Slides-Align 真实切片合同

- Dataset：`Yqy6/Slides-Align`。
- Revision：`2f50ac6674a506acb245275e58c8a452c00e6a14`。
- Difficulty：`topic_introduction`。
- 主题：`chinese_new_year` 7 份、`stock_market` 8 份、`modern_architecture` 7 份。
- 总规模：22 份 PPTX、420 张官方渲染图。
- 数据 root manifest SHA-256：
  `8527c2cadaa3cbdbd88c827dd432b69028963ebae267413e896727616716aaa6`。
- Manifest 完整校验：447 个文件、500,387,155 bytes。

运行前必须重新校验 manifest 数量、size/SHA-256/LFS SHA、PPTX ZIP/CRC 和 slide XML 数量。
官方渲染图复制到只含页码/hash 的匿名运行路径后才能进入 Oracle。产品名、`rank_*`
和人评顺序不得进入 EvalCase、artifacts、运行路径或模型输入。人评 GT 仅由 root manifest
hash 锚定的主题排名子集提供。

## 统计与合法性口径

### 模型响应合法率

合法率使用 `POST_FALLBACK_LOGICAL_AUDIT_CONTRACT_V2`：

- 统计单位是 fallback 和内部重试后的“逻辑原子审计”，不是简单的 HTTP 次数。
- initial/final 两阶段通过 request fingerprint 去重，同一 seed 不重复计数。
- 成功 fallback 以最终可用响应计一次合法审计；失败的中间尝试仍保留在完整 lineage。
- 模型节点超时、成本预算导致未返回结构响应时，必须计为一次不合法逻辑审计，
  不允许因缺少 routing attempt 而从分母消失。

### 排名统计

- 只在同一主题内计算 Spearman 和 pairwise，禁止跨主题 global rank。
- 只有某主题全部 case 均为 FULL 时，该主题才输出 formal rank statistics。
- 任一 DEGRADED case 会抑制对应主题的 formal 统计，但仍可输出标记为
  `NOT_FOR_GATING_OR_WEIGHT_FIT` 的探索诊断。
- `all_topics_rank_eligible` 继续报告，但不得作为稳定性 validation gate。排名统计也不是
  本轮 release gate。

稳定性 validation gate 只在以下条件同时满足时通过：22 份全部 COMPLETED、报告合法、
base score 有限、append-only audit chain 有效，且逻辑审计合法率达到 98%。

## 余额耗尽实验条件的处置

余额耗尽是已纠正的外部实验条件，不是下列任一结论：

- 不是 Oracle 将合法 PPT 判为失败；
- 不是 Profile 8.4 的规则、选页或聚合失效；
- 不是 Qwen/GLM 质量基线；
- 余额期未完成或非法的 checkpoint 不能用于计算 Spearman、合法率、ERROR 率、
  成本或墙钟时间。

最终流程保留 8 个原本已经满足逻辑审计合同的 checkpoint，只重跑 14 个失败
checkpoint；随后对仍有错误的 4 个长尾 case 使用 2 workers 复核。Qwen 中间尝试失败但
GLM 以同一 request fingerprint 完成 fallback 的 case 仍是一个合法逻辑审计；中间 HTTP
attempt 只保留在完整 lineage 和 request telemetry 中，不计作最终逻辑失败。

原余额期未完成产物保持在 ignored `var/` 中仅供故障溯源，不进入 Git 或最终统计引用。
最终聚合只读取 22 个 checkpoint 明确指向的报告，不扫描 runtime 中的历史 runs。复用必须
同时匹配评测 Git SHA、Profile fingerprint、数据与 render hash，并验证唯一 hash-linked
`RUN_COMPLETED`、报告合法性和 append-only 审计链。

## 22 份余额纠正后选择性重跑结果

最终聚合文件为 `var/benchmarks/slides-align-profile84-live/comparison.json`，SHA-256 为
`1c4593113e4b014b0985e3fdefbd55d51f32026ebd24cd0b829d4db51937a0e6`。以下数值只来自
该文件及其 22 份 checkpoint 绑定的 EvalReport/Manifest/Audit。

### 运行完整性

| 指标 | 门槛 | 最终选择性重跑观测 | 状态 |
|---|---:|---|---|
| COMPLETED cases | 22/22 | 22/22 | 通过 |
| 合法 EvalReport | 22/22 | 22/22 | 通过 |
| 有限 base score | 22/22 | 22/22 | 通过 |
| 有效 append-only audit chain | 22/22 | 22/22 | 通过 |
| 模型逻辑审计合法率 | ≥98% | 212/218，97.2477% | **未通过** |
| 稳定性 validation gate | 全部条件成立 | `false` | **未通过** |

### 结果与覆盖

| 指标 | 最终选择性重跑观测 |
|---|---|
| Decision：PASS / REVIEW / FAIL | 0 / 17 / 5 |
| Coverage：FULL / DEGRADED / BASE_ONLY / UNASSESSABLE | 5 / 17 / 0 / 0 |
| OracleResult execution SUCCESS / ERROR | 698 / 6 |
| metric SCORED / PASS / FAIL / N/A / ERROR | 457 / 66 / 5 / 170 / 6 |
| Atlas 全页覆盖完整 | 22/22 |
| Visual Coverage certificate complete | 0/22 |
| stopping reason | unresolved risk 7；forced page 6；selection budget 5；mandatory criterion unrouted 4 |
| visual 训练准入 | TRAIN 1 / REVIEW 13 / REJECT 8 |
| layout 训练准入 | TRAIN 8 / REVIEW 10 / REJECT 4 |
| content 训练准入 | TRAIN 0 / REVIEW 17 / REJECT 5 |
| full_deck 训练准入 | TRAIN 0 / REVIEW 14 / REJECT 8 |

6 个 ERROR 均为 `ORACLE_EXCEPTION`，错误文本统一为
`TimeoutError: Oracle exceeded configured timeout of 600 seconds`：

- Chinese New Year / Quake：composition、typography、color、imagery，共 4 个；
- stock market / Quake：composition、color，共 2 个。

它们没有可验证的 response fingerprint，按 V2 合同各计一个不合法逻辑审计。最终 6 个
不合法逻辑审计全部是 600 秒 timeout；5 个保留 case 中的余额期 Qwen HTTP 400 均已被
GLM fallback 恢复，只作为 provider-attempt telemetry 留在完整 lineage，不计作失败基线或
不合法逻辑审计。

唯一进入 visual TRAIN 的 case 是 `modern_architecture/kimi_banana_rank_3`；没有 case
进入 content 或 full_deck TRAIN。报告 Coverage 的 5 个 FULL 不等于 Visual Coverage
certificate 完整：后者要求所有必审风险、cluster 与 criterion 停止条件同时闭合，本轮为 0/22。
训练轨计数来自 22 个 checkpoint 绑定的 EvalReport；`comparison.json` 只保存其路径，
不重复嵌入训练准入对象。

### 请求、Token 与成本

| 指标 | 最终选择性重跑观测 |
|---|---|
| 逻辑审计数 / 合法响应数 | 218 / 212 |
| 全局 request ledger | settled 383；in-flight upper bound 4；accounted upper bound 387 |
| request budget | maximum 1,408；remaining 1,021；settled reservations 317；0 case exhausted |
| ledger reconciliation | 20/22；两个超时 case 中一个仍保留 4 次 fail-closed in-flight 预留 |
| input / output / total Token | 3,682,576 / 659,528 / 4,342,104，仅 11/22 usage 完整 case 可汇总 |
| image / cached / cache-creation Token | 440,227（3 case）/ 1,137,836（11 case）/ 未完整报告 |
| request bytes | 924,602,081，仅 11 个具有可完整相加字段的 case |
| `cost_known=true` cases 与可验证费用 | 0/22；无可验证账单费用 |
| 单 case duration 平均 / P50 / P95 | 17.66 / 14.53 / 38.21 分钟；P95 为 nearest-rank |
| 单 case duration min / max | 7.32 / 47.99 分钟 |

usage 不完整时不得伪造 0 Token 总量或把 `cost=0` 解释为免费。任一 in-flight 预留、
ledger 与 seed/final 去重结果不一致，或任一 attempt 缺 usage，都必须将 `usage_complete`
标为 false。

### 同主题人评对齐诊断

| 主题 | N | 报告 FULL | Visual complete | Formal Spearman | Exploratory Spearman | Pairwise |
|---|---:|---:|---:|---:|---:|---:|
| chinese_new_year | 7 | 2 | 0 | suppressed | -0.142857 | 0.476190 |
| modern_architecture | 7 | 2 | 0 | suppressed | -0.214286 | 0.428571 |
| stock_market | 8 | 1 | 0 | suppressed | -0.071429 | 0.464286 |

Macro Spearman 只能对可用的主题内统计等权汇总；Micro Pairwise 只能汇总主题内可比 pair。
不计算跨主题全局排序。无论数值如何，本轮都不用该切片重拟权重、阈值或 Prompt。

本轮探索 Macro Spearman 为 `-0.142857`，Micro Pairwise 为 `0.457143`（70 个主题内
pair）。三个主题都因 Visual Coverage certificate 不完整而抑制 formal 统计。下面的
Composite 结果同样只用于诊断；方差是三个主题内 population variance 的等权平均：

| Composite | Macro Spearman | 平均主题内方差 |
|---|---:|---:|
| `content_structure` | 0.218254 | 0.011272 |
| `composition_craft` | 0.108500 | 0.016187 |
| `typography_craft` | -0.151587 | 0.016668 |
| `palette_craft` | 0.102381 | 0.011450 |
| `visual_communication` | 0.200770 | 0.019095 |
| `visual_system_sequence` | -0.353175 | 0.016605 |
| `authorship_specificity_v2` | -0.210317 | 0.009980 |
| `language_consistency` | 0.024282 | 0.027628 |
| `v8_functional_integrity` | 1.000000 | 0.020833 |
| `critical_content_visibility` | N/A（常量） | 0.000000 |
| `file_deliverability` | N/A（常量） | 0.000000 |
| `internal_data_consistency` | N/A（常量） | 0.000000 |

`v8_functional_integrity` 的高相关来自稀疏可用值，不能据此宣称稳定预测能力；负相关或
低方差也不在本轮调权，因为该切片没有原子/Composite 级 GT，且所有 formal 统计均被抑制。

## 审计与可重现性要求

每个有效 case 必须同时保留并完成 hash 绑定：

- EvalReport 与 RunManifest；
- 原子 Observation 全量 artifact；
- VisualPageIndex；
- AtlasScoutResult；
- VisualSelectionPlan；
- 真实 VisualAuditRound 列表；
- VisualCoverageCertificate；
- 匿名页图与 render manifest；
- append-only AuditEvent 链及唯一的 hash-linked `RUN_COMPLETED`。

Resume 必须同时绑定 Evaluation Git SHA、Profile fingerprint、固定派生报告路径与有效
审计链。HTML 只是审计入口，不是数据源；每个展示数值都必须能回溯到上述
JSON/artifact。

## 通用工程门禁

| 门禁 | 最终结果 |
|---|---|
| 全量 pytest | 380 passed |
| dependency-free runner | 380 passed, 0 skipped, 0 failed |
| Ruff（src/tests/scripts） | 通过 |
| 本轮触及生产 Python strict mypy | 通过 |
| UI TypeScript/Vite | 通过，1809 modules |
| 版本同步检查 | 通过，product 0.9.0 / Profile 8.4 |
| OpenAPI / `docker compose config --quiet` | 通过 |
| Docker 无缓存构建 | 通过，镜像内 `pip check` 无破损依赖 |
| Docker 无 Key 评测—审计闭环 | 通过：上传、报告、页图、P1 resolution、不可变 ReviewEvent |
| GitHub 隔离 clone 候选分支冷启动 | `PENDING_FINAL_BRANCH_COMMIT` |
| `git diff --check` / 密钥扫描 / 宿主路径扫描 | 当前通过；提交后复核 |
| 真实切片 Manifest/artifact hash 与审计链 | 22/22 通过 |

## 限制与剩余风险

- 离线长 deck 挑战集使用冻结 Scout 和精确审计 fixture，不能替代真实模型响应质量。
- Slides-Align 只提供主题内整体顺序，没有 Composite/defect 级 GT；不能由整体相关性
  推导某个 Oracle 的因果有效性。
- 本轮不新增重型 OCR/embedding 依赖；图内文字和复杂图解仍依赖 Scout 路由和局部
  VLM 审计。
- 对象树—像素矛盾代理保持保守路由定位，不是通用渲染差分或质量分数。
- 供应商模型、计费和缓存行为可漂移；只有实际 `cached_tokens`、usage 和账单能证明真实节省。
- Base64 仍是单机默认。Signed URL 只在配置公网 HTTPS 基址与 HMAC 密钥时启用，
  未验证的外部对象存储不是本轮默认依赖。
- 本轮未实施数据飞轮任务池、自动候选生成、Shadow/A-B 或发布控制面。

## 最终决策

- 真实切片 validation gate：`FAILED_MODEL_RESPONSE_LEGALITY_97.2477_PERCENT`。
- 离线平衡验收：`PASSED_OFFLINE_FIXTURE_ONLY`。
- `main`：发布 0.8.7，继续默认 Profile 8.3。
- `codex/adaptive-visual-090`：保留 Profile 8.4 完整实现、验证证据与未通过原因。
- 不将 Profile 8.4 合并到 `main`，不创建 `v0.9.0` 或 `eval-profile/v8.4` 标签。

该决策只由预注册稳定性门槛触发，不把已纠正的余额事件伪装成模型能力失败，也不通过
继续重试、删除超时节点或挑选较好 run 来改变结论。下一轮应先降低长 deck criterion
的单节点尾延迟并完善 timeout 后的可续跑原子 checkpoint，再重新执行同一冻结协议。
