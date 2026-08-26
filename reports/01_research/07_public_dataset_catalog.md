# 公开 PPT 数据集与基准审计

审计日期：2026-08-26。这里的“可访问”只表示网络入口可用，不表示允许商用、训练或再分发。
许可证必须覆盖具体内容，而不能只引用代码仓库许可证或数据托管页标签。

## 推荐矩阵

| 数据集 | 可验证规模与格式 | 语言 | 许可/访问 | 对四场景的价值 | 建议 |
|---|---|---|---|---|---|
| UniPPTBench | 126 个任务：长文档 32、多模态 48、多源 18、模糊需求 28；源 PDF、intent、task/annotation YAML，不含金标 PPTX；1.61 GB | 124 EN、2 mixed | ModelScope 标 Apache-2.0；39 ready、87 draft | 与本项目条件生成场景最接近 | `A`：先使用 39 ready 任务，逐项复核源文件权利 |
| SlideTailor PSP | 150 个目标论文 PDF、50 组论文-演示 PDF、10 个可编辑 PPTX 模板，1.51 GB | EN 学术 | CC BY 4.0，自动 gated | 项目总结、论文到 PPT、偏好参数化 | `A`：接受条款后用于 source-grounded 研究 |
| PPTAgent parsed data | 761 PPTX/603 PDF 索引，639/492 个唯一文件，约 19.1 GB；原始/标准化 PPTX、媒体与结构 JSON | 以英文为主 | PPTX 去重后 612 CC-BY-4.0、14 BY-SA、4 CC0、3 BY-NC-SA、6 其他/未明 | 成品本体、解析器、缺陷注入、版式/可编辑性 | `A`：CC-BY/CC0 白名单并按 checksum 去重 |
| SlideAudit | 600 个原始页 + 1,800 个受控缺陷页；2,400 PNG、缺陷 taxonomy、bbox、对象描述 | EN 视觉页 | CC BY 4.0 | 成品视觉质量、缺陷定位、变形测试、元评测 | `A`：按 600 个父样本聚类切分 |
| RealSlide / SynSlides | 1,050 真实高校课件页 + 2,200 合成页；16 类 COCO bbox 与摘要 | EN | 两者数据卡 MIT；RealSlide 仍需保留上游课件清单 | 元素检测、图表/图片/文本区域 Oracle | `A/B`：适合作为视觉组件 fixture |
| Zenodo10K | 10,448 条 PPTX 元数据和原始入口 | 多领域，英文为主 | 无统一总许可；论文 90 records 抽查为 87 CC-BY-4.0、2 BY-SA、1 BY | 扩展本体质量与 OOD 模板 | `A/B`：只按 DOI/record 许可入库 |
| DECKEDIT-BENCH | 28 个真实 PPTX、183 条编辑指令；短/中/长 deck | EN 21、KO 7，目标含多语言 | CC BY-NC 4.0 | 编辑 diff、目标定位、非目标保护、飞轮修复成本 | `A-research`：不可用于商业训练 |
| PPTBench Generation/Modification/Understanding | 单页任务 800/1,201/1,039；截图、对象 JSON、指令/问答/ground truth | EN | Understanding 为 Apache-2.0；Generation/Modification 未声明数据许可 | 元素生成、局部修改、图表理解和 Oracle 单测 | `B`：仅 Understanding 可先进入许可候选池 |
| Slides-Align | 卡片声称 1,326 人工排名、9 产品、187 主题；当前托管含 1,215 PPTX、47,742 PNG，约 57.1 GB | 多产品，主题主要英文 | MIT 数据卡，但商业产品 ToS 另行约束；卡片宣称的根排名 JSON 在当前根目录未找到 | 审美 pairwise、人类偏好与产品外 OOD | `B/C`：取得完整标注和产品授权后再用 |
| SlideVQA | 2,619 个 deck × 20 张图；14,484 QA、890,945 bbox | EN | gated；NTT evaluation-only、不可转让/再分发 | 跨页检索、证据页定位、数字推理 | `B-research`：不能进入工业训练池 |
| DOC2PPT | 5,873 组文档/slide-deck；约 70K 文档页、98,856 幻灯片页，PDF 文档、JPEG 幻灯片和层级对齐 | EN 学术 | Google Drive 可访问，项目页未给出明确数据许可 | 长文档总结、图文检索、slide sequencing | `B/C`：许可澄清前只做隔离研究 |
| PPT4Web | 182,405 个原始 PPTX；50 个压缩包，约 266 GB | 俄语为主，含 EN/KK/UK/BE | 数据卡 CC0；上游用户上传内容权利仍需法律确认 | 大规模 intrinsic/OOD/模板统计 | `C-quarantine`：不因 CC0 标签直接商用 |
| Zenodo Presentations Open | 663,949 页图像，约 88.4 GB 数据/119.9 GB 下载；PDF URL 与逐页图像 | 多语言 | 数据卡 `cc`，具体许可在每条 Zenodo record | 视觉质量、首页/尾页、跨领域页面分布 | `B`：视觉降级集，不支持 PPTX 结构指标 |
| X+SlidesBench examples | 45 论文主题/6,849 probes；公开工作集 113 源文档、8,127 probes、27,059 audience weights | EN | 代码 MIT；源文档需按上游许可 | 受众条件覆盖、信息效率、source-grounded correctness | `A-metric`：优先借鉴指标，不当作 PPTX 训练集 |
| LOC Government PowerPoint | 美国政府网页归档随机抽取 1,000 个原始 PowerPoint 对象；3.458 GB，带 checksum 与 15 项元数据 | EN | LOC rights statement；具体权利按来源网站 | 旧格式、解析、安全、兼容和恶意对象挑战集 | `A-safety`：只在沙箱内解包/渲染 |
| AutoPresent SlidesBench | 论文 300 train deck、10 test deck、585 instruction；公开仓库只有 1 个 test reference PPTX | EN | 代码 MIT；SlideShare 内容未获项目方再分发授权 | reference-based 单页指标和三档需求难度 | `C`：概念基线，不作为工业数据源 |

