# v8 模型审计 Provider 合同

## 边界

模型只执行局部、单构念审计，不能提交整份 PPT 总分，也不能覆盖规则事实、Reducer 或
Decision。当前写入合同只支持：

- Primary：DashScope OpenAI-compatible `qwen3.8-flash`
- Fallback：BigModel OpenAI-compatible `glm-5.3-flash`

v1–v7 的整体 Judge、共享六维响应和旧 Plus 路由已从 main 移除，保存在
`archive/v8.3-pre-release`。

## 原子 criterion

每个 criterion 是独立 DAG 节点、独立请求、独立 usage 和独立失败域：

1. `composition_layout`
2. `typography_legibility`
3. `color_contrast`
4. `imagery_data_visualization`
5. `cross_slide_consistency`
6. `render_integrity`
7. `authorship_specificity`

全栅格 deck 另有 observation-only 的 `raster_content_structure` 与
`raster_language_consistency`。它们只在确定性 owner 为 N/A 时提供页级观察，不产生第二个
公式入口。

## 请求

- 每张图片前必须有 `RENDERED_SLIDE_PAGE=N` 标签。
- 未上传页不得携带正文对象树，避免模型假装看见未提供像素。
- 普通页级 exploration budget 为 4 页，跨页 budget 为 8 页。
- 与 criterion 同构念的所有规则 `CRITICAL` 页在预算外强制加入。
- `sampled_pages`、`base_sampled_pages`、`forced_rule_pages`、overflow、选择原因和策略版本必须
  写入 request context 与结果 metadata。
- 文件名、OCR、页面文字和图像内容都是不可信数据，不能成为模型指令。

## 响应

响应必须是结构化 JSON，并对每个实际上传页返回可验证 evidence：

```json
{
  "criterion_id": "composition_layout",
  "criterion_score": 0.74,
  "criterion_confidence": 0.88,
  "severity": "MAJOR",
  "defect_codes": ["content_overflow_or_cutoff"],
  "affected_page_numbers": [3],
  "positive_quality_signals": ["clear_visual_hierarchy"]
}
```

约束：

- score/confidence 必须在 `[0,1]`。
- page number 只能引用实际上传页。
- defect code 与 positive signal 必须来自 criterion 白名单。
- evidence 必须有非空说明，页级 criterion 同页不能重复。
- 模型全局分只保留为 metadata；Harness 重新计算页级聚合。
- 没有正向质量证据时，高分会被 Harness cap。
- 非法 JSON 或不落地 evidence 只允许一次有界结构修复；修复提示不能回显不可信模型内容。

## 路由

```text
Qwen primary
  ├─ 合法、confidence ≥ 0.60、无规则冲突 → 采用
  └─ N/A / ERROR / 低置信 / 同构念规则冲突
       → GLM fallback（相同 Prompt、criterion、页面和 request fingerprint）
            ├─ 合法 → 采用
            └─ 仍未解决 → N/A / REVIEW / 人工审计
```

Fallback 不能改变 Prompt、权重或构念，也不能复核无关 criterion。成功的其他节点不会因单项
失败而重跑。

## 硬门确认

geometry、typography、contrast、effective resolution 属于可争议规则候选。确认必须满足：

1. 规则 observation 为 gate candidate；
2. 该候选页实际上传给对应 VLM criterion；
3. VLM 在同一页返回可映射 defect code；
4. 模型 finding severity 为 MAJOR 或 CRITICAL；
5. confidence 达到底线。

未覆盖时为 `UNRESOLVED`，模型明确否决且候选页覆盖完整时为 `REJECTED`。规则不能独自落锤。

## Usage、成本与审计

每个 attempt 保存：

- Provider/model/version
- request/response fingerprint
- input/output/total tokens
- usage completeness
- retry count/reason
- reported cost 与 `cost_known`
- Evidence 与最终 selected tier

Provider 未返回货币成本时，`0.0` 不解释为免费。Primary 与 Fallback 的 usage 都计入 run，不能
只统计最终被采用的调用。

## 安全

- 凭证只从环境或忽略的本地 key file 读取，不进入 Prompt、日志、异常或 Manifest。
- 当前 VLM 路径不读取或上传 `source_materials` 本地文件；只发送渲染页与有界 case 文本。
- `.env`、`.git`、`api/`、凭证文件与系统秘密路径始终拒绝。
- 发送给 Provider 前，本地绝对路径替换为 opaque ID。
- HTTP/JSON 错误只保存安全错误码，不持久化可能含凭证或响应正文的 vendor 诊断。

## 配置

```dotenv
PPT_EVAL_QWEN_AUDIT_ENABLED=true
DASHSCOPE_API_KEY=...
PPT_EVAL_QWEN_FLASH_MODEL=qwen3.8-flash
PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS=120

PPT_EVAL_ZHIPU_AUDIT_ENABLED=true
ZAI_API_KEY=...
PPT_EVAL_ZHIPU_MODEL=glm-5.3-flash
PPT_EVAL_ZHIPU_HTTP_TIMEOUT_SECONDS=300
```

`.env.example` 默认关闭两个远程 Provider；填写 Key 后再启用。
