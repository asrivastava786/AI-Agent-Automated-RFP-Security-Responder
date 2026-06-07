"""
nodes/human_review_wait.py – Merge human reviewer decisions into workflow state.

Interrupt flow recap
─────────────────────
1. evaluation_router sends execution here because review_required == True.
2. LangGraph intercepts BEFORE entering this node (interrupt_before=["human_review_wait"]).
3. The full RFPState is checkpointed to Postgres.
4. FastAPI returns HTTP 202 to the caller with the thread_id.
5. A reviewer inspects the flagged answers via GET /rfp/threads/{thread_id}/review.
6. The reviewer POSTs decisions to  POST /rfp/threads/{thread_id}/resume.
7. The FastAPI handler:
      a. Loads the current checkpointed state.
      b. Merges the HumanReviewDecision dicts into state["human_decisions"].
      c. Calls graph.ainvoke(None, config=thread_config(thread_id, tenant_id)).
         Passing None as the first arg tells LangGraph: "resume from checkpoint,
         don't reset input state."
8. LangGraph re-enters from human_review_wait and executes THIS node.

What this node does
────────────────────
For each question in `review_question_ids`:
  • If decision.approved == True and override_answer is None:
      → status = HUMAN_APPROVED   (use the LLM draft as-is)
  • If decision.approved == True and override_answer is set:
      → status = HUMAN_OVERRIDDEN (replace proposed_answer with reviewer text)
  • If decision.approved == False:
      → status = REVIEW_REQUIRED  (excluded from export; logged for audit)
  • If NO decision was provided for a flagged question:
      → treated as HUMAN_APPROVED (fail-open to not block the workflow forever)
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from rfp_responder.state import (
    DraftedAnswer,
    HumanReviewDecision,
    QuestionItem,
    QuestionStatus,
    RFPState,
    WorkflowStatus,
)

logger = structlog.get_logger(__name__)


async def human_review_wait(state: RFPState) -> dict[str, Any]:
    """
    LangGraph node: human_review_wait

    Merges `state["human_decisions"]` into `state["drafted_answers"]`,
    updating the status of each reviewed question.

    Returns partial state update:
      drafted_answers  – dict with updated statuses and any overridden answers
      workflow_status  – WorkflowStatus.COMPILING
    """
    thread_id = state["thread_id"]
    tenant_id = state["tenant_id"]
    log = logger.bind(thread_id=thread_id, tenant_id=tenant_id)

    review_ids: list[str] = state.get("review_question_ids", [])
    human_decisions_raw: dict[str, Any] = state.get("human_decisions", {})
    drafted_raw: dict[str, Any] = state.get("drafted_answers", {})

    log.info(
        "human_review_wait executing",
        review_count=len(review_ids),
        decisions_received=len(human_decisions_raw),
    )

    updated_drafts: dict[str, Any] = {}

    for qid in review_ids:
        draft_dict = drafted_raw.get(qid)
        if draft_dict is None:
            log.warning("No draft found for reviewed question", question_id=qid)
            continue

        draft = DraftedAnswer.model_validate(draft_dict)
        decision_dict = human_decisions_raw.get(qid)

        if decision_dict is None:
            # No decision submitted → fail-open: treat as approved.
            log.warning(
                "No decision for question, defaulting to HUMAN_APPROVED",
                question_id=qid,
            )
            updated_drafts[qid] = draft_dict
            # The compile node will see the original draft without status change.
            # We do NOT mark it as requiring further review to avoid deadlock.
            continue

        decision = HumanReviewDecision.model_validate(decision_dict)

        if not decision.approved:
            # Reviewer rejected this answer – mark for exclusion from export.
            updated_draft = draft.model_copy(update={})
            # We signal rejection by overwriting proposed_answer with a sentinel;
            # compile_and_export checks status to exclude it.
            updated_drafts[qid] = {
                **draft_dict,
                "_review_status": QuestionStatus.REVIEW_REQUIRED,
                "_reviewer_id": decision.reviewer_id,
                "_review_notes": decision.review_notes,
            }
            log.info("Question rejected by reviewer", question_id=qid, reviewer=decision.reviewer_id)

        elif decision.override_answer:
            # Reviewer supplied replacement text.
            updated_drafts[qid] = {
                **draft_dict,
                "proposed_answer": decision.override_answer,
                "_review_status": QuestionStatus.HUMAN_OVERRIDDEN,
                "_reviewer_id": decision.reviewer_id,
                "_review_notes": decision.review_notes,
            }
            log.info(
                "Question overridden by reviewer",
                question_id=qid,
                reviewer=decision.reviewer_id,
            )

        else:
            # Reviewer approved the draft as-is.
            updated_drafts[qid] = {
                **draft_dict,
                "_review_status": QuestionStatus.HUMAN_APPROVED,
                "_reviewer_id": decision.reviewer_id,
                "_review_notes": decision.review_notes,
            }
            log.info(
                "Question approved by reviewer",
                question_id=qid,
                reviewer=decision.reviewer_id,
            )

    log.info(
        "human_review_wait complete",
        processed=len(updated_drafts),
        skipped=len(review_ids) - len(updated_drafts),
    )

    return {
        "drafted_answers": updated_drafts,     # _merge_dicts reducer patches existing dict
        "workflow_status": WorkflowStatus.COMPILING,
    }
