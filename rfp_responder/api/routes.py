"""
api/routes.py – FastAPI HTTP interface for the RFP Responder workflow.

Endpoints
─────────
  POST   /api/v1/rfp/ingest
  GET    /api/v1/rfp/threads/{thread_id}/status
  GET    /api/v1/rfp/threads/{thread_id}/review
  POST   /api/v1/rfp/threads/{thread_id}/resume

Authentication
──────────────
Tenant identity is carried on the X-Tenant-ID request header.  In production
this header would be populated by an API gateway that verifies a JWT and
injects the claim – the service itself never receives raw credentials.
The `_tenant_id` dependency validates presence and format.

LangGraph interrupt protocol
────────────────────────────
After each graph.ainvoke() call, routes inspect `graph.aget_state().next`:
  • Tuple is non-empty  →  graph is paused at an interrupt; return HTTP 202.
  • Tuple is empty      →  graph ran to completion; return HTTP 200.

This is the recommended LangGraph pattern – `ainvoke` always returns the
final state dict rather than raising; the caller determines interrupt vs
completion by inspecting `next`.

The `graph.aupdate_state()` call in /resume patches the checkpointed state
with the reviewer's decisions before re-invoking the graph with `None` as
input (sentinel: "continue from checkpoint, do not reset state").
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from rfp_responder.api.schemas import (
    AuditMetricsSummary,
    ErrorResponse,
    IngestRequest,
    IngestResponse,
    ReviewItem,
    ReviewItemsResponse,
    ResumeRequest,
    ResumeResponse,
    ThreadStatusResponse,
)
from rfp_responder.app.lifespan import get_arq_pool, get_graph
from rfp_responder.graph import thread_config
from rfp_responder.rate_limit import limiter
from rfp_responder.state import (
    DraftedAnswer,
    QuestionItem,
    WorkflowStatus,
    make_initial_state,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/rfp", tags=["RFP Workflow"])


# ─────────────────────────────────────────────────────────────────────────────
# Shared dependency: tenant identity
# ─────────────────────────────────────────────────────────────────────────────

async def _tenant_id(x_tenant_id: str = Header(..., alias="X-Tenant-ID")) -> str:
    """
    Extract and validate the tenant identity from the X-Tenant-ID header.

    In production an API gateway injects this after JWT verification.
    The service treats it as trusted once it arrives here.
    """
    tenant = x_tenant_id.strip()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-ID header must not be blank.",
        )
    if len(tenant) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-ID must be ≤ 128 characters.",
        )
    return tenant


# ─────────────────────────────────────────────────────────────────────────────
# Helper: resolve interrupt/complete state from LangGraph snapshot
# ─────────────────────────────────────────────────────────────────────────────

async def _get_thread_snapshot(graph, thread_id: str) -> Any:
    """
    Fetch the latest checkpoint snapshot for a thread.

    Returns None if the thread has no checkpoint (does not exist).
    """
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.aget_state(config)


def _snapshot_next_node(snapshot) -> str | None:
    """Return the name of the first pending node, or None if graph completed."""
    if snapshot is None:
        return None
    next_nodes = getattr(snapshot, "next", ()) or ()
    return next_nodes[0] if next_nodes else None


def _build_audit_summary(audit_dict: dict | None) -> AuditMetricsSummary | None:
    if not audit_dict:
        return None
    return AuditMetricsSummary(
        total_questions=audit_dict.get("total_questions", 0),
        auto_approved_count=audit_dict.get("auto_approved_count", 0),
        human_reviewed_count=audit_dict.get("human_reviewed_count", 0),
        avg_vector_confidence=audit_dict.get("avg_vector_confidence", 0.0),
        total_tokens=(
            audit_dict.get("total_prompt_tokens", 0)
            + audit_dict.get("total_completion_tokens", 0)
        ),
        processing_duration_seconds=audit_dict.get("processing_duration_seconds", 0.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/ingest
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
        429: {"description": "Rate limit exceeded"},
    },
    summary="Ingest a questionnaire and enqueue the RFP workflow",
)
@limiter.limit("20/hour")  # per-tenant; key_func defined in rate_limit.py
async def ingest_questionnaire(
    body: IngestRequest,
    request: Request,
    tenant_id: str = Depends(_tenant_id),
    arq_pool=Depends(get_arq_pool),
) -> IngestResponse:
    """
    Enqueue a new RFP workflow and return immediately with a `thread_id`.

    The workflow executes asynchronously in the arq worker process.
    Poll `GET /rfp/threads/{thread_id}/status` to follow progress.
    When status reaches `awaiting_review`, fetch items and POST decisions.

    This endpoint never blocks on LLM or DB calls – it only writes a job
    to Redis and returns 202 in < 50ms.
    """
    thread_id = str(uuid.uuid4())
    log = logger.bind(
        thread_id=thread_id,
        tenant_id=tenant_id,
        questionnaire_id=body.questionnaire_id,
    )
    log.info("ingest: enqueueing workflow job")

    await arq_pool.enqueue_job(
        "run_workflow",
        thread_id=thread_id,
        tenant_id=tenant_id,
        questionnaire_id=body.questionnaire_id,
        payload=body.payload,
        _job_id=f"workflow:{thread_id}",   # idempotency key
    )

    log.info("ingest: job enqueued", thread_id=thread_id)

    return IngestResponse(
        thread_id=thread_id,
        questionnaire_id=body.questionnaire_id,
        status=WorkflowStatus.INITIALISED,
        message=(
            f"Workflow enqueued. Poll GET /rfp/threads/{thread_id}/status for progress."
        ),
    )


# ── Legacy synchronous ingest (kept for tests / local dev without Redis) ─────

@router.post(
    "/ingest/sync",
    status_code=status.HTTP_200_OK,
    responses={
        202: {"description": "Workflow paused – human review required"},
        500: {"model": ErrorResponse},
    },
    summary="[Dev] Ingest synchronously without job queue",
    include_in_schema=False,   # hidden from production API docs
)
async def ingest_questionnaire_sync(
    body: IngestRequest,
    request: Request,
    tenant_id: str = Depends(_tenant_id),
    graph=Depends(get_graph),
) -> IngestResponse:
    """Synchronous path used in tests and local development (no Redis required)."""
    thread_id = str(uuid.uuid4())
    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id)
    log.info("ingest/sync: starting")

    initial_state = make_initial_state(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id=body.questionnaire_id,
        raw_payload=body.payload,
    )
    cfg = thread_config(thread_id, tenant_id)

    try:
        await graph.ainvoke(initial_state, config=cfg)
    except Exception as exc:
        log.error("ingest/sync: graph error", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Workflow failed: {exc}")

    snapshot = await _get_thread_snapshot(graph, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=500, detail="No checkpoint produced.")

    state_values: dict[str, Any] = snapshot.values
    next_node = _snapshot_next_node(snapshot)

    if next_node:
        review_ids: list[str] = state_values.get("review_question_ids", [])
        return IngestResponse(
            thread_id=thread_id,
            questionnaire_id=body.questionnaire_id,
            status=WorkflowStatus.AWAITING_REVIEW,
            message=f"{len(review_ids)} question(s) require human review.",
            review_question_ids=review_ids,
        )

    audit = _build_audit_summary(state_values.get("audit_metrics"))
    questionnaire_id = body.questionnaire_id
    return IngestResponse(
        thread_id=thread_id,
        questionnaire_id=questionnaire_id,
        status=WorkflowStatus.COMPLETE,
        message="All questions processed and exported.",
        total_questions=audit.total_questions if audit else 0,
        auto_approved_count=audit.auto_approved_count if audit else 0,
        human_reviewed_count=audit.human_reviewed_count if audit else 0,
        export_json_path=f"/tmp/rfp_exports/{questionnaire_id}.json",
        export_excel_path=f"/tmp/rfp_exports/{questionnaire_id}.xlsx",
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/threads/{thread_id}/status",
    summary="Poll the current lifecycle state of a workflow thread",
    responses={404: {"model": ErrorResponse}},
)
async def get_thread_status(
    thread_id: str,
    tenant_id: str = Depends(_tenant_id),
    graph=Depends(get_graph),
) -> ThreadStatusResponse:
    """
    Returns the current `WorkflowStatus`, any pending review IDs, and aggregate
    audit metrics (once available).

    Safe to poll repeatedly; reads from the Postgres checkpoint without
    triggering any workflow execution.
    """
    snapshot = await _get_thread_snapshot(graph, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")

    state_values: dict[str, Any] = snapshot.values

    # Guard against cross-tenant access (tenant_id is embedded in state)
    if state_values.get("tenant_id") != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Thread does not belong to the requesting tenant.",
        )

    return ThreadStatusResponse(
        thread_id=thread_id,
        questionnaire_id=state_values.get("questionnaire_id", ""),
        workflow_status=state_values.get("workflow_status", WorkflowStatus.INITIALISED),
        next_node=_snapshot_next_node(snapshot),
        review_question_ids=state_values.get("review_question_ids", []),
        audit_metrics=_build_audit_summary(state_values.get("audit_metrics")),
        error_message=state_values.get("error_message"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/review
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/threads/{thread_id}/review",
    summary="Retrieve the flagged questions awaiting human review",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Thread not in AWAITING_REVIEW state"},
    },
)
async def get_review_items(
    thread_id: str,
    tenant_id: str = Depends(_tenant_id),
    graph=Depends(get_graph),
) -> ReviewItemsResponse:
    """
    Returns each pending review item with its proposed answer, confidence
    scores, and reasoning trace so the reviewer has full context to decide.

    Only callable when the thread is in AWAITING_REVIEW state.
    """
    snapshot = await _get_thread_snapshot(graph, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")

    state_values: dict[str, Any] = snapshot.values

    if state_values.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    if not _snapshot_next_node(snapshot):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Thread is not awaiting review. "
                f"Current status: {state_values.get('workflow_status')}."
            ),
        )

    review_ids: list[str] = state_values.get("review_question_ids", [])
    drafted_raw: dict[str, Any]  = state_values.get("drafted_answers", {})
    questions_raw: list[dict]    = state_values.get("questions", [])

    # Build a question_id → QuestionItem lookup
    questions_map: dict[str, QuestionItem] = {
        q["question_id"]: QuestionItem.model_validate(q)
        for q in questions_raw
    }

    items: list[ReviewItem] = []
    for qid in review_ids:
        draft_dict = drafted_raw.get(qid)
        question   = questions_map.get(qid)

        if draft_dict is None or question is None:
            continue

        draft = DraftedAnswer.model_validate(draft_dict)
        items.append(ReviewItem(
            question_id=qid,
            row_index=question.row_index,
            category=question.category,
            control_id=question.control_id,
            question_text=question.question_text,
            proposed_answer=draft.proposed_answer,
            vector_confidence=draft.vector_confidence,
            graph_verified=draft.graph_verified,
            discrepancy_detected=draft.discrepancy_detected,
            reasoning_trace=draft.reasoning_trace,
        ))

    items.sort(key=lambda i: i.row_index)

    return ReviewItemsResponse(
        thread_id=thread_id,
        questionnaire_id=state_values.get("questionnaire_id", ""),
        items=items,
        total_pending=len(items),
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/threads/{thread_id}/resume
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/threads/{thread_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit human review decisions and enqueue workflow resumption",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Thread not in AWAITING_REVIEW state"},
        403: {"model": ErrorResponse},
    },
)
async def resume_thread(
    thread_id: str,
    body: ResumeRequest,
    tenant_id: str = Depends(_tenant_id),
    graph=Depends(get_graph),
    arq_pool=Depends(get_arq_pool),
) -> ResumeResponse:
    """
    Validate that the thread is awaiting review, patch its checkpoint with
    the reviewer's decisions, then enqueue background continuation.

    Returns 202 immediately.  Poll GET /status to know when the workflow
    finishes the compile_and_export step.
    """
    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id, reviewer_id=body.reviewer_id)

    # ── 1. Guard: thread must exist and be awaiting review ────────────────────
    snapshot = await _get_thread_snapshot(graph, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")

    state_values: dict[str, Any] = snapshot.values

    if state_values.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    if not _snapshot_next_node(snapshot):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Thread is not awaiting review and cannot be resumed. "
                f"Current status: {state_values.get('workflow_status')}."
            ),
        )

    # ── 2. Build decisions dict and enqueue ───────────────────────────────────
    decisions_dict: dict[str, Any] = {d.question_id: d.model_dump() for d in body.decisions}
    log.info("resume: enqueueing resume job", decision_count=len(decisions_dict))

    await arq_pool.enqueue_job(
        "resume_workflow",
        thread_id=thread_id,
        tenant_id=tenant_id,
        decisions=decisions_dict,
        _job_id=f"resume:{thread_id}",   # idempotency: only one resume job per thread
    )

    return ResumeResponse(
        thread_id=thread_id,
        status=WorkflowStatus.AWAITING_REVIEW,
        message=(
            f"Review decisions accepted. Poll GET /rfp/threads/{thread_id}/status "
            "for completion."
        ),
    )


# ── Legacy synchronous resume (kept for tests / local dev without Redis) ──────

@router.post(
    "/threads/{thread_id}/resume/sync",
    status_code=status.HTTP_200_OK,
    summary="[Dev] Resume synchronously without job queue",
    include_in_schema=False,
)
async def resume_thread_sync(
    thread_id: str,
    body: ResumeRequest,
    tenant_id: str = Depends(_tenant_id),
    graph=Depends(get_graph),
) -> ResumeResponse:
    """Synchronous resume used in tests and local development."""
    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id)

    snapshot = await _get_thread_snapshot(graph, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")
    state_values: dict[str, Any] = snapshot.values
    if state_values.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not _snapshot_next_node(snapshot):
        raise HTTPException(status_code=409, detail="Thread is not awaiting review.")

    decisions_dict: dict[str, Any] = {d.question_id: d.model_dump() for d in body.decisions}
    cfg = thread_config(thread_id, tenant_id)
    await graph.aupdate_state(config=cfg, values={"human_decisions": decisions_dict}, as_node="human_review_wait")

    try:
        await graph.ainvoke(None, config=cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Workflow resume failed: {exc}")

    snapshot = await _get_thread_snapshot(graph, thread_id)
    state_values = snapshot.values if snapshot else {}
    next_node = _snapshot_next_node(snapshot)
    questionnaire_id = state_values.get("questionnaire_id", "")

    if next_node:
        review_ids = state_values.get("review_question_ids", [])
        return ResumeResponse(
            thread_id=thread_id,
            status=WorkflowStatus.AWAITING_REVIEW,
            message=f"Workflow paused again. {len(review_ids)} items pending.",
            review_question_ids=review_ids,
        )

    log.info("resume/sync: workflow complete")
    return ResumeResponse(
        thread_id=thread_id,
        status=WorkflowStatus.COMPLETE,
        message="Workflow complete. Export files are ready.",
        export_json_path=f"/tmp/rfp_exports/{questionnaire_id}.json",
        export_excel_path=f"/tmp/rfp_exports/{questionnaire_id}.xlsx",
    )
