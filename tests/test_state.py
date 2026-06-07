"""
tests/test_state.py – Unit tests for state models and factories.

Focus: idempotency guarantees, UUID5 derivation, initial state shape.
"""

import uuid

import pytest

from rfp_responder.state import (
    AuditMetrics,
    DraftedAnswer,
    FinalAnswer,
    HumanReviewDecision,
    QuestionItem,
    QuestionStatus,
    RetrievalBundle,
    RFPState,
    WorkflowStatus,
    VectorRetrievalResult,
    make_initial_state,
    _merge_dicts,
)


# ─────────────────────────────────────────────────────────────────────────────
# QuestionItem.create() – idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestQuestionItemCreate:

    def test_deterministic_question_id(self):
        """Same (thread_id, row_index) always produces the same question_id."""
        item1 = QuestionItem.create(
            thread_id="t1", row_index=0, question_text="Q?"
        )
        item2 = QuestionItem.create(
            thread_id="t1", row_index=0, question_text="Q?"
        )
        assert item1.question_id == item2.question_id

    def test_different_row_index_different_id(self):
        item0 = QuestionItem.create(thread_id="t1", row_index=0, question_text="Q?")
        item1 = QuestionItem.create(thread_id="t1", row_index=1, question_text="Q?")
        assert item0.question_id != item1.question_id

    def test_different_thread_id_different_id(self):
        item_a = QuestionItem.create(thread_id="t1", row_index=0, question_text="Q?")
        item_b = QuestionItem.create(thread_id="t2", row_index=0, question_text="Q?")
        assert item_a.question_id != item_b.question_id

    def test_execution_id_is_uuid5(self):
        """execution_id must be a valid UUID string."""
        item = QuestionItem.create(thread_id="t1", row_index=0, question_text="Q?")
        # This will raise ValueError if not a valid UUID
        parsed = uuid.UUID(item.execution_id)
        assert parsed.version == 5

    def test_execution_id_differs_from_question_id(self):
        item = QuestionItem.create(thread_id="t1", row_index=0, question_text="Q?")
        assert item.question_id != item.execution_id

    def test_question_text_preserved(self):
        text = "Is all data encrypted at rest using AES-256?"
        item = QuestionItem.create(thread_id="t1", row_index=0, question_text=text)
        assert item.question_text == text

    def test_optional_fields_passed_through(self):
        item = QuestionItem.create(
            thread_id="t1",
            row_index=0,
            question_text="Q?",
            category="Encryption",
            control_id="SOC2-CC6.7",
            context_hint="Sub-section: KMS",
        )
        assert item.category == "Encryption"
        assert item.control_id == "SOC2-CC6.7"
        assert item.context_hint == "Sub-section: KMS"

    def test_default_status_is_pending(self):
        item = QuestionItem.create(thread_id="t1", row_index=0, question_text="Q?")
        assert item.status == QuestionStatus.PENDING


# ─────────────────────────────────────────────────────────────────────────────
# make_initial_state()
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeInitialState:

    def test_returns_typed_dict(self, initial_state):
        assert isinstance(initial_state, dict)

    def test_identity_fields_set(self, initial_state, tenant_id, thread_id, questionnaire_id):
        assert initial_state["tenant_id"]       == tenant_id
        assert initial_state["thread_id"]       == thread_id
        assert initial_state["questionnaire_id"] == questionnaire_id

    def test_collections_start_empty(self, initial_state):
        assert initial_state["questions"]          == []
        assert initial_state["retrieval_bundles"]  == {}
        assert initial_state["drafted_answers"]    == {}
        assert initial_state["human_decisions"]    == {}
        assert initial_state["final_answers"]      == {}
        assert initial_state["messages"]           == []

    def test_routing_signals_false(self, initial_state):
        assert initial_state["review_required"]      is False
        assert initial_state["review_question_ids"]  == []

    def test_initial_workflow_status(self, initial_state):
        assert initial_state["workflow_status"] == WorkflowStatus.INITIALISED

    def test_raw_payload_preserved(self, initial_state, raw_json_payload):
        assert initial_state["raw_payload"] == raw_json_payload

    def test_error_fields_none(self, initial_state):
        assert initial_state["error_message"]   is None
        assert initial_state["langsmith_run_id"] is None
        assert initial_state["audit_metrics"]    is None

    def test_retry_count_zero(self, initial_state):
        assert initial_state["retry_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# _merge_dicts reducer
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeDictsReducer:

    def test_merges_disjoint_keys(self):
        a = {"k1": "v1"}
        b = {"k2": "v2"}
        result = _merge_dicts(a, b)
        assert result == {"k1": "v1", "k2": "v2"}

    def test_b_overwrites_a_on_conflict(self):
        result = _merge_dicts({"k": "old"}, {"k": "new"})
        assert result["k"] == "new"

    def test_original_dicts_not_mutated(self):
        a = {"x": 1}
        b = {"y": 2}
        _merge_dicts(a, b)
        assert a == {"x": 1}
        assert b == {"y": 2}

    def test_empty_b_returns_copy_of_a(self):
        a = {"k": "v"}
        result = _merge_dicts(a, {})
        assert result == a

    def test_empty_a_returns_copy_of_b(self):
        b = {"k": "v"}
        result = _merge_dicts({}, b)
        assert result == b


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic model round-trips (serialise → validate)
# ─────────────────────────────────────────────────────────────────────────────

class TestModelRoundTrips:

    def test_drafted_answer_roundtrip(self):
        draft = DraftedAnswer(
            question_id="q-001",
            proposed_answer="Yes, we support SAML 2.0.",
            vector_confidence=0.95,
            graph_verified=True,
            discrepancy_detected=False,
            reasoning_trace="Strong vector match; Neo4j confirms IdP is active.",
            model_used="gpt-4o-2024-08-06",
        )
        restored = DraftedAnswer.model_validate(draft.model_dump())
        assert restored.question_id        == draft.question_id
        assert restored.vector_confidence  == draft.vector_confidence
        assert restored.graph_verified     == draft.graph_verified

    def test_human_review_decision_roundtrip(self):
        decision = HumanReviewDecision(
            question_id="q-001",
            approved=True,
            override_answer="Updated answer text.",
            reviewer_id="alice@acme.com",
        )
        restored = HumanReviewDecision.model_validate(decision.model_dump())
        assert restored.override_answer == "Updated answer text."
        assert restored.approved is True

    def test_vector_retrieval_confidence_bounds(self):
        """cosine_similarity must stay in [0, 1]."""
        with pytest.raises(Exception):
            VectorRetrievalResult(
                matched_question="Q",
                matched_answer="A",
                cosine_similarity=1.5,   # invalid
                source_document_id="doc-1",
                tenant_id="t1",
            )
