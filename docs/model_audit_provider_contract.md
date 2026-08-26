# LLM/VLM 模型审计 Provider 接入契约

本项目不让任何厂商 SDK 或自由文本模型输出直接进入评分。v3 默认内置三项
Qwen Flash 模型审计：

- `llm_content_quality_audit`：语义清晰度、连贯性、具体性、内部一致性与可行动性。指标 ID 为兼容保留；当文本层不可观测时可以使用页图多模态回退。
- `vlm_visual_quality_audit`：对渲染页检查可读性、层次、对齐、遮挡、裁切、对比度与风格一致性。
- `llm_scenario_compliance_audit`：对 `text_to_ppt` / `project_summary` / `multimodal` 进行语义级场景合规检查。

它们组成 Composite `high_cost.model_audits`。这个 ID 为了 v2 回放保持稳定，v3 中的
实际默认 Provider 是低成本 `qwen3.7-flash`。四个 v3 Profile 都将 Flash 内容、视觉
以及生成场景合规分纳入 PPT-PDMS，并把它们列为 required metric。因此 Provider 未配置、
渲染缺失或调用失败时不会被当作 0 分，而是显式降级 Coverage 并转人工复核。

内容指标的输入是自适应的：有文本页比例至少 25% 时使用可提取文本；低于该比例时，
如果有完整或 canonical sample 页图，则用 `ppt-vlm-semantic-content-recovery-audit`
从像素评内容。后者的 `metadata.modality=VLM`，但分数仍属于 content 构念；模态不决定
聚合构念。无文本且无页图时返回 `SEMANTIC_INPUT_UNOBSERVABLE` N/A。

v3 的分层路由是：

```text
qwen3.7-flash 全量默认评测
  -> 分数灰区 / 单项低于复核线 / Flash 与确定性结果分歧 / Flash 高困惑
qwen3.7-plus 条件式高级审计
  -> 缺失 / 低置信 / 灰区 / 审计项互相分歧
人工复核
```

确定性硬门失败始终有最高权威，Plus 不能覆盖；非 `FULL` Coverage 也不会由模型
“补齐”。Plus 只在高置信且适用的审计项一致时给出最终建议，其余转人工。

v5 还提供非默认的 `structured.model_audits`：视觉 Oracle 在一次请求中返回 6 个
固定 criterion summary，Harness 校验完整性/唯一性/分数范围后重算结果，并忽略模型全局分。
详见 [structured_visual_profile_method.md](structured_visual_profile_method.md)。由于尚无同契约 Plus Oracle，
v5 的 `model_audit_routing=STRUCTURED_FLASH_ONLY`，不调用旧标量 Plus VLM。

仓库另有一个非默认的 `experimental_text_generation_model_scoring.json`，仅用于评分链路和金标校准实验。它明确标记 `EXPERIMENTAL / UNVALIDATED / production_approved=false`，其 3%/4% 权重不是生产标准。

默认映射明确指向 `*_v3.json`，脱离仓库时的 `EvalProfile.default()` 回退也使用 v3。
原有 v1 与 `*_v2.json` 文件均保留：v1 为纯确定性快照，v2 执行但不计分的 Shadow
审计，v3 才是 Flash 默认计分与 Flash -> Plus -> Human 路由。回放时应显式指定历史
Profile，不会被默认版本静默改写。

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
| 未配置 Provider | `SUCCESS` | `NA` | v2 Shadow 中性；v3 required 指标会导致 Coverage 降级与人工复核 |
| VLM 缺渲染图或页面不完整 | `SUCCESS` | `NA` | 不记 0 分；v3 明确降级 |
| Provider 超时/限流/网络异常 | `ERROR` | `ERROR` | 不记 0 分；已配置指标成为 unresolved |
| 响应违反 Schema | `ERROR` | `ERROR` | 不记 0 分；错误码 `MODEL_RESPONSE_INVALID`；已配置指标成为 unresolved |
| Flash 响应通过校验 | `SUCCESS` | `SCORED` | v3 进入权重公式和单项复核线；v2 仍只记录 |
| Plus 响应通过校验 | `SUCCESS` | `SCORED` | 作为 `DIAGNOSTIC` 证据进报告，不二次进入分数公式 |

实际 model/prompt 版本、用量、成本、`request_fingerprint`、`response_fingerprint` 和 evidence 会保存在 `OracleResult.metadata`。Response 指纹是对通过 Schema 校验的 Provider mapping 做 canonical JSON 序列化后计算的 SHA-256，因此无需持久化可能敏感的原始返回也能校验回放一致性。`RunManifest` 也会汇总实际 model/prompt 版本；若它们与 Profile 的预声明值冲突，经严格验证的实际响应值优先。

