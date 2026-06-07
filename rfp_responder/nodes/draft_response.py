"""
nodes/draft_response.py – LLM synthesis node: reconcile retrieval streams → draft answer.

What this node does
───────────────────
For every question that has a RetrievalBundle it:

1.  Builds a structured prompt supplying:
      • The question text and category
      • The best historical answer (with its cosine similarity score)
      • The current live infrastructure state from Neo4j

2.  Calls an LLM with structured output parsing (Pydantic model) so the
    response is never freeform text that needs fragile regex extraction.

3.  Stamps the routing signals on each DraftedAnswer:
      vector_confidence   – from the vector retrieval score (or 0.0 if no match)
      graph_verified      – True iff Neo4j found an active AND compliant component
      discrepancy_detected – True iff historical answer implies a different state
                             than the live graph

4.  After processing all questions, sets state-level routing flags:
      review_required       – True if ANY question fails the AUTO_APPROVE threshold
      review_question_ids   – list of question_ids that need human review

The LLM call is wrapped with the same tenacity retry used in retrieval.

Structured output
─────────────────
We use `ChatOpenAI.with_structured_output(SynthesisOutput)` which invokes
OpenAI function-calling under the hood – the model is constrained to return
a JSON object conforming to the Pydantic schema.  This eliminates parse errors
and gives deterministic field types.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from rfp_responder.config import settings
from rfp_responder.graph import AUTO_APPROVE_VECTOR_THRESHOLD
from rfp_responder.state import (
    DraftedAnswer,
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
# LLM structured output schema
# ─────────────────────────────────────────────────────────────────────────────

class SynthesisOutput(BaseModel):
    """
    Schema enforced on every LLM response via function-calling.
    The model MUST return all fields; there is no optional ambiguity.
    """

    proposed_answer: str = Field(
        description="The complete, ready-to-submit answer to the security question."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "0.0–1.0 confidence that the proposed answer is accurate based on the "
            "evidence provided. Use the cosine similarity as a prior."
        ),
    )
    discrepancy_detected: bool = Field(
        description=(
            "True if the historical answer contradicts the current infrastructure state "
            "reported by the graph (e.g., historical says 'encryption enabled' but graph "
            "shows the KMS key is inactive)."
        ),
    )
    reasoning: str = Field(
        description="One-paragraph explanation of how the answer was derived. Used for audit."
    )


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a senior security engineer completing a vendor security questionnaire.
Your task is to produce accurate, professional answers based on the provided evidence.

Rules:
- Ground every claim in the supplied evidence; do not invent unseen facts.
- If historical evidence and the live infrastructure graph disagree, set discrepancy_detected=true
  and explain both states in the reasoning field.
- Keep answers concise (1-3 sentences) and compliance-framework-aware.
- confidence should reflect your certainty: 1.0 = perfect evidence, 0.0 = no relevant evidence."""


# ─────────────────────────────────────────────────────────────────────────────
# Retry decorator
# ─────────────────────────────────────────────────────────────────────────────

_LLM_RETRY = retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential_jitter(
        initial=settings.retry_initial_wait_seconds,
        max=settings.retry_max_wait_seconds,
    ),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# LLM client (module-level; lazily constructed on first call)
# ─────────────────────────────────────────────────────────────────────────────

_llm: ChatOpenAI | None = None


