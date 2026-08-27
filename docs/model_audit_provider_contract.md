# LLM/VLM 模型审计 Provider 接入契约

本项目不让任何厂商 SDK 或自由文本模型输出直接进入评分。v3.1 默认内置三项
Qwen Flash 模型审计：

- `llm_content_quality_audit`：语义清晰度、连贯性、具体性、内部一致性与可行动性。指标 ID 为兼容保留；当文本层不可观测时可以使用页图多模态回退。
- `vlm_visual_quality_audit`：对渲染页检查可读性、层次、对齐、遮挡、裁切、对比度与风格一致性。
- `llm_scenario_compliance_audit`：对 `text_to_ppt` / `project_summary` / `multimodal` 进行语义级场景合规检查。

它们组成 Composite `high_cost.model_audits`。这个 ID 为了 v2 回放保持稳定，v3 中的
实际默认 Provider 是低成本 `qwen3.7-flash`。四个 v3.1 Profile 都将 Flash 内容、视觉
以及生成场景合规分纳入 PPT-PDMS，并把它们列为 required metric。因此 Provider 未配置、
渲染缺失或调用失败时不会被当作 0 分，而是显式降级 Coverage 并转人工复核。

内容指标的输入是自适应的：有文本页比例至少 25% 时使用可提取文本；低于该比例时，
如果有完整或 canonical sample 页图，则用 `ppt-vlm-semantic-content-recovery-audit`
从像素评内容。后者的 `metadata.modality=VLM`，但分数仍属于 content 构念；模态不决定
聚合构念。无文本且无页图时返回 `SEMANTIC_INPUT_UNOBSERVABLE` N/A。

v3.1 的分层路由是：

```text
qwen3.7-flash 全量默认评测
  -> 分数灰区 / 单项低于复核线 / Flash 与确定性结果分歧 / Flash 高困惑
qwen3.8-flash 条件式 Advanced 审计
  -> 缺失 / 低置信 / 灰区 / 审计项互相分歧
人工复核
```

确定性硬门失败始终有最高权威，Advanced 不能覆盖；非 `FULL` Coverage 也不会由模型
“补齐”。Advanced 只在高置信且适用的审计项一致时给出最终建议，其余转人工。
历史 Git ref 中的 v3.0 Profile 使用 `FLASH_PLUS_HUMAN` / `qwen3.7-plus`；v3.1 使用
`FLASH_ADVANCED_HUMAN` / `qwen3.8-flash`。旧 route 和旧环境变量仅作显式回放兼容。

v5 还提供非默认的 `structured.model_audits`：视觉 Oracle 在一次请求中返回 6 个
固定 criterion summary，Harness 校验完整性/唯一性/分数范围后重算结果，并忽略模型全局分。
详见 [structured_visual_profile_method.md](structured_visual_profile_method.md)。由于尚无同契约 Plus Oracle，
v5 的 `model_audit_routing=STRUCTURED_FLASH_ONLY`，不调用旧标量 Plus VLM。

v6 候选使用 `structured_dimensions.model_audits` 和
`ppt-vlm-structured-visual-dimensions-audit@1.2.0`，一次 VLM 请求投影为六个
`BASE_ADDITIVE` metric：`composition_layout`、`typography_legibility`、`color_contrast`、
`imagery_data_visualization`、`cross_slide_consistency` 和 `render_integrity`。每个 summary
必须提供 `criterion_score`、`criterion_confidence` 与
`criterion_observability=FULL|PARTIAL|INSUFFICIENT`；INSUFFICIENT 时 score 必须为 JSON null。
Profile 的单维置信底线为 `0.60`，低于底线或不可观测的维度投影为 required N/A 并转
REVIEW。六个结果共用一个 request/response fingerprint；cost 等分、token usage 只由一个 owner
metric 记录，不伪造六次调用。v6 仍为 `STRUCTURED_DIMENSIONS_FLASH_ONLY`；
`qwen3.8-flash` 只是未来同构 Advanced reviewer 的预留模型，尚未接入本 Profile。

