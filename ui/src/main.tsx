import React from "react";
import ReactDOM from "react-dom/client";
import { AlertTriangle, Check, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { listReports, submitReview } from "./api";
import type { EvalReport } from "./types";
import "./styles.css";

function score(value?: number) {
  return value == null ? "--" : value.toFixed(1);
}

function metricScore(result: EvalReport["results"][number]) {
  if (result.score != null) return result.score;
  if (result.normalized_score != null) return result.normalized_score * 100;
  if (result.multiplier != null) return result.multiplier * 100;
  return undefined;
}

function App() {
  const [reports, setReports] = React.useState<EvalReport[]>([]);
  const [selected, setSelected] = React.useState<EvalReport | null>(null);
  const [query, setQuery] = React.useState("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  const load = React.useCallback(async () => {
    try {
      setError("");
      const data = await listReports();
      setReports(data);
      setSelected((current) => current ?? data[0] ?? null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载失败");
    }
  }, []);

  React.useEffect(() => void load(), [load]);

  const filtered = reports.filter((item) =>
    `${item.case_id} ${item.scenario} ${item.decision}`.toLowerCase().includes(query.toLowerCase()),
  );

  async function review(verdict: "APPROVE" | "REJECT") {
    if (!selected) return;
    await submitReview(selected.run_id, verdict, note);
    setNote("");
    await load();
  }

  return (
    <main className="workspace">
      <header className="topbar">
        <div>
          <h1>PPT Eval</h1>
          <span>人工复核队列</span>
        </div>
        <button className="icon-button" title="刷新" onClick={load}><RefreshCw size={17} /></button>
      </header>
      <section className="queue-panel">
        <label className="search"><Search size={16} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索任务" /></label>
        {error && <div className="error"><AlertTriangle size={16} />{error}</div>}
        <div className="queue-list">
          {filtered.map((item) => (
            <button key={item.run_id} className={selected?.run_id === item.run_id ? "queue-row active" : "queue-row"} onClick={() => setSelected(item)}>
              <span className={`decision ${item.decision.toLowerCase()}`}>{item.decision}</span>
              <span className="row-copy"><strong>{item.case_id}</strong><small>{item.scenario} · {item.coverage}</small></span>
              <span className="row-score">{score(item.full_score ?? item.base_score)}</span>
              <ChevronRight size={16} />
            </button>
          ))}
        </div>
      </section>
      <section className="detail-panel">
        {!selected ? <div className="empty">暂无待复核任务</div> : <>
          <div className="detail-head">
            <div><span className="eyebrow">{selected.run_id}</span><h2>{selected.case_id}</h2></div>
            <div className="score-block"><span>本体分</span><strong>{score(selected.base_score)}</strong></div>
            <div className="score-block"><span>完整分</span><strong>{score(selected.full_score)}</strong></div>
          </div>
          {selected.degradation_reasons?.length ? <div className="degraded"><AlertTriangle size={17} /><div><strong>{selected.coverage}</strong>{selected.degradation_reasons.join("；")}</div></div> : null}
          <div className="results-table">
            <div className="table-header"><span>指标</span><span>状态</span><span>严重度</span><span>分数</span><span>置信度</span></div>
            {selected.results.map((result) => {
              const criterion = result.metric_id ?? result.criterion_id ?? "unknown";
              return <div className="result" key={criterion}>
                <span><strong>{criterion}</strong>{result.evidence?.[0]?.message && <small>{result.evidence[0].message}</small>}</span>
                <span>{result.metric_status ?? result.metric_state ?? "--"}</span><span>{result.severity}</span><span>{score(metricScore(result))}</span><span>{Math.round(result.confidence * 100)}%</span>
              </div>;
            })}
          </div>
          <div className="review-bar">
            <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="复核意见" />
            <button className="reject" onClick={() => review("REJECT")}><X size={17} />驳回</button>
            <button className="approve" onClick={() => review("APPROVE")}><Check size={17} />通过</button>
          </div>
        </>}
      </section>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
