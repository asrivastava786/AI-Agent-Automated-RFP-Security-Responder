"""
worker/tasks.py – arq job task definitions.

These functions are executed by the arq worker process, NOT in the FastAPI
request/response cycle.  The API enqueues a job and returns immediately;
the worker picks it up asynchronously.

Why arq over Celery?
────────────────────
arq is asyncio-native: task functions are `async def` and share the same
event loop, so LangGraph's async graph.ainvoke() works without thread-pool
bridging.  Celery requires greenlet patches or sync wrappers for async code.

Task flow
─────────
POST /ingest  →  enqueue_job("run_workflow", …)   →  arq worker
POST /resume  →  enqueue_job("resume_workflow", …) →  arq worker

Both tasks write their results into the Postgres LangGraph checkpoint store,
which the API reads via GET /status.  No shared in-memory state.

Job IDs
───────
arq generates a unique job_id per enqueue call.  We also store the
thread_id in the job kwargs so callers can match jobs to threads.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from rfp_responder.config import settings
from rfp_responder.metrics import JOB_PROCESSING_TIME, WORKFLOW_COMPLETIONS
from rfp_responder.state import WorkflowStatus, make_initial_state

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Startup / shutdown hooks (called once per worker process lifecycle)
# ─────────────────────────────────────────────────────────────────────────────

async def startup(ctx: dict) -> None:
    """
    Initialise shared resources for the worker process.
    Runs once when the arq worker starts up.
    """
    from psycopg_pool import AsyncConnectionPool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from rfp_responder.graph import build_graph
    from rfp_responder.nodes import (
        compile_and_export,
        draft_response,
        dual_stream_retrieval,
        human_review_wait,
        parse_questionnaire,
    )
    from rfp_responder.clients import ensure_qdrant_collection, get_neo4j_driver

    log = logger.bind(process="worker")

    # ── Postgres connection pool ──────────────────────────────────────────────
    pool = AsyncConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await pool.open()
    log.info("Worker Postgres pool opened")

    # ── Checkpointer ─────────────────────────────────────────────────────────
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()

    # ── Production graph ──────────────────────────────────────────────────────
    graph = build_graph(
        checkpointer=checkpointer,
        node_parse=parse_questionnaire,
        node_retrieve=dual_stream_retrieval,
        node_draft=draft_response,
        node_review=human_review_wait,
        node_compile=compile_and_export,
    )

    # ── External clients ──────────────────────────────────────────────────────
    await ensure_qdrant_collection()
    driver = get_neo4j_driver()
    await driver.verify_connectivity()

    # Store in arq context dict – available as ctx["graph"] etc. in task fns
    ctx["pool"] = pool
    ctx["graph"] = graph
    log.info("Worker startup complete")


async def shutdown(ctx: dict) -> None:
    """Gracefully drain connections when the worker process stops."""
    from rfp_responder.clients import close_all_clients

    await close_all_clients()
    if pool := ctx.get("pool"):
        await pool.close()
    logger.info("Worker shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# Task: run_workflow
# ─────────────────────────────────────────────────────────────────────────────

async def run_workflow(
    ctx: dict,
    *,
    thread_id: str,
    tenant_id: str,
    questionnaire_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute the full RFP workflow for a new questionnaire submission.

    Called by POST /rfp/ingest after enqueuing the job.
    The workflow checkpoints its state to Postgres; the API reads progress
    via GET /rfp/threads/{thread_id}/status.

    Returns a summary dict (stored by arq in Redis for 24h).
    """
    from rfp_responder.graph import thread_config

    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id, job="run_workflow")
    log.info("run_workflow started")

    start = time.monotonic()

    initial_state = make_initial_state(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id=questionnaire_id,
        raw_payload=payload,
    )
    cfg = thread_config(thread_id, tenant_id)
    graph = ctx["graph"]

    try:
        await graph.ainvoke(initial_state, config=cfg)
        elapsed = time.monotonic() - start
        JOB_PROCESSING_TIME.labels(job_type="run_workflow").observe(elapsed)
        log.info("run_workflow complete", duration_s=round(elapsed, 2))
        return {"thread_id": thread_id, "status": "done", "duration_s": elapsed}
    except Exception as exc:
        WORKFLOW_COMPLETIONS.labels(tenant_id=tenant_id, status="failed").inc()
        log.error("run_workflow failed", error=str(exc))
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Task: resume_workflow
# ─────────────────────────────────────────────────────────────────────────────

async def resume_workflow(
    ctx: dict,
    *,
    thread_id: str,
    tenant_id: str,
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """
    Inject human review decisions and resume a paused workflow.

    Called by POST /rfp/threads/{thread_id}/resume after enqueuing.
    1. Patches checkpoint via aupdate_state (as_node="human_review_wait")
    2. Resumes via ainvoke(None, config)
    """
    from rfp_responder.graph import thread_config

    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id, job="resume_workflow")
    log.info("resume_workflow started", decision_count=len(decisions))

    start = time.monotonic()
    cfg = thread_config(thread_id, tenant_id)
    graph = ctx["graph"]

    try:
        await graph.aupdate_state(
            config=cfg,
            values={"human_decisions": decisions},
            as_node="human_review_wait",
        )
        await graph.ainvoke(None, config=cfg)
        elapsed = time.monotonic() - start
        JOB_PROCESSING_TIME.labels(job_type="resume_workflow").observe(elapsed)
        log.info("resume_workflow complete", duration_s=round(elapsed, 2))
        return {"thread_id": thread_id, "status": "done", "duration_s": elapsed}
    except Exception as exc:
        log.error("resume_workflow failed", error=str(exc))
        raise
