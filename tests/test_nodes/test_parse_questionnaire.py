"""
tests/test_nodes/test_parse_questionnaire.py

Tests for the parse_questionnaire node covering:
  • JSON format happy path
  • Excel format (with mocked openpyxl)
  • Blank row skipping
  • Missing question text
  • Idempotency: same input → same question_ids
  • Failed payload → FAILED status
"""

import base64
import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rfp_responder.nodes.parse_questionnaire import parse_questionnaire
from rfp_responder.state import QuestionStatus, WorkflowStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _state(payload: dict, thread_id="tid-1", tenant_id="t1", questionnaire_id="q1") -> dict:
    from rfp_responder.state import make_initial_state
    return make_initial_state(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id=questionnaire_id,
        raw_payload=payload,
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON format
# ─────────────────────────────────────────────────────────────────────────────

class TestParseJSON:

    @pytest.mark.asyncio
    async def test_parses_question_list(self, raw_json_payload, thread_id):
        state = _state(raw_json_payload, thread_id=thread_id)
        result = await parse_questionnaire(state)

        assert result["workflow_status"] == WorkflowStatus.RETRIEVING
        assert len(result["questions"]) == 3

    @pytest.mark.asyncio
    async def test_question_fields_populated(self, raw_json_payload, thread_id):
        state = _state(raw_json_payload, thread_id=thread_id)
        result = await parse_questionnaire(state)

        first = result["questions"][0]
        assert first["question_text"] == "Do you support SAML SSO for enterprise customers?"
        assert first["category"]      == "Authentication"
        assert first["control_id"]    == "SOC2-CC6.1"
        assert first["status"]        == QuestionStatus.PENDING

    @pytest.mark.asyncio
    async def test_idempotent_ids(self, raw_json_payload, thread_id):
        """Running parse twice on the same input must produce identical IDs."""
        state = _state(raw_json_payload, thread_id=thread_id)
        result1 = await parse_questionnaire(state)
        result2 = await parse_questionnaire(state)

        ids1 = [q["question_id"] for q in result1["questions"]]
        ids2 = [q["question_id"] for q in result2["questions"]]
        assert ids1 == ids2

    @pytest.mark.asyncio
    async def test_blank_rows_skipped(self, thread_id):
        payload = {
            "format": "json",
            "questions": [
                {"question_text": "Real question?"},
                {"question_text": ""},            # blank – should be skipped
                {"question_text": "   "},          # whitespace only – skipped
                {"question_text": "Another real?"},
            ],
        }
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert len(result["questions"]) == 2

    @pytest.mark.asyncio
    async def test_missing_questions_key(self, thread_id):
        payload = {"format": "json", "not_questions": []}
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["workflow_status"] == WorkflowStatus.FAILED
        assert result["error_message"] is not None

    @pytest.mark.asyncio
    async def test_questions_not_list(self, thread_id):
        payload = {"format": "json", "questions": "not a list"}
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["workflow_status"] == WorkflowStatus.FAILED

    @pytest.mark.asyncio
    async def test_unsupported_format(self, thread_id):
        payload = {"format": "csv"}
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["workflow_status"] == WorkflowStatus.FAILED

    @pytest.mark.asyncio
    async def test_default_category_when_missing(self, thread_id):
        payload = {"format": "json", "questions": [{"question_text": "Q?"}]}
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["questions"][0]["category"] == "General"


# ─────────────────────────────────────────────────────────────────────────────
# Excel format
# ─────────────────────────────────────────────────────────────────────────────

class TestParseExcel:

    def _make_excel_payload(self, rows: list[tuple]) -> dict:
        """Build a minimal base64-encoded .xlsx with given rows."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        # Header row
        ws.append(["Question", "Category", "Control ID", "Context / Notes"])
        for row in rows:
            ws.append(list(row))
        buf = io.BytesIO()
        wb.save(buf)
        encoded = base64.b64encode(buf.getvalue()).decode()
        return {"format": "excel", "file_content": encoded}

    @pytest.mark.asyncio
    async def test_parses_excel_rows(self, thread_id):
        payload = self._make_excel_payload([
            ("Do you support MFA?", "Authentication", "SOC2-CC6.1", None),
            ("Is data encrypted?",  "Encryption",     "SOC2-CC6.7", None),
        ])
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["workflow_status"] == WorkflowStatus.RETRIEVING
        assert len(result["questions"]) == 2

    @pytest.mark.asyncio
    async def test_excel_blank_rows_skipped(self, thread_id):
        payload = self._make_excel_payload([
            ("Real question?",  "Auth", None, None),
            (None,              None,   None, None),  # blank row
        ])
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert len(result["questions"]) == 1

    @pytest.mark.asyncio
    async def test_excel_missing_file_content(self, thread_id):
        payload = {"format": "excel"}
        result = await parse_questionnaire(_state(payload, thread_id=thread_id))
        assert result["workflow_status"] == WorkflowStatus.FAILED
