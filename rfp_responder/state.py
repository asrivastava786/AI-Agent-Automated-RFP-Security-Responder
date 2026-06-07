"""
state.py – Canonical data contracts for the RFP Responder LangGraph workflow.

Design principles
─────────────────
1. LangGraph requires the top-level state to be a TypedDict.
   All nested / structured data uses Pydantic v2 models so that field-level
   validation, JSON serialisation, and IDE autocompletion work cleanly.

2. Dict fields that multiple nodes write to (retrieval_bundles, drafted_answers,
   etc.) use a merge reducer (lambda a, b: {**a, **b}) so each node can return
   *only the keys it touched* – avoiding full-state rewrites and race conditions
   when fan-out nodes run concurrently.

3. Idempotency keys are UUID5-derived and embedded at construction time so
   downstream writes are safe to replay after a transient failure.

4. Every model that leaves the process boundary (stored in DB, returned by API,
   logged to LangSmith) inherits from a common base with `model_config` set to
   `frozen=False, populate_by_name=True` for forward-compat serialisation.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from operator import or_
from typing import Annotated, Any

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


# ─────────────────────────────────────────────────────────────────────────────
# Shared Pydantic base
# ─────────────────────────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,   # serialise enums as plain strings
        extra="ignore",         # tolerate unknown fields from external payloads
    )


# ─────────────────────────────────────────────────────────────────────────────
# Domain enumerations
# ─────────────────────────────────────────────────────────────────────────────

class QuestionStatus(StrEnum):
    """Lifecycle of a single questionnaire row through the workflow."""
    PENDING         = "pending"
    RETRIEVED       = "retrieved"       # dual_stream_retrieval completed
    DRAFTED         = "drafted"         # draft_response completed
    AUTO_APPROVED   = "auto_approved"   # confidence >= threshold, graph verified
    REVIEW_REQUIRED = "review_required" # below threshold or discrepancy detected
    HUMAN_APPROVED  = "human_approved"  # reviewer accepted the draft
    HUMAN_OVERRIDDEN = "human_overridden" # reviewer supplied replacement text
    EXPORTED        = "exported"        # included in final document


class WorkflowStatus(StrEnum):
    """Coarse lifecycle of the entire RFP thread."""
    INITIALISED    = "initialised"
    PARSING        = "parsing"
    RETRIEVING     = "retrieving"
    DRAFTING       = "drafting"
    AWAITING_REVIEW = "awaiting_review"  # graph interrupted; waiting for /resume
    COMPILING      = "compiling"
    COMPLETE       = "complete"
    FAILED         = "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion models
# ─────────────────────────────────────────────────────────────────────────────

class QuestionItem(_Base):
    """
    A single, normalised row extracted from the incoming questionnaire.

    Idempotency guarantee
    ─────────────────────
    Both `question_id` and `execution_id` are UUID5 values derived
    deterministically from (thread_id, row_index).  Re-ingesting the same
    file for the same thread produces identical IDs, making every downstream
    write idempotent.
    """

    question_id: str = Field(
        description="UUID5(NAMESPACE_OID, '{thread_id}:{row_index}')"
    )
    execution_id: str = Field(
        description="Idempotency key for DB writes: UUID5(NAMESPACE_OID, '{thread_id}:{question_id}:exec')"
    )
    row_index: int = Field(ge=0)
    category: str = Field(
        default="General",
        description="Section or control category label from the source document.",
    )
    control_id: str | None = Field(
        default=None,
        description="Framework control reference (e.g. 'SOC2-CC6.1', 'ISO27001-A.12.1').",
    )
    question_text: str
    context_hint: str | None = Field(
        default=None,
        description="Adjacent cell text used to disambiguate the question (e.g. sub-category).",
    )
    status: QuestionStatus = QuestionStatus.PENDING

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        row_index: int,
        question_text: str,
        category: str = "General",
        control_id: str | None = None,
        context_hint: str | None = None,
    ) -> "QuestionItem":
        """Factory that stamps deterministic idempotency keys at construction time."""
        question_id  = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{thread_id}:{row_index}"))
        execution_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{thread_id}:{question_id}:exec"))
        return cls(
            question_id=question_id,
            execution_id=execution_id,
            row_index=row_index,
            question_text=question_text,
            category=category,
            control_id=control_id,
            context_hint=context_hint,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval models  (dual_stream_retrieval node output)
# ─────────────────────────────────────────────────────────────────────────────

class VectorRetrievalResult(_Base):
    """
    Result from a semantic similarity search against the Vector DB.

    Tenant isolation is enforced at query time via a `must` filter on the
    `tenant_id` payload field.  The value is echoed here for audit purposes.
    """

    matched_question: str
    matched_answer: str
    cosine_similarity: float = Field(ge=0.0, le=1.0)
    source_document_id: str  # e.g. Confluence page ID or past RFP file hash
    tenant_id: str


class GraphRetrievalResult(_Base):
    """
    Result from a parameterised Cypher query against the Neo4j infrastructure graph.

    The query is logged verbatim so LangSmith traces can reconstruct the full
    verification path.
    """

    component_name: str
    component_type: str          # Node label, e.g. "Database", "KMSKey", "IAMPolicy"
    is_active: bool
    is_compliant: bool
    compliance_frameworks: list[str] = Field(default_factory=list)
    cypher_query_used: str
    # Raw Neo4j records kept for the LLM synthesis step and debugging.
    raw_records: list[dict[str, Any]] = Field(default_factory=list)


class RetrievalBundle(_Base):
    """
    Aggregated dual-stream result for a single question.

    Either retrieval leg can be None if the corresponding system returned no
    match or timed out.  `retrieval_error` captures transient failures after
    all retry attempts are exhausted.
    """

    question_id: str
    vector_result: VectorRetrievalResult | None = None
    graph_result:  GraphRetrievalResult  | None = None
    retrieval_error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Drafting models  (draft_response node output)
# ─────────────────────────────────────────────────────────────────────────────

class DraftedAnswer(_Base):
    """
    LLM-synthesised proposed answer with confidence metadata.

    Routing thresholds (see evaluation_router in graph.py):
      AUTO_APPROVED   →  vector_confidence >= 0.92  AND  graph_verified == True
      REVIEW_REQUIRED →  any other combination, or discrepancy_detected == True
    """

    question_id: str
    proposed_answer: str

    # Confidence / verification signals
    vector_confidence: float = Field(ge=0.0, le=1.0)
    graph_verified: bool = Field(
        description="True when Neo4j confirms the infrastructure component is active and compliant."
    )
    discrepancy_detected: bool = Field(
        default=False,
        description="True when the historical vector answer contradicts the current graph state.",
    )

    # Audit / traceability
    reasoning_trace: str = Field(
        description="LLM chain-of-thought summary used for audit log and LangSmith annotation."
    )
    model_used: str          # e.g. "gpt-4o-2024-08-06" or "claude-3-5-sonnet-20241022"
    prompt_tokens: int     = 0
    completion_tokens: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Human-in-the-loop models  (resume webhook payload & node output)
# ─────────────────────────────────────────────────────────────────────────────

class HumanReviewDecision(_Base):
    """
    Payload submitted by a reviewer via POST /api/v1/rfp/threads/{thread_id}/resume.

    If `override_answer` is None the draft is accepted as-is.
    If `approved` is False the question is marked as rejected (not exported).
    """

    question_id: str
    approved: bool
    override_answer: str | None = Field(
        default=None,
        description="Reviewer-provided replacement text. None means the LLM draft is accepted.",
    )
    reviewer_id: str
    review_notes: str | None = None


class ResumePayload(_Base):
    """
    Full body of a /resume webhook request.
    May carry decisions for multiple questions in one call.
    """

    decisions: list[HumanReviewDecision] = Field(min_length=1)
    reviewer_id: str


# ─────────────────────────────────────────────────────────────────────────────
# Export models  (compile_and_export node output)
# ─────────────────────────────────────────────────────────────────────────────

class FinalAnswer(_Base):
    """Post-approval answer ready for document assembly."""

    question_id: str
    row_index: int
    category: str
    question_text: str
    final_answer_text: str
    status: QuestionStatus
    auto_approved: bool
    reviewer_id: str | None       = None
    langsmith_run_url: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Observability model
# ─────────────────────────────────────────────────────────────────────────────

class AuditMetrics(_Base):
    """
    Aggregate telemetry pushed to LangSmith at workflow completion.
    All counters are zero-initialised and accumulated by compile_and_export.
    """

    total_prompt_tokens: int      = 0
    total_completion_tokens: int  = 0
    total_questions: int          = 0
    auto_approved_count: int      = 0
    human_reviewed_count: int     = 0
    avg_vector_confidence: float  = 0.0
    processing_duration_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Dict merge reducer
# ─────────────────────────────────────────────────────────────────────────────

def _merge_dicts(a: dict, b: dict) -> dict:
    """
    State reducer for dict fields shared across concurrent nodes.

    Nodes return only the key(s) they produced; this reducer merges them into
    the accumulated state dict without overwriting unrelated keys.

    Example: dual_stream_retrieval fans out over 10 questions concurrently.
    Each task returns {"q-abc": RetrievalBundle(...)}.  The reducer merges
    all 10 partial updates into a single retrieval_bundles dict.
    """
    return {**a, **b}


# ─────────────────────────────────────────────────────────────────────────────
# Top-level LangGraph TypedDict state
# ─────────────────────────────────────────────────────────────────────────────

class RFPState(TypedDict):
    """
    Mutable state threaded through every node of the RFP Responder workflow.

    Reducer annotations
    ───────────────────
    - `messages`            uses add_messages  → append-only; LLM history
                            is never overwritten on graph resume.
    - Dict working fields   use _merge_dicts   → nodes return partial key
                            updates; no full-state rewrites required.
    - All other fields      use default "replace" reducer (last write wins).

    Serialisation notes
    ───────────────────
    LangGraph's AsyncPostgresSaver serialises state via `json.dumps`.
    Pydantic model instances serialise cleanly via model_dump(); they are
    re-hydrated with model_validate() in nodes that need typed access.
    Storing them as plain dicts in state and calling model_validate() at
    node entry is the recommended pattern for Postgres-backed checkpointers.
    """

    # ── Identity & multi-tenant isolation ────────────────────────────────────
    tenant_id: str          # Injected at API layer; propagated to every DB query
    thread_id: str          # LangGraph thread identifier (== checkpointer key)
    questionnaire_id: str   # Stable ID for the source document

    # ── Raw ingestion payload ─────────────────────────────────────────────────
    raw_payload: dict[str, Any]   # Original POST body, preserved for audit trail

    # ── Parsed questions ──────────────────────────────────────────────────────
    questions: list[dict[str, Any]]   # List of QuestionItem.model_dump() dicts

    # ── Per-question working state  (keyed by question_id) ───────────────────
    # _merge_dicts reducer: each node writes only the questions it processed.
    retrieval_bundles: Annotated[dict[str, Any], _merge_dicts]  # RetrievalBundle dicts
    drafted_answers:   Annotated[dict[str, Any], _merge_dicts]  # DraftedAnswer dicts
    human_decisions:   Annotated[dict[str, Any], _merge_dicts]  # HumanReviewDecision dicts
    final_answers:     Annotated[dict[str, Any], _merge_dicts]  # FinalAnswer dicts

    # ── Routing signals ───────────────────────────────────────────────────────
    review_required: bool        # True if ANY question's confidence is below threshold
    review_question_ids: list[str]  # Subset of question_ids routed to human review

    # ── Workflow lifecycle ────────────────────────────────────────────────────
    workflow_status: str         # WorkflowStatus enum value
    error_message: str | None
    retry_count: int

    # ── Observability ─────────────────────────────────────────────────────────
    langsmith_run_id: str | None
    audit_metrics: dict[str, Any] | None   # AuditMetrics.model_dump() dict

    # ── LLM message history ───────────────────────────────────────────────────
    # add_messages reducer: appends new messages, de-duplicates by message ID.
    # This ensures the full conversation replay is available after graph resume
    # without double-counting tokens.
    messages: Annotated[list[Any], add_messages]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: initial state factory
# ─────────────────────────────────────────────────────────────────────────────

def make_initial_state(
    *,
    tenant_id: str,
    thread_id: str,
    questionnaire_id: str,
    raw_payload: dict[str, Any],
) -> RFPState:
    """
    Returns a fully-initialised RFPState with all fields populated to their
    zero values.  Called by the FastAPI ingestion endpoint before the first
    graph.invoke() call.
    """
    return RFPState(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id=questionnaire_id,
        raw_payload=raw_payload,
        questions=[],
        retrieval_bundles={},
        drafted_answers={},
        human_decisions={},
        final_answers={},
        review_required=False,
        review_question_ids=[],
        workflow_status=WorkflowStatus.INITIALISED,
        error_message=None,
        retry_count=0,
        langsmith_run_id=None,
        audit_metrics=None,
        messages=[],
    )
