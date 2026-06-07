"""
metrics.py – Custom Prometheus business metrics for the RFP Responder.

The HTTP-level metrics (latency, status codes, throughput) are handled
automatically by prometheus-fastapi-instrumentator in main.py.

This module adds the business-level signals that matter for LLM ops:
  • workflow completions / failures per tenant
  • auto-approve rate (signal for retrieval quality degradation)
  • vector confidence distribution
  • LLM token usage
  • human override rate (high rate → knowledge base gap)
  • job queue depth (when arq worker is behind)

Usage in nodes
──────────────
    from rfp_responder.metrics import (
        WORKFLOW_COMPLETIONS,
        AUTO_APPROVE_TOTAL,
        VECTOR_CONFIDENCE,
    )
    WORKFLOW_COMPLETIONS.labels(tenant_id="acme", status="complete").inc()
    VECTOR_CONFIDENCE.observe(0.87)
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ─────────────────────────────────────────────────────────────────────────────
# Workflow lifecycle
# ─────────────────────────────────────────────────────────────────────────────

WORKFLOW_COMPLETIONS = Counter(
    "rfp_workflow_completions_total",
    "Total RFP workflows that reached a terminal state (complete or failed).",
    labelnames=["tenant_id", "status"],  # status: complete | failed
)

WORKFLOW_DURATION = Histogram(
    "rfp_workflow_duration_seconds",
    "End-to-end workflow duration from ingest to export.",
    buckets=[5, 15, 30, 60, 120, 300, 600, 1800],
    labelnames=["tenant_id"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Retrieval quality
# ─────────────────────────────────────────────────────────────────────────────

AUTO_APPROVE_TOTAL = Counter(
    "rfp_questions_auto_approved_total",
    "Questions that passed the confidence threshold and were auto-approved.",
    labelnames=["tenant_id"],
)

HUMAN_REVIEW_TOTAL = Counter(
    "rfp_questions_human_reviewed_total",
    "Questions that required human review.",
    labelnames=["tenant_id"],
)

HUMAN_OVERRIDE_TOTAL = Counter(
    "rfp_questions_human_overridden_total",
    "Questions where the reviewer changed the proposed answer (override).",
    labelnames=["tenant_id"],
)

VECTOR_CONFIDENCE = Histogram(
    "rfp_vector_confidence",
    "Distribution of vector cosine similarity scores at draft time.",
    buckets=[0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.92, 0.95, 0.98, 1.0],
    labelnames=["tenant_id"],
)

RETRIEVAL_ERRORS = Counter(
    "rfp_retrieval_errors_total",
    "Retrieval leg failures (vector or graph) per question.",
    labelnames=["tenant_id", "leg"],  # leg: vector | graph
)

# ─────────────────────────────────────────────────────────────────────────────
# LLM token usage
# ─────────────────────────────────────────────────────────────────────────────

LLM_TOKENS = Counter(
    "rfp_llm_tokens_total",
    "Total LLM tokens consumed (prompt + completion).",
    labelnames=["tenant_id", "model", "token_type"],  # token_type: prompt | completion
)

LLM_ERRORS = Counter(
    "rfp_llm_errors_total",
    "LLM call failures (after all retries exhausted).",
    labelnames=["tenant_id", "model"],
)

# ───────────────────────────────────────────────────────────────────────���─────
# Job queue (arq)
# ─────────────────────────────────────────────────────────────────────────────

JOB_QUEUE_DEPTH = Gauge(
    "rfp_job_queue_depth",
    "Current number of jobs waiting in the arq queue.",
    labelnames=["queue"],
)

JOB_PROCESSING_TIME = Histogram(
    "rfp_job_processing_seconds",
    "Time taken by the arq worker to process a single job.",
    buckets=[1, 5, 15, 30, 60, 120, 300, 600],
    labelnames=["job_type"],  # job_type: run_workflow | resume_workflow
)

# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers
# ─────────────────────────────────────────────────────────────────────────────

def record_workflow_complete(
    tenant_id: str,
    *,
    duration_s: float,
    auto_approved: int,
    human_reviewed: int,
    human_overridden: int,
    avg_confidence: float,
) -> None:
    """
    Record all post-workflow metrics in a single call.
    Called from compile_and_export after the audit bundle is assembled.
    """
    WORKFLOW_COMPLETIONS.labels(tenant_id=tenant_id, status="complete").inc()
    WORKFLOW_DURATION.labels(tenant_id=tenant_id).observe(duration_s)
    AUTO_APPROVE_TOTAL.labels(tenant_id=tenant_id).inc(auto_approved)
    HUMAN_REVIEW_TOTAL.labels(tenant_id=tenant_id).inc(human_reviewed)
    HUMAN_OVERRIDE_TOTAL.labels(tenant_id=tenant_id).inc(human_overridden)
    VECTOR_CONFIDENCE.labels(tenant_id=tenant_id).observe(avg_confidence)
