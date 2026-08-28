import React from "react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleUserRound,
  Download,
  FileJson2,
  FileSearch,
  Filter,
  History,
  Layers3,
  LoaderCircle,
  Plus,
  RefreshCw,
  Route,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { getReviewAudit, getReviewTask, listReviewTasks, submitReview } from "./api";
import NewEvaluationWorkspace from "./NewEvaluationWorkspace";
import type {
  AttentionIssue,
  Decision,
  FullAuditPayload,
  IssueResolution,
  ReviewSubmission,
  ReviewTaskDetail,
  ReviewTaskSummary,
  ReviewVerdict,
} from "./types";
import { RESEARCH_RELEASE_LABEL } from "./version";

const DEMO_MODE = import.meta.env.DEV && import.meta.env.VITE_DEMO_MODE === "true";
const priorityLabel = { P0: "立即处理", P1: "优先复核", P2: "质量抽查", P3: "常规抽查" };
const viewLabel = { queue: "审计队列", all: "全部运行", completed: "已完成" };
const trackLabel = { visual: "视觉", layout: "版式", content: "内容", full_deck: "整套" };
const severityLabel = { INFO: "信息", MINOR: "轻微", MAJOR: "主要", CRITICAL: "严重" };
type ReviewView = keyof typeof viewLabel;
type AuditTab = "score" | "facts" | "routes" | "manifest" | "history";

function locationSelection(): { view: ReviewView; runId: string } {
  const params = new URLSearchParams(window.location.search);
  const rawView = params.get("view");
  const view: ReviewView = rawView === "all" || rawView === "completed" ? rawView : "queue";
  return { view, runId: params.get("run")?.trim() ?? "" };
}

function updateLocation(view: ReviewView, runId: string, mode: "push" | "replace") {
  const url = new URL(window.location.href);
  url.searchParams.set("view", view);
  if (runId) url.searchParams.set("run", runId);
  else url.searchParams.delete("run");
  window.history[mode === "push" ? "pushState" : "replaceState"]({}, "", url);
}

function QueueRow({
  task,
  active,
  onSelect,
}: {
  task: ReviewTaskSummary;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`queue-row ${active ? "active" : ""}`} onClick={onSelect}>
      <span className={`priority priority-${task.priority}`}>{task.priority}</span>
      <span className="queue-copy">
        <strong>{task.case_id}</strong>
        <small>{task.priority_reason}</small>
        <span>
          {task.scenario} · {task.issue_count} 个疑点
        </span>
      </span>
      <span className="queue-state">
        <b className={`decision decision-${task.decision}`}>{task.decision}</b>
        <small>{task.coverage}</small>
      </span>
      <ChevronRight size={16} />
    </button>
  );
}

