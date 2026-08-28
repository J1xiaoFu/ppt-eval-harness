export type Decision = "PASS" | "FAIL" | "REVIEW" | "ERROR";
export type Coverage = "FULL" | "DEGRADED" | "BASE_ONLY" | "UNASSESSABLE";
export type AttentionPriority = "P0" | "P1" | "P2" | "P3";
export type ReviewState = "OPEN" | "NEEDS_EVIDENCE" | "RESOLVED";
export type Severity = "INFO" | "MINOR" | "MAJOR" | "CRITICAL";
export type IssueResolution = "CONFIRMED" | "FALSE_POSITIVE" | "INSUFFICIENT_EVIDENCE";
export type ReviewVerdict =
  | "CONFIRM_SYSTEM_DECISION"
  | "OVERRIDE_DECISION"
  | "REQUEST_MORE_EVIDENCE";
export type EvaluationScene =
  | "ready_made"
  | "text_to_ppt"
  | "project_summary"
  | "multimodal";
export type EvaluationJobStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";

export interface EvaluationJob {
  job_id: string;
  status: EvaluationJobStatus;
  stage?: string;
  run_id?: string;
  review_url?: string;
  review_task_url?: string;
  evaluation_url?: string;
  error?: string;
  error_code?: string;
  created_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface EvidenceRef {
  evidence_id?: string;
  source: "RULE" | "MODEL" | "REDUCER" | "SYSTEM";
  oracle_id?: string;
  metric_id?: string;
  page_number?: number;
  object_id?: string;
  bbox?: [number, number, number, number];
  kind?: string;
  message: string;
  confidence?: number;
  severity?: Severity;
}

export interface AttentionIssue {
  issue_id: string;
  priority: AttentionPriority;
  kind: string;
  title: string;
  summary: string;
  severity: Severity;
  status: "OPEN" | "RESOLVED";
  metric_id?: string;
  page_numbers: number[];
  evidence: EvidenceRef[];
  lineage?: Record<string, unknown>;
}

export interface SlideRef {
  page_number: number;
  image_url?: string;
  thumbnail_url?: string;
  available: boolean;
}

export interface TrainingTrack {
  track: "visual" | "layout" | "content" | "full_deck";
  status: "TRAIN" | "REVIEW" | "REJECT";
  score?: number;
  reason_codes: string[];
}

export interface ReviewTaskSummary {
  run_id: string;
  case_id: string;
  scenario: string;
  decision: Decision;
  coverage: Coverage;
  score?: number;
  priority: AttentionPriority;
  priority_reason: string;
  issue_count: number;
  page_count: number;
  review_state: ReviewState;
  created_at?: string;
  profile_id?: string;
  profile_version?: string;
}

export interface ReviewEvent {
  review_id: string;
  run_id: string;
  reviewer_id: string;
  verdict: ReviewVerdict | "APPROVE" | "REJECT";
  note?: string;
  created_at: string;
  issue_resolutions?: Array<{
    issue_id: string;
    resolution: IssueResolution;
    note?: string;
  }>;
}

export interface ReviewTaskDetail extends ReviewTaskSummary {
  triage_policy_version: string;
  report_hash?: string;
  observation_hash?: string;
  review_reasons: string[];
  issues: AttentionIssue[];
  slides: SlideRef[];
  inputs?: Array<{
    role: string;
    index: number;
    original_name: string;
    media_type: string;
    size_bytes?: number | null;
    sha256?: string | null;
    available: boolean;
    download_url?: string | null;
  }>;
  training_tracks: TrainingTrack[];
  audit_url: string;
  artifacts: {
    report_url: string;
    observations_url?: string;
    source_pptx_url?: string;
    render_manifest_url?: string;
  };
  reviews: ReviewEvent[];
  audit_integrity?: { chain_valid: boolean };
}

export interface FullAuditPayload {
  run_id: string;
  results: Array<Record<string, unknown>>;
  gate_results: Array<Record<string, unknown>>;
  model_routes: Array<Record<string, unknown>>;
  manifest?: Record<string, unknown>;
  reviews: ReviewEvent[];
  audit_integrity?: { chain_valid: boolean; observation_artifact_valid?: boolean };
}

export interface ReviewQueueResponse {
  items: ReviewTaskSummary[];
  total: number;
  limit: number;
  offset: number;
  triage_policy_version: string;
}

export interface ReviewSubmission {
  run_id: string;
  reviewer_id: string;
  verdict: ReviewVerdict;
  target_decision?: Decision;
  note: string;
  client_request_id: string;
  issue_resolutions: Array<{
    issue_id: string;
    resolution: IssueResolution;
    note?: string;
  }>;
  track_resolutions: Record<string, "TRAIN" | "REVIEW" | "REJECT">;
}