v7 实验候选使用 `grounded_structured_dimensions.model_audits`，Oracle 版本 `2.0.0`。
它保留六个 metric ID，但不再共享响应：五个局部构念各自使用最多 4 页的单构念 Prompt，
cross-slide 使用最多 8 页。局部调用要求每个实际上传页一条 observation，由 Harness 求
页级均值；每个结果拥有独立 request/response fingerprint、usage 和 cost。任一维 ERROR/N/A
只影响该维，不重做已成功调用。VLM 页码证据只能引用 `request.images`，每张图在消息中由
`RENDERED_SLIDE_PAGE=N` 直接绑定。详见
[grounded_visual_oracle_method.md](grounded_visual_oracle_method.md)。

v8 已成为四场景默认路径。v8.2 保留历史六个视觉构念，并新增独立
`v8.visual.authorship_specificity` 跨页原子节点；它不修改 v6/v7 的六维常量。
`v8.visual.<criterion_id>` 先调用 `qwen3.8-flash`，只有主结果 N/A/ERROR、单维低置信或
同构念规则冲突时才通过独立 BigModel Provider 调用 `glm-5.3-flash`。模型仍只提交页级或跨页原子判断；最终
`composition_craft`、`typography_craft`、`palette_craft`、`visual_communication` 和
`visual_system_sequence` 由确定性 Reducer 生成。规则提供缺陷 cap，模型提供正向视觉信号，
两者不作为两个独立构念重复计权。

v8.3 在上述七个视觉节点之外新增两个 raster-only 文字观察节点。它们仍使用相同
Qwen3.8 → GLM-5.3 同构念路由，但只产生 page-scoped observations；只有 deterministic
content/language owner 因无可提取文本而 N/A 时，Reducer 才采用这些观察。普通可编辑
deck 不调用它们。可争议硬门的 MAJOR/CRITICAL 候选页也会优先占用对应视觉 criterion
的四页预算，避免用未看过风险页的 VLM 结果裁决规则候选。

authorship 节点只判断跨页机械卡片化、图标仪式、模板轮廓重复、公式化文案和缺少特定主张，
不得推断生成来源，也不得重判 composition、legibility、image relevance 或视觉系统一致性。
规则与 VLM 只融合为 `authorship_specificity_v2` 一个公式项；旧字段保留为不计分诊断别名。

functional hard gate 也不再对异构 observation 做全局生计数。规则先产生按 primary owner
归一化的候选；geometry、typography、contrast、resolution 等可争议视觉候选必须由对应
VLM 在已采样页面确认。模型未覆盖候选页、低置信或调用失败时返回 N/A/REVIEW，不能由规则
单独作最终硬门决定；文件损坏、媒体载荷和有 GT 的 correctness 事实仍可确定性落门。

仓库另有一个非默认的 `experimental_text_generation_model_scoring.json`，仅用于评分链路和金标校准实验。它明确标记 `EXPERIMENTAL / UNVALIDATED / production_approved=false`，其 3%/4% 权重不是生产标准。

默认文件映射指向 `*_v8.json`；脱离仓库、缺少 Profile 文件时的
`EvalProfile.default()` 仍是兼容性 v3.1 回退，不能据此声称执行了 v8。
原有 v1 与 `*_v2.json` 文件均保留：v1 为纯确定性快照，v2 执行但不计分的 Shadow
审计；v3.1 是历史整体模型计分路径，v8 是 scoped observation → reducer → training
eligibility 默认路径。回放时应显式指定历史 Profile，不会被默认版本静默改写。

这里的“回放”只保证 Profile 权重、required 和路由语义，不是位级算法回放。当前代码仍会执行
Baseline `2.0.0`、Layout `1.1.0` 和新 `template_residue` Leaf。需要完全复现历史结果/证据时，
必须同时固定历史 Git SHA 或容器镜像。

