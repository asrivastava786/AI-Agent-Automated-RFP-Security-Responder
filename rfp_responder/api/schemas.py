"""
api/schemas.py – Pydantic v2 request and response models for the FastAPI layer.

These are intentionally separate from the internal state models in state.py.

Why separate schemas?
─────────────────────
• API contracts evolve independently of internal workflow state.  A v2 API
  can accept a richer payload without changing LangGraph state shape.
• External schemas use snake_case for Python but can alias to camelCase for
  JavaScript clients via `model_config = ConfigDict(populate_by_name=True)`.
• Response models strip internal fields (execution_id, retry_count, etc.)
  that are implementation details clients should never see.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rfp_responder.state import HumanReviewDecision, WorkflowStatus


# ─────────────────────────────────────────────────────────────────────────────
# Shared base
# ─────────────────────────────────────────────────────────────────────────────

class _APIBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/ingest  – start a new workflow
# ─────────────────────────────────────────────────────────────────────────────

class IngestRequest(_APIBase):
    """
    Body of the questionnaire ingestion request.

    `questionnaire_id` is the caller's stable identifier for the source
    document (e.g., a Jira ticket ID or an S3 object key).  It is stored in
    state and used to name the export files.

    `payload` is passed verbatim into `state["raw_payload"]`; the
    parse_questionnaire node handles format-specific deserialization.

    Example – JSON format
    ─────────────────────
    {
      "questionnaire_id": "rfp-acme-2024-q4",
      "payload": {
        "format": "json",
        "questions": [
          {"question_text": "Do you support SAML SSO?", "category": "Authentication"},
          {"question_text": "Is data encrypted at rest?", "category": "Encryption",
           "control_id": "SOC2-CC6.7"}
        ]
      }
    }

    Example – Excel format
    ──────────────────────
    {
      "questionnaire_id": "rfp-acme-2024-q4",
      "payload": {
        "format": "excel",
        "file_content": "<base64-encoded .xlsx>",
        "column_map": {"question": "Security Question", "category": "Domain"}
      }
    }
    """

    questionnaire_id: str = Field(
        min_length=1,
        max_length=256,
        description="Caller-assigned stable identifier for the source questionnaire document.",
    )
    payload: dict[str, Any] = Field(
        description="Format-specific ingestion payload. Must include a 'format' key."
    )


class IngestResponse(_APIBase):
    """
    Returned by POST /rfp/ingest.

    HTTP 200  →  workflow ran to completion (all questions auto-approved).
    HTTP 202  →  workflow paused awaiting human review (interrupt hit).
    HTTP 500  →  workflow failed; `error_message` carries the reason.
    """

    thread_id: str
    questionnaire_id: str
    status: str          # WorkflowStatus value
    message: str

    # Populated on 202 AWAITING_REVIEW
    review_question_ids: list[str] = Field(default_factory=list)

    # Populated on 200 COMPLETE
    total_questions: int        = 0
    auto_approved_count: int    = 0
    human_reviewed_count: int   = 0
    export_json_path: str | None = None
    export_excel_path: str | None = None

    # Populated on 500 FAILED
    error_message: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/status
# ─────────────────────────────────────────────────────────────────────────────

class AuditMetricsSummary(_APIBase):
    """Lightweight subset of AuditMetrics surfaced in the status response."""
    total_questions: int       = 0
    auto_approved_count: int   = 0
    human_reviewed_count: int  = 0
    avg_vector_confidence: float = 0.0
    total_tokens: int          = 0
    processing_duration_seconds: float = 0.0


class ThreadStatusResponse(_APIBase):
    """
    Current lifecycle state of a workflow thread.

    `next_node` mirrors LangGraph's `state.next` tuple – the node that
    will execute when the thread is resumed.  Empty when the graph has
    completed or failed.
    """

    thread_id: str
    questionnaire_id: str
    workflow_status: str
    next_node: str | None = Field(
        default=None,
        description="Name of the node awaiting execution (set when graph is interrupted).",
    )
    review_question_ids: list[str] = Field(default_factory=list)
    audit_metrics: AuditMetricsSummary | None = None
    error_message: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/review
# ─────────────────────────────────────────────────────────────────────────────

class ReviewItem(_APIBase):
    """
    One question flagged for human review.

    Exposes only the fields a reviewer needs to make a decision.
    Internal fields (execution_id, model_used, token counts) are omitted.
    """

    question_id: str
    row_index: int
    category: str
    control_id: str | None
    question_text: str
    proposed_answer: str
    vector_confidence: float
    graph_verified: bool
    discrepancy_detected: bool
    reasoning_trace: str


class ReviewItemsResponse(_APIBase):
    """All items currently awaiting human review for a given thread."""

    thread_id: str
    questionnaire_id: str
    items: list[ReviewItem]
    total_pending: int


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/threads/{thread_id}/resume
# ─────────────────────────────────────────────────────────────────────────────

class ResumeRequest(_APIBase):
    """
    Body of the human-review resume request.

    `decisions` must contain one entry per item in the pending review list.
    Omitted items are treated as approved-as-is (fail-open; logged).
    """

    decisions: list[HumanReviewDecision] = Field(min_length=1)
    reviewer_id: str = Field(
        description="Identifier of the human reviewer (email, LDAP DN, etc.)."
    )


class ResumeResponse(_APIBase):
    """
    Returned by POST /rfp/threads/{thread_id}/resume.

    HTTP 200 → graph completed after resume.
    HTTP 202 → graph interrupted again (rare; only if a second review wave exists).
    HTTP 409 → thread not in AWAITING_REVIEW state; resume was rejected.
    """

    thread_id: str
    status: str
    message: str
    review_question_ids: list[str] = Field(default_factory=list)
    export_json_path: str | None   = None
    export_excel_path: str | None  = None


# ─────────────────────────────────────────────────────────────────────────────
# Error envelope
# ─────────────────────────────────────────────────────────────────────────────

class ErrorResponse(_APIBase):
    """Standard error body returned for 4xx/5xx responses."""
    detail: str
    error_code: str | None = None
