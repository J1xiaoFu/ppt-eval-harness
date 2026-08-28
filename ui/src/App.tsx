import React from "react";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
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
  RefreshCw,
  Route,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { getReviewAudit, getReviewTask, listReviewTasks, submitReview } from "./api";
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

const DEMO_MODE = import.meta.env.DEV && import.meta.env.VITE_DEMO_MODE === "true";
const priorityLabel = { P0: "立即处理", P1: "优先复核", P2: "质量抽查", P3: "常规抽查" };
const viewLabel = { queue: "审计队列", all: "全部运行", completed: "已完成" };

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
  onResolve,
}: {
  issue: AttentionIssue;
  active: boolean;
  resolution?: IssueResolution;
  onSelect: () => void;
  onResolve: (value: IssueResolution) => void;
}) {
  return (
    <article
      className={`issue-card ${active ? "active" : ""}`}
      onClick={onSelect}
      aria-current={active ? "true" : undefined}
    >
      <div className="issue-topline">
        <span className={`severity severity-${issue.severity}`}>{issue.severity}</span>
        <code>{issue.kind}</code>
      </div>
      <h3>{issue.title}</h3>
      <p>{issue.summary}</p>
      {issue.page_numbers.length > 0 && (
        <div className="page-pills">
          {issue.page_numbers.slice(0, 8).map((page) => (
            <span key={page}>P{page}</span>
          ))}
          {issue.page_numbers.length > 8 && <span>+{issue.page_numbers.length - 8}</span>}
        </div>
      )}
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
  tab: "score" | "routes" | "manifest" | "history";
  onTab: (tab: "score" | "routes" | "manifest" | "history") => void;
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
        </footer>
      </aside>
    </div>
  );
}

export default function App() {
  const [view, setView] = React.useState<"queue" | "all" | "completed">("queue");
  const [query, setQuery] = React.useState("");
  const [tasks, setTasks] = React.useState<ReviewTaskSummary[]>([]);
  const [activeRun, setActiveRun] = React.useState("");
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
  const [drawerTab, setDrawerTab] = React.useState<"score" | "routes" | "manifest" | "history">("score");
  const [audit, setAudit] = React.useState<FullAuditPayload | null>(null);
  const [auditLoading, setAuditLoading] = React.useState(false);
  const requestId = React.useRef(crypto.randomUUID());

  const loadTasks = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      if (DEMO_MODE) {
        const { demoTasks } = await import("./demo");
        const filtered = demoTasks.filter((item) =>
          `${item.case_id} ${item.run_id} ${item.scenario}`.toLowerCase().includes(query.toLowerCase()),
        );
        setTasks(view === "completed" ? [] : filtered);
        if (!activeRun && filtered[0]) setActiveRun(filtered[0].run_id);
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
  }, [activeRun, query, view]);

  React.useEffect(() => {
    const timer = window.setTimeout(() => void loadTasks(), 180);
    return () => window.clearTimeout(timer);
  }, [loadTasks]);

  React.useEffect(() => {
    if (!activeRun) {
      setTask(null);
      return;
    }
    let cancelled = false;
    setError("");
    const load = async () => {
      try {
        const detail = DEMO_MODE
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
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法加载审计详情");
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [activeRun]);

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

  function selectIssue(nextIssue: AttentionIssue) {
    setActiveIssue(nextIssue.issue_id);
    if (nextIssue.page_numbers[0]) setPage(nextIssue.page_numbers[0]);
  }

  async function openAudit() {
    if (!task) return;
    setDrawerOpen(true);
    if (audit?.run_id === task.run_id) return;
    setAuditLoading(true);
    try {
      const payload = DEMO_MODE
        ? (await import("./demo")).demoAudit
        : await getReviewAudit(task.run_id);
      setAudit({ ...payload, run_id: task.run_id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法加载完整审计事实");
      setAudit(null);
    } finally {
      setAuditLoading(false);
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
      if (DEMO_MODE) {
        await new Promise((resolve) => window.setTimeout(resolve, 350));
      } else {
        await submitReview(payload);
      }
      requestId.current = crypto.randomUUID();
      setNotice(verdict === "REQUEST_MORE_EVIDENCE" ? "补证请求已写入审计事件" : "审计结论已不可变追加");
      if (!DEMO_MODE) {
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
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark"><Layers3 size={18} /></span><div><strong>PPT Eval</strong><small>审计控制台</small></div></div>
        <nav aria-label="主导航">
          {(["queue", "all", "completed"] as const).map((item) => (
            <button key={item} className={view === item ? "active" : ""} onClick={() => { setView(item); setActiveRun(""); }}>{viewLabel[item]}</button>
          ))}
        </nav>
        <div className="top-status">
          <span className={task?.audit_integrity?.chain_valid === false ? "bad" : ""}><ShieldCheck size={15} />{task?.audit_integrity?.chain_valid === false ? "审计链异常" : "审计链正常"}</span>
          <label className="reviewer-input"><CircleUserRound size={17} /><input value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="审核员 ID" aria-label="审核员 ID" /></label>
        </div>
      </header>

      <aside className="queue-panel">
        <div className="queue-heading"><div><span>{viewLabel[view]}</span><strong>{tasks.length}</strong></div><button aria-label="刷新队列" onClick={() => void loadTasks()}><RefreshCw size={16} /></button></div>
        <label className="search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 case / run" /></label>
        <div className="queue-tabs"><button className={view === "queue" ? "active" : ""} onClick={() => setView("queue")}>待处理</button><button className={view === "completed" ? "active" : ""} onClick={() => setView("completed")}>已结案</button><button className={view === "all" ? "active" : ""} onClick={() => setView("all")}>全部</button></div>
        {loading && <div className="queue-message"><LoaderCircle className="spin" size={18} />加载队列</div>}
        {!loading && tasks.length === 0 && <div className="queue-message"><Filter size={18} />当前筛选没有任务</div>}
        <div className="queue-list">{tasks.map((item) => <QueueRow key={item.run_id} task={item} active={activeRun === item.run_id} onSelect={() => setActiveRun(item.run_id)} />)}</div>
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
                <div className="evidence-strip"><strong>当前疑点证据</strong>{issue?.evidence.length ? issue.evidence.map((entry, index) => <div key={index}><span>{entry.source}</span><p>{entry.message}</p></div>) : <p className="muted">该疑点为 deck 级结论，没有单独的对象证据。</p>}</div>
              </section>

              <aside className="issue-panel">
                <div className="issue-heading"><div><span>Human attention</span><strong>{task.issues.length} 个待判断事项</strong></div><AlertTriangle size={19} /></div>
                <div className="track-strip">{task.training_tracks.map((track) => <span key={track.track}><small>{track.track}</small><b className={`track-${track.status}`}>{track.status}</b></span>)}</div>
                <div className="issue-list">{task.issues.map((item) => <IssueCard key={item.issue_id} issue={item} active={activeIssue === item.issue_id} resolution={resolutions[item.issue_id]} onSelect={() => selectIssue(item)} onResolve={(value) => setResolutions((current) => ({ ...current, [item.issue_id]: value }))} />)}{task.issues.length === 0 && <div className="no-issues"><CheckCircle2 size={22} /><strong>没有系统优先疑点</strong><span>仍可从完整审计事实进行常规抽查。</span></div>}</div>
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
      {task && drawerOpen && <FullAuditDrawer task={task} audit={audit} loading={auditLoading} tab={drawerTab} onTab={setDrawerTab} onClose={() => setDrawerOpen(false)} />}
    </main>
  );
}