function IssueCard({
  issue,
  active,
  resolution,
  onSelect,
  onPageSelect,
  onResolve,
}: {
  issue: AttentionIssue;
  active: boolean;
  resolution?: IssueResolution;
  onSelect: () => void;
  onPageSelect: (page: number) => void;
  onResolve: (value: IssueResolution) => void;
}) {
  const [rationalesOpen, setRationalesOpen] = React.useState(false);
  const rationaleId = `rationales-${issue.issue_id}`;
  const visibleRationales = issue.rationales.slice(0, 3);
  return (
    <article className={`issue-card ${active ? "active" : ""}`}>
      <button
        className="issue-card-select"
        onClick={onSelect}
        aria-current={active ? "true" : undefined}
      >
        <span className="issue-topline">
          <span className={`severity severity-${issue.severity}`}>{severityLabel[issue.severity]}</span>
          <span className={`consensus consensus-${issue.consensus.status.toLowerCase()}`}>
            {issue.consensus.label}
          </span>
        </span>
        <h3>{issue.title}</h3>
        <p>{issue.summary}</p>
        <span className="issue-fact-count">{issue.detail_count} 条底层事实</span>
      </button>
      {issue.page_numbers.length > 0 && (
        <div className="page-pills" aria-label="关联页面">
          {issue.page_numbers.slice(0, 8).map((page) => (
            <button key={page} onClick={() => onPageSelect(page)} aria-label={`查看第 ${page} 页`}>
              P{page}
            </button>
          ))}
          {issue.page_numbers.length > 8 && <span>+{issue.page_numbers.length - 8}</span>}
        </div>
      )}
      {issue.rationales.length > 0 && <>
        <button
          className="rationale-toggle"
          onClick={() => setRationalesOpen((current) => !current)}
          aria-expanded={rationalesOpen}
          aria-controls={rationaleId}
        >
          <ChevronDown size={14} />
          {rationalesOpen ? "收起判断依据" : `查看判断依据（${issue.rationales.length}）`}
        </button>
        {rationalesOpen && <div className="issue-rationales" id={rationaleId}>
          <ul>{visibleRationales.map((rationale, index) => <li key={index}>{rationale}</li>)}</ul>
          {issue.rationales.length > visibleRationales.length && <small>另有 {issue.rationales.length - visibleRationales.length} 条详见完整审计。</small>}
        </div>}
      </>}
      <div className="issue-actions" onClick={(event) => event.stopPropagation()}>
        <button
          className={resolution === "CONFIRMED" ? "selected" : ""}
          onClick={() => onResolve("CONFIRMED")}
        >
          确认
        </button>
        <button
          className={resolution === "FALSE_POSITIVE" ? "selected" : ""}
          onClick={() => onResolve("FALSE_POSITIVE")}
        >
          误报
        </button>
        <button
          className={resolution === "INSUFFICIENT_EVIDENCE" ? "selected" : ""}
          onClick={() => onResolve("INSUFFICIENT_EVIDENCE")}
        >
          证据不足
        </button>
      </div>
    </article>
  );
}

function EmptyAttentionState({ task }: { task: ReviewTaskDetail }) {
  const tone = task.decision === "PASS"
    ? "success"
    : task.decision === "REVIEW"
      ? "warning"
      : "danger";
  const fallbackTitle = task.decision === "PASS"
    ? "没有需要优先处理的语义问题"
    : task.decision === "REVIEW"
      ? "系统要求人工复核，但没有可定位的语义问题"
      : "机器结论异常，但系统没有生成可定位项";
  const fallbackDescription = task.decision === "PASS"
    ? "仍可打开完整审计进行常规抽查。"
    : "请抽查关键页或打开完整审计，必要时请求补充证据。";
  return (
    <div
      className={`attention-empty attention-empty-${tone}`}
      role={tone === "danger" ? "alert" : "status"}
    >
      {tone === "success" ? <CheckCircle2 size={23} /> : tone === "warning" ? <AlertTriangle size={23} /> : <AlertCircle size={23} />}
      <strong>{task.attention_summary.title || fallbackTitle}</strong>
      <span>{task.attention_summary.description || fallbackDescription}</span>
      {task.attention_summary.raw_fact_count > 0 && <small>完整审计中仍保留 {task.attention_summary.raw_fact_count} 条底层事实。</small>}
    </div>
  );
}

