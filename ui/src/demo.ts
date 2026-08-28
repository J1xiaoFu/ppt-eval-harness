import type { FullAuditPayload, ReviewTaskDetail, ReviewTaskSummary } from "./types";

const slideBase =
  "http://127.0.0.1:8766/topics/chinese_new_year/renders/skyworks_banana_rank_1";

export const demoTasks: ReviewTaskSummary[] = [
  {
    run_id: "run-7f31-cny-delivery",
    case_id: "delivery-candidate-1842",
    scenario: "ready_made",
    decision: "REVIEW",
    coverage: "DEGRADED",
    score: 70.6,
    priority: "P0",
    priority_reason: "功能硬门证据未收敛",
    issue_count: 4,
    page_count: 13,
    review_state: "OPEN",
    created_at: "2026-08-28T09:42:00+08:00",
    profile_id: "finished-deck-v8",
    profile_version: "8.3",
  },
  {
    run_id: "run-81aa-project-summary",
    case_id: "project-summary-1839",
    scenario: "project_summary",
    decision: "FAIL",
    coverage: "FULL",
    score: 35.4,
    priority: "P0",
    priority_reason: "几何硬门已确认",
    issue_count: 3,
    page_count: 10,
    review_state: "OPEN",
    created_at: "2026-08-28T09:16:00+08:00",
    profile_id: "project-summary-v8",
    profile_version: "8.3",
  },
  {
    run_id: "run-a027-training-sample",
    case_id: "training-sample-1817",
    scenario: "multimodal",
    decision: "PASS",
    coverage: "FULL",
    score: 84.9,
    priority: "P2",
    priority_reason: "主动质量抽查",
    issue_count: 1,
    page_count: 15,
    review_state: "OPEN",
    created_at: "2026-08-28T08:54:00+08:00",
    profile_id: "multimodal-generation-v8",
    profile_version: "8.3",
  },
];

export const demoTask: ReviewTaskDetail = {
  ...demoTasks[0],
  triage_policy_version: "1.0.0",
  report_hash: "292b35887e09…",
  observation_hash: "5bbaabaab4a2…",
  review_reasons: ["coverage:DEGRADED", "unresolved_metric:v8_functional_integrity"],
  slides: Array.from({ length: 13 }, (_, index) => ({
    page_number: index + 1,
    image_url: `${slideBase}/slide_${String(index + 1).padStart(4, "0")}.png`,
    thumbnail_url: `${slideBase}/slide_${String(index + 1).padStart(4, "0")}.png`,
    available: true,
  })),
  issues: [
    {
      issue_id: "issue-geometry-gate",
      priority: "P0",
      kind: "GATE_UNRESOLVED",
      title: "几何硬门尚未形成完整结论",
      summary: "规则在 13 个页面提出越界候选；当前视觉复核没有覆盖全部候选页。",
      severity: "CRITICAL",
      status: "OPEN",
      metric_id: "slide_geometry_integrity",
      page_numbers: [2, 3, 4, 5, 6, 7, 8, 9, 12, 13],
      evidence: [
        {
          source: "RULE",
          metric_id: "slide_geometry_integrity",
          page_number: 2,
          kind: "out_of_bounds",
          message: "页眉装饰对象超出画布右侧边界。",
          severity: "CRITICAL",
          confidence: 1,
          bbox: [0.03, 0.02, 1.12, 0.06],
        },
        {
          source: "MODEL",
          metric_id: "structured_vlm_composition_layout",
          page_number: 6,
          kind: "content_overflow_or_cutoff",
          message: "视觉模型仅在已采样页确认轻微裁切，其余候选页仍需人工核对。",
          severity: "MINOR",
          confidence: 0.85,
        },
      ],
      lineage: { rule_severity: "CRITICAL", model_severity: "MINOR", verdict: "UNRESOLVED" },
    },
    {
      issue_id: "issue-provider-disagreement",
      priority: "P1",
      kind: "RULE_MODEL_DISAGREEMENT",
      title: "规则与视觉模型对版式风险判断不一致",
      summary: "规则认为存在系统性画布越界，模型只在少数页面观察到轻微裁切。",
      severity: "MAJOR",
      status: "OPEN",
      metric_id: "composition_craft",
      page_numbers: [6, 12],
      evidence: [],
    },
    {
      issue_id: "issue-training-tracks",
      priority: "P1",
      kind: "TRAINING_ELIGIBILITY",
      title: "四条训练轨均处于 REVIEW",
      summary: "在硬门结论收敛前，该样本不能自动进入任何训练轨。",
      severity: "MAJOR",
      status: "OPEN",
      page_numbers: [],
      evidence: [],
    },
  ],
  training_tracks: [
    { track: "visual", status: "REVIEW", score: 78.4, reason_codes: ["gate:unresolved"] },
    { track: "layout", status: "REVIEW", score: 70.2, reason_codes: ["gate:unresolved"] },
    { track: "content", status: "REVIEW", score: 82.1, reason_codes: ["content_evidence:missing"] },
    { track: "full_deck", status: "REVIEW", score: 70.6, reason_codes: ["coverage:degraded"] },
  ],
  audit_url: "#",
  artifacts: { report_url: "#", observations_url: "#", source_pptx_url: "#" },
  reviews: [],
};

export const demoAudit: FullAuditPayload = {
  run_id: demoTask.run_id,
  results: [],
  gate_results: [],
  model_routes: [],
  manifest: {},
  reviews: [],
  audit_integrity: { chain_valid: true, observation_artifact_valid: true },
};

export function demoDetailForRun(runId: string): ReviewTaskDetail {
  const summary = demoTasks.find((item) => item.run_id === runId) ?? demoTasks[0];
  return {
    ...demoTask,
    ...summary,
    run_id: summary.run_id,
    case_id: summary.case_id,
  };
}
