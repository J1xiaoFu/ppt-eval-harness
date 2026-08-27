# v6 六维视觉指标真实金标切片重放

## 结论

v6 以“VLM 占 visual 20%、占总分 10%”作为解盲前预注册的强视觉实验先验。
在 Slides-Align 固定的 7 份 `market_analysis` 真实 PPTX 切片上，重放达到
`7/7 FULL`、`42/42` 六维全部 `SCORED`，因此 21 个人评 pair 全部可比。

但 20% 主方案的合并 visual construct 与人评排名只有 Spearman `-0.21`、
pairwise accuracy `43%`。扩到 25% 后进一步降到 `-0.32 / 38%`。这不支持继续扩权，
也不允许把 20% 标记为已校准生产权重。20% 可保留为 `EXPERIMENTAL` 候选，
但必须先修复单维证据精度、跨维重复归因和 Advanced 同构复核。

## 固定实验契约

- Dataset：`slides_align_market_analysis_sample`
- Upstream revision：`2f50ac6674a506acb245275e58c8a452c00e6a14`
- 输入：7 份 PPTX + 130 张逐页 PNG；137 个文件全部匹配 manifest SHA-256
- Git SHA：`f533076cdb8ae61655d4b569fd24016873fa105f`
- Profile：`finished-deck-v6-structured-visual-dimensions-candidate@6.0`
- Composite：`structured_dimensions.model_audits`
- Prompt / Oracle：`ppt-vlm-structured-visual-dimensions-audit@1.2.0` / `1.2.0`
- Baseline model：`qwen3.7-flash`
- VLM 维度内部预算：`10/10/20/25/10/25`
- 主权重：VLM / visual `20%`；总分硬上限 `10%`
- 敏感性：`10% / 15% / 20% / 25%`；只有 20% 是预注册主结果
- 统计资格：必须 7/7 FULL、每份恰好六个 SCORED 维度、Profile/Prompt/
  Oracle/model/usage/cost/manifest/构念算术全部对齐

正式重放共记录 `563,183` model tokens。DashScope 响应未返回费用字段，因此
Harness 记录 cost `0.0`；这只表示“费用不可观测”，不表示调用免费。

## 主结果与敏感性

| VLM / visual | 角色 | Visual Spearman | 21-pair accuracy | Harness visual 顺序 |
|---:|---|---:|---:|---|
| 10% | sensitivity only | -0.07 | 48% | Skywork-Banana > Gamma > Kimi-Smart > Kimi-Standard > Quake > Skywork > Kimi-Banana |
| 15% | sensitivity only | 0.00 | 52% | Skywork-Banana > Kimi-Smart > Gamma > Kimi-Standard > Quake > Skywork > Kimi-Banana |
| 20% | **preregistered primary** | **-0.21** | **43%** | Kimi-Smart > Gamma > Skywork-Banana > Kimi-Standard > Quake > Skywork > Kimi-Banana |
| 25% | sensitivity only | -0.32 | 38% | Kimi-Smart > Gamma > Kimi-Standard > Skywork-Banana > Quake > Skywork > Kimi-Banana |

不得因 15% 在这 7 份上的 pairwise 数值最高就回填权重。该切片只有一个 topic，
人评是整体可见偏好排序，不是六维标签；delivery/handoff 也没有参与这个排名校准。

作为参照，v6 总分与人评的 Spearman 是 `-0.29`、pairwise `43%`；只含确定性
视觉代理时是 `0.21 / 57%`。这说明当前的主要瓶颈不是“VLM 总权重太低”，而是
VLM 观测契约与人评构念尚未对齐。

## 逐例结果

| Product | Human rank | Decision | v6 total | Deterministic visual | Six-dim VLM | Visual@20% |
|---|---:|---|---:|---:|---:|---:|
| Kimi-Banana | 1 | REVIEW | 58.95 | 79.6 | 38.2 | 71.3 |
| Skywork-Banana | 2 | REVIEW | 83.51 | 96.6 | 45.2 | 86.4 |
| Quake | 3 | REVIEW | 77.35 | 85.9 | 76.2 | 84.0 |
| Kimi-Smart | 4 | PASS | 88.79 | 86.8 | 91.5 | 87.8 |
| Kimi-Standard | 5 | PASS | 88.69 | 85.7 | 85.2 | 85.6 |
| Gamma | 6 | REVIEW | 83.24 | 88.0 | 81.5 | 86.7 |
| Skywork | 8 | REVIEW | 81.19 | 77.1 | 84.5 | 78.6 |

