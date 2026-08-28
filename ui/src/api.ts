import type {
  EvaluationJob,
  FullAuditPayload,
  ReviewEvent,
  ReviewQueueResponse,
  ReviewSubmission,
  ReviewTaskDetail,
} from "./types";

const API = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const value = detail as { code?: unknown; message?: unknown };
    if (typeof value.message === "string") return value.message;
    if (typeof value.code === "string") return value.code;
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(typeof init?.body === "string" ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    const detail = payload?.detail;
    throw new ApiError(response.status, errorMessage(detail, `HTTP ${response.status}`), detail);
  }
  return response.json() as Promise<T>;
}

function xhrErrorMessage(xhr: XMLHttpRequest): string {
  const payload = xhr.response as {
    detail?: string | { code?: unknown; message?: unknown };
  } | null;
  return errorMessage(payload?.detail, xhr.status > 0 ? `HTTP ${xhr.status}` : "上传连接失败");
}

export function uploadEvaluation(
  formData: FormData,
  clientRequestId: string,
  onProgress: (percent: number | null) => void,
  signal?: AbortSignal,
): Promise<EvaluationJob> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/v1/evaluations/upload?async=true`);
    xhr.responseType = "json";
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Idempotency-Key", clientRequestId);

    xhr.upload.onprogress = (event) => {
      onProgress(event.lengthComputable && event.total > 0
        ? Math.min(100, Math.round((event.loaded / event.total) * 100))
        : null);
    };
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(xhr.status, xhrErrorMessage(xhr), xhr.response));
        return;
      }
      const payload = xhr.response as EvaluationJob | null;
      if (
        !payload
        || typeof payload.job_id !== "string"
        || !["PENDING", "RUNNING", "COMPLETED", "FAILED"].includes(payload.status)
      ) {
        reject(new Error("服务端返回了无效的 Job 响应"));
        return;
      }
      onProgress(100);
      resolve(payload);
    };
    xhr.onerror = () => reject(new ApiError(0, "上传连接失败"));
    xhr.onabort = () => reject(new DOMException("上传已取消", "AbortError"));

    const abort = () => xhr.abort();
    if (signal?.aborted) {
      reject(new DOMException("上传已取消", "AbortError"));
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    xhr.onloadend = () => signal?.removeEventListener("abort", abort);
    xhr.send(formData);
  });
}

export async function getEvaluationJob(jobId: string): Promise<EvaluationJob> {
  return request(`/v1/jobs/${encodeURIComponent(jobId)}`);
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
