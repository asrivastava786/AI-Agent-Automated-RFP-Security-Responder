"""
app/lifespan.py – FastAPI application lifespan: startup, shutdown, and state.

Responsibilities
────────────────
1. Open a psycopg v3 AsyncConnectionPool backed by PostgreSQL.
2. Initialise the AsyncPostgresSaver and call `.setup()` once (creates the
   `checkpoints` table if it does not already exist – idempotent).
3. Build the production LangGraph with real node implementations and the
   durable checkpointer, storing it on `app.state.graph`.
4. Ensure the Qdrant collection exists.
5. Verify the Neo4j driver can connect.
6. On shutdown: close pool + all client singletons.

Why lifespan and not module-level singletons?
─────────────────────────────────────────────
The AsyncConnectionPool must be opened *after* the event loop starts – it
cannot be created at import time.  FastAPI's lifespan context manager is the
idiomatic location for any async initialisation that must survive the entire
process lifetime.

Access pattern in routes
────────────────────────
    graph = request.app.state.graph   # in route handlers
    # OR via the dependency:
    from rfp_responder.app.lifespan import get_graph
    graph = get_graph(request)

Note: the module-level `rfp_responder.graph.graph` instance (built with
MemorySaver) is intentionally NOT used in production routes.  It exists only
for tests and CLI tooling.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncGenerator

import structlog
from fastapi import FastAPI, Request

from rfp_responder.clients import (
    close_all_clients,
    ensure_qdrant_collection,
    get_neo4j_driver,
)
from rfp_responder.config import settings
from rfp_responder.graph import build_graph
from rfp_responder.nodes import (
    compile_and_export,
    draft_response,
    dual_stream_retrieval,
    human_review_wait,
    parse_questionnaire,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan context manager
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Async context manager passed to FastAPI(lifespan=lifespan).

    Everything before `yield` runs at startup; everything after runs at
    shutdown.  The checkpointer pool is kept alive for the entire server
    lifetime by keeping the pool context manager open across the yield.
    """
    log = logger.bind(env=settings.app_env)

    # ── 1. PostgreSQL connection pool ─────────────────────────────────────────
    # AsyncConnectionPool from psycopg[pool].
    # - min_size=2: keep 2 warm connections at all times (handles bursts without
    #   cold-start latency).
    # - max_size=10: hard ceiling; tune based on Postgres max_connections.
    # - autocommit=True: required by AsyncPostgresSaver – it manages its own
    #   transactions internally.
    from psycopg_pool import AsyncConnectionPool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    pool = AsyncConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=2,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,   # we open explicitly below for clear error reporting
    )
    await pool.open()
    log.info("PostgreSQL connection pool opened", max_size=10)

    # ── 2. Checkpointer setup ─────────────────────────────────────────────────
    # .setup() is idempotent – runs CREATE TABLE IF NOT EXISTS for the
    # checkpoints, checkpoint_writes, and checkpoint_blobs tables.
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    log.info("AsyncPostgresSaver initialised and schema verified")

    # ── 3. Build production graph ─────────────────────────────────────────────
    prod_graph: CompiledStateGraph = build_graph(
        checkpointer=checkpointer,
        node_parse=parse_questionnaire,
        node_retrieve=dual_stream_retrieval,
        node_draft=draft_response,
        node_review=human_review_wait,
        node_compile=compile_and_export,
    )
    app.state.graph = prod_graph
    log.info("Production LangGraph compiled", checkpointer="AsyncPostgresSaver")

    # ── 4. Qdrant – ensure collection exists ──────────────────────────────────
    await ensure_qdrant_collection()

    # ── 5. Neo4j – verify connectivity ───────────────────────────────────────
    driver = get_neo4j_driver()
    await driver.verify_connectivity()
    log.info("Neo4j driver connectivity verified", uri=settings.neo4j_uri)

    # ── 6. arq Redis pool (job queue for background workflows) ────────────────
    from arq import create_pool
    from rfp_responder.worker.main import _parse_redis_settings
    arq_pool = await create_pool(_parse_redis_settings(settings.redis_dsn))
    app.state.arq_pool = arq_pool
    log.info("arq Redis pool opened", dsn=settings.redis_dsn)

    # ── 7. Expose pg pool on app.state for /ready health checks ───────────────
    app.state.pg_pool = pool

    log.info("RFP Responder startup complete – ready to serve requests")

    # ── Hand control to FastAPI ───────────────────────────────────────────────
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("RFP Responder shutting down")
    await arq_pool.aclose()
    await close_all_clients()
    await pool.close()
    log.info("PostgreSQL connection pool closed")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency: graph access in route handlers
# ─────────────────────────────────────────────────────────────────────────────

def get_arq_pool(request: Request):
    """FastAPI dependency: returns the arq Redis pool for job enqueueing."""
    return request.app.state.arq_pool


def get_graph(request: Request) -> "CompiledStateGraph":
    """
    FastAPI dependency that returns the production graph from app state.

    Usage in a route:
        from fastapi import Depends
        from rfp_responder.app.lifespan import get_graph

        @router.post("/ingest")
        async def ingest(
            body: IngestRequest,
            graph: CompiledStateGraph = Depends(get_graph),
        ): ...
    """
    graph: "CompiledStateGraph" = request.app.state.graph
    if graph is None:
        raise RuntimeError(
            "Graph not initialised. Was the FastAPI lifespan registered? "
            "Pass lifespan=lifespan to FastAPI(...)."
        )
    return graph