## Provider 端口

Provider 只需实现一个方法：

```python
from typing import Any, Mapping

from ppt_eval.adapters import ModelAuditRequest


class MyProvider:
    def audit(self, request: ModelAuditRequest) -> Mapping[str, Any]:
        vendor_response = call_vendor_api(request.to_mapping())
        return translate_to_ppt_eval_contract(vendor_response, request)
```

`ModelAuditRequest` 明确分离了版本化的可信 Prompt 与不可信的 Case/PPT/Source/Asset 数据。`request.prompt.reference()` 包含 `prompt_id` / `version` / `sha256`，Provider 必须在响应中原样回显。

VLM Provider 不直接打开 PPTX。调用方可把已渲染的每页图片通过
`artifacts={"slide_images": (...)}` 传入；环境感知 Runtime 在 Flash VLM 已启用且没有该
artifact 时，也会尝试将渲染结果写入按 PPTX 输入哈希隔离的本地缓存。渲染失败
会显式降级，不会让整个 Harness 崩溃。

```python
runtime = LocalEvaluationRuntime(
    "var",
    llm_provider=my_llm_provider,
    vlm_provider=my_vlm_provider,
)
report = runtime.evaluate(
    case,
    artifacts={"slide_images": rendered_slide_paths},
)
```

## 严格响应 Schema

Provider 适配层必须返回且只返回下列顶层字段：

```json
{
  "score": 0.82,
  "confidence": 0.88,
  "model": {
    "provider": "provider-name",
    "model_id": "model-name",
    "version": "immutable-model-snapshot"
  },
  "prompt": {
    "prompt_id": "copy from request.prompt",
    "version": "copy from request.prompt",
    "sha256": "copy from request.prompt"
  },
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 180,
    "cost": 0.0123
  },
  "evidence": [
    {
      "evidence_id": "provider-unique-finding-id",
      "kind": "visual_overlap",
      "message": "Objects overlap on the rendered slide.",
      "page_number": 3,
      "object_id": "optional-existing-object-id",
      "bbox": [0.1, 0.2, 0.3, 0.2],
      "payload": {"criterion": "overlap"}
    }
  ]
}
```

校验规则包括：

- `score` 和 `confidence` 必须是 `[0,1]` 内的有限数字，不会被静默 clamp。
- model 三个字段都必须非空；Prompt ID、版本和哈希必须与请求完全一致。
- token 用量必须是非负整数，cost 必须是非负有限数字。
- evidence 不能为空；幻觉出来的页码、对象 ID 或 source URI 会被拒绝。
- bbox 使用 `[x, y, width, height]` 归一化坐标，必须完整落在页面内。
- 未知字段、非 JSON 元数据、重复 evidence ID 均会被拒绝。

## 状态语义

| 情况 | Execution | Metric | 计分 / Coverage |
|---|---|---|---|
| 未配置 Provider | `SUCCESS` | `NA` | v2 Shadow 中性；v3.1 required 指标会导致 Coverage 降级与人工复核 |
| VLM 缺渲染图或页面不完整 | `SUCCESS` | `NA` | 不记 0 分；v3.1 明确降级 |
| Provider 超时/限流/网络异常 | `ERROR` | `ERROR` | 不记 0 分；已配置指标成为 unresolved |
| 响应违反 Schema | `ERROR` | `ERROR` | 不记 0 分；错误码 `MODEL_RESPONSE_INVALID`；已配置指标成为 unresolved |
| Flash 响应通过校验 | `SUCCESS` | `SCORED` | v3.1 进入权重公式和单项复核线；v2 仍只记录 |
| Advanced 响应通过校验 | `SUCCESS` | `SCORED` | 作为 `DIAGNOSTIC` 证据进报告，不二次进入分数公式 |

