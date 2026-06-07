"""
nodes/dual_stream_retrieval.py – Concurrent Vector DB + Graph DB retrieval.

Architecture
────────────
For every PENDING question this node fans out two async tasks in parallel:

  ┌─ Task A ──────────────────────────────────────────────────────────────┐
  │  1. Embed question_text via OpenAI text-embedding-3-small             │
  │  2. Search Qdrant collection with must-filter: tenant_id == X         │
  │  3. Return VectorRetrievalResult (matched answer + cosine similarity) │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Task B ──────────────────────────────────────────────────────────────┐
  │  1. Extract infrastructure keyword from question_text (regex heuristic│
  │     – replace with spaCy NER / dedicated LLM call in production)      │
  │  2. Run parameterised async Cypher query against Neo4j                │
  │  3. Return GraphRetrievalResult (is_active, is_compliant, frameworks) │
  └───────────────────────────────────────────────────────────────────────┘

Both tasks are wrapped with a tenacity retry (exponential backoff + jitter).
If one leg fails after all retries, the RetrievalBundle carries a
`retrieval_error` string rather than blowing up the entire workflow.

Tenant isolation
────────────────
• Qdrant: `must` payload filter  →  {"key": "tenant_id", "match": {"value": tenant_id}}
• Neo4j:  Cypher WHERE clause    →  WHERE n.tenant_id = $tenant_id
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import structlog
from qdrant_client.models import FieldCondition, Filter, MatchValue
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from rfp_responder.clients import embed_text, get_neo4j_driver, get_qdrant_client
from rfp_responder.config import settings
from rfp_responder.state import (
    GraphRetrievalResult,
    QuestionItem,
    QuestionStatus,
    RetrievalBundle,
    RFPState,
    VectorRetrievalResult,
    WorkflowStatus,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Retry decorator – shared by both retrieval legs
# ─────────────────────────────────────────────────────────────────────────────

_RETRIEVAL_RETRY = retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential_jitter(
        initial=settings.retry_initial_wait_seconds,
        max=settings.retry_max_wait_seconds,
    ),
    # Retry on transient network / timeout errors only; don't swallow auth failures.
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure keyword extraction
# ─────────────────────────────────────────────────────────────────────────────

# Common infrastructure terms found in security questionnaires.
# Order matters – more specific patterns are checked first.
_INFRA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(SAML|OAuth|SSO|MFA|2FA|TOTP|LDAP|Active Directory)\b", re.IGNORECASE),
    re.compile(r"\b(KMS|HSM|key management|encryption key)\b", re.IGNORECASE),
    re.compile(r"\b(S3|blob storage|object storage|GCS|Azure Blob)\b", re.IGNORECASE),
    re.compile(r"\b(WAF|firewall|IDS|IPS|SIEM)\b", re.IGNORECASE),
    re.compile(r"\b(VPN|VPC|subnet|peering|transit gateway)\b", re.IGNORECASE),
    re.compile(r"\b(backup|restore|recovery|RTO|RPO)\b", re.IGNORECASE),
    re.compile(r"\b(TLS|SSL|mTLS|certificate|PKI)\b", re.IGNORECASE),
    re.compile(r"\b(database|DB|PostgreSQL|MySQL|MongoDB|DynamoDB|RDS|Aurora)\b", re.IGNORECASE),
    re.compile(r"\b(logging|audit log|SIEM|CloudTrail|CloudWatch)\b", re.IGNORECASE),
    re.compile(r"\b(IAM|role|policy|permission|privilege)\b", re.IGNORECASE),
    re.compile(r"\b(SOC\s*2|ISO\s*27001|PCI|HIPAA|GDPR|FedRAMP)\b", re.IGNORECASE),
]


def _extract_infra_keyword(question_text: str) -> str:
    """
    Return the first infrastructure keyword found in the question text.

    Fallback to the first four words of the question if no known term is matched.
    In production, replace this with a spaCy NER pass or a cheap LLM extraction call.
    """
    for pattern in _INFRA_PATTERNS:
        match = pattern.search(question_text)
        if match:
            return match.group(0).strip()
    # Fallback: first significant noun phrase (crude approximation)
    words = question_text.split()
    return " ".join(words[:4]) if len(words) >= 4 else question_text


# ─────────────────────────────────────────────────────────────────────────────
# Vector retrieval leg
# ─────────────────────────────────────────────────────────────────────────────

@_RETRIEVAL_RETRY
async def _vector_search(
    question_text: str,
    tenant_id: str,
) -> VectorRetrievalResult | None:
    """
    Embed the question and run a Qdrant similarity search filtered by tenant_id.

    Returns None if no relevant past answers exist (score < 0.5 cutoff).
    """
    embedding = await embed_text(question_text)

    client = get_qdrant_client()
    results = await client.search(
        collection_name=settings.qdrant_collection_name,
        query_vector=embedding,
        query_filter=Filter(
            must=[
                FieldCondition(
                    key="tenant_id",
                    match=MatchValue(value=tenant_id),
                )
            ]
        ),
        limit=1,
        score_threshold=0.5,   # discard low-relevance matches outright
        with_payload=True,
    )

    if not results:
        return None

    hit = results[0]
    payload = hit.payload or {}
    return VectorRetrievalResult(
        matched_question=payload.get("question_text", ""),
        matched_answer=payload.get("answer_text", ""),
        cosine_similarity=float(hit.score),
        source_document_id=payload.get("source_document_id", hit.id),
        tenant_id=tenant_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Graph retrieval leg
# ─────────────────────────────────────────────────────────────────────────────

# Cypher template: find any infrastructure node matching the keyword and its
# compliance relationships.  Uses parameterised inputs to prevent Cypher injection.
_CYPHER_TEMPLATE = """
MATCH (n {tenant_id: $tenant_id})
WHERE (n.name CONTAINS $keyword OR n.alias CONTAINS $keyword)
  AND n.tenant_id = $tenant_id
