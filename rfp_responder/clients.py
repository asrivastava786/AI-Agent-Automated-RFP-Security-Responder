"""
clients.py – Lazy singleton accessors for every external service client.

Pattern
───────
Each client is initialised once on first access and cached at module level.
The application lifespan (app/lifespan.py) calls `close_all_clients()` on
shutdown to drain connection pools gracefully.

Why singletons and not FastAPI `Depends`?
  LangGraph nodes are plain async functions – they have no FastAPI request
  context.  Module-level singletons are the correct pattern here; FastAPI
  dependency injection is reserved for the API layer (routes.py).

Thread-safety note
  Python's GIL makes module-level assignment atomic for CPython.  For
  multi-process deployments (gunicorn workers) each process gets its own
  singleton, which is correct behaviour for connection pools.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from rfp_responder.config import settings

if TYPE_CHECKING:
    from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Module-level cache slots
# ─────────────────────────────────────────────────────────────────────────────
_qdrant: AsyncQdrantClient | None = None
_neo4j: "AsyncDriver | None" = None
_openai: AsyncOpenAI | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Qdrant
# ─────────────────────────────────────────────────────────────────────────────

def get_qdrant_client() -> AsyncQdrantClient:
    """
    Return the process-wide Qdrant async client.

    On first call, also ensures the target collection exists with the correct
    vector dimension and distance metric.  This is idempotent – if the
    collection already exists the call is a no-op.
    """
    global _qdrant
    if _qdrant is None:
        _qdrant = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=10,
        )
        logger.info("Qdrant async client initialised", extra={"url": settings.qdrant_url})
    return _qdrant


async def ensure_qdrant_collection() -> None:
    """
    Idempotently create the RFP answers collection if it does not exist.
    Called once from the application lifespan, not per-request.
    """
    client = get_qdrant_client()
    collections = await client.get_collections()
    names = {c.name for c in collections.collections}

    if settings.qdrant_collection_name not in names:
        await client.create_collection(
            collection_name=settings.qdrant_collection_name,
            vectors_config=VectorParams(
                size=settings.embedding_dimensions,
                distance=Distance.COSINE,
            ),
        )
        logger.info(
            "Created Qdrant collection",
            extra={"collection": settings.qdrant_collection_name},
        )
    else:
        logger.debug(
            "Qdrant collection already exists",
            extra={"collection": settings.qdrant_collection_name},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Neo4j
# ─────────────────────────────────────────────────────────────────────────────

def get_neo4j_driver() -> "AsyncDriver":
    """
    Return the process-wide Neo4j async driver.

    The driver manages an internal connection pool.  Always reuse this instance
    rather than creating a new driver per query.
    """
    global _neo4j
    if _neo4j is None:
        # Import here to avoid top-level import cost if Neo4j is not used.
        from neo4j import AsyncGraphDatabase

        _neo4j = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
        )
        logger.info(
            "Neo4j async driver initialised",
            extra={"uri": settings.neo4j_uri, "database": settings.neo4j_database},
        )
    return _neo4j


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────────────────────────────────────

def get_openai_client() -> AsyncOpenAI:
    """Return the process-wide async OpenAI client."""
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.openai_api_key)
        logger.info("OpenAI async client initialised")
    return _openai


async def embed_text(text: str) -> list[float]:
    """
    Embed a single string using the configured embedding model.

    Returns a normalised float vector of length `settings.embedding_dimensions`.
    This function is called inside the dual_stream_retrieval node and is wrapped
    there with a tenacity retry decorator.
    """
    client = get_openai_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
        dimensions=settings.embedding_dimensions,
    )
    return response.data[0].embedding


# ─────────────────────────────────────────────────────────────────────────────
# Health checks (used by /ready probe)
# ─────────────────────────────────────────────────────────────────────────────

async def ping_qdrant() -> bool:
    """Return True if Qdrant responds to a health check within 3 seconds."""
    try:
        client = get_qdrant_client()
        await client.get_collections()
        return True
    except Exception:
        return False


async def ping_neo4j() -> bool:
    """Return True if Neo4j Bolt connection is reachable."""
    try:
        driver = get_neo4j_driver()
        async with driver.session(database=settings.neo4j_database) as session:
            result = await session.run("RETURN 1 AS ok")
            await result.data()
        return True
    except Exception:
        return False


async def ping_postgres(pool) -> bool:
    """Return True if the Postgres connection pool can execute a trivial query."""
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────

async def close_all_clients() -> None:
    """
    Drain all connection pools.  Called from the FastAPI lifespan on shutdown.
    Safe to call even if a client was never initialised.
    """
    global _qdrant, _neo4j, _openai

    if _qdrant is not None:
        await _qdrant.close()
        _qdrant = None
        logger.info("Qdrant client closed")

    if _neo4j is not None:
        await _neo4j.close()
        _neo4j = None
        logger.info("Neo4j driver closed")

    if _openai is not None:
        await _openai.close()
        _openai = None
        logger.info("OpenAI client closed")