def _get_structured_llm():
    """Return a ChatOpenAI instance bound to SynthesisOutput structured output."""
    global _llm
    if _llm is None:
        base_llm = ChatOpenAI(
            model=settings.synthesis_model,
            temperature=0,          # deterministic for security answers
            api_key=settings.openai_api_key,
        )
        _llm = base_llm.with_structured_output(SynthesisOutput)
    return _llm


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_message(
    question: QuestionItem,
    vector: VectorRetrievalResult | None,
    graph: GraphRetrievalResult | None,
) -> str:
    """Compose the per-question evidence block shown to the LLM."""

    lines: list[str] = [
        f"## Security Question",
        f"Category: {question.category}",
        f"Control ID: {question.control_id or 'N/A'}",
        f"Question: {question.question_text}",
        "",
    ]

    # ── Historical evidence ───────────────────────────────────────────────────
    if vector:
        lines += [
            f"## Historical Evidence  (similarity: {vector.cosine_similarity:.3f})",
            f"Past question : {vector.matched_question}",
            f"Past answer   : {vector.matched_answer}",
            f"Source doc    : {vector.source_document_id}",
            "",
        ]
    else:
        lines += [
            "## Historical Evidence",
            "No prior answer found for this question in the knowledge base.",
            "",
        ]

    # ── Live infrastructure state ─────────────────────────────────────────────
    if graph:
        lines += [
            f"## Live Infrastructure State  (from Neo4j graph)",
            f"Component     : {graph.component_name}  ({graph.component_type})",
            f"Active        : {graph.is_active}",
            f"Compliant     : {graph.is_compliant}",
            f"Frameworks    : {', '.join(graph.compliance_frameworks) or 'None recorded'}",
            "",
        ]
    else:
        lines += [
            "## Live Infrastructure State",
            "No matching infrastructure component found in the architecture graph.",
            "",
        ]

    lines.append(
        "Using the above evidence, answer the security question. "
        "Set confidence based on evidence quality and flag any discrepancy."
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Per-question synthesizer
# ─────────────────────────────────────────────────────────────────────────────

@_LLM_RETRY
async def _synthesize_one(
    question: QuestionItem,
    bundle: RetrievalBundle,
) -> DraftedAnswer:
    """Invoke the LLM and return a fully-populated DraftedAnswer."""
    llm = _get_structured_llm()

    user_msg = _build_user_message(question, bundle.vector_result, bundle.graph_result)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    output: SynthesisOutput = await llm.ainvoke(messages)

    # Derive the effective vector confidence:
    # If the LLM raised confidence it means the model has high internal certainty
    # even without a strong vector match.  We take the minimum of both to be
    # conservative – both signals must be strong for AUTO_APPROVE.
    vector_sim = bundle.vector_result.cosine_similarity if bundle.vector_result else 0.0
    effective_confidence = min(output.confidence, vector_sim if vector_sim > 0 else output.confidence)

    graph_verified = bool(
        bundle.graph_result
        and bundle.graph_result.is_active
        and bundle.graph_result.is_compliant
    )

    # Token counts – LangChain structured output returns usage in response_metadata.
    usage = getattr(output, "response_metadata", {}).get("token_usage", {})

    return DraftedAnswer(
        question_id=question.question_id,
        proposed_answer=output.proposed_answer,
        vector_confidence=effective_confidence,
        graph_verified=graph_verified,
        discrepancy_detected=output.discrepancy_detected,
        reasoning_trace=output.reasoning,
        model_used=settings.synthesis_model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public node
# ─────────────────────────────────────────────────────────────────────────────

async def draft_response(state: RFPState) -> dict[str, Any]:
    """
    LangGraph node: draft_response

    For each question with a retrieval bundle, calls the LLM to produce a
    DraftedAnswer.  After all questions are processed, evaluates the routing
    condition and sets `review_required` and `review_question_ids`.

    Returns partial state update:
      drafted_answers      – dict[question_id, DraftedAnswer dict]
      review_required      – bool
      review_question_ids  – list[str]
      workflow_status      – WorkflowStatus.AWAITING_REVIEW or COMPILING
    """
    tenant_id = state["tenant_id"]
    thread_id = state["thread_id"]
    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id)

    # Hydrate models from state dicts
    questions_map: dict[str, QuestionItem] = {
        q["question_id"]: QuestionItem.model_validate(q)
        for q in state.get("questions", [])
    }
    bundles_raw: dict[str, Any] = state.get("retrieval_bundles", {})

    if not bundles_raw:
        log.warning("draft_response: no retrieval bundles found, skipping synthesis")
        return {
            "workflow_status": WorkflowStatus.AWAITING_REVIEW,
            "review_required": False,
            "review_question_ids": [],
        }

    log.info("draft_response starting", question_count=len(bundles_raw))

    # Fan out synthesis calls concurrently (one per question)
    async def _safe_synthesize(qid: str, bundle_dict: dict) -> tuple[str, DraftedAnswer | Exception]:
        question = questions_map.get(qid)
        if question is None:
            return qid, ValueError(f"No QuestionItem found for id={qid}")
        bundle = RetrievalBundle.model_validate(bundle_dict)
        try:
            result = await _synthesize_one(question, bundle)
            return qid, result
        except Exception as exc:
            return qid, exc

    tasks = [_safe_synthesize(qid, bd) for qid, bd in bundles_raw.items()]
    raw_results = await asyncio.gather(*tasks)

    drafted_answers: dict[str, Any] = {}
    review_required = False
    review_question_ids: list[str] = []

    for qid, result in raw_results:
        if isinstance(result, Exception):
            log.error("Synthesis failed for question", question_id=qid, error=str(result))
            # Force review on synthesis failure – never silently drop a question
            review_required = True
            review_question_ids.append(qid)
            continue

        draft: DraftedAnswer = result

        # ── AUTO-APPROVE gate ─────────────────────────────────────────────────
        needs_review: bool = (
            draft.vector_confidence < AUTO_APPROVE_VECTOR_THRESHOLD
            or not draft.graph_verified
            or draft.discrepancy_detected
        )

        if needs_review:
            review_required = True
            review_question_ids.append(qid)

        drafted_answers[qid] = draft.model_dump()

        log.info(
            "Question drafted",
            question_id=qid,
            confidence=round(draft.vector_confidence, 3),
            graph_verified=draft.graph_verified,
            discrepancy=draft.discrepancy_detected,
            needs_review=needs_review,
        )

    next_status = (
        WorkflowStatus.AWAITING_REVIEW if review_required else WorkflowStatus.COMPILING
    )

    log.info(
        "draft_response complete",
        total=len(bundles_raw),
        review_required=review_required,
        review_count=len(review_question_ids),
    )

    return {
        "drafted_answers": drafted_answers,
        "review_required": review_required,
        "review_question_ids": review_question_ids,
        "workflow_status": next_status,
    }
