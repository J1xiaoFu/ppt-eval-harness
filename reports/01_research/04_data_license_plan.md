# 数据、许可与切分计划

## 目标规模

约 400 个基础任务，四场景近似均衡；由此构造约 2,000 个单一/组合缺陷变体。首期以中文企业
汇报、方案、复盘、项目总结为主，保留少量中英混排与 OOD 模板。

## 数据登记字段

`dataset_id/version/source_uri/license/license_evidence/allowed_use/pii_level/consent/retention/owner/sha256`
是入库最低字段。`license=unknown` 或缺少授权证据的数据只能进入隔离研究区，不得进入正式基准。

## 切分

| 集合 | 比例 | 用途 | 可否调参 |
|---|---:|---|---:|
| 工作池 | 50% | 开发与失败分析 | 是 |
| 校准集 | 20% | 权重、阈值、置信校准 | 是，留痕 |
| 冻结回归集 | 20% | 发布门禁 | 否 |
| OOD 挑战集 | 10% | 语言/模板/模型外推 | 否 |

按项目、主题、模板、来源血缘和生成模型分组切分；派生样本必须跟随父样本，禁止按页随机切分。

## 隐私与保留

- 入库前移除姓名、电话、账号、客户标识与隐写元数据；映射表独立加密保存。
- 原始业务 PPT 最短必要保留，默认 90 天；派生的脱敏特征按授权另定。
- 最小权限访问，下载和人工查看均产生日志；删除请求沿血缘传播。
- 外部模型调用必须通过脱敏策略和供应商数据保留审查。

## 污染与可编辑性

记录生成模型是否可能见过公开样本；公开模板与公开 deck 分开标记。正式集优先保留原始 PPTX，
PDF/图片只能进入视觉降级子集，不能作为结构指标金标。

## 人工标注

客观指标双人独立标注并仲裁；审美指标使用盲化 A/B。标注员先完成锚点培训和 20 例资格集，
低于预注册一致性不得进入正式标注。

## 已核实的公开数据入口

- 第一优先：按记录许可筛选的 PPTAgent/Zenodo 可编辑 PPTX，用于本体质量和受控缺陷。
- 视觉冻结/挑战：SlideAudit、RealSlide/SynSlides 与 LOC；父样本、真实/合成和旧格式必须分层。
- 条件任务：UniPPTBench 先纳入 39 个 `ready` task；87 个 `draft` task 只进工作池。
- 项目总结：SlideTailor PSP 与 UniPPTBench 长文档子集，保留 source-document 血缘。
- 编辑研究：DECKEDIT-BENCH 仅用于非商用研究与指标开发。
- 视觉/理解：SlideVQA、DOC2PPT、Zenodo Presentations Open 只进入视觉降级或隔离研究区。
- 隔离候选：PPT4Web、Slides-Align、PPTBench Generation/Modification 和非官方 SlidesBench，
  必须先解决上游内容权利、缺失标注或数据卡许可问题。

完整规模、revision、下载实测和排除项见 `07_public_dataset_catalog.md` 及其 JSON 证据。
