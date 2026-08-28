import React from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileArchive,
  FilePlus2,
  FolderUp,
  LoaderCircle,
  Play,
  RefreshCw,
  UploadCloud,
  X,
} from "lucide-react";
import { ApiError, getEvaluationJob, uploadEvaluation } from "./api";
import type { EvaluationJob, EvaluationScene } from "./types";
import { RESEARCH_RELEASE_LABEL } from "./version";

const ACTIVE_JOB_KEY = "ppt-eval-active-upload-job";
const PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;
const MAX_PRESENTATION_BYTES = 100 * 1024 * 1024;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const MAX_ATTACHMENT_TOTAL_BYTES = 100 * 1024 * 1024;
const MAX_SOURCE_MATERIALS = 16;
const MAX_ASSETS = 32;
const SOURCE_SUFFIXES = new Set([
  ".csv", ".json", ".md", ".tsv", ".txt", ".yaml", ".yml",
]);
const ASSET_SUFFIXES = new Set([
  ".csv", ".gif", ".jpeg", ".jpg", ".mov", ".mp4", ".pdf", ".png", ".svg", ".tsv", ".webm", ".webp", ".xls", ".xlsx",
]);
const WINDOWS_RESERVED_NAMES = new Set([
  "CON", "PRN", "AUX", "NUL",
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
]);
const WINDOWS_INVALID_FILENAME_CHARACTER = /[<>:"/\\|?*]/;

const sceneOptions: Array<{ value: EvaluationScene; label: string; description: string }> = [
  { value: "ready_made", label: "现成 PPT", description: "评估可直接交付的完整演示文稿" },
  { value: "text_to_ppt", label: "文本生成", description: "对照生成要求与目标受众" },
  { value: "project_summary", label: "项目总结", description: "对照项目资料、数字与关键结论" },
  { value: "multimodal", label: "多模态生成", description: "对照指定图片、音视频等素材" },
];

const jobLabel: Record<EvaluationJob["status"], string> = {
  PENDING: "等待执行",
  RUNNING: "评测运行中",
  COMPLETED: "评测已完成",
  FAILED: "评测失败",
};

interface FormErrors {
  presentation?: string;
  caseId?: string;
  sourceMaterials?: string;
  assets?: string;
}

function restoredJob(): EvaluationJob | null {
  try {
    const raw = sessionStorage.getItem(ACTIVE_JOB_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<EvaluationJob>;
    if (
      typeof value.job_id !== "string"
      || !["PENDING", "RUNNING", "COMPLETED", "FAILED"].includes(String(value.status))
    ) {
      sessionStorage.removeItem(ACTIVE_JOB_KEY);
      return null;
    }
    return value as EvaluationJob;
  } catch {
    sessionStorage.removeItem(ACTIVE_JOB_KEY);
    return null;
  }
}

function caseIdFromFile(file: File): string {
  const normalized = file.name.replace(/\.pptx$/i, "").trim().slice(0, 128);
  return normalized || `评测-${crypto.randomUUID().slice(0, 8)}`;
}

function suffix(name: string): string {
  const index = name.lastIndexOf(".");
  return index < 0 ? "" : name.slice(index).toLowerCase();
}

function filenameError(name: string, label: string): string | undefined {
  const normalized = name.normalize("NFC");
  const trimmed = normalized.trim();
  const lastDot = trimmed.lastIndexOf(".");
  const stem = (lastDot > 0 ? trimmed.slice(0, lastDot) : trimmed).toUpperCase();
  if (
    !trimmed
    || trimmed === "."
    || trimmed === ".."
    || CONTROL_CHARACTER.test(trimmed)
    || WINDOWS_INVALID_FILENAME_CHARACTER.test(trimmed)
    || WINDOWS_RESERVED_NAMES.has(stem)
  ) return `${label}文件名不安全`;
  if (Array.from(trimmed).length > 120) return `${label}文件名不能超过 120 个字符`;
  if (new TextEncoder().encode(trimmed).length > 240) return `${label}文件名 UTF-8 长度不能超过 240 字节`;
  return undefined;
}

function attachmentError(
  files: File[],
  allowed: Set<string>,
  maximumCount: number,
  label: string,
): string | undefined {
  if (files.length > maximumCount) return `${label}最多上传 ${maximumCount} 份`;
  for (const file of files) {
    const unsafeName = filenameError(file.name, label);
    if (unsafeName) return unsafeName;
  }
  if (files.some((file) => !allowed.has(suffix(file.name)))) return `${label}中包含不支持的文件类型`;
  if (files.some((file) => file.size === 0)) return `${label}不能包含空文件`;
  if (files.some((file) => file.size > MAX_ATTACHMENT_BYTES)) return `${label}中单个文件不能超过 25 MB`;
  const names = files.map((file) => file.name.toLowerCase());
  if (new Set(names).size !== names.length) return `${label}中不能有同名文件`;
  return undefined;
}

function fileList(files: File[]) {
  if (files.length === 0) return null;
  return (
    <ul className="context-file-list">
      {files.map((file, index) => (
        <li key={`${file.name}-${file.size}-${index}`}>
          <FileArchive size={14} />
          <span>{file.name}</span>
          <small>{(file.size / 1024).toFixed(file.size >= 1024 * 1024 ? 0 : 1)} KB</small>
        </li>
      ))}
    </ul>
  );
}

export default function NewEvaluationWorkspace({
  onEnterAudit,
  onOpenAll,
}: {
  onEnterAudit: (runId: string) => void;
  onOpenAll: () => void;
}) {
  const [presentation, setPresentation] = React.useState<File | null>(null);
  const [caseId, setCaseId] = React.useState("");
  const [caseIdEdited, setCaseIdEdited] = React.useState(false);
  const [scene, setScene] = React.useState<EvaluationScene>("ready_made");
  const [request, setRequest] = React.useState("");
  const [audience, setAudience] = React.useState("");
  const [sourceMaterials, setSourceMaterials] = React.useState<File[]>([]);
  const [assets, setAssets] = React.useState<File[]>([]);
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [dragging, setDragging] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);
  const [uploadProgress, setUploadProgress] = React.useState<number | null>(null);
  const [job, setJob] = React.useState<EvaluationJob | null>(() => restoredJob());
  const [jobError, setJobError] = React.useState("");
  const [jobLost, setJobLost] = React.useState(false);
  const [pollingStopped, setPollingStopped] = React.useState(false);
  const uploadController = React.useRef<AbortController | null>(null);
  const idempotencyKey = React.useRef(crypto.randomUUID());
  const presentationInput = React.useRef<HTMLInputElement | null>(null);
  const sourceInput = React.useRef<HTMLInputElement | null>(null);
  const assetInput = React.useRef<HTMLInputElement | null>(null);

  const busy = uploading || job?.status === "PENDING" || job?.status === "RUNNING";
  const needsPrompt = scene !== "ready_made";
  const needsSources = scene === "project_summary";
  const needsAssets = scene === "multimodal";

  React.useEffect(() => {
    if (!job || (job.status !== "PENDING" && job.status !== "RUNNING")) return;
    let cancelled = false;
    let timer = 0;

    const poll = async () => {
      try {
        const latest = await getEvaluationJob(job.job_id);
        if (cancelled) return;
        setJob(latest);
        setJobError("");
        setJobLost(false);
        setPollingStopped(false);
        if (latest.status === "PENDING" || latest.status === "RUNNING") {
          sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(latest));
          timer = window.setTimeout(() => void poll(), 1800);
        } else {
          sessionStorage.removeItem(ACTIVE_JOB_KEY);
        }
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof ApiError && cause.status === 404) {
          sessionStorage.removeItem(ACTIVE_JOB_KEY);
          setJob(null);
          setJobLost(true);
          setPollingStopped(false);
          setJobError("旧 Job 已无法查询，API 进程可能已重启。可重新评测，或去“全部运行”确认是否已产生 run。");
          return;
        }
        setJobError(cause instanceof Error ? cause.message : "无法读取评测 Job");
        timer = window.setTimeout(() => void poll(), 3000);
      }
    };

    timer = window.setTimeout(() => void poll(), 500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [job?.job_id, job?.status]);

  React.useEffect(() => () => uploadController.current?.abort(), []);

  function markFormChanged() {
    idempotencyKey.current = crypto.randomUUID();
  }

  function choosePresentation(file: File | undefined) {
    if (!file) return;
    markFormChanged();
    setPresentation(file);
    const presentationError = filenameError(file.name, "PPTX")
      ?? (!file.name.toLowerCase().endsWith(".pptx")
      ? "主文件必须是 .pptx"
      : file.size === 0
        ? "PPTX 文件为空"
        : file.size > MAX_PRESENTATION_BYTES
          ? "PPTX 不能超过 100 MB"
          : undefined);
    setErrors((current) => ({ ...current, presentation: presentationError }));
    if (!caseIdEdited || !caseId.trim()) setCaseId(caseIdFromFile(file));
  }

  function validate(): boolean {
    const next: FormErrors = {};
    if (!presentation) next.presentation = "请选择一份 PPTX";
    else if (filenameError(presentation.name, "PPTX")) {
      next.presentation = filenameError(presentation.name, "PPTX");
    } else if (!presentation.name.toLowerCase().endsWith(".pptx")) {
      next.presentation = "主文件必须是 .pptx";
    } else if (presentation.size === 0) next.presentation = "PPTX 文件为空";
    else if (presentation.size > MAX_PRESENTATION_BYTES) next.presentation = "PPTX 不能超过 100 MB";
    if (!caseId.trim()) next.caseId = "请输入 case ID";
    else if (caseId.trim().length > 128) next.caseId = "case ID 不能超过 128 个字符";
    else if (CONTROL_CHARACTER.test(caseId)) next.caseId = "case ID 不能包含控制字符";
    if (needsSources && sourceMaterials.length > 0) {
      const sourceError = attachmentError(
        sourceMaterials,
        SOURCE_SUFFIXES,
        MAX_SOURCE_MATERIALS,
        "来源材料",
      );
      if (sourceError) next.sourceMaterials = sourceError;
    }
    if (needsAssets && assets.length > 0) {
      const assetError = attachmentError(assets, ASSET_SUFFIXES, MAX_ASSETS, "指定素材");
      if (assetError) next.assets = assetError;
    }
    const activeAttachments = needsSources ? sourceMaterials : needsAssets ? assets : [];
    const attachmentBytes = activeAttachments.reduce((total, file) => total + file.size, 0);
    if (attachmentBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
      if (needsSources) next.sourceMaterials = "附件总大小不能超过 100 MB";
      if (needsAssets) next.assets = "附件总大小不能超过 100 MB";
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!validate() || !presentation || busy || uploadController.current) return;
    const formData = new FormData();
    formData.append("presentation", presentation, presentation.name);
    formData.append("case_id", caseId.trim());
    formData.append("scene", scene);
    if (needsPrompt) {
      if (request.trim()) formData.append("request", request.trim());
      if (audience.trim()) formData.append("audience", audience.trim());
    }
    if (needsSources) {
      sourceMaterials.forEach((file) => formData.append("source_materials", file, file.name));
    }
    if (needsAssets) assets.forEach((file) => formData.append("assets", file, file.name));

    const controller = new AbortController();
    uploadController.current = controller;
    setUploading(true);
    setUploadProgress(0);
    setJob(null);
    setJobError("");
    setJobLost(false);
    setPollingStopped(false);
    try {
      const created = await uploadEvaluation(
        formData,
        idempotencyKey.current,
        setUploadProgress,
        controller.signal,
      );
      setJob(created);
      if (created.status === "PENDING" || created.status === "RUNNING") {
        sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(created));
      }
      setJobLost(false);
      setPollingStopped(false);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        setJobError("上传已停止；若请求已到达服务端，请在“全部运行”中确认是否已创建任务。");
      } else {
        setJobError(cause instanceof Error ? cause.message : "无法上传评测文件");
      }
    } finally {
      uploadController.current = null;
      setUploading(false);
    }
  }

  function resetJob(renewKey = false) {
    sessionStorage.removeItem(ACTIVE_JOB_KEY);
    setJob(null);
    setJobError("");
    setJobLost(false);
    setPollingStopped(false);
    setUploadProgress(null);
    if (renewKey) markFormChanged();
  }

  function resetForm() {
    resetJob();
    markFormChanged();
    setPresentation(null);
    setCaseId("");
    setCaseIdEdited(false);
    setRequest("");
    setAudience("");
    setSourceMaterials([]);
    setAssets([]);
    setErrors({});
    if (presentationInput.current) presentationInput.current.value = "";
    if (sourceInput.current) sourceInput.current.value = "";
    if (assetInput.current) assetInput.current.value = "";
  }

  function stopPolling() {
    sessionStorage.removeItem(ACTIVE_JOB_KEY);
    setJob(null);
    setJobLost(false);
    setPollingStopped(true);
    setJobError("已停止本页轮询；这不会取消服务端已创建的任务。可稍后去“全部运行”查找已落盘的 run。");
  }

  return (
    <section className="evaluation-workspace" aria-labelledby="new-evaluation-title">
      <header className="evaluation-heading">
        <div>
          <span>Evaluation intake</span>
          <h1 id="new-evaluation-title">新建评测</h1>
          <p>上传真实 PPT 与场景证据。系统完成原子评测后，可直接进入对应 run 的人工审计。</p>
        </div>
        <div className="evaluation-contract"><CheckCircle2 size={17} />{RESEARCH_RELEASE_LABEL}</div>
      </header>

      <div className="evaluation-layout">
        <form className="evaluation-form" onSubmit={(event) => void submit(event)} noValidate>
          <div className="form-section-title"><span>01</span><div><strong>评测对象</strong><small>原始 PPTX 将成为可追溯的 run 制品</small></div></div>
          <label
            className={`pptx-dropzone ${dragging ? "dragging" : ""} ${errors.presentation ? "invalid" : ""}`}
            aria-disabled={busy}
            onDragEnter={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
            onDragOver={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
            onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              if (!busy) choosePresentation(event.dataTransfer.files[0]);
            }}
          >
            <input
              ref={presentationInput}
              type="file"
              accept={`.pptx,${PPTX_MIME}`}
              disabled={busy}
              aria-required="true"
              aria-invalid={Boolean(errors.presentation)}
              onChange={(event) => choosePresentation(event.target.files?.[0])}
              aria-describedby={errors.presentation ? "presentation-error" : undefined}
            />
            {presentation ? <>
              <FileArchive size={30} />
              <strong>{presentation.name}</strong>
              <span>{(presentation.size / 1024 / 1024).toFixed(2)} MB · 点击可重新选择</span>
            </> : <>
              <UploadCloud size={31} />
              <strong>拖拽 PPTX 到这里，或点击选择</strong>
              <span>仅接受 Open XML PowerPoint 文件</span>
            </>}
          </label>
          {errors.presentation && <p className="field-error" id="presentation-error"><AlertCircle size={13} />{errors.presentation}</p>}

          <div className="field-grid two-columns">
            <label>
              <span>Case ID <b>*</b></span>
              <input
                value={caseId}
                maxLength={128}
                disabled={busy}
                aria-required="true"
                aria-invalid={Boolean(errors.caseId)}
                aria-describedby={errors.caseId ? "case-id-error" : undefined}
                onChange={(event) => { markFormChanged(); setCaseId(event.target.value); setCaseIdEdited(true); setErrors((current) => ({ ...current, caseId: undefined })); }}
                placeholder="例如 market-research-01"
              />
              {errors.caseId && <small className="field-error" id="case-id-error"><AlertCircle size={12} />{errors.caseId}</small>}
            </label>
            <label>
              <span>评测场景 <b>*</b></span>
              <select
                value={scene}
                disabled={busy}
                aria-required="true"
                onChange={(event) => { markFormChanged(); setScene(event.target.value as EvaluationScene); }}
              >
                {sceneOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <small>{sceneOptions.find((option) => option.value === scene)?.description}</small>
            </label>
          </div>

          {needsPrompt && <>
            <div className="form-section-title"><span>02</span><div><strong>任务上下文</strong><small>这些信息进入场景 Oracle，不影响机器事实的不可变性</small></div></div>
            <label className="wide-field">
              <span>原始生成要求</span>
              <textarea
                value={request}
                maxLength={20000}
                disabled={busy}
                onChange={(event) => { markFormChanged(); setRequest(event.target.value); }}
                placeholder="粘贴交付要求、内容范围与约束"
              />
            </label>
            <label className="wide-field">
              <span>目标受众</span>
              <input value={audience} maxLength={2000} disabled={busy} onChange={(event) => { markFormChanged(); setAudience(event.target.value); }} placeholder="例如：消费行业管理层" />
            </label>
          </>}

          {needsSources && <div className="context-upload">
            <div><strong>来源材料</strong><small>可选；支持 TXT/Markdown 与 CSV/JSON/YAML 结构化资料</small></div>
            <label className="file-picker"><FolderUp size={15} />选择来源文件<input ref={sourceInput} type="file" accept={[...SOURCE_SUFFIXES].join(",")} multiple disabled={busy} aria-invalid={Boolean(errors.sourceMaterials)} aria-describedby={errors.sourceMaterials ? "source-materials-error" : undefined} onChange={(event) => { markFormChanged(); setSourceMaterials(Array.from(event.target.files ?? [])); setErrors((current) => ({ ...current, sourceMaterials: undefined })); }} /></label>
            {fileList(sourceMaterials)}
            {errors.sourceMaterials && <p className="field-error" id="source-materials-error"><AlertCircle size={12} />{errors.sourceMaterials}</p>}
          </div>}

          {needsAssets && <div className="context-upload">
            <div><strong>指定素材</strong><small>可选；多份文件分别作为 assets 上传</small></div>
            <label className="file-picker"><FilePlus2 size={15} />选择素材文件<input ref={assetInput} type="file" accept={[...ASSET_SUFFIXES].join(",")} multiple disabled={busy} aria-invalid={Boolean(errors.assets)} aria-describedby={errors.assets ? "assets-error" : undefined} onChange={(event) => { markFormChanged(); setAssets(Array.from(event.target.files ?? [])); setErrors((current) => ({ ...current, assets: undefined })); }} /></label>
            {fileList(assets)}
            {errors.assets && <p className="field-error" id="assets-error"><AlertCircle size={12} />{errors.assets}</p>}
          </div>}

          <div className="evaluation-actions">
            <button type="button" className="quiet" disabled={busy} onClick={resetForm}>清空</button>
            {uploading && <button type="button" className="danger-quiet" onClick={() => uploadController.current?.abort()}><X size={15} />取消上传</button>}
            <button type="submit" className="evaluation-primary" disabled={busy}><Play size={15} />上传并开始评测</button>
          </div>
        </form>

        <aside className="job-panel" aria-live="polite">
          <div className="form-section-title"><span>03</span><div><strong>运行状态</strong><small>只展示服务端真实状态，不推测 DAG 百分比</small></div></div>
          {!uploading && !job && !jobError && <div className="job-empty"><UploadCloud size={28} /><strong>尚未创建任务</strong><span>文件上传成功后，系统将返回可轮询的 Job ID。</span></div>}

          {uploading && <div className="job-state uploading">
            <LoaderCircle className="spin" size={25} />
            <strong>正在上传文件</strong>
            <span>{uploadProgress == null ? "浏览器未提供可计算的总字节数" : `已上传 ${uploadProgress}%`}</span>
            {uploadProgress != null && <progress max={100} value={uploadProgress}>{uploadProgress}%</progress>}
          </div>}

          {job && <div className={`job-state status-${job.status.toLowerCase()}`}>
            {job.status === "COMPLETED" ? <CheckCircle2 size={26} /> : job.status === "FAILED" ? <AlertCircle size={26} /> : <LoaderCircle className="spin" size={26} />}
            <strong>{jobLabel[job.status]}</strong>
            <code>{job.job_id}</code>
            {(job.status === "PENDING" || job.status === "RUNNING") && <>
              <span>{job.status === "PENDING" ? "任务已进入执行队列。" : "Harness 正在运行评测节点。"}</span>
              <p>服务端尚未提供节点级进度，因此此处不显示推测百分比。</p>
            </>}
            {job.status === "COMPLETED" && job.run_id && <>
              <span>Run 已持久化，可以查看页图、Attention、完整 Matrix 与审计制品。</span>
              <code className="run-code">{job.run_id}</code>
              <button className="evaluation-primary" onClick={() => onEnterAudit(job.run_id!)}>进入审计</button>
            </>}
            {job.status === "COMPLETED" && !job.run_id && <span>服务端的完成响应缺少 run_id，暂时无法进入审计。</span>}
            {job.status === "FAILED" && <>
              <span>{job.error || job.error_code || "服务端未返回失败详情"}</span>
              <button className="retry-button" onClick={() => resetJob(true)}><RefreshCw size={14} />修正后重新提交</button>
            </>}
          </div>}

          {jobError && <div className="job-poll-error"><AlertCircle size={15} /><div><strong>{job ? "状态读取暂时失败" : jobLost ? "旧 Job 已失效" : pollingStopped ? "已停止轮询" : "上传请求未完成"}</strong><span>{jobError}</span>{job && (job.status === "PENDING" || job.status === "RUNNING") && <><small>系统会继续重试，不会重复提交评测。</small><div className="job-error-actions"><button type="button" onClick={stopPolling}>停止轮询</button><button type="button" onClick={onOpenAll}>去全部运行</button></div></>}{!job && <div className="job-error-actions"><button type="button" onClick={() => { if (jobLost) markFormChanged(); setJobLost(false); setPollingStopped(false); setJobError(""); }}>重新评测</button><button type="button" onClick={onOpenAll}>去全部运行</button></div>}</div></div>}

          <div className="job-notes">
            <strong>本地运行边界</strong>
            <ul>
              <li>关闭本页面不会取消已创建的 Job。</li>
              <li>未完成 Job 会在本次浏览器会话中自动恢复轮询。</li>
              <li>API 进程重启后，进程内 Job 可能无法恢复。</li>
            </ul>
          </div>
        </aside>
      </div>
    </section>
  );
}
