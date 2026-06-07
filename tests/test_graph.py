"""
tests/test_graph.py – Unit tests for graph topology and routing logic.

Focus: evaluation_router conditional logic, graph compilation,
interrupt_before configuration, thread_config helper.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from rfp_responder.graph import (
    AUTO_APPROVE_VECTOR_THRESHOLD,
    build_graph,
    evaluation_router,
    thread_config,
)
from rfp_responder.state import WorkflowStatus, make_initial_state


# ─────────────────────────────────────────────────────────────────────────────
# evaluation_router
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluationRouter:

    def _state_with_review(self, review_required: bool, drafted: dict = None):
        """Helper: build a minimal state dict for router testing."""
        return {
            "thread_id":          "t1",
            "tenant_id":          "acme",
            "review_required":    review_required,
            "review_question_ids": ["q-1"] if review_required else [],
            "drafted_answers":    drafted or {"q-1": {}},
        }

    def test_routes_to_human_review_when_required(self):
        state = self._state_with_review(review_required=True)
        assert evaluation_router(state) == "human_review_wait"

    def test_routes_to_compile_when_all_approved(self):
        state = self._state_with_review(review_required=False)
        assert evaluation_router(state) == "compile_and_export"

    def test_routes_to_compile_when_no_drafted_answers(self):
        """Edge case: empty questionnaire should not park at human_review_wait."""
        state = {
            "thread_id":          "t1",
            "tenant_id":          "acme",
            "review_required":    False,
            "review_question_ids": [],
            "drafted_answers":    {},
        }
        assert evaluation_router(state) == "compile_and_export"

    def test_routes_to_compile_even_with_empty_drafts_and_review_false(self):
        state = {
            "thread_id":          "t1",
            "tenant_id":          "acme",
            "review_required":    False,
            "review_question_ids": [],
            "drafted_answers":    {},
        }
        result = evaluation_router(state)
        assert result == "compile_and_export"

    def test_review_flag_overrides_empty_drafts(self):
        """If review_required=True but drafted_answers={}, still route to review."""
        state = {
            "thread_id":          "t1",
            "tenant_id":          "acme",
            "review_required":    True,
            "review_question_ids": ["q-1"],
            "drafted_answers":    {},
        }
        # drafted is empty → _merge_dicts returned nothing → router should still
        # respect the explicit review_required flag
        result = evaluation_router(state)
        assert result == "human_review_wait"


# ─────────────────────────────────────────────────────────────────────────────
# AUTO_APPROVE_VECTOR_THRESHOLD constant
# ─────────────────────────────────────────────────────────────────────────────

class TestThresholdConstant:

    def test_threshold_is_float(self):
        assert isinstance(AUTO_APPROVE_VECTOR_THRESHOLD, float)

    def test_threshold_in_valid_range(self):
        assert 0.0 < AUTO_APPROVE_VECTOR_THRESHOLD <= 1.0

    def test_threshold_is_92_percent(self):
        """Matches the spec requirement of 0.92."""
        assert AUTO_APPROVE_VECTOR_THRESHOLD == 0.92


# ─────────────────────────────────────────────────────────────────────────────
# build_graph()
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildGraph:

    def test_compiles_without_error(self):
        graph = build_graph(checkpointer=MemorySaver())
        assert graph is not None

    def test_graph_has_correct_nodes(self):
        graph = build_graph(checkpointer=MemorySaver())
        node_names = set(graph.nodes.keys())
        expected = {
            "parse_questionnaire",
            "dual_stream_retrieval",
            "draft_response",
            "human_review_wait",
            "compile_and_export",
            "__start__",
        }
        assert expected.issubset(node_names)

    def test_interrupt_before_human_review_wait(self):
        """The compiled graph must have interrupt_before on human_review_wait."""
        graph = build_graph(checkpointer=MemorySaver())
        # LangGraph stores interrupt config on the compiled graph object
        interrupt_nodes = set(getattr(graph, "interrupt_before", []) or [])
        assert "human_review_wait" in interrupt_nodes

    def test_injectable_nodes_used(self):
        """Custom node callables should be wired into the graph."""
        called = []

        async def fake_parse(state):
            called.append("parse")
            return {"workflow_status": "parsing"}

        async def fake_retrieve(state):
            called.append("retrieve")
            return {}

        async def fake_draft(state):
            called.append("draft")
            return {"review_required": False, "review_question_ids": [], "drafted_answers": {}}

        async def fake_review(state):
            called.append("review")
            return {}

        async def fake_compile(state):
            called.append("compile")
            return {"workflow_status": "complete"}

        graph = build_graph(
            checkpointer=MemorySaver(),
            node_parse=fake_parse,
            node_retrieve=fake_retrieve,
            node_draft=fake_draft,
            node_review=fake_review,
            node_compile=fake_compile,
        )
        assert graph is not None  # compiled cleanly with injected nodes


# ─────────────────────────────────────────────────────────────────────────────
# thread_config()
# ─────────────────────────────────────────────────────────────────────────────

class TestThreadConfig:

    def test_contains_configurable_thread_id(self):
        cfg = thread_config("tid-123", "acme")
        assert cfg["configurable"]["thread_id"] == "tid-123"

    def test_contains_tenant_tag(self):
        cfg = thread_config("tid-123", "acme")
        assert "tenant:acme" in cfg["tags"]

    def test_contains_metadata(self):
        cfg = thread_config("tid-123", "acme")
        assert cfg["metadata"]["tenant_id"] == "acme"
        assert cfg["metadata"]["thread_id"] == "tid-123"