实际 model/prompt 版本、用量、成本、`request_fingerprint`、`response_fingerprint` 和 evidence 会保存在 `OracleResult.metadata`。Response 指纹是对通过 Schema 校验的 Provider mapping 做 canonical JSON 序列化后计算的 SHA-256，因此无需持久化可能敏感的原始返回也能校验回放一致性。`RunManifest` 也会汇总实际 model/prompt 版本；若它们与 Profile 的预声明值冲突，经严格验证的实际响应值优先。

注意：只要 metric 被某个实验 Profile 纳入权重表，它的 `ERROR` 就会按现有聚合器语义记为 unresolved，导致 Coverage 降级和 REVIEW，即使它未列入 `required_metric_ids`。`required_metric_ids` 只决定 NA 是否不完整，不会把已配置 metric 的执行 ERROR 静默忽略。

## Qwen OpenAI-compatible 适配器

`QwenOpenAICompatibleProvider` 是标准库实现，不依赖厂商 SDK。运行时优先从
`DASHSCOPE_API_KEY` 读取密钥，本地开发时可回退到已被 Git 和 Docker 忽略的
`api/qwen3.7_flash_api.txt`。也可在自定义 composition root 中显式构造 Provider：

```python
import os

from ppt_eval.infrastructure import (
    QWEN_PRIMARY_MODEL,
    QwenOpenAICompatibleProvider,
)

base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key = os.environ["DASHSCOPE_API_KEY"]

baseline_provider = QwenOpenAICompatibleProvider(
    api_key,
    base_url,
    QWEN_PRIMARY_MODEL,
)
```

v8.2 将 `qwen3.8-flash` 注入主线。`QWEN_FLASH_MODEL=qwen3.7-flash` 只作为历史代码兼容
符号保留；Provider 本身不做路由决策。

线上请求具有以下约束：

- 调用 `/chat/completions`，`stream=false`，不处理流式 chunk。
- 发送 `response_format={"type":"json_object"}` 和 wire-level `enable_thinking=true`。
- 显式发送 `temperature=0`、`seed=0` 和 `max_tokens=4096`，与 Manifest 的默认随机种子对齐并限制单次输出成本。
- 模型只输出 `score` / `confidence` / `evidence`；Provider 负责补齐实际 model ID、Prompt 引用和 usage，之后仍由 `ModelAuditResponse` 作最终严格校验。
- Qwen 偶尔会把像素 bbox 误当成归一化坐标、产生无法在请求对象树/来源集核对的
  可选 `object_id/source_uri`，或对它们返回 null/空串。当 evidence 已有合法页码时，适配器只删除这些
  无效可选值；若模型把合法 `related_page_numbers` 放在 evidence 顶层，则迁移至 payload。
  所有修复都在 `adapter_sanitized_fields` 记录；页码、必填字段和非空定位仍严格校验。
- VLM 请求在本地重算图片 SHA-256，仅在与 `ModelImageInput` 一致时转换为 `data:image/...;base64,...`；本地路径不进入请求体。
- 兼容 Qwen 的 `reasoning_content`，但主动丢弃该字段，不进入 Report、指纹或错误信息。
- HTTP/JSON 异常只暴露分类和 HTTP 状态码，不回显 Authorization、原始响应体或模型原文。
- 若同一请求的模型返回非法 JSON、错误顶层字段或未定位 evidence，Provider 使用完全相同的
  request 最多重试一次。成功时累计两次 usage/cost，并在 evidence payload 记录
  `adapter_retry_count` / `adapter_retry_reasons` / `adapter_usage_complete`；某次响应未带 usage 时，
  只累计可恢复部分并标记 incomplete。两次都失败时仍将可恢复的总 usage/cost 附在
  `MODEL_PROVIDER_ERROR` 上。模型身份不匹配、本地输入校验和运输安全错误不使用该重试。
- 若响应已通过通用 Provider Schema，但不满足六维 criterion 专用契约，结构化视觉 Oracle
  会以同一 request 再调用一次。它同样累计 usage/cost，并记录 `criterion_retry_count`、
  首次/最终 response fingerprint 和 usage 完整性；第二次仍不合法时继续 ERROR，不用第一次的部分维度凑分。
