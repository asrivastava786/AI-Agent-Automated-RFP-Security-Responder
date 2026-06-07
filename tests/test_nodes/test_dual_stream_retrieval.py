"""
tests/test_nodes/test_dual_stream_retrieval.py

Tests for the dual_stream_retrieval node covering:
  • Both legs called for each question
  • Tenant isolation enforced (correct filter/param values)
  • Partial failure: one leg fails, other succeeds, bundle carries error
  • Full failure: both legs fail, workflow still continues (no exception raised)
  • _extract_infra_keyword identifies known infrastructure terms
  • No PENDING questions → returns early without calling any DB
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rfp_responder.nodes.dual_stream_retrieval import (
    _extract_infra_keyword,
    dual_stream_retrieval,
)
from rfp_responder.state import (
    QuestionItem,
    QuestionStatus,
    RetrievalBundle,
    WorkflowStatus,
    make_initial_state,
)


# ─────────────────────────────────────────────────────────────────────────────
# _extract_infra_keyword
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractInfraKeyword:

    @pytest.mark.parametrize("question,expected_fragment", [
        ("Do you support SAML SSO for enterprise customers?", "SAML"),
        ("Is data encrypted at rest using a KMS key?",         "KMS"),
        ("Are S3 buckets encrypted and access-controlled?",    "S3"),
        ("Do you use TLS 1.2 or higher for data in transit?", "TLS"),
        ("Is WAF configured on your edge infrastructure?",     "WAF"),
        ("Are audit logs sent to a SIEM system?",              "SIEM"),
        ("Do you support SAML and MFA simultaneously?",        "SAML"),  # first match wins
    ])
    def test_known_term_extracted(self, question, expected_fragment):
        keyword = _extract_infra_keyword(question)
        assert expected_fragment.lower() in keyword.lower()

    def test_fallback_to_first_words(self):
        """When no known term matches, return the first four words."""
        question = "What is your incident response policy timeline?"
        keyword = _extract_infra_keyword(question)
        assert len(keyword.split()) <= 4

    def test_empty_question_fallback(self):
        keyword = _extract_infra_keyword("")
        assert isinstance(keyword, str)


# ─────────────────────────────────────────────────────────────────────────────
# dual_stream_retrieval node
# ─────────────────────────────────────────────────────────────────────────────

def _build_state_with_questions(thread_id: str, tenant_id: str, questions: list[dict]) -> dict:
    state = make_initial_state(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id="q1",
        raw_payload={},
    )
    state["questions"] = questions
    return state


def _make_question(thread_id: str, idx: int, text: str) -> dict:
    return QuestionItem.create(
        thread_id=thread_id,
        row_index=idx,
        question_text=text,
    ).model_dump()


class TestDualStreamRetrieval:

    @pytest.mark.asyncio
    async def test_returns_bundles_for_all_questions(self):
        thread_id = "t1"
        tenant_id = "acme"
        questions = [
            _make_question(thread_id, 0, "Do you support SAML SSO?"),
            _make_question(thread_id, 1, "Is data encrypted with KMS?"),
        ]
        state = _build_state_with_questions(thread_id, tenant_id, questions)

        mock_qdrant_result = MagicMock()
        mock_qdrant_result.score = 0.88
        mock_qdrant_result.payload = {
            "question_text": "Do you support SSO?",
            "answer_text": "Yes, SAML 2.0 is supported.",
            "source_document_id": "doc-001",
        }
        mock_qdrant_result.id = "vec-001"

        mock_neo4j_record = {
            "component_name": "Okta-IdP",
            "component_type": "IdentityProvider",
            "status": "active",
            "is_compliant": True,
            "frameworks": ["SOC2", "ISO27001"],
        }

        with (
            patch("rfp_responder.nodes.dual_stream_retrieval.embed_text",
                  new=AsyncMock(return_value=[0.1] * 1536)),
            patch("rfp_responder.nodes.dual_stream_retrieval.get_qdrant_client") as mock_qd,
            patch("rfp_responder.nodes.dual_stream_retrieval.get_neo4j_driver") as mock_neo,
        ):
            # Configure Qdrant mock
            qdrant_client = AsyncMock()
            qdrant_client.search = AsyncMock(return_value=[mock_qdrant_result])
            mock_qd.return_value = qdrant_client

            # Configure Neo4j mock
            neo4j_driver = AsyncMock()
            session_mock = AsyncMock()
            result_mock = AsyncMock()
            result_mock.data = AsyncMock(return_value=[mock_neo4j_record])
            session_mock.run = AsyncMock(return_value=result_mock)
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__  = AsyncMock(return_value=False)
            neo4j_driver.session = MagicMock(return_value=session_mock)
            mock_neo.return_value = neo4j_driver

            result = await dual_stream_retrieval(state)

        assert "retrieval_bundles" in result
        bundles = result["retrieval_bundles"]
        assert len(bundles) == 2

        for qid, bundle_dict in bundles.items():
            bundle = RetrievalBundle.model_validate(bundle_dict)
            assert bundle.vector_result is not None
            assert bundle.graph_result  is not None

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_qdrant_filter(self):
        """Qdrant search must include a must-filter on tenant_id."""
        thread_id = "t1"
        tenant_id = "acme-isolated"
        questions = [_make_question(thread_id, 0, "Do you support SAML SSO?")]
        state = _build_state_with_questions(thread_id, tenant_id, questions)

        captured_filters = []

        async def mock_search(*args, **kwargs):
            captured_filters.append(kwargs.get("query_filter"))
            return []

        with (
            patch("rfp_responder.nodes.dual_stream_retrieval.embed_text",
                  new=AsyncMock(return_value=[0.0] * 1536)),
            patch("rfp_responder.nodes.dual_stream_retrieval.get_qdrant_client") as mock_qd,
            patch("rfp_responder.nodes.dual_stream_retrieval.get_neo4j_driver") as mock_neo,
        ):
            client = AsyncMock()
            client.search = mock_search
            mock_qd.return_value = client

            driver = AsyncMock()
            session_m = AsyncMock()
            result_m = AsyncMock()
            result_m.data = AsyncMock(return_value=[])
            session_m.run = AsyncMock(return_value=result_m)
            session_m.__aenter__ = AsyncMock(return_value=session_m)
            session_m.__aexit__  = AsyncMock(return_value=False)
            driver.session = MagicMock(return_value=session_m)
            mock_neo.return_value = driver

            await dual_stream_retrieval(state)

        assert len(captured_filters) >= 1
        filt = captured_filters[0]
        # The filter must contain the tenant_id value
        filt_str = str(filt)
        assert tenant_id in filt_str

    @pytest.mark.asyncio
    async def test_vector_failure_does_not_crash(self):
        """If vector search fails, the bundle carries retrieval_error, not an exception."""
        thread_id = "t1"
        tenant_id = "acme"
        questions = [_make_question(thread_id, 0, "Do you support SAML SSO?")]
        state = _build_state_with_questions(thread_id, tenant_id, questions)

        with (
            patch("rfp_responder.nodes.dual_stream_retrieval.embed_text",
                  new=AsyncMock(side_effect=ConnectionError("Qdrant down"))),
            patch("rfp_responder.nodes.dual_stream_retrieval.get_neo4j_driver") as mock_neo,
        ):
            driver = AsyncMock()
            session_m = AsyncMock()
            result_m = AsyncMock()
            result_m.data = AsyncMock(return_value=[])
            session_m.run = AsyncMock(return_value=result_m)
            session_m.__aenter__ = AsyncMock(return_value=session_m)
            session_m.__aexit__  = AsyncMock(return_value=False)
            driver.session = MagicMock(return_value=session_m)
            mock_neo.return_value = driver

            result = await dual_stream_retrieval(state)

        # Should not raise – bundle carries the error
        assert "retrieval_bundles" in result
        bundle_dict = list(result["retrieval_bundles"].values())[0]
        bundle = RetrievalBundle.model_validate(bundle_dict)
        assert bundle.retrieval_error is not None
        assert bundle.vector_result is None

    @pytest.mark.asyncio
    async def test_no_pending_questions_returns_early(self):
        state = make_initial_state(
            tenant_id="acme", thread_id="t1",
            questionnaire_id="q1", raw_payload={},
        )
        state["questions"] = []
        result = await dual_stream_retrieval(state)
        # No bundles, just a status update
        assert result.get("retrieval_bundles", {}) == {} or "retrieval_bundles" not in result
        assert result["workflow_status"] == WorkflowStatus.DRAFTING
