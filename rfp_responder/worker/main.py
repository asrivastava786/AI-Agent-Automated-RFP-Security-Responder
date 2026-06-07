"""
worker/main.py – arq worker process entrypoint.

Run:
    python -m rfp_responder.worker.main

Or via docker-compose:
    command: python -m rfp_responder.worker.main

The WorkerSettings class tells arq:
  - which Redis to connect to
  - which task functions to register
  - lifecycle hooks (startup / shutdown)
  - concurrency and timeout limits
"""

from __future__ import annotations

import asyncio

from arq import run_worker
from arq.connections import RedisSettings

from rfp_responder.config import settings
from rfp_responder.worker.tasks import resume_workflow, run_workflow, shutdown, startup

# ─────────────────────────────────────────────────────────────────────────────
# Parse Redis DSN → arq RedisSettings
# ─────────────────────────────────────────────────────────────────────────────
# arq doesn't accept a raw DSN string; it uses a RedisSettings dataclass.
# redis://[:password@]host[:port]/db

def _parse_redis_settings(dsn: str) -> RedisSettings:
    from urllib.parse import urlparse
    parsed = urlparse(dsn)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password,
        database=int(parsed.path.lstrip("/")) if parsed.path and parsed.path != "/" else 0,
    )


class WorkerSettings:
    """arq worker configuration."""

    # Task functions registered with this worker
    functions = [run_workflow, resume_workflow]

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Redis connection
    redis_settings = _parse_redis_settings(settings.redis_dsn)

    # Concurrency: number of jobs processed in parallel per worker process.
    # LangGraph workflows are I/O-bound (LLM + DB calls), so higher concurrency
    # is safe. Start conservatively and tune based on observed Postgres pool usage.
    max_jobs = 10

    # Per-job timeout (seconds). Large questionnaires (100+ Qs) can take >5 min.
    job_timeout = 1800   # 30 minutes

    # Keep job results in Redis for 24h (readable by admin/debug tools)
    keep_result = 86_400

    # Retry failed jobs up to 2 times with 60s delay
    max_tries = 3
    retry_jobs = True


if __name__ == "__main__":
    asyncio.run(run_worker(WorkerSettings))  # type: ignore[arg-type]
