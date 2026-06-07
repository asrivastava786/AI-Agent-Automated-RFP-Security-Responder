"""
app/main.py – FastAPI application entry point.

Run (development):
    uvicorn rfp_responder.app.main:app --reload --port 8000

Run (production):
    uvicorn rfp_responder.app.main:app --workers 2 --port 8000
"""

from __future__ import annotations

import logging
import time

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from rfp_responder.api.routes import router
from rfp_responder.app.lifespan import lifespan
from rfp_responder.clients import ping_neo4j, ping_postgres, ping_qdrant
from rfp_responder.config import settings
from rfp_responder.rate_limit import limiter

# ─────────────────────────────────────────────────────────────────────────────
# Structured logging bootstrap
# ─────────────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer()
        if settings.app_env == "development"
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logging.basicConfig(
    level=logging.getLevelName(settings.log_level),
    handlers=[logging.StreamHandler()],
)

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="RFP Responder API",
    description=(
        "Automated RFP & Security Questionnaire Responder – "
        "async multi-agent pipeline with LangGraph HITL workflow."
    ),
    version="0.1.0",
    lifespan=lifespan,
    # Disable interactive docs in production (avoids schema enumeration)
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url="/redoc" if settings.app_env != "production" else None,
    openapi_url="/openapi.json" if settings.app_env != "production" else None,
)

# Attach rate limiter to app state (required by slowapi)
app.state.limiter = limiter

# ─────────────────────────────────────────────────────────────────────────────
# Middleware
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _request_timing(request: Request, call_next):
    """Add X-Process-Time response header and emit a structured access log line."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time"] = f"{elapsed:.4f}"
    structlog.get_logger("access").info(
        request.method,
        path=request.url.path,
        status=response.status_code,
        duration_s=round(elapsed, 4),
    )
    return response

# ─────────────────────────────────────────────────────────────────────────────
# Prometheus instrumentation
# ─────────────────────────────────────────────────────────────────────────────

try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/health", "/ready", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
except ImportError:
    pass  # prometheus_fastapi_instrumentator not installed; /metrics unavailable

# ─────────────────────────────────────────────────────────────────────────────
# Exception handlers
# ─────────────────────────────────────────────────────────────────────────────

app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger(__name__).error(
        "Unhandled exception",
        path=str(request.url.path),
        method=request.method,
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error.", "error_code": "INTERNAL"},
    )

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(router, prefix="/api/v1")

# ─────────────────────────────────────────────────────────────────────────────
# Ops endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Ops"], summary="Liveness probe", include_in_schema=False)
async def health() -> dict:
    """Returns 200 immediately. Used by Docker/K8s to detect process crashes."""
    return {"status": "ok", "env": settings.app_env}


@app.get("/ready", tags=["Ops"], summary="Readiness probe", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    """
    Deep health check: verifies all downstream dependencies are reachable.
    Kubernetes readinessProbe / ECS healthCheck should point here.
    Returns 503 if any dependency is down so the load balancer stops routing
    traffic to this instance until it recovers.
    """
    pool = getattr(request.app.state, "pg_pool", None)

    checks: dict[str, bool] = {
        "postgres": await ping_postgres(pool) if pool else False,
        "qdrant":   await ping_qdrant(),
        "neo4j":    await ping_neo4j(),
    }

    all_ok = all(checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
    )
