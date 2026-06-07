"""
graph.py – LangGraph StateGraph topology for the RFP Responder workflow.

Topology overview
─────────────────

  START
    │
    ▼
  parse_questionnaire      Ingest raw payload → structured QuestionItem list
    │
    ▼
  dual_stream_retrieval    Concurrent: Vector DB + Neo4j Cypher for every question
    │
    ▼
  draft_response           LLM synthesises answers; stamps vector_confidence +
    │                      graph_verified on each DraftedAnswer
    │
    ├──[review_required=True]──► human_review_wait  ◄── INTERRUPT POINT
    │                                 │
    └──[all AUTO_APPROVED]────────────┤
                                      ▼
                              compile_and_export    Build JSON/Excel, push LangSmith metrics
                                      │
                                     END

Interrupt mechanics
───────────────────
`interrupt_before=["human_review_wait"]` tells LangGraph to:
  1. Checkpoint the full RFPState before executing human_review_wait.
  2. Raise a GraphInterrupt exception, surfaced by FastAPI as a 202 response.
  3. Block the thread until graph.invoke() is called again on the same thread_id
     (triggered by POST /api/v1/rfp/threads/{thread_id}/resume).

The resume call injects human_decisions into state and the graph continues
from human_review_wait → compile_and_export.

Checkpointer strategy
─────────────────────
- Dev / test  → MemorySaver()       (in-process, ephemeral)
- Production  → AsyncPostgresSaver  (durable, survives process restarts)
  Instantiated in app/lifespan.py and injected via `build_graph(checkpointer=…)`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from rfp_responder.nodes.compile_and_export import compile_and_export as _real_compile
from rfp_responder.nodes.draft_response import draft_response as _real_draft
from rfp_responder.nodes.dual_stream_retrieval import dual_stream_retrieval as _real_retrieve
from rfp_responder.nodes.human_review_wait import human_review_wait as _real_review
from rfp_responder.nodes.parse_questionnaire import parse_questionnaire as _real_parse
from rfp_responder.state import (
    DraftedAnswer,
    QuestionStatus,
    RFPState,
    WorkflowStatus,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Routing thresholds  (single source of truth – referenced by tests too)
# ─────────────────────────────────────────────────────────────────────────────

AUTO_APPROVE_VECTOR_THRESHOLD: float = 0.92   # cosine similarity minimum
# Both conditions must hold for AUTO_APPROVE:
#   drafted_answer.vector_confidence >= AUTO_APPROVE_VECTOR_THRESHOLD
#   drafted_answer.graph_verified    == True


# ─────────────────────────────────────────────────────────────────────────────
# Node stubs
# Full implementations live in rfp_responder/nodes/*.py and are imported in
# the build_graph() factory.  Stubs here satisfy the type-checker and make
# the topology easy to audit in isolation.
# ─────────────────────────────────────────────────────────────────────────────

async def _stub(node_name: str, state: RFPState) -> dict[str, Any]:
    raise NotImplementedError(
        f"Node '{node_name}' not yet wired. "
        f"Import the real implementation in build_graph()."
    )


async def parse_questionnaire(state: RFPState) -> dict[str, Any]:
    """
    Ingests raw_payload → extracts QuestionItem rows → initialises per-row tracking.

    Returns
    -------
    questions        : list[dict]   – QuestionItem.model_dump() for each row
    workflow_status  : str          – WorkflowStatus.PARSING
    """
    return await _stub("parse_questionnaire", state)


async def dual_stream_retrieval(state: RFPState) -> dict[str, Any]:
    """
    Fans out over all PENDING questions; for each fires two concurrent tasks:
      • Vector DB semantic search  (Qdrant, tenant_id filter)
      • Neo4j Cypher query         (async driver, tenant_id param)

    Both legs wrapped in tenacity retry with exponential backoff + jitter.

    Returns
    -------
    retrieval_bundles : dict[question_id, dict]  – partial merge via _merge_dicts
    workflow_status   : str                      – WorkflowStatus.RETRIEVING
    """
    return await _stub("dual_stream_retrieval", state)


async def draft_response(state: RFPState) -> dict[str, Any]:
    """
    For each retrieved question, invokes an LLM (gpt-4o / claude-3-5-sonnet)
    to reconcile the vector answer with the graph topology and produce a
    DraftedAnswer.  Stamps vector_confidence, graph_verified, discrepancy_detected.

    After processing all questions, evaluates routing signals:
      review_required      = True if any question fails the AUTO_APPROVE threshold
      review_question_ids  = list of question_ids that need human review

    Returns
    -------
    drafted_answers      : dict[question_id, dict]
    review_required      : bool
    review_question_ids  : list[str]
    workflow_status      : str
    """
    return await _stub("draft_response", state)


async def human_review_wait(state: RFPState) -> dict[str, Any]:
    """
    Execution parks here while the graph thread is interrupted.

    On graph resume (POST /resume webhook), human_decisions have already
    been injected into state by the FastAPI endpoint before calling
    graph.invoke().  This node merges those decisions into drafted_answers
    and updates per-question statuses:
      • approved + no override  → HUMAN_APPROVED
      • approved + override     → HUMAN_OVERRIDDEN
      • rejected                → excluded from final_answers

    Returns
    -------
    drafted_answers  : dict  – updated with reviewer decisions
    workflow_status  : str   – WorkflowStatus.COMPILING
    """
    return await _stub("human_review_wait", state)


async def compile_and_export(state: RFPState) -> dict[str, Any]:
    """
    Constructs the finalised FinalAnswer list, serialises to JSON + Excel,
    persists to object storage, and pushes AuditMetrics to LangSmith.

    Returns
    -------
    final_answers   : dict[question_id, dict]
    audit_metrics   : dict
    workflow_status : str  – WorkflowStatus.COMPLETE
    """
    return await _stub("compile_and_export", state)


# ─────────────────────────────────────────────────────────────────────────────
# Conditional router
# ─────────────────────────────────────────────────────────────────────────────

def evaluation_router(
    state: RFPState,
) -> Literal["human_review_wait", "compile_and_export"]:
    """
    Post-draft routing gate.  Returns the *name of the next node* so LangGraph
    can resolve the conditional edge.

    Decision table
    ──────────────
    Condition                                          → Next node
    ─────────────────────────────────────────────────────────────────────────
    review_required == False (all questions passed     → compile_and_export
    vector threshold AND graph verified)

    review_required == True  (≥ 1 question below       → human_review_wait
    threshold OR discrepancy detected)                   ← INTERRUPT fires here

    Secondary guard: if drafted_answers is empty (edge case on empty
    questionnaire), route directly to compile_and_export so the workflow
    terminates cleanly rather than parking indefinitely.
    """
    drafted: dict[str, Any] = state.get("drafted_answers", {})

    if not drafted:
        logger.warning(
            "evaluation_router: no drafted answers found, routing to compile_and_export",
            extra={"thread_id": state.get("thread_id"), "tenant_id": state.get("tenant_id")},
        )
        return "compile_and_export"

    if state.get("review_required", False):
        review_ids = state.get("review_question_ids", [])
        logger.info(
            "evaluation_router → human_review_wait",
            extra={
                "thread_id": state.get("thread_id"),
                "tenant_id": state.get("tenant_id"),
                "review_count": len(review_ids),
                "review_question_ids": review_ids,
            },
        )
        return "human_review_wait"

    logger.info(
        "evaluation_router → compile_and_export (all AUTO_APPROVED)",
        extra={
            "thread_id": state.get("thread_id"),
            "tenant_id": state.get("tenant_id"),
            "approved_count": len(drafted),
        },
    )
    return "compile_and_export"


# ─────────────────────────────────────────────────────────────────────────────
# Graph factory
# ─────────────────────────────────────────────────────────────────────────────

def build_graph(
    checkpointer=None,
    *,
    # Allow callers to inject real node implementations (production) or
    # lightweight test doubles (unit tests) without monkey-patching globals.
    node_parse: Any = parse_questionnaire,
    node_retrieve: Any = dual_stream_retrieval,
    node_draft: Any = draft_response,
    node_review: Any = human_review_wait,
    node_compile: Any = compile_and_export,
) -> "CompiledStateGraph":
    """
    Construct and compile the RFP Responder StateGraph.

    Parameters
    ──────────
    checkpointer
        A LangGraph BaseCheckpointSaver.  Pass None for MemorySaver (dev/test).
        Production: pass an AsyncPostgresSaver wired to the app's connection pool.

    node_*
        Injectable node callables.  Defaults to the module-level async stubs;
        real implementations are imported and injected by the app lifespan.

    Returns
    ───────
    A compiled CompiledStateGraph ready for `.invoke()` / `.astream()`.

    Interrupt configuration
    ───────────────────────
    `interrupt_before=["human_review_wait"]`

    LangGraph pauses *before* entering human_review_wait whenever the
    evaluation_router sends execution there.  The thread state is durably
    checkpointed.  The workflow resumes only when:
      1. POST /api/v1/rfp/threads/{thread_id}/resume is received.
      2. The FastAPI handler merges human_decisions into the checkpointed state.
      3. graph.ainvoke(None, config={"configurable": {"thread_id": thread_id}})
         is called with a None input (signals "continue from checkpoint").
    """
    workflow: StateGraph = StateGraph(RFPState)

    # ── Register nodes ────────────────────────────────────────────────────────
    workflow.add_node("parse_questionnaire",   node_parse)
    workflow.add_node("dual_stream_retrieval", node_retrieve)
    workflow.add_node("draft_response",        node_draft)
    workflow.add_node("human_review_wait",     node_review)
    workflow.add_node("compile_and_export",    node_compile)

    # ── Linear edges (happy path) ─────────────────────────────────────────────
    workflow.add_edge(START,                   "parse_questionnaire")
    workflow.add_edge("parse_questionnaire",   "dual_stream_retrieval")
    workflow.add_edge("dual_stream_retrieval", "draft_response")

    # ── Conditional branch from draft_response ────────────────────────────────
    # The router returns the string name of the next node.
    # The explicit path_map below makes the graph topology visible to LangSmith
    # and allows the graph visualiser to render both branches.
    workflow.add_conditional_edges(
        "draft_response",
        evaluation_router,
        {
            "human_review_wait":  "human_review_wait",   # review path  (interrupted)
            "compile_and_export": "compile_and_export",  # fast path    (no interrupt)
        },
    )

    # human_review_wait always converges back into compilation after resume.
    workflow.add_edge("human_review_wait", "compile_and_export")
    workflow.add_edge("compile_and_export", END)

    # ── Checkpointer ─────────────────────────────────────────────────────────
    if checkpointer is None:
        checkpointer = MemorySaver()
        logger.warning(
            "build_graph: using MemorySaver checkpointer – "
            "state is NOT durable across process restarts. "
            "Inject AsyncPostgresSaver for production."
        )

    # ── Compile ───────────────────────────────────────────────────────────────
    # interrupt_before=["human_review_wait"]:
    #   Execution pauses *before* this node if the router sends control there.
    #   The graph emits a GraphInterrupt; the FastAPI endpoint catches it and
    #   returns HTTP 202 with the thread_id so the client can poll or be notified.
    compiled: CompiledStateGraph = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_review_wait"],
    )

    logger.info(
        "RFP Responder graph compiled",
        extra={"interrupt_before": ["human_review_wait"], "checkpointer": type(checkpointer).__name__},
    )
    return compiled


# ─────────────────────────────────────────────────────────────────────────────
# Module-level dev instance
# Replaced at application startup (see app/lifespan.py) with a Postgres-backed
# graph.  Import this symbol only in tests or CLI tooling – never in
# production request handlers.
# ─────────────────────────────────────────────────────────────────────────────
graph: CompiledStateGraph = build_graph(
    node_parse=_real_parse,
    node_retrieve=_real_retrieve,
    node_draft=_real_draft,
    node_review=_real_review,
    node_compile=_real_compile,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build LangGraph thread config dict
# Callers pass this as the `config` kwarg to graph.ainvoke() / graph.astream()
# ─────────────────────────────────────────────────────────────────────────────

def thread_config(thread_id: str, tenant_id: str) -> dict[str, Any]:
    """
    Returns the LangGraph run config for a specific thread.

    Usage
    ─────
    await graph.ainvoke(initial_state, config=thread_config(tid, org_id))

    The `tags` list is surfaced in LangSmith for filtering runs by tenant.
    """
    return {
        "configurable": {
            "thread_id": thread_id,
        },
        "tags": [f"tenant:{tenant_id}"],
        "metadata": {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
        },
    }