function valueText(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  if (typeof value === "string" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function FullAuditDrawer({
  task,
  audit,
  loading,
  tab,
  onTab,
  onClose,
}: {
  task: ReviewTaskDetail;
  audit: FullAuditPayload | null;
  loading: boolean;
  tab: AuditTab;
  onTab: (tab: AuditTab) => void;
  onClose: () => void;
}) {
  return (
    <div className="drawer-backdrop" role="presentation" onClick={onClose}>
      <aside
        className="audit-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="完整审计事实"
        onClick={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <span>完整审计事实</span>
            <strong>{task.case_id}</strong>
          </div>
          <button aria-label="关闭完整审计" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <nav aria-label="审计事实分类">
          <button className={tab === "score" ? "active" : ""} onClick={() => onTab("score")}>
            <SlidersHorizontal size={15} />分数与 Lineage
          </button>
          <button className={tab === "facts" ? "active" : ""} onClick={() => onTab("facts")}>
            <Layers3 size={15} />底层事实
          </button>
          <button className={tab === "routes" ? "active" : ""} onClick={() => onTab("routes")}>
            <Route size={15} />模型路由
          </button>
          <button className={tab === "manifest" ? "active" : ""} onClick={() => onTab("manifest")}>
            <FileJson2 size={15} />Manifest
          </button>
          <button className={tab === "history" ? "active" : ""} onClick={() => onTab("history")}>
            <History size={15} />人审历史
          </button>
        </nav>
        <div className="drawer-content">
          {loading && <p className="drawer-empty"><LoaderCircle className="spin" size={20} />加载完整审计事实</p>}
          {!loading && !audit && <p className="drawer-empty">完整审计事实不可用。</p>}
          {audit && <>
          {tab === "score" && (
            <div className="fact-table">
              <div className="fact-row header"><span>Metric</span><span>状态</span><span>严重度</span><span>分数</span><span>置信度</span></div>
              {audit.results.map((result, index) => (
                <div className="fact-row" key={`${valueText(result.metric_id)}-${index}`}>
                  <span><code>{valueText(result.metric_id)}</code></span>
                  <span>{valueText(result.metric_status)}</span>
                  <span>{valueText(result.severity)}</span>
                  <span>{valueText(result.normalized_score ?? result.multiplier)}</span>
                  <span>{valueText(result.confidence)}</span>
                </div>
              ))}
              {audit.results.length === 0 && <p className="drawer-empty">当前报告没有可展示的结果 Matrix。</p>}
            </div>
          )}
          {tab === "facts" && (
            <div className="atomic-facts">
              {audit.observation_artifact && <div className={`observation-artifact ${audit.observation_artifact.available && audit.observation_artifact.valid ? "valid" : audit.observation_artifact.valid === false ? "invalid" : "unavailable"}`}>
                <div>
                  <strong>完整 Observation</strong>
                  <span>{audit.observation_artifact.valid === false
                    ? "Observation 制品校验失败，不可下载"
                    : audit.observation_artifact.available
                      ? `${audit.observation_artifact.count} 条 · 哈希校验通过`
                      : "该历史运行未保存 Observation 制品"}</span>
                </div>
                {audit.observation_artifact.available && audit.observation_artifact.url && <a href={audit.observation_artifact.url} target="_blank" rel="noreferrer"><Download size={14} />下载全量事实</a>}
              </div>}
              <section>
                <h3>语义问题与底层 Lineage</h3>
                {audit.attention_details.map((detail, index) => (
                  <article key={`${valueText(detail.issue_id)}-${index}`}>
                    <strong>{valueText(detail.issue_id ?? detail.semantic_code ?? `detail-${index + 1}`)}</strong>
                    <pre>{JSON.stringify(detail, null, 2)}</pre>
                  </article>
                ))}
                {audit.attention_details.length === 0 && <p className="drawer-empty">没有语义问题的底层映射。</p>}
              </section>
              <section>
                <h3>硬门与聚合事实</h3>
                {audit.gate_results.map((gate, index) => (
                  <article key={`${valueText(gate.metric_id)}-${index}`}>
                    <strong>{valueText(gate.metric_id ?? `gate-${index + 1}`)}</strong>
                    <pre>{JSON.stringify(gate, null, 2)}</pre>
                  </article>
                ))}
                {audit.gate_results.length === 0 && <p className="drawer-empty">该运行没有硬门记录。</p>}
              </section>
            </div>
          )}
          {tab === "routes" && (
            <div className="route-list">
              {audit.model_routes.map((route, index) => (
                <article key={index}>
                  <strong>{valueText(route.metric_id ?? route.criterion_id)}</strong>
                  <p>selected={valueText(route.selected_tier)} · escalation={valueText(route.escalation_reason)}</p>
                  <code>{JSON.stringify(route, null, 2)}</code>
                </article>
              ))}
              {audit.model_routes.length === 0 && <p className="drawer-empty">该运行没有模型路由记录。</p>}
            </div>
          )}
          {tab === "manifest" && <pre>{JSON.stringify(audit.manifest ?? {}, null, 2)}</pre>}
          {tab === "history" && (
            <div className="history-list">
              {audit.reviews.map((review) => (
                <article key={review.review_id}>
                  <strong>{review.verdict}</strong>
                  <span>{review.reviewer_id} · {new Date(review.created_at).toLocaleString()}</span>
                  <p>{review.note || "未填写备注"}</p>
                  <details>
                    <summary>查看完整人审事件</summary>
                    <pre>{JSON.stringify(review, null, 2)}</pre>
                  </details>
                </article>
              ))}
              {audit.reviews.length === 0 && <p className="drawer-empty">尚无人工审计事件。</p>}
            </div>
          )}
          </>}
        </div>
        <footer className="artifact-actions">
          <a href={task.artifacts.report_url} target="_blank" rel="noreferrer"><FileJson2 size={15} />EvaluationReport</a>
          {task.artifacts.observations_url && <a href={task.artifacts.observations_url} target="_blank" rel="noreferrer"><Download size={15} />完整 Observation</a>}
          {task.artifacts.render_manifest_url && <a href={task.artifacts.render_manifest_url} target="_blank" rel="noreferrer"><Download size={15} />Render Manifest</a>}
          {task.artifacts.source_pptx_url && <a href={task.artifacts.source_pptx_url}><Download size={15} />原始 PPTX</a>}
          {task.inputs?.filter((input) => input.role === "source_material" || input.role === "asset").map((input) => {
            const label = `${input.role === "source_material" ? "来源" : "素材"}：${input.original_name}`;
            return input.available && input.download_url ? (
              <a key={`${input.role}-${input.index}-${input.sha256 ?? "missing"}`} href={input.download_url} download>
                <Download size={15} />{label}
              </a>
            ) : (
              <span className="artifact-unavailable" key={`${input.role}-${input.index}-${input.sha256 ?? "missing"}`}>
                <AlertCircle size={15} />{label}（不可用）
              </span>
            );
          })}
        </footer>
      </aside>
    </div>
  );
}

export default function App() {
  const initialSelection = React.useMemo(locationSelection, []);
  const [workspace, setWorkspace] = React.useState<"review" | "create">("review");
  const [view, setView] = React.useState<ReviewView>(initialSelection.view);
  const [query, setQuery] = React.useState("");
  const [tasks, setTasks] = React.useState<ReviewTaskSummary[]>([]);
  const [activeRun, setActiveRun] = React.useState(initialSelection.runId);
  const [liveRunId, setLiveRunId] = React.useState("");
  const [task, setTask] = React.useState<ReviewTaskDetail | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [activeIssue, setActiveIssue] = React.useState("");
  const [resolutions, setResolutions] = React.useState<Record<string, IssueResolution>>({});
  const [reviewer, setReviewer] = React.useState(() => localStorage.getItem("ppt-eval-reviewer") ?? "");
  const [verdict, setVerdict] = React.useState<ReviewVerdict>("CONFIRM_SYSTEM_DECISION");
  const [targetDecision, setTargetDecision] = React.useState<Decision>("REVIEW");
  const [note, setNote] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [drawerTab, setDrawerTab] = React.useState<AuditTab>("score");
  const [audit, setAudit] = React.useState<FullAuditPayload | null>(null);
  const [auditLoading, setAuditLoading] = React.useState(false);
  const [showAllIssues, setShowAllIssues] = React.useState(false);
  const requestId = React.useRef(crypto.randomUUID());
  const auditRequestSequence = React.useRef(0);

  const loadTasks = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      if (DEMO_MODE && !liveRunId) {
        const { demoTasks } = await import("./demo");
        const filtered = demoTasks.filter((item) =>
          `${item.case_id} ${item.run_id} ${item.scenario}`.toLowerCase().includes(query.toLowerCase()),
        );
        setTasks(view === "completed" ? [] : filtered);
        setActiveRun((current) => current || filtered[0]?.run_id || "");
      } else {
        const params = new URLSearchParams({ view });
        if (query.trim()) params.set("query", query.trim());
        const response = await listReviewTasks(params);
        setTasks(response.items);
        setActiveRun((current) => current || response.items[0]?.run_id || "");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法加载审计队列");
    } finally {
      setLoading(false);
    }
  }, [liveRunId, query, view]);

  React.useEffect(() => {
    if (workspace !== "review") return;
    const timer = window.setTimeout(() => void loadTasks(), 180);
    return () => window.clearTimeout(timer);
  }, [loadTasks, workspace]);

  React.useEffect(() => {
    if (workspace === "review") updateLocation(view, activeRun, "replace");
  }, [activeRun, view, workspace]);

  React.useEffect(() => {
    const restoreLocation = () => {
      const restored = locationSelection();
      setWorkspace("review");
      setView(restored.view);
      setActiveRun(restored.runId);
    };
    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);

  React.useEffect(() => {
    auditRequestSequence.current += 1;
    setDrawerOpen(false);
    setAudit(null);
    setAuditLoading(false);
    if (!activeRun) {
      setTask(null);
      return;
    }
    let cancelled = false;
    setError("");
    const load = async () => {
      try {
        const detail = DEMO_MODE && !liveRunId
          ? (await import("./demo")).demoDetailForRun(activeRun)
          : await getReviewTask(activeRun);
        if (cancelled) return;
        setTask(detail);
        const firstIssue = detail.issues[0];
        setActiveIssue(firstIssue?.issue_id ?? "");
        setPage(firstIssue?.page_numbers[0] ?? 1);
        setResolutions({});
        setNote("");
        setNotice("");
        setAudit(null);
        setShowAllIssues(false);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法加载审计详情");
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [activeRun, liveRunId]);

  React.useEffect(() => {
    if (reviewer.trim()) localStorage.setItem("ppt-eval-reviewer", reviewer.trim());
  }, [reviewer]);

  const slide = task?.slides[page - 1];
  const issue = task?.issues.find((item) => item.issue_id === activeIssue) ?? task?.issues[0];
  const requiredIssueIds = task?.issues.filter((item) => item.priority === "P0" || item.priority === "P1").map((item) => item.issue_id) ?? [];
  const unresolvedRequired = requiredIssueIds.filter((issueId) => !resolutions[issueId]);
  const noteRequired = verdict !== "CONFIRM_SYSTEM_DECISION" || Object.values(resolutions).some((value) => value !== "CONFIRMED");
  const canSubmit = Boolean(
    task &&
      reviewer.trim() &&
      (verdict === "REQUEST_MORE_EVIDENCE" || unresolvedRequired.length === 0) &&
      (!noteRequired || note.trim()),
  );
  const defaultIssueIds = new Set([
    ...(task?.issues.slice(0, 5).map((item) => item.issue_id) ?? []),
    ...(task?.issues.filter((item) => item.priority === "P0" || item.priority === "P1").map((item) => item.issue_id) ?? []),
  ]);
  const visibleIssues = task?.issues.filter((item) => showAllIssues || defaultIssueIds.has(item.issue_id)) ?? [];
  const hiddenIssueCount = (task?.issues.length ?? 0) - visibleIssues.length;
  const hasCollapsibleIssues = defaultIssueIds.size < (task?.issues.length ?? 0);

  function selectIssue(nextIssue: AttentionIssue) {
    setActiveIssue(nextIssue.issue_id);
    if (nextIssue.page_numbers[0]) setPage(nextIssue.page_numbers[0]);
  }

  function selectView(nextView: ReviewView) {
    setWorkspace("review");
    setView(nextView);
    setActiveRun("");
    updateLocation(nextView, "", "push");
  }

  function selectRun(runId: string, nextView: ReviewView = view) {
    setWorkspace("review");
    setView(nextView);
    setActiveRun(runId);
    updateLocation(nextView, runId, "push");
  }

  function enterCreatedRun(runId: string) {
    setLiveRunId(runId);
    setQuery("");
    selectRun(runId, "all");
  }

  async function openAudit() {
    if (!task) return;
    const requestedRun = task.run_id;
    const sequence = auditRequestSequence.current + 1;
    auditRequestSequence.current = sequence;
    setDrawerOpen(true);
    if (audit?.run_id === requestedRun) return;
    setAuditLoading(true);
    try {
      const payload = DEMO_MODE && !liveRunId
        ? (await import("./demo")).demoAudit
        : await getReviewAudit(requestedRun);
      if (auditRequestSequence.current !== sequence) return;
      if (payload.run_id !== requestedRun) throw new Error("完整审计响应与当前运行不匹配");
      setAudit(payload);
    } catch (cause) {
      if (auditRequestSequence.current !== sequence) return;
      setError(cause instanceof Error ? cause.message : "无法加载完整审计事实");
      setAudit(null);
    } finally {
      if (auditRequestSequence.current === sequence) setAuditLoading(false);
    }
  }

  async function submit() {
    if (!task || !canSubmit) return;
    setSubmitting(true);
    setError("");
    try {
      const payload: ReviewSubmission = {
        run_id: task.run_id,
        reviewer_id: reviewer.trim(),
        verdict,
        target_decision: verdict === "OVERRIDE_DECISION" ? targetDecision : undefined,
        note: note.trim(),
        client_request_id: requestId.current,
        issue_resolutions: Object.entries(resolutions).map(([issue_id, resolution]) => ({ issue_id, resolution })),
        track_resolutions: Object.fromEntries(task.training_tracks.map((track) => [track.track, track.status])),
      };
      if (DEMO_MODE && !liveRunId) {
        await new Promise((resolve) => window.setTimeout(resolve, 350));
      } else {
        await submitReview(payload);
      }
      requestId.current = crypto.randomUUID();
      setAudit(null);
      setNotice(verdict === "REQUEST_MORE_EVIDENCE" ? "补证请求已写入审计事件" : "审计结论已不可变追加");
      if (!DEMO_MODE || liveRunId) {
        setTask(await getReviewTask(task.run_id));
        await loadTasks();
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交审计失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className={`app-shell ${workspace === "create" ? "evaluation-mode" : ""}`}>
      <header className="topbar">
        <div className="brand"><span className="brand-mark"><Layers3 size={18} /></span><div><strong>PPT Eval</strong><small>{RESEARCH_RELEASE_LABEL}</small></div></div>
        <nav aria-label="主导航">
          <button className={workspace === "create" ? "active" : ""} onClick={() => setWorkspace("create")} aria-current={workspace === "create" ? "page" : undefined}><Plus size={15} />新建评测</button>
          {(["queue", "all", "completed"] as const).map((item) => (
            <button key={item} className={workspace === "review" && view === item ? "active" : ""} onClick={() => selectView(item)} aria-current={workspace === "review" && view === item ? "page" : undefined}>{viewLabel[item]}</button>
          ))}
        </nav>
        <div className="top-status">
          <span className={workspace === "review" && task?.audit_integrity?.chain_valid === false ? "bad" : ""}><ShieldCheck size={15} />{workspace === "create" ? "真实评测入口" : task?.audit_integrity?.chain_valid === false ? "审计链异常" : "审计链正常"}</span>
          <button className="mobile-workspace-toggle" onClick={() => setWorkspace(workspace === "create" ? "review" : "create")}><Plus size={15} />{workspace === "create" ? "返回审计" : "新建评测"}</button>
          <label className="reviewer-input"><CircleUserRound size={17} /><input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="审核员 ID" aria-label="审核员 ID" /></label>
        </div>
      </header>

      {workspace === "create" ? <NewEvaluationWorkspace onEnterAudit={enterCreatedRun} onOpenAll={() => selectView("all")} /> : <>
      <aside className="queue-panel">
        <div className="queue-heading"><div><span>{viewLabel[view]}</span><strong>{tasks.length}</strong></div><button aria-label="刷新队列" onClick={() => void loadTasks()}><RefreshCw size={16} /></button></div>
        <label className="search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 case / run" /></label>
        <div className="queue-tabs"><button className={view === "queue" ? "active" : ""} onClick={() => selectView("queue")}>待处理</button><button className={view === "completed" ? "active" : ""} onClick={() => selectView("completed")}>已结案</button><button className={view === "all" ? "active" : ""} onClick={() => selectView("all")}>全部</button></div>
        {loading && <div className="queue-message"><LoaderCircle className="spin" size={18} />加载队列</div>}
        {!loading && tasks.length === 0 && <div className="queue-message"><Filter size={18} />当前筛选没有任务</div>}
        <div className="queue-list">{tasks.map((item) => <QueueRow key={item.run_id} task={item} active={activeRun === item.run_id} onSelect={() => selectRun(item.run_id)} />)}</div>
      </aside>

      <section className="review-workspace">
        {error && <div className="global-error"><AlertCircle size={17} />{error}</div>}
        {!task ? (
          <div className="empty-workspace"><FileSearch size={30} /><strong>选择一个审计任务</strong><span>机器报告不会在此界面被修改；人工结论将追加为 ReviewEvent。</span></div>
        ) : (
          <>
            <header className="case-header">
              <div><span className={`priority priority-${task.priority}`}>{task.priority} · {priorityLabel[task.priority]}</span><h1>{task.case_id}</h1><p>{task.run_id} · {task.profile_id}@{task.profile_version}</p></div>
              <div className="case-facts"><span><small>机器结论</small><b className={`decision decision-${task.decision}`}>{task.decision}</b></span><span><small>完整性</small><b>{task.coverage}</b></span><span><small>当前得分</small><b>{task.score?.toFixed(1) ?? "—"}</b></span></div>
            </header>

            <div className="workspace-grid">
              <section className="slide-column" aria-label="幻灯片审计区">
                <div className="slide-toolbar"><span aria-live="polite">第 {page} / {task.slides.length || 0} 页</span><div><button onClick={() => setPage(Math.max(1, page - 1))} aria-label="上一页"><ChevronLeft size={17} /></button><button onClick={() => setPage(Math.min(task.slides.length, page + 1))} aria-label="下一页"><ChevronRight size={17} /></button></div></div>
                <div className="slide-stage" tabIndex={0} onKeyDown={(event) => { if (event.key === "ArrowLeft") setPage(Math.max(1, page - 1)); if (event.key === "ArrowRight") setPage(Math.min(task.slides.length, page + 1)); }}>
                  {slide?.image_url ? <img src={slide.image_url} alt={`第 ${page} 页幻灯片`} /> : <div className="slide-empty"><FileSearch size={28} /><span>渲染页不可用</span></div>}
                  {issue?.evidence.filter((item) => item.page_number === page && item.bbox).map((item, index) => { const [x, y, width, height] = item.bbox!; return <span key={index} className="bbox" style={{ left: `${x * 100}%`, top: `${y * 100}%`, width: `${width * 100}%`, height: `${height * 100}%` }} />; })}
                </div>
                <div className="filmstrip">{task.slides.map((item) => <button key={item.page_number} className={item.page_number === page ? "active" : ""} aria-current={item.page_number === page ? "page" : undefined} onClick={() => setPage(item.page_number)}>{item.thumbnail_url ? <img src={item.thumbnail_url} alt="" /> : <span className="thumb-missing">P{item.page_number}</span>}<span>{item.page_number}</span>{task.issues.some((entry) => entry.page_numbers.includes(item.page_number)) && <i />}</button>)}</div>
                <div className="evidence-strip"><strong>当前语义判断</strong>{issue ? <><div><span>来源共识</span><p>{issue.consensus.label}</p></div><div><span>人工关注</span><p>{issue.summary}</p></div></> : <p className="muted">当前没有可定位的语义问题；可进行常规页面抽查。</p>}</div>
              </section>

              <aside className="issue-panel">
                <div className="issue-heading"><div><span>语义关注</span><strong>{task.attention_summary.total_count} 个待判断事项</strong></div><AlertTriangle size={19} /></div>
                <div className="track-strip">{task.training_tracks.map((track) => <span key={track.track}><small>{trackLabel[track.track]}</small><b className={`track-${track.status}`}>{track.status}</b></span>)}</div>
                <div className="issue-list" id="semantic-issue-list">
                  {visibleIssues.map((item) => <IssueCard key={item.issue_id} issue={item} active={activeIssue === item.issue_id} resolution={resolutions[item.issue_id]} onSelect={() => selectIssue(item)} onPageSelect={(nextPage) => { setActiveIssue(item.issue_id); setPage(nextPage); }} onResolve={(value) => setResolutions((current) => ({ ...current, [item.issue_id]: value }))} />)}
                  {task.issues.length === 0 && <EmptyAttentionState task={task} />}
                  {hasCollapsibleIssues && <button className="issue-list-toggle" onClick={() => setShowAllIssues((current) => !current)} aria-expanded={showAllIssues} aria-controls="semantic-issue-list">{showAllIssues ? "收起低优先项" : `展开其余 ${hiddenIssueCount} 项`}</button>}
                </div>
                <button className="full-audit" onClick={() => void openAudit()}><FileSearch size={16} />打开完整审计事实</button>
              </aside>
            </div>

            <footer className="review-footer expanded">
              <div className="review-progress"><CheckCircle2 size={18} /><span>高优先事项尚余 {unresolvedRequired.length} 个</span></div>
              <textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="审计备注；推翻系统或请求补证时必填" aria-label="审计备注" />
              <select value={verdict} onChange={(event) => setVerdict(event.target.value as ReviewVerdict)} aria-label="审计结论"><option value="CONFIRM_SYSTEM_DECISION">确认系统结论</option><option value="REQUEST_MORE_EVIDENCE">请求补充证据</option><option value="OVERRIDE_DECISION">覆盖系统结论</option></select>
              {verdict === "OVERRIDE_DECISION" && <select value={targetDecision} onChange={(event) => setTargetDecision(event.target.value as Decision)} aria-label="覆盖后的结论"><option value="PASS">改为 PASS</option><option value="REVIEW">改为 REVIEW</option><option value="FAIL">改为 FAIL</option></select>}
              <button className="primary" disabled={!canSubmit || submitting} onClick={() => void submit()}>{submitting ? <LoaderCircle className="spin" size={16} /> : null}{verdict === "REQUEST_MORE_EVIDENCE" ? "写入补证请求" : "提交审计事件"}</button>
              {notice && <span className="submit-notice">{notice}</span>}
            </footer>
          </>
        )}
      </section>
      </>}
      {workspace === "review" && task && drawerOpen && <FullAuditDrawer task={task} audit={audit} loading={auditLoading} tab={drawerTab} onTab={setDrawerTab} onClose={() => setDrawerOpen(false)} />}
    </main>
  );
}
