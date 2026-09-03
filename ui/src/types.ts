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
export type ConsensusStatus = "AGREED" | "CONFLICT" | "SINGLE_SOURCE" | "INSUFFICIENT";
export type AttentionSummaryState =
  | "ACTIONABLE"
  | "REVIEW_WITHOUT_LOCALIZED_ISSUE"
  | "NO_ISSUE"
  | "UNLOCATED_FAILURE";

export interface AttentionSummary {
  state: AttentionSummaryState;
  title: string;
  description: string;
  total_count: number;
  required_count: number;
  raw_fact_count: number;
}

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
  source: "RULE" | "MODEL" | "REDUCER" | "SYSTEM";
  page_number: number | null;
  bbox?: [number, number, number, number];
}

export interface AttentionIssue {
  issue_id: string;
  priority: AttentionPriority;
  title: string;
  summary: string;
  severity: Severity;
  status: "OPEN" | "RESOLVED";
  semantic_code: string;
  consensus: {
    status: ConsensusStatus;
    sources: Array<"RULE" | "MODEL" | "REDUCER" | "SYSTEM">;
    label: string;
    supporting_count: number;
    conflicting_count: number;
  };
  rationales: string[];
  detail_count: number;
  page_numbers: number[];
  evidence: EvidenceRef[];
}

export interface SlideRef {
  page_number: number;
  image_url: string | null;
  thumbnail_url: string | null;
  available: boolean;
}

export interface TrainingTrack {
  track: "visual" | "layout" | "content" | "full_deck";
  status: "TRAIN" | "REVIEW" | "REJECT";
  score: number | null;
  reason_codes: string[];
}

export interface ReviewTaskSummary {
  run_id: string;
  case_id: string;
  scenario: string;
  decision: Decision;
  coverage: Coverage;
  score: number | null;
  priority: AttentionPriority;
  priority_reason: string;
  issue_count: number;
  page_count: number;
  review_state: ReviewState;
  created_at: string | null;
  profile_id: string | null;
  profile_version: string | null;
  training_tracks: TrainingTrack[];
  latest_review: ReviewEvent | null;
  triage_policy_version: string;
  attention_summary: AttentionSummary;
}

export interface ReviewEvent {
  review_id: string;
  run_id: string;
  reviewer_id: string;
  verdict: ReviewVerdict | "APPROVE" | "REJECT";
  note?: string | null;
  created_at: string;
  client_request_id?: string | null;
  target_decision?: Decision | null;
  machine_decision?: Decision | null;
  machine_coverage?: Coverage | null;
  report_hash?: string | null;
  observation_hash?: string | null;
  triage_policy_version?: string | null;
  track_resolutions?: Record<string, "TRAIN" | "REVIEW" | "REJECT">;
  issue_resolutions?: Array<{
    issue_id: string;
    resolution: IssueResolution;
    note?: string;
  }>;
}

export interface ReviewTaskDetail extends ReviewTaskSummary {
  service_version: string;
  report_hash: string | null;
  observation_hash: string | null;
  review_reasons: string[];
  issues: AttentionIssue[];
  slides: SlideRef[];
  inputs: Array<{
    role: string;
    index: number;
    original_name: string;
    media_type: string;
    size_bytes?: number | null;
    sha256?: string | null;
    available: boolean;
    download_url?: string | null;
  }>;
  audit_url: string;
  artifacts: {
    report: ArtifactAvailability;
    atomic_observations: ArtifactAvailability;
    source_pptx: ArtifactAvailability;
    slide_render_manifest: ArtifactAvailability;
    report_url: string;
    observations_url: string | null;
    source_pptx_url: string | null;
    render_manifest_url: string | null;
    visual_contract_urls?: Record<string, string | null>;
  };
  reviews: ReviewEvent[];
  audit_integrity: {
    chain_valid: boolean;
    observation_artifact_valid?: boolean | null;
  };
}

export interface ArtifactAvailability {
  available: boolean;
  sha256?: string | null;
}

export interface FullAuditPayload {
  run_id: string;
  service_version: string;
  attention_summary: AttentionSummary;
  results: Array<Record<string, unknown>>;
  gate_results: Array<Record<string, unknown>>;
  model_routes: Array<Record<string, unknown>>;
  attention_details: Array<Record<string, unknown>>;
  visual_audit_summary?: Record<string, unknown>;
  visual_contract_artifacts?: Record<
    string,
    ArtifactAvailability & { url?: string | null }
  >;
  observation_artifact: {
    available: boolean;
    url: string | null;
    count: number;
    sha256: string | null;
    valid: boolean | null;
  };
  manifest: Record<string, unknown>;
  reviews: ReviewEvent[];
  audit_integrity: {
    chain_valid: boolean;
    observation_artifact_valid?: boolean | null;
  };
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
