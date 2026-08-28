import type {
  FullAuditPayload,
  ReviewEvent,
  ReviewQueueResponse,
  ReviewSubmission,
  ReviewTaskDetail,
} from "./types";

const API = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const message = detail && typeof detail.detail === "string" ? detail.detail : `HTTP ${response.status}`;
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export async function listReviewTasks(params: URLSearchParams): Promise<ReviewQueueResponse> {
  const query = params.toString();
  return request(`/v1/review/tasks${query ? `?${query}` : ""}`);
}

export async function getReviewTask(runId: string): Promise<ReviewTaskDetail> {
  return request(`/v1/review/tasks/${encodeURIComponent(runId)}`);
}

export async function getReviewAudit(runId: string): Promise<FullAuditPayload> {
  return request(`/v1/review/tasks/${encodeURIComponent(runId)}/audit`);
}

export async function submitReview(payload: ReviewSubmission): Promise<ReviewEvent> {
  return request("/v1/reviews", {
    method: "POST",
    body: JSON.stringify(payload),
    headers: { "Idempotency-Key": payload.client_request_id },
  });
}