## 实际可用性抽检

- 从 `Forceless/PPTAgent-parsed_data@14dcdc1...` 下载一份 CC-BY-4.0 PPTX：SHA-256
  `a60cf83be84650188a9508ae47d7af8afb9b8031906b961dd67d9453a40332a8`，10 页、51 对象、13 个媒体，
  本系统 `FULL/PASS`，本体分 `88.046903`。
- 从 `EditPPT/DECKEDIT-BENCH@0c58abf...` 下载 `01_CausalAGI.pptx`：SHA-256
  `0daee97fc3403c2934af061335c11c8de0bc5bd4370ae9dcce7bea896c099b03`，9 页、70 对象、22 个媒体，
  本系统 `FULL/REVIEW`，本体分 `74.247606`；183 条编辑指令可直接访问。
- UniPPTBench 数据仓固定为 `0cc571f2943a5fc06983ba29aea0cdca7fc4a811`，代码仓为
  `05567903b3209519a75ecf645b5060414ef5020e`；126 个 task YAML 和 annotation 均可枚举。
- PPTBench 三个 HF 数据集的首行 API 可读，Generation/Modification 确含对象级前后 JSON，
  但数据卡没有许可证字段，不能继承软件仓库 MIT。
- SlideAudit `642d490...` 的 2,400 个图像/annotation/description 与 CC BY 4.0 文件均可直接枚举；
  SlideTailor PSP `aba1cfc...` 为可访问的 1.51 GB gated 数据仓。
- LOC README 与下载端点返回 200，明确给出 1,000 个 PowerPoint 对象、BagIt checksum 和 rights statement。
- 名为 `ppt127k` 的候选抽检后是普通 prompt/response preference CSV，并非 PowerPoint 数据，已排除。

## 其他已核实研究候选

- PPTC：279 个多轮会话、50 个 PPTX 模板和 API 金标，适合 Agent 编辑/新建操作评测；模板上游权利需逐项检查。
- PSED：1,776 页、137,864 个 token 和 8 人强调标注，适合内容重点选择；无原图且仓库未明确许可。
- LPM Dataset：334 个视频、187 小时、9,031 页及鼠标/OCR/图形对齐，CC BY-NC-SA 4.0；只能非商用研究。
- SlideSpeech/TalkSumm：面向演讲转写和摘要，不含可编辑 PPTX，可辅助要点召回与演讲一致性。
- DreamStruct：约 10,053 个合成 slide-code 对和元素描述；优先使用合成部分，Google Drive 原始数据许可仍需确认。
- LecSlides-370K 与 LecSD：规模大但缺许可证/来源说明或链接不稳定，当前不入库。

## 中文覆盖结论

高质量公开数据几乎都偏英文；UniPPTBench 仅 2 个 mixed task，SlideVQA 和 DOC2PPT 为英文，
PPT4Web 以俄语为主。公开数据可用于解析器、视觉和英文条件评测，但不能替代中文企业汇报金标。
中文正式集仍需来自脱敏业务任务、专家制作样例和受控缺陷注入。

## 入库策略

1. 建立 `research_quarantine`、`license_allowlist` 和 `frozen_eval` 三层，不直接从公开下载进入训练池。
2. 每份 deck 保存原 URL、revision、内容 hash、作者/DOI、逐文件许可证和 attribution；未知/NC/SA 分流。
3. 按 deck/来源/模板/生成产品聚类切分，禁止同一 deck 的图片、PPTX、标准化副本跨集合泄漏。
4. 先落地四个小切片：100 个 CC-BY PPTX 本体集、SlideAudit/RealSlide 视觉集、UniPPTBench 39 个 ready 条件任务、LOC 安全挑战集。
5. SlideTailor PSP 与 DECKEDIT 分别进入 source-grounded 和非商用编辑研究分区，不能混用许可。
6. Slides-Align、PPT4Web、DOC2PPT 和非官方 SlidesBench 在权利或标注缺口关闭前只保留元数据。

结构化目录与 revision 证据见 `reports/01_research/evidence/public_dataset_catalog.json`。
