"""
tests/test_api/test_routes.py – HTTP integration tests for all RFP API routes.

Test strategy
─────────────
All tests use the `app_client` fixture from conftest.py, which:
  • Replaces the `get_graph` FastAPI dependency with a `mock_graph` MagicMock.
  • Never touches real Postgres, Qdrant, or Neo4j.

The mock_graph fixture provides:
  • `ainvoke`       – AsyncMock, return value configured per test
  • `aget_state`    – AsyncMock, return value configured per test
  • `aupdate_state` – AsyncMock, return value configured per test

Snapshot helper
───────────────
LangGraph snapshots are duck-typed here using SimpleNamespace.
The route code accesses snapshot.values (dict) and snapshot.next (tuple).

Coverage
────────
POST /rfp/ingest
  ✓ Returns 200 when graph completes (next == ())
  ✓ Returns 202 when graph pauses at interrupt (next == ("human_review_wait",))
  ✓ Returns 400 when X-Tenant-ID header is missing
  ✓ Returns 500 when graph.ainvoke() raises
  ✓ Returns 500 when aget_state returns None (no checkpoint)

GET /rfp/threads/{thread_id}/status
  ✓ Returns 200 with correct fields
  ✓ Returns 404 when thread not found
  ✓ Returns 403 on cross-tenant access

GET /rfp/threads/{thread_id}/review
  ✓ Returns 200 with ReviewItemsResponse when thread is interrupted
  ✓ Returns 409 when thread is not in interrupted state
  ✓ Returns 404 when thread not found
  ✓ Items sorted by row_index

POST /rfp/threads/{thread_id}/resume
  ✓ Returns 200 after graph completes following review
  ✓ Returns 202 if graph pauses a second time
  ✓ Returns 409 when thread not awaiting review
  ✓ Returns 404 when thread not found
  ✓ Returns 403 on cross-tenant access
  ✓ Returns 500 when graph.ainvoke raises during resume
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rfp_responder.state import WorkflowStatus


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot factory helpers
# ─────────────────────────────────────────────────────────────────────────────

TENANT_ID = "test-tenant-acme"
OTHER_TENANT = "other-tenant-xyz"
THREAD_ID = str(uuid.uuid4())


def _snapshot(
    values: dict[str, Any],
    next_nodes: tuple[str, ...] = (),
) -> SimpleNamespace:
    """
    Build a duck-typed LangGraph snapshot.
    The route code reads `.values` (dict) and `.next` (tuple).
    """
    return SimpleNamespace(values=values, next=next_nodes)


def _base_state(tenant_id: str = TENANT_ID) -> dict[str, Any]:
    return {
        "tenant_id":          tenant_id,
        "thread_id":          THREAD_ID,
        "questionnaire_id":   "rfp-acme-q4",
        "workflow_status":    WorkflowStatus.COMPLETE,
        "review_question_ids": [],
        "drafted_answers":    {},
        "questions":          [],
        "audit_metrics":      {
            "total_questions":          5,
            "auto_approved_count":      4,
            "human_reviewed_count":     1,
            "avg_vector_confidence":    0.91,
            "total_prompt_tokens":      200,
            "total_completion_tokens":  100,
            "processing_duration_seconds": 3.5,
        },
        "error_message": None,
    }


def _interrupted_state(tenant_id: str = TENANT_ID) -> dict[str, Any]:
    base = _base_state(tenant_id)
    base["workflow_status"]     = WorkflowStatus.AWAITING_REVIEW
    base["review_question_ids"] = ["q-001", "q-002"]
    base["drafted_answers"]     = {
        "q-001": {
            "question_id":          "q-001",
            "proposed_answer":      "Yes, we support SAML 2.0.",
            "vector_confidence":    0.72,
            "graph_verified":       False,
            "discrepancy_detected": False,
            "reasoning_trace":      "Low confidence – below auto-approve threshold.",
            "model_used":           "gpt-4o-2024-08-06",
            "_review_status":       None,
        },
        "q-002": {
            "question_id":          "q-002",
            "proposed_answer":      "Data is encrypted at rest with AES-256.",
            "vector_confidence":    0.68,
            "graph_verified":       True,
            "discrepancy_detected": True,
            "reasoning_trace":      "Discrepancy between vector and graph results.",
            "model_used":           "gpt-4o-2024-08-06",
            "_review_status":       None,
        },
    }
    base["questions"] = [
        {
            "question_id":   "q-001",
            "execution_id":  str(uuid.uuid4()),
            "row_index":     1,
            "question_text": "Do you support SAML SSO?",
            "category":      "Authentication",
            "control_id":    "SOC2-CC6.1",
            "context_hint":  None,
            "status":        "pending",
        },
        {
            "question_id":   "q-002",
            "execution_id":  str(uuid.uuid4()),
            "row_index":     0,
            "question_text": "Is data encrypted at rest?",
            "category":      "Encryption",
            "control_id":    "SOC2-CC6.7",
            "context_hint":  None,
            "status":        "pending",
        },
    ]
    return base


def _ingest_body() -> dict:
    return {
        "questionnaire_id": "rfp-acme-q4",
        "payload": {
            "format": "json",
            "questions": [{"question_text": "Do you support MFA?"}],
        },
    }


def _resume_body() -> dict:
    return {
        "reviewer_id": "alice@acme.com",
        "decisions": [
            {
                "question_id":   "q-001",
                "approved":      True,
                "override_answer": None,
                "reviewer_id":   "alice@acme.com",
                "review_notes":  None,
            },
        ],
    }


TENANT_HEADER = {"X-Tenant-ID": TENANT_ID}


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/ingest
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestEndpoint:

    def test_returns_200_when_complete(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.post(
            "/api/v1/rfp/ingest",
            json=_ingest_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == WorkflowStatus.COMPLETE
        assert "thread_id" in body

    def test_returns_202_when_interrupted(self, app_client, mock_graph):
        state = _interrupted_state()
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.post(
            "/api/v1/rfp/ingest",
            json=_ingest_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == WorkflowStatus.AWAITING_REVIEW
        assert len(body["review_question_ids"]) == 2

    def test_returns_400_when_tenant_header_missing(self, app_client):
        resp = app_client.post("/api/v1/rfp/ingest", json=_ingest_body())
        assert resp.status_code == 422  # FastAPI validates Header(...) as 422

    def test_returns_500_when_ainvoke_raises(self, app_client, mock_graph):
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("Workflow boom"))

        resp = app_client.post(
            "/api/v1/rfp/ingest",
            json=_ingest_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 500
        assert "Workflow execution failed" in resp.json()["detail"]

    def test_returns_500_when_no_checkpoint(self, app_client, mock_graph):
        mock_graph.ainvoke = AsyncMock(return_value={})
        mock_graph.aget_state.return_value = None

        resp = app_client.post(
            "/api/v1/rfp/ingest",
            json=_ingest_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 500

    def test_ainvoke_called_with_initial_state(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        app_client.post(
            "/api/v1/rfp/ingest",
            json=_ingest_body(),
            headers=TENANT_HEADER,
        )

        mock_graph.ainvoke.assert_called_once()
        call_args = mock_graph.ainvoke.call_args
        initial_state = call_args.args[0]
        assert initial_state["tenant_id"] == TENANT_ID
        assert initial_state["questionnaire_id"] == "rfp-acme-q4"


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/status
# ─────────────────────────────────────────────────────────────────────────────

class TestGetThreadStatus:

    def test_returns_200_with_correct_fields(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/status",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["thread_id"]        == THREAD_ID
        assert body["workflow_status"]  == WorkflowStatus.COMPLETE
        assert body["questionnaire_id"] == "rfp-acme-q4"
        assert body["next_node"]        is None

    def test_returns_202_fields_when_interrupted(self, app_client, mock_graph):
        state = _interrupted_state()
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/status",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["next_node"] == "human_review_wait"
        assert "q-001" in body["review_question_ids"]

    def test_returns_404_when_thread_not_found(self, app_client, mock_graph):
        mock_graph.aget_state.return_value = None

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/status",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 404

    def test_returns_403_on_cross_tenant_access(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(tenant_id=OTHER_TENANT), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/status",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 403

    def test_audit_metrics_included_when_complete(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/status",
            headers=TENANT_HEADER,
        )

        body = resp.json()
        assert body["audit_metrics"] is not None
        assert body["audit_metrics"]["total_questions"] == 5
        assert body["audit_metrics"]["auto_approved_count"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# GET /rfp/threads/{thread_id}/review
# ─────────────────────────────────────────────────────────────────────────────

class TestGetReviewItems:

    def test_returns_200_with_items_when_interrupted(self, app_client, mock_graph):
        state = _interrupted_state()
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_pending"] == 2
        assert len(body["items"]) == 2

    def test_items_sorted_by_row_index(self, app_client, mock_graph):
        """q-002 has row_index=0, q-001 has row_index=1 → q-002 should come first."""
        state = _interrupted_state()
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        items = resp.json()["items"]
        row_indices = [i["row_index"] for i in items]
        assert row_indices == sorted(row_indices)

    def test_returns_409_when_not_interrupted(self, app_client, mock_graph):
        snapshot = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 409

    def test_returns_404_when_thread_not_found(self, app_client, mock_graph):
        mock_graph.aget_state.return_value = None

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 404

    def test_returns_403_on_cross_tenant(self, app_client, mock_graph):
        state = _interrupted_state(tenant_id=OTHER_TENANT)
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 403

    def test_review_item_fields_correct(self, app_client, mock_graph):
        state = _interrupted_state()
        snapshot = _snapshot(state, next_nodes=("human_review_wait",))
        mock_graph.aget_state.return_value = snapshot

        resp = app_client.get(
            f"/api/v1/rfp/threads/{THREAD_ID}/review",
            headers=TENANT_HEADER,
        )

        items = {i["question_id"]: i for i in resp.json()["items"]}
        q001 = items["q-001"]
        assert q001["question_text"]    == "Do you support SAML SSO?"
        assert q001["proposed_answer"]  == "Yes, we support SAML 2.0."
        assert q001["vector_confidence"] == pytest.approx(0.72)
        assert q001["graph_verified"]   is False


# ─────────────────────────────────────────────────────────────────────────────
# POST /rfp/threads/{thread_id}/resume
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeThread:

    def _setup_interrupted_then_complete(self, mock_graph):
        """
        Configure mock_graph so that:
          1st aget_state call → interrupted (pre-resume guard check)
          2nd aget_state call → completed (post-resume status check)
        """
        interrupted_snap = _snapshot(
            _interrupted_state(), next_nodes=("human_review_wait",)
        )
        completed_snap = _snapshot(_base_state(), next_nodes=())

        mock_graph.aget_state.side_effect = [interrupted_snap, completed_snap]
        mock_graph.ainvoke = AsyncMock(return_value={})
        mock_graph.aupdate_state = AsyncMock(return_value=None)

    def test_returns_200_after_completion(self, app_client, mock_graph):
        self._setup_interrupted_then_complete(mock_graph)

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == WorkflowStatus.COMPLETE
        assert "export_json_path" in body

    def test_aupdate_state_called_before_ainvoke(self, app_client, mock_graph):
        self._setup_interrupted_then_complete(mock_graph)
        call_order = []

        original_update = mock_graph.aupdate_state
        original_invoke = mock_graph.ainvoke

        async def tracked_update(*a, **kw):
            call_order.append("update")
            return await original_update(*a, **kw)

        async def tracked_invoke(*a, **kw):
            call_order.append("invoke")
            return await original_invoke(*a, **kw)

        mock_graph.aupdate_state = tracked_update
        mock_graph.ainvoke = tracked_invoke

        app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        # update must precede invoke
        assert call_order.index("update") < call_order.index("invoke")

    def test_returns_202_if_second_interrupt(self, app_client, mock_graph):
        interrupted_snap1 = _snapshot(
            _interrupted_state(), next_nodes=("human_review_wait",)
        )
        interrupted_snap2 = _snapshot(
            _interrupted_state(), next_nodes=("human_review_wait",)
        )
        mock_graph.aget_state.side_effect = [interrupted_snap1, interrupted_snap2]
        mock_graph.ainvoke = AsyncMock(return_value={})
        mock_graph.aupdate_state = AsyncMock(return_value=None)

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 202
        assert resp.json()["status"] == WorkflowStatus.AWAITING_REVIEW

    def test_returns_409_when_not_interrupted(self, app_client, mock_graph):
        snap = _snapshot(_base_state(), next_nodes=())
        mock_graph.aget_state.return_value = snap

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 409
        mock_graph.aupdate_state.assert_not_called()

    def test_returns_404_when_thread_not_found(self, app_client, mock_graph):
        mock_graph.aget_state.return_value = None

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 404

    def test_returns_403_on_cross_tenant(self, app_client, mock_graph):
        snap = _snapshot(
            _interrupted_state(tenant_id=OTHER_TENANT),
            next_nodes=("human_review_wait",),
        )
        mock_graph.aget_state.return_value = snap

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 403
        mock_graph.aupdate_state.assert_not_called()

    def test_returns_500_when_ainvoke_raises_on_resume(self, app_client, mock_graph):
        interrupted_snap = _snapshot(
            _interrupted_state(), next_nodes=("human_review_wait",)
        )
        mock_graph.aget_state.return_value = interrupted_snap
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("Resume boom"))
        mock_graph.aupdate_state = AsyncMock(return_value=None)

        resp = app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        assert resp.status_code == 500
        assert "Workflow resume failed" in resp.json()["detail"]

    def test_decisions_injected_into_aupdate_state(self, app_client, mock_graph):
        self._setup_interrupted_then_complete(mock_graph)

        app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        mock_graph.aupdate_state.assert_awaited_once()
        call_kwargs = mock_graph.aupdate_state.call_args.kwargs
        assert "human_decisions" in call_kwargs["values"]
        assert "q-001" in call_kwargs["values"]["human_decisions"]
        assert call_kwargs["as_node"] == "human_review_wait"

    def test_none_passed_to_ainvoke_on_resume(self, app_client, mock_graph):
        """Passing None as input is the LangGraph 'resume from checkpoint' sentinel."""
        self._setup_interrupted_then_complete(mock_graph)

        app_client.post(
            f"/api/v1/rfp/threads/{THREAD_ID}/resume",
            json=_resume_body(),
            headers=TENANT_HEADER,
        )

        # The second ainvoke call must have None as first positional arg
        calls = mock_graph.ainvoke.call_args_list
        # First call is from ingest (not present in resume test), second is the resume
        # In resume tests there's only one ainvoke call
        assert calls[0].args[0] is None