OPTIONAL MATCH (n)-[:IS_COMPLIANT_WITH]->(f:ComplianceFramework)
OPTIONAL MATCH (n)-[:USES_ENCRYPTION]->(k:KMSKey)
WITH n,
     collect(DISTINCT f.name)   AS frameworks,
     count(k)                   AS kms_count
RETURN
    n.name       AS component_name,
    labels(n)[0] AS component_type,
    n.status     AS status,
    n.is_compliant AS is_compliant,
    frameworks,
    kms_count
LIMIT 1
"""


@_RETRIEVAL_RETRY
async def _graph_query(
    keyword: str,
    tenant_id: str,
) -> GraphRetrievalResult | None:
    """
    Run a parameterised Cypher query against the Neo4j infrastructure graph.

    Returns None if no matching component is found in this tenant's graph.
    """
    driver = get_neo4j_driver()

    async with driver.session(database=settings.neo4j_database) as session:
        result = await session.run(
            _CYPHER_TEMPLATE,
            tenant_id=tenant_id,
            keyword=keyword,
        )
        records = await result.data()

    if not records:
        return None

    row = records[0]
    status: str = (row.get("status") or "unknown").lower()
    is_active: bool = status in {"active", "enabled", "running"}
    raw_compliant = row.get("is_compliant")
    is_compliant: bool = bool(raw_compliant) if raw_compliant is not None else False

    return GraphRetrievalResult(
        component_name=row.get("component_name", keyword),
        component_type=row.get("component_type", "Unknown"),
        is_active=is_active,
        is_compliant=is_compliant,
        compliance_frameworks=row.get("frameworks", []),
        cypher_query_used=_CYPHER_TEMPLATE.strip(),
        raw_records=records,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-question orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _retrieve_for_question(
    question: QuestionItem,
    tenant_id: str,
) -> tuple[str, dict[str, Any]]:
    """
    Run both retrieval legs concurrently for a single question.

    Returns (question_id, RetrievalBundle.model_dump()) so the caller can
    merge the result into state["retrieval_bundles"] via the dict-merge reducer.
    """
    keyword = _extract_infra_keyword(question.question_text)

    log = logger.bind(
        question_id=question.question_id,
        keyword=keyword,
        tenant_id=tenant_id,
    )

    vector_result: VectorRetrievalResult | None = None
    graph_result: GraphRetrievalResult | None = None
    retrieval_error: str | None = None

    try:
        # Fire both legs simultaneously; exceptions are captured individually.
        vector_task = asyncio.create_task(_vector_search(question.question_text, tenant_id))
        graph_task  = asyncio.create_task(_graph_query(keyword, tenant_id))

        vector_raw, graph_raw = await asyncio.gather(
            vector_task, graph_task, return_exceptions=True
        )

        if isinstance(vector_raw, BaseException):
            log.warning("Vector retrieval failed", error=str(vector_raw))
            retrieval_error = f"Vector: {vector_raw}"
        else:
            vector_result = vector_raw

        if isinstance(graph_raw, BaseException):
            log.warning("Graph retrieval failed", error=str(graph_raw))
            retrieval_error = (
                f"{retrieval_error}; Graph: {graph_raw}"
                if retrieval_error
                else f"Graph: {graph_raw}"
            )
        else:
            graph_result = graph_raw

    except Exception as exc:
        log.error("Unexpected retrieval error", error=str(exc))
        retrieval_error = str(exc)

    bundle = RetrievalBundle(
        question_id=question.question_id,
        vector_result=vector_result,
        graph_result=graph_result,
        retrieval_error=retrieval_error,
    )

    log.info(
        "Retrieval complete",
        has_vector=vector_result is not None,
        has_graph=graph_result is not None,
        error=retrieval_error,
    )

    return question.question_id, bundle.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# Public node
# ─────────────────────────────────────────────────────────────────────────────

async def dual_stream_retrieval(state: RFPState) -> dict[str, Any]:
    """
    LangGraph node: dual_stream_retrieval

    Fans out retrieval for all PENDING questions using asyncio.gather.
    Each question's two legs (vector + graph) run concurrently within
    `_retrieve_for_question`.

    Returns a partial state update:
      retrieval_bundles  – dict[question_id, RetrievalBundle dict]
                           merged into state via _merge_dicts reducer
      workflow_status    – WorkflowStatus.DRAFTING
    """
    tenant_id = state["tenant_id"]
    log = logger.bind(
        thread_id=state["thread_id"],
        tenant_id=tenant_id,
        questionnaire_id=state["questionnaire_id"],
    )

    # Hydrate Pydantic models from state dicts
    questions: list[QuestionItem] = [
        QuestionItem.model_validate(q) for q in state.get("questions", [])
        if q.get("status") == QuestionStatus.PENDING
    ]

    if not questions:
        log.warning("dual_stream_retrieval: no PENDING questions found")
        return {"workflow_status": WorkflowStatus.DRAFTING}

    log.info("dual_stream_retrieval starting", question_count=len(questions))

    # Fan out: one coroutine per question, all running concurrently.
    tasks = [_retrieve_for_question(q, tenant_id) for q in questions]
    results: list[tuple[str, dict] | BaseException] = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    bundles: dict[str, Any] = {}
    for item in results:
        if isinstance(item, BaseException):
            log.error("Retrieval task raised unexpectedly", error=str(item))
            continue
        question_id, bundle_dict = item
        bundles[question_id] = bundle_dict

    log.info(
        "dual_stream_retrieval complete",
        retrieved_count=len(bundles),
        failed_count=len(questions) - len(bundles),
    )

    return {
        "retrieval_bundles": bundles,
        "workflow_status": WorkflowStatus.DRAFTING,
    }
