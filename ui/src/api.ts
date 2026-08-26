import type { EvalReport } from "./types";

const API = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export async function listReports(): Promise<EvalReport[]> {
  const response = await fetch(`${API}/v1/evaluations`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

export async function submitReview(runId: string, verdict: "APPROVE" | "REJECT", note: string) {
  const response = await fetch(`${API}/v1/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId, verdict, note, reviewer_id: "local-reviewer" }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

