export type Decision = "PASS" | "FAIL" | "REVIEW" | "ERROR";
export type Coverage = "FULL" | "DEGRADED" | "BASE_ONLY" | "UNASSESSABLE";

export interface Evidence {
  slide?: number;
  object_id?: string;
  bbox?: [number, number, number, number];
  message: string;
}

export interface AtomicResult {
  metric_id: string;
  criterion_id?: string;
  score?: number;
  normalized_score?: number;
  multiplier?: number;
  severity: "INFO" | "MINOR" | "MAJOR" | "CRITICAL";
  confidence: number;
  metric_status?: string;
  metric_state?: string;
  evidence?: Evidence[];
}

export interface EvalReport {
  run_id: string;
  case_id: string;
  scenario: string;
  decision: Decision;
  coverage: Coverage;
  base_score?: number;
  full_score?: number;
  results: AtomicResult[];
  degradation_reasons?: string[];
  created_at?: string;
}