注意：只要 metric 被某个实验 Profile 纳入权重表，它的 `ERROR` 就会按现有聚合器语义记为 unresolved，导致 Coverage 降级和 REVIEW，即使它未列入 `required_metric_ids`。`required_metric_ids` 只决定 NA 是否不完整，不会把已配置 metric 的执行 ERROR 静默忽略。

## Qwen OpenAI-compatible 适配器

`QwenOpenAICompatibleProvider` 是标准库实现，不依赖厂商 SDK。运行时优先从
`DASHSCOPE_API_KEY` 读取密钥，本地开发时可回退到已被 Git 和 Docker 忽略的
`api/qwen3.7_flash_api.txt`。也可在自定义 composition root 中显式构造 Provider：

```python
import os

from ppt_eval.infrastructure import (
    QWEN_FLASH_MODEL,
    QWEN_PLUS_MODEL,
    QwenOpenAICompatibleProvider,
)

base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key = os.environ["DASHSCOPE_API_KEY"]

baseline_provider = QwenOpenAICompatibleProvider(
    api_key,
    base_url,
    QWEN_FLASH_MODEL,
)
escalation_provider = QwenOpenAICompatibleProvider(
    api_key,
    base_url,
    QWEN_PLUS_MODEL,
)
```

`model` 同时是调用层级选择器：`qwen3.7-flash` 用于便宜基线，`qwen3.7-plus` 用于升级审计。Provider 本身不做路由决策，因此两个实例可分别注入基线和 escalation 组合器。

线上请求具有以下约束：

- 调用 `/chat/completions`，`stream=false`，不处理流式 chunk。
- 发送 `response_format={"type":"json_object"}` 和 wire-level `enable_thinking=true`。
- 显式发送 `temperature=0`、`seed=0` 和 `max_tokens=4096`，与 Manifest 的默认随机种子对齐并限制单次输出成本。
- 模型只输出 `score` / `confidence` / `evidence`；Provider 负责补齐实际 model ID、Prompt 引用和 usage，之后仍由 `ModelAuditResponse` 作最终严格校验。
- Qwen 偶尔会把像素 bbox 误当成归一化坐标。适配器只会删除这个无效的可选字段，并在 evidence payload 记录 `adapter_sanitized_fields=["bbox"]`；页码、必填字段和其它定位仍严格校验。
- VLM 请求在本地重算图片 SHA-256，仅在与 `ModelImageInput` 一致时转换为 `data:image/...;base64,...`；本地路径不进入请求体。
- 兼容 Qwen 的 `reasoning_content`，但主动丢弃该字段，不进入 Report、指纹或错误信息。
- HTTP/JSON 异常只暴露分类和 HTTP 状态码，不回显 Authorization、原始响应体或模型原文。
- 保存 API 响应的实际 `model` 值和 prompt/completion token；当兼容接口不提供费用字段时，`cost` 暂记为 `0.0`，不把它解读为免费。
- Provider 会校验响应的实际 model 与配置层级一致，Flash 请求不接受 Plus provenance，反之亦然。
- `PPT_EVAL_QWEN_HTTP_TIMEOUT_SECONDS` 配置 Flash HTTP 传输超时（默认 120 秒）；
  `PPT_EVAL_QWEN_PLUS_HTTP_TIMEOUT_SECONDS` 单独配置 Plus（默认 240 秒）。真实 17 页
  VLM 样本证明 Plus 可超过 120 秒。这两个都是传输上限，不表示 Profile 中的
  `oracle_timeout_seconds` 已被 Scheduler 强制执行。

Provider 的 `repr` 不包含 API key。非本机 HTTP URL 会被拒绝，生产请求必须使用 HTTPS；`http://127.0.0.1` / `localhost` 仅用于本地 mock 测试。

### 本地来源文件边界

`source_materials` 的 inline 文本默认可用于模型审计，但本地文件默认不可读。CLI/API
运行时只有在 `PPT_EVAL_MODEL_SOURCE_ROOTS`（Windows 用 `;`、Linux/macOS 用 `:`
分隔）显式声明受控目录后，才会读取该目录内的普通文件。读取前会解析真实路径并校验
根目录边界，因此 `..` 与符号链接不能越界；远端只收到不含本机目录结构的 opaque
source ID。`.env`、`.git`、`api/`、密钥/证书文件、配置的 DashScope key 文件以及
`/proc`、`/sys`、`/dev` 等系统位置始终拒绝。任一文件型来源被拒绝时，该次 scenario
模型 Oracle 返回 `MODEL_SOURCE_ACCESS_DENIED`，不发起远端请求；确定性本地 Oracle
的来源读取语义不受影响。