| Product | composition | typography | color | imagery/data-viz | consistency | render |
|---|---:|---:|---:|---:|---:|---:|
| Kimi-Banana | .40 | .20 | .65 | .50 | .30 | .15 |
| Skywork-Banana | .40 | .30 | .70 | .50 | .55 | .25 |
| Quake | .75 | .75 | .90 | .60 | .70 | .85 |
| Kimi-Smart | .95 | .95 | .90 | .90 | .95 | .90 |
| Kimi-Standard | .90 | .85 | .85 | .75 | .95 | .90 |
| Gamma | .90 | .80 | .90 | .80 | .90 | .70 |
| Skywork | .50 | .95 | .90 | .90 | .95 | .80 |

## 三个代表性实例

### 1. Kimi-Banana：可证伪的 VLM 误判与重复扣分

人评第 1，六维 VLM 只有 `.382`。qwen3.7 审计声称第 10/11 页存在大量乱码，
并把这一个判断重复计入 typography、color、imagery 和 render。直接查看固定 PNG 可见，
两页的英文和图示实际清晰可读，“大量乱码”证据不成立。这同时违反了 Prompt 的
“语义/拼写不归 typography”和“同一缺陷只归一个主维度”约束。

这是本轮最重要的定性发现：六维拆分已经使误判可定位，但尚未让模型真正做到构念互斥。

### 2. Kimi-Smart：表面规整度获得高分

人评第 4，六维 VLM `.915`。页面干净、字大、格式统一，因此 composition、typography、
consistency 都在 `.95`左右。但从人评相对名次看，“简洁且一致”不等于更高的信息价值或
视觉表达深度。当前 imagery/data-viz 的 adequacy 约束尚不足以防止模型过度奖励简单整齐。

### 3. Gamma：干净模板与人评偏好不等价

人评第 6，六维 VLM `.815`。页面在对比度、字体和跨页一致性上确实整洁，但部分页面
是通用背景、稀疏文字和低信息密度。VLM 视觉维度给出较高分是内部一致的，却不足以解释
人类对内容丰富度、专业设计深度或主题契合度的偏好。

## qwen3.8-flash Advanced 迁移验证

默认 Advanced API 角色已从 `qwen3.7-plus` 迁移为 `qwen3.8-flash`；最小文本冒烟返回的
实际 model ID 为 `qwen3.8-flash`，且不会静默回退到旧 Plus。

但在 Kimi-Banana 上使用同一 1.2 六维契约做一次影子复核时，qwen3.8 有 evidence 未提供
`page_number/source_uri`，因而六维按契约全部 ERROR。这证明“API 可调用”不等于“已是可用的
六维 Advanced reviewer”。因此 v6 仍保持 Flash-only，qwen3.8 只能在后续新 Profile 中以同构评审
契约接入，失败时直接转人工。

## 下一轮 Oracle / Profile 优化

1. **发现类型化与去重**：每维只允许白名单 defect code，同一 defect ID 不得出现在多维；
   如果模型无法遵守，该批次不计分。
2. **收紧 typography/render 边界**：typography 只判字形清晰、字号、行距和密度，不得以拼写/
   语义扣分；render 必须提供可核对的对象树—像素差异，不得把原始图片内容当导出故障。
3. **改造 imagery adequacy**：新增“本应图解却只有文字”和“通用背景/稀疏占位”类型，防止简单整齐
   自动获得高视觉分。
4. **另发同构 Advanced Profile**：以 `qwen3.8-flash` 只复核触发问题的同一维度，输出独立
   DIAGNOSTIC metric + `reviews_metric_id`，不重算已冻结总分；定位或契约失败直接人工。
5. **建立六维人工金标**：至少双人标注+仲裁，单维 defect precision、页码定位 recall、重复运行
   方差和分层 pairwise 都达标后，才允许 20% 进入生产审批。

## 产物

- 可视化报告：`var/datasets/slides_align_sample/report_qwen_v6_dimensions7_r2/index.html`
- 机器可读对照：`var/datasets/slides_align_sample/report_qwen_v6_dimensions7_r2/comparison.json`
- 逐例原子报告：`var/datasets/slides_align_sample/report_qwen_v6_dimensions7_r2/*.report.json`
- 上一轮契约失败诊断：`var/datasets/slides_align_sample/report_qwen_v6_dimensions7/`
- qwen3.8 影子复核：`var/datasets/slides_align_sample/report_qwen_v6_qwen38_kimi_shadow/`

上述 `var/` 文件保留在本机且被 Git 忽略；本文件保存可复核结论和精确路径。
