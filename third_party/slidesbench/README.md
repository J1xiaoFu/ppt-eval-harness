# AutoPresent / SlidesBench 官方基线复现审计

审计日期：2026-08-26。结论：**部分复现（PARTIAL_REPRODUCTION）**。官方的 PPTX
单页 reference-based 评测、媒体抽取和 canonical code 生成已真实运行；论文完整分数、
AutoPresent 训练/推理和 585 例全量评测，无法仅靠当前公开产物等价复现。不要把论文表格
数字或本次 smoke 输出标成“本项目复现结果”。

结构化结论见 `reproduction-manifest.json`，原始输出见 `evidence/`。

## 1. 官方来源与版本

| 对象 | 固定版本/URL | 公开许可与结论 |
|---|---|---|
| 论文 | [arXiv 2501.00912 v2](https://arxiv.org/abs/2501.00912) | AutoPresent，CVPR 2025；论文源码是指标定义的依据 |
| 代码 | [para-lost/AutoPresent](https://github.com/para-lost/AutoPresent/tree/98e0c012e89469863d9c3c8bc87eac967d82b2e6) | commit `98e0c012e89469863d9c3c8bc87eac967d82b2e6`，MIT，无 release/tag |
| 三个模型适配器 | [HF 作者主页](https://huggingface.co/JiaxinGe) | 各为约 1.342 GB、LoRA `r=128`，模型卡标 Apache-2.0；仍依赖 `meta-llama/Llama-3.1-8B-Instruct` 的访问和 Llama 许可 |
| 在线 Demo | [JiaxinGe/AutoPresent](https://huggingface.co/spaces/JiaxinGe/AutoPresent) | Space 标 CC-BY-NC-2.0；当前代码实际调用用户提供 key 的 GPT-4o，并非加载 AutoPresent 权重 |
| SlidesBench 原始幻灯片 | [训练收藏](https://www.slideshare.net/saved/27575466/autopresent_train)、[测试收藏](https://www.slideshare.net/saved/27572898/slides) | 论文明确说不再分发幻灯片，只提供原站 URL 和 opt-out；代码的 MIT 不能替代原作者内容许可 |

网上名为 `JustinZekai/SlidesBench` 和 `F171636/SlidesBench` 的 Hugging Face 数据仓库
不是论文作者发布的 benchmark artifact。前者看起来是跨 20 余类别的 PPTX 抓取集合，
不对应论文的 310 decks/10 domains 划分；二者缺少足以证明来源、划分与逐文件授权的
dataset card。即使 README 声明 Apache-2.0，也不能据此推定第三方幻灯片内容可商用。

## 2. 公开数据实际盘点

仓库静态审计结果：

| 内容 | 公开快照 |
|---|---:|
| 测试领域 | 10 |
| 测试单页（每页 3 种 instruction） | 195 |
| 测试 instruction task | 585 |
| 仓库中可用的测试 reference PPTX | **1/10**（仅 `food.pptx`） |
| 仓库中测试 media 目录 | **0/195** |
| 训练 detailed-with-images instruction | 7,622 |
| 训练 detailed-only instruction | 7,045 |
| 训练 high-level instruction | 7,045 |
| 可用于 `train.py` 的 `(instruction, code)` 对 | **0** |

三个训练 CSV 的列都只有 `instruction,slide_folder`，而官方 `autopresent/train.py` 直接读取
`row['code']`。GitHub issue #8 对 code-pair 数据的请求截至审计日仍未解决。README 推理命令
引用的三个 `dataset_test_*.csv` 也不在仓库内。逐域明细和 SHA-256 在
`evidence/dataset-audit.json`。

论文对数据的口径是 300 个训练 deck、10 个测试 deck、10 个领域；每个测试单页生成
三类 instruction：

1. Detailed instructions with images：内容、素材、格式和位置均给出。
2. Detailed instructions only：将素材路径替换成素材自然语言描述。
3. High-level instructions：仅给主题，模型自行补齐内容和设计。

当前网络环境访问 SlideShare 收藏及单 deck URL 均返回 `Client Challenge`，不能自动批量
取回缺失 reference。即便可下载，也必须先完成原作者许可、隐私、商用和保留期核验，
不能直接进入工业金标集。

## 3. 复现环境与真实结果

环境：Windows 11 x64、Python 3.10.11、PyTorch 2.0.1+cu118（未确认可用 GPU）、
`python-pptx 1.0.2`、`sentence-transformers 2.7.0`、`transformers 4.41.2`。
官方两个 requirements 文件缺失 `sentence-transformers`、`scikit-learn`、`datasets`、
`unsloth`、`peft`、`opencv-python` 等实际 import；本次 reference-based smoke 使用
`requirements-smoke.txt` 补齐最小依赖。完整现场包清单见 `evidence/pip-freeze.txt`。

### 3.1 Reference-based 单页评测

官方 `food.pptx` 第 1 页与自身比较：

```text
# of blocks 2 generated 2 reference
match     : 100.0
text      : 100.0
color     : 25.0
position  : 100.0
exit=0, cached elapsed=12.208s
```

项目 `aurora_demo.pptx` 第 1 页与自身比较：

```text
# of blocks 4 generated 4 reference
match     : 100.0
text      : 100.0
color     : 50.0
position  : 100.0
exit=0, cached elapsed=11.952s
```

这两个 identity test 揭示出核心有效性问题：同一文件的 color 不能达到 100。实现只比较
shape fill/image bytes 和 background，未按论文描述完整比较字体颜色；未显式填充时的
`FillFormat` 与 background 对象比较也会产生伪差异。该指标未经修复和校准不得接入生产总分。

另有两个确定性失败：

- 官方 `food.pptx` 空白第 2 页与自身比较触发 `ZeroDivisionError`。
- 传入 `--output_path` 时程序反而不写 JSON；只有未传该参数才写默认 `ref_eval.json`。

### 3.2 Deck 评测

`slides_eval.py` 在 Windows 上以字面量 `/` 拆分 `os.path.join` 产生的路径，首次排序即
`IndexError`，全 deck 评测未能进入指标聚合。它的 `eval_page()` 还把 PPTX `FillFormat`
或 bytes 传给要求 RGB tuple 的 `get_color_similarity()`，修复路径问题后仍需处理该类型错误。

### 3.3 数据预处理与 canonical program

对官方 `food.pptx` 运行 `parse_media.py`：`exit=0`、0.687 秒，生成 32 个 slide 目录、
36 个图片 blob（17,979,686 bytes）。但代码把全部文件命名为 `.jpg`；Pillow 实测实际格式
为 JPEG 7、PNG 27、GIF 2。下游又用 `data:image/jpeg` 发送这些文件，存在 MIME 错配。

运行 `reproduce_code.py`：`exit=0`、0.850 秒，生成 32 个 `code_library.py` 和 53 个
图片文件。执行第 1 页 canonical program 时在 `SlidesLib.search` 的未声明依赖
`chromedriver_py` 处失败；代码中还存在无条件 `from mysearchlib import LLM`，该模块没有
公开实现。因而“生成程序”可复现，“程序可执行并还原 PPTX”不可复现。

### 3.4 Reference-free 与 AutoPresent

- Reference-free 使用 GPT-4o 对 text/image/layout/color 各打 0--5。本环境无
  `OPENAI_API_KEY`，程序在 import 时即停止；官方没有离线缓存、mock fixture 或论文原始
  judge response，故无法重放。传入自定义 `--response_path` 同样不会写结果。
- AutoPresent 训练先受 `unsloth`/Linux-GPU 依赖约束，更根本的阻塞是公开 CSV 没有
  `code` 列，无法形成论文所述 `(instruction, code)` 对。
- AutoPresent 推理脚本在加载模型前即缺 `datasets`；它还无条件 import `unsloth`，没有
  发布依赖锁。三个 checkpoint 是 LoRA adapter，不是独立 8B 模型；需要 gated Llama base、
  `peft`、足够显存/内存及约 1.342 GB/场景的 adapter 下载。
- 在线 Demo 不能替代模型复现：其源码调用 `gpt-4o`，并直接执行模型生成的 Python；
  这同时构成供应链、任意代码执行、网络访问和用户 API key 泄露风险。

## 4. 官方指标和论文基线

官方指标值得作为**候选特征**，不能原样作为工业评分协议：

| 类型 | 指标 | 官方计算思路 |
|---|---|---|
| Reference-based | Element matching | 匹配元素面积 / 全部元素面积 |
| Reference-based | Content | 文本用 `all-MiniLM-L6-v2` cosine；论文称图片用 CLIP |
| Reference-based | Color | CIEDE2000 + 背景颜色 |
| Reference-based | Position | 归一化坐标中心点的 `1 - max(|dx|, |dy|)` |
| Reference-free | Text/Image/Layout/Color | GPT-4o 单图、单准则、0--5 分 |
| Delivery | Execution success | 三次候选中至少一个程序可执行的比例 |

对代码生成方法，论文表格中的 `Overall` 与“八个质量指标的均值 × execution rate”吻合，
这与本项目将根本交付性放在外层乘算的 PPT-PDMS 思想相容。不过论文的 execution 仅代表
生成 Python 能否运行，不等价于工业交付门禁（文件可打开、内容可见、字体/媒体/兼容性、
可编辑性等）。端到端图片方法的 `Overall` 在不同表中的聚合口径并不稳定，不能反推成同一
通用公式，也是接入时必须重新定义而非沿用论文总分的原因。

论文报告的关键基线（**paper-reported，未在本机全量复现**）：

| 场景 | 方法 | Execution | Overall |
|---|---|---:|---:|
| Detailed + images | GPT-4o | 89.2 | 55.1 |
| Detailed + images | GPT-4o + SlidesLib | 86.7 | 58.0 |
| Detailed + images | AutoPresent | 79.0 | 45.2 |
| Detailed + images | AutoPresent + SlidesLib | 84.1 | 55.0 |
| Detailed only | GPT-4o + SlidesLib | 87.7 | 56.3 |
| Detailed only | AutoPresent | 89.2 | 55.2 |
| High level | GPT-4o + SlidesLib | 97.4 | 58.5 |
| High level | AutoPresent | 86.6 | 47.8 |

Reference-free 的论文验证报告两名人工标注者与 GPT-4o 的 ICC 为 73.8%--85.3%，但公开
仓库没有逐样本人工标签、judge response、ICC 类型/置信区间计算产物或冻结模型请求日志，
无法独立审计这一结论。

## 5. 工业评测可借鉴与不可照搬部分

可借鉴：

- reference-based 与 reference-free 分开，避免“与唯一参考不同”被误判为低质量。
- 将 execution/交付性作为乘子，不允许审美得分补偿完全不可用的产物。
- 以 element 为证据单元，输出内容、颜色、位置等可定位分量。
- 用人工一致性验证 Judge，而不是直接接受 VLM/LLM 分数。
- 三档上下文难度适合映射到文字生成、多模态要求和高层需求生成场景。

不可照搬：

- `viz_scores()` 把无有效样本的指标记成 100，空页却可能直接除零；`ERROR/NA/FAIL`
  没有分离。
- 图片 CLIP 实现虽然存在，却在指标入口被注释；结构解析遗漏图表、表格、组合形状、
  font color 等大量 PPT 对象，论文描述与公开代码不完全一致。
- GPT-4o Judge 使用可变模型别名、自然语言首字符解析、无 JSON schema、无重试/置信度/
  版本快照，且 prompt 同时写“0--N”和“1--5”，不适合审计。
- 最大匹配主要由文本相似度驱动；图片相互匹配成本都为 0，可能任意配对并污染颜色和位置。
- 执行生成代码没有沙箱；SlidesLib 可联网搜索、调用模型和下载文件，不能用于不可信生产输入。
- benchmark 是英文、单页、10 个 SlideShare 领域，不能验证中文企业整套 PPT 的叙事、
  跨页一致性、来源忠实度、图表数据或合规性。
- 仓库无 release、锁文件、原始模型输出、完整 references、judge 响应和逐样本论文结果，
  当前只能做概念基线而非数值回归 oracle。

## 6. 接入本项目的边界

1. 把 SlidesBench 作为 `SlidesBenchReferenceOracle` 的候选设计来源，不直接复制其总分。
2. 保留 element/content/color/position 为 reference profile 的 additional 指标；先修复 identity、
   empty、类型、MIME 和跨平台问题，并在中文金标上重新校准。
3. 将 execution 映射为本项目更严格的公共硬乘子，但区分生成程序失败、PPTX 不可打开、
   渲染失败和内容质量失败。
4. Reference-free 四项只作为现有本体 Oracle 的对照实验；Judge 必须固定模型快照、结构化
   输出、证据和置信度，并走 Harness 的 `ERROR/NA/REVIEW` 状态。
5. 任何 SlideShare/HF PPTX 在完成逐文件许可与来源血缘核验前只能进隔离调研池，不能进入
   可训练的数据飞轮或对外发布的 benchmark。

## 7. 达成“论文完整复现”仍需的外部条件

- 从合法来源取得并固定 10 个测试 reference deck、逐页 media，以及与 195 页对齐的血缘。
- 向作者取得训练 code pairs、三个测试 CSV、论文所有模型输出、执行日志和 GPT-4o judge
  response；否则只能重新实验，不能称为 exact reproduction。
- 在固定 Linux/CUDA 容器中锁定 Unsloth、PEFT、Transformers、LibreOffice/unoconv、字体、
  Llama base revision 和三个 adapter revision。
- 配置有预算上限的 OpenAI 固定 checkpoint；论文使用 `gpt-4o-2024-08-06`，不能用滚动
  `gpt-4o` 别名替代并期待数值一致。
- 对 585 任务执行三样本代码生成、沙箱运行、渲染、八指标评测，按论文规则聚合，并同时
  报告缺失率、失败原因、成本、延迟和置信区间。
