/**
 * types/api.ts – TypeScript mirror of the FastAPI Pydantic schemas.
 *
 * These types are derived directly from rfp_responder/api/schemas.py.
 * Any backend schema change must be reflected here.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Enums
// ─────────────────────────────────────────────────────────────────────────────

export type WorkflowStatus =
  | "initialised"
  | "parsing"
  | "retrieving"
  | "drafting"
  | "awaiting_review"
  | "compiling"
  | "complete"
  | "failed";

// ─────────────────────────────────────────────────────────────────────────────
// POST /rfp/ingest
// ─────────────────────────────────────────────────────────────────────────────

export interface QuestionRow {
  question_text: string;
  category?: string;
  control_id?: string;
  context_hint?: string;
}

export interface IngestPayloadJson {
  format: "json";
  questions: QuestionRow[];
}

export interface IngestPayloadExcel {
  format: "excel";
  file_content: string;           // base64-encoded .xlsx bytes
  column_map?: Record<string, string>;
}

export type IngestPayload = IngestPayloadJson | IngestPayloadExcel;

export interface IngestRequest {
  questionnaire_id: string;
  payload: IngestPayload;
}

export interface IngestResponse {
  thread_id: string;
  questionnaire_id: string;
  status: WorkflowStatus;
  message: string;
  review_question_ids: string[];
  total_questions: number;
  auto_approved_count: number;
  human_reviewed_count: number;
  export_json_path?: string;
  export_excel_path?: string;
  error_message?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /rfp/threads/{thread_id}/status
// ─────────────────────────────────────────────────────────────────────────────

export interface AuditMetricsSummary {
  total_questions: number;
  auto_approved_count: number;
  human_reviewed_count: number;
  avg_vector_confidence: number;
  total_tokens: number;
  processing_duration_seconds: number;
}

export interface ThreadStatusResponse {
  thread_id: string;
  questionnaire_id: string;
  workflow_status: WorkflowStatus;
  next_node?: string;
  review_question_ids: string[];
  audit_metrics?: AuditMetricsSummary;
  error_message?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /rfp/threads/{thread_id}/review
// ─────────────────────────────────────────────────────────────────────────────

export interface ReviewItem {
  question_id: string;
  row_index: number;
  category: string;
  control_id?: string;
  question_text: string;
  proposed_answer: string;
  vector_confidence: number;   // 0.0 – 1.0
  graph_verified: boolean;
  discrepancy_detected: boolean;
  reasoning_trace: string;
}

export interface ReviewItemsResponse {
  thread_id: string;
  questionnaire_id: string;
  items: ReviewItem[];
  total_pending: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// POST /rfp/threads/{thread_id}/resume
// ─────────────────────────────────────────────────────────────────────────────

export type DecisionType = "approve" | "override" | "reject";

export interface HumanReviewDecision {
  question_id: string;
  approved: boolean;
  override_answer?: string;
  reviewer_id: string;
  review_notes?: string;
}

export interface ResumeRequest {
  decisions: HumanReviewDecision[];
  reviewer_id: string;
}

export interface ResumeResponse {
  thread_id: string;
  status: WorkflowStatus;
  message: string;
  review_question_ids: string[];
  export_json_path?: string;
  export_excel_path?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// SAML SSO Admin types  (BoxyHQ Jackson API)
// ─────────────────────────────────────────────────────────────────────────────

export interface SSOConfigRequest {
  name: string;           // Human-readable label, e.g. "Acme Corp Okta"
  tenant: string;         // Domain used to identify the tenant, e.g. "acme.com"
  product: string;        // Fixed: process.env.NEXT_PUBLIC_SAML_PRODUCT
  rawMetadata?: string;   // IdP metadata XML (paste method)
  metadataUrl?: string;   // IdP metadata URL (URL method – Jackson fetches it)
  defaultRedirectUrl: string;
  redirectUrl: string;
}

export interface SSOConfigResponse {
  clientID: string;
  clientSecret: string;
  idpMetadata: {
    sso: { postUrl?: string; redirectUrl?: string };
    entityID: string;
    thumbprint?: string;
  };
  tenant: string;
  product: string;
}