- 保存 API 响应的实际 `model` 值和 prompt/completion token；当兼容接口不提供费用字段时，`cost` 暂记为 `0.0`，并以 `adapter_cost_known=false` / `routing_usage.cost_known=false` 标记，不把它解读为免费。
- Provider 会校验响应的实际 model 与配置角色一致，不接受另一个已配置模型的 provenance。
- `PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS` 配置 Qwen 主线 HTTP 传输超时（默认 120 秒）；
  `PPT_EVAL_QWEN_ADVANCED_HTTP_TIMEOUT_SECONDS` 只保留给历史 Qwen-only composition root。
  `PPT_EVAL_QWEN_ADVANCED_MODEL` 默认为 `qwen3.8-flash`。旧 `PPT_EVAL_QWEN_PLUS_*`
  仅在新变量未设置时作兼容入口；新旧同时设置且冲突会 fail closed。这些都是传输上限，不表示 Profile 中的
  `oracle_timeout_seconds` 已被 Scheduler 强制执行。

Provider 的 `repr` 不包含 API key。非本机 HTTP URL 会被拒绝，生产请求必须使用 HTTPS；`http://127.0.0.1` / `localhost` 仅用于本地 mock 测试。

## Zhipu BigModel OpenAI-compatible 适配器

`ZhipuOpenAICompatibleProvider` 使用独立的 endpoint、credential 和模型身份：

```python
import os

from ppt_eval.infrastructure import ZhipuOpenAICompatibleProvider

fallback_provider = ZhipuOpenAICompatibleProvider(
    os.environ["ZAI_API_KEY"],
    "https://open.bigmodel.cn/api/paas/v4",
    "glm-5.3-flash",
)
```

官方模型码是 `glm-5.3-flash`。请求使用 Bearer 鉴权、OpenAI-compatible
`/chat/completions`、`response_format={"type":"json_object"}`、
`thinking={"type":"enabled","clear_thinking":false}` 和
`reasoning_effort=max`；GLM-5.3-Flash 不允许关闭 thinking。图片继续通过校验后的
Base64 Data URL 发送，但按官方限制收紧为 PNG/JPEG 且单图小于 5 MB。默认传输超时为 300 秒，使用
`PPT_EVAL_ZHIPU_HTTP_TIMEOUT_SECONDS` 调整。

运行时优先读取官方环境变量 `ZAI_API_KEY`，也接受显式的
`PPT_EVAL_ZHIPU_API_KEY`；本地开发可使用被忽略的
`api/glm5.3_flash_api.txt`。多个环境变量别名若含不同密钥会 fail closed。GLM 响应仍经过
同一 `ModelAuditResponse` 严格合同，`reasoning_content` 不进入报告或指纹。
环境感知 Runtime 会把两套已配置凭据同时加入每个 Provider 的 outbound secret guard，
防止 DashScope key 被发往 BigModel，或 BigModel key 被发往 DashScope。

### 本地来源文件边界

`source_materials` 的 inline 文本默认可用于模型审计，但本地文件默认不可读。CLI/API
运行时只有在 `PPT_EVAL_MODEL_SOURCE_ROOTS`（Windows 用 `;`、Linux/macOS 用 `:`
分隔）显式声明受控目录后，才会读取该目录内的普通文件。读取前会解析真实路径并校验
根目录边界，因此 `..` 与符号链接不能越界；远端只收到不含本机目录结构的 opaque
source ID。`.env`、`.git`、`api/`、密钥/证书文件、配置的 DashScope/BigModel key 文件以及
`/proc`、`/sys`、`/dev` 等系统位置始终拒绝。任一文件型来源被拒绝时，该次 scenario
模型 Oracle 返回 `MODEL_SOURCE_ACCESS_DENIED`，不发起远端请求；确定性本地 Oracle
的来源读取语义不受影响。
