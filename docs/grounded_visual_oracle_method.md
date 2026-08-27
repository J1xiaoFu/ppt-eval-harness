# Grounded Atomic Visual Oracle：基线研究与 v2 方法

## 结论

公开基线最一致的工程信号不是“用更强 VLM 做一次更长的整套打分”，而是：

1. 把视觉判断拆成单页或小批、单构念、可定位的问题；
2. 把像素观察、评分、确定性验证和整套一致性分开；
3. 只让 VLM 判断像素证据真正适合判断的内容；
4. 使用人类偏好验证组合指标，而不是把模型自信度当有效性。

因此，本项目没有继续把“六维一次大 JSON”当成最终方案。真实烟测证明冗余 kind、重复
criterion 和一维定位不足会让整批结果失效；最终落地的 v2 将六个维度拆成六次独立原子
调用，并保留 v6 供历史回放。

## 已核查的公开基线

### AutoPresent / SlidesBench

- 论文：[AutoPresent: Designing Structured Visuals from Scratch](https://arxiv.org/abs/2501.00912)
- 实现快照：[`reference_free_eval.py`，commit `98e0c012`](https://github.com/para-lost/AutoPresent/blob/98e0c012e89469863d9c3c8bc87eac967d82b2e6/evaluate/reference_free_eval.py)
- GPT-4o 每次只看一页、一个构念，分别评价 text、image、layout、color；使用 0–5
  量表、`temperature=0`。text 调用额外提供 PPTX 抽取文字。
- 论文报告两名人工与模型在四项上的 ICC 为 `73.8%–85.3%`。

可迁移点：单页、单构念、短 rubric；“无图片不罚”；layout 明确检查对齐、重叠、
留白和越界。该实现没有本项目所需的严格证据 Schema、版本与成本审计。

### PPTAgent / PPTEval

- 论文：[PPTAgent / PPTEval](https://aclanthology.org/2025.emnlp-main.728/)
- 实现快照：[`ppteval.py`，commit `2419d30b`](https://github.com/icip-cas/PPTAgent/blob/2419d30b134a71486523e95ded60b32489fd3c61/pptagent/ppteval/ppteval.py)
- [视觉描述 Prompt](https://github.com/icip-cas/PPTAgent/blob/2419d30b134a71486523e95ded60b32489fd3c61/pptagent/prompts/ppteval/ppteval_describe_style.txt)
- [评分 Prompt](https://github.com/icip-cas/PPTAgent/blob/2419d30b134a71486523e95ded60b32489fd3c61/pptagent/prompts/ppteval/ppteval_style.txt)

PPTEval 先让 VLM 对单页客观描述 Visual Consistency、Color Scheme、supporting visual
elements，再让文本模型按 1–5 rubric 打分；content/design 逐页平均，coherence 整套一次。
论文内 Design Spearman 为 `0.88`。但其评分规则把黑白和缺少装饰性视觉元素固定放在较低
档位，容易奖励“表面丰富”。PresentBench 的跨域实验中，PPTEval 对人类总体排序仅
`0.303`，所以不能把论文内相关性直接迁移到本项目。

可迁移点是“感知与评分分离”和单图输入，不是“彩色/装饰越多越好”的先验。

### SlidesGen-Bench / Slides-Align

- 论文：[SlidesGen-Bench](https://arxiv.org/abs/2601.09487)
- VLM 配置：[`eval_config.py`，commit `b202e60c`](https://github.com/YunqiaoYang/SlidesGen-Bench/blob/b202e60c39e19c5f9da57029fe53ff189e6642b0/eval/eval_config.py)
- 人类对齐结果：[`human_alignment.tex`](https://github.com/YunqiaoYang/SlidesGen-Bench/blob/b202e60c39e19c5f9da57029fe53ff189e6642b0/results/human_alignment.tex)

该项目的主审美指标是计算得到的 Harmony、Engagement、Usability、Visual Rhythm；另有
VLM Rating 和 A/B Arena。公开的人类对齐表报告：计算视觉组合 Spearman 约 `0.71`，
VLM Rating `0.57`，VLM Arena `0.52`，PPTEval `0.53`，Human `0.85`。

这直接支持本项目保留确定性视觉层，并把 VLM 作为强但有限的视觉证据，而不是替代品。

### PresentBench

- 论文：[PresentBench](https://arxiv.org/abs/2603.07244)
- 仓库快照：[`PresentBench`，commit `e70ff01d`](https://github.com/PresentBench/PresentBench/tree/e70ff01da962274e1e3cc03f77ec435ad66c5eb6)
- 公开 rubric 示例：[Academia common judge prompt](https://huggingface.co/datasets/PresentBench/PresentBench/blob/main/academia/common_judge_prompt.json)

PresentBench 平均每例约 `54.1` 个二值 checklist，其中视觉/布局约 `17` 项；每个问题
单独一次 VLM 调用，部分满足也判 `no`，并要求具体页码。其 24 个任务、每任务五个系统的
人评实验中，PresentBench Spearman 为 `0.532`，整体 MLLM 排名为 `0.258`。

可迁移点是把整体“好看”拆成可核验问题，而不是把一次整体偏好当作事实性分数。

### SlideAudit

- 论文与数据：[SlideAudit，DOI `10.1145/3746059.3747736`](https://doi.org/10.1145/3746059.3747736)
- 本地 starter：`var/datasets/slideaudit_starter_v1/`

SlideAudit 提供 2,400 张幻灯片图像，人工缺陷 taxonomy 分为 Composition & Layout、
Typography、Color、Imagery & Visualizations，共 19 个二元缺陷，带一致性和 bbox。
公开仓库尚未提供通用 evaluator/scoring utility。v2 使用其 taxonomy 作为缺陷码白名单，
但不会把数据集不存在的“人类偏好总分”伪造成 GT。

### PPTArena 与 Microsoft PPT-Eval

- [PPTArena](https://arxiv.org/abs/2512.03042) 面向编辑任务：结构 IF 与截图 VQ 分开；先用
  SSIM 选变化页，再以最多五页一批做高分辨率比较。它不是 reference-free 成品审美基线，
  公开论文也没有给出 VQ Judge 与人类偏好的 Spearman/ICC。
- [Microsoft PPT-Eval Prompt](https://github.com/microsoft/ppteval/blob/1b8b55a29e48fdc65d423689b6f2370ad91beeea/ppteval/verify/ppt/prompts.py)
  明确要求：能由 Python 验证时优先确定性方法；VLM 用于颜色、对象位置、布局和美学。

## 当前 v1.2 的失效原因

1. 一次请求最多上传 12 页，但旧通用证据只检查页码不超过整套页数。15 页 deck 的固定
   样本是 `1,2,3,4,6,7,8,9,11,12,13,15`；旧响应仍可把未上传第 10 页作为视觉证据。
2. 图片连续附在一大段 JSON 后，没有“页码标签 → 图片”的直接绑定。
3. 模型一次隐式完成最多 `12×6=72` 个判断，再压成六个 summary，容易缺字段、串页和
   跨维重复。
4. `1.0=无 material defect` 会把稀疏、模板化、缺少设计深度但规整的 deck 推到高分。
5. `render_integrity` 占 VLM 内部预算 25%，但单次截图难以可靠区分源内容、布局选择和
   导出损坏。
6. FULL/PARTIAL 由模型自报，尽管 Harness 已知道真实采样覆盖。

## v2 已实现的合同

### 输入与证据

- 每张图片前发送 `RENDERED_SLIDE_PAGE=N` 文本标签。
- VLM 的 `evidence.page_number` 只能引用 `request.images` 中实际上传的页。
- `affected_page_numbers` 同样只能引用上传页；观测范围由 Harness 根据真实覆盖生成
  `FULL` 或 `PARTIAL`，不接受模型自报。
- composition、typography、color、imagery 和 render 每次最多上传 4 页；cross-slide
  consistency 每次最多 8 页。长 deck 使用确定性等距覆盖，不只取前几页。
- 五个局部维度对每个上传页各返回一条原子观察，Harness 对通过校验的页级分求均值，保留
  `page_scores` 并合并缺陷码；cross-slide 仍只返回一条跨页 summary。同一页重复记录会失败。
- 未上传页的对象树正文在原子请求中被清空，只保留页号；模型不能借完整文本假装看见像素。
- 每次只请求一个 criterion 和一个 summary。Prompt 要求 `kind=criterion_summary`，但 Harness
  把 `payload.criterion_id` 作为唯一语义标识；其他 vendor kind 只有在该单项 payload 的
  字段、缺陷码、页码和严重度全部通过后才被规范化，并保留原值供审计。
- 结构重试只发送机器生成的错误类别和修复要求，不回传上一次模型内容。

### 六个互斥维度

v2 保留 v6 的六个 metric ID 以便 A/B，但六项不再共用一个模型响应：

1. composition/layout；
2. typography/legibility；
3. color/contrast；
4. imagery/data visualization；
5. cross-slide consistency；
6. render integrity。

每维只能使用固定缺陷码和正向质量信号。同一缺陷只能有一个 primary owner。typography
不得评价拼写、事实或颜色；render 不得把源文字本身的乱码归因于导出；黑白、极简、暗色、
无图片或无渐变本身不扣分。

### 审美锚点

“没有发现缺陷”只表示视觉卫生合格，不自动等于优秀：

- `0.95–1.00`：有明确证据的卓越视觉执行，无 material defect；
- `0.80–0.94`：强专业水准；
- `0.65–0.79`：可用但普通、稀疏、模板化或视觉表达意图有限；
- `0.45–0.64`：有重复可见弱点；
- `0.25–0.44`：重大问题；
- `0.00–0.24`：系统性严重失败。

Harness 额外验证并确定性修正：超过 `0.79` 但不足两个正向信号时封顶 `0.79`；超过
`0.94` 但不足三个强信号时封顶 `0.94`；MAJOR 封顶 `0.64`，CRITICAL 封顶 `0.34`。
所有调整写入 `score_adjustments`，不因量表内部矛盾再次调用模型。render 缺陷必须带
上传页上的归一化 bbox。若模型报告 render 缺陷却无法定位，Harness 不会重试并吞掉其他
五维，而是只将 render 设为 `INSUFFICIENT/N/A`，保留模型原分为未采用的审计 metadata。
跨页缺陷不足两个实际上传页支撑时采用同样的按维降级语义。

### v7 实验 Profile

`finished_deck_v7_grounded_visual_candidate.json` 仍把 VLM 固定在总分 10%、合并视觉构念
20%。VLM 内部预算从 v6 的 `10/10/20/25/10/25` 调整为：

| 维度 | v2 内部预算 |
|---|---:|
| composition/layout | 20% |
| typography/legibility | 15% |
| color/contrast | 15% |
| imagery/data visualization | 25% |
| cross-slide consistency | 20% |
| render integrity | 5% |

该调整把难以归因的 render 因果判断降为窄门，同时提高整体构图、视觉表达和跨页系统。
Profile 仍是 `EXPERIMENTAL/UNVALIDATED`，没有根据现有七份 Slides-Align 排名拟合权重。

## 真实调用烟测

在 Kimi-Banana 15 页真实 deck 上，旧“六维一次返回”修补版会在重复 criterion、冗余 kind、
render bbox 缺失和 severity/score 冲突之间变化，证明继续扩充统一 Prompt 不能得到稳定合同。

改成 v2 原子模式后，一次完整集成运行中 composition、typography、color、cross-slide 和
render 五项均合法评分；composition/typography/color/render 各自聚合 4 个页级观察，
cross-slide 使用 8 页样本的一条整套观察。imagery 当次为纯传输错误
`Qwen endpoint request failed`，没有使其他五项失效；随后仅重试 imagery，一次成功返回
4 个页级观察、得分 `0.85`、confidence `0.90`、Prompt `2.0.0`。这证明六个 Prompt 均能
产生合法合同，但不构成相关性或生产稳定性验证。

完整运行的五个成功视觉调用合计记录 77,160 token；imagery 独立重试为 13,616 token。
DashScope 兼容响应没有提供可用货币成本，因此报告中的 cost 为 0，不能解释成真实免费。

## 当前边界与下一阶段

当前 v2 已实现第一条“单构念、小批高分辨率”通道，并用独立 cross-slide 原子调用替代
六维共享响应：

```text
页级高分辨率图（每批 1–4 页、单构念）
  → SlideAudit 白名单缺陷 + bbox + severity
  → Harness 聚合 visual hygiene

最多 8 张带清晰页码的跨页样本
  → 跨页视觉系统 + visual rhythm

对象树—像素差异
  → 独立 render-integrity Oracle
  → 不混入纯审美 Judge
```

尚未完成的正式化工作包括：页图级模型结果缓存、真正的 contact-sheet overview、基于
对象树—像素差异的独立 render Oracle，以及确定性颜色/对比和视觉节奏指标。确定性 Oracle
继续负责重叠、越界、显式字号和可计算结构，VLM 只补主题适配、视觉层级、留白意图、图像
相关性、图表表达和跨页一致性。

进入生产前应固定模型、Prompt 和采样策略，每份 deck 至少重复三次，报告合法响应率、各维
方差和 pair flip；用冻结的真实 PPT 集验证 composite Spearman/pairwise accuracy。现有
Slides-Align 只有整体序数偏好，不能证明六个单维已校准；单维校准需要另建人工标签。
