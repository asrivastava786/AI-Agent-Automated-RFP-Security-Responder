"""
tests/conftest.py – Shared pytest fixtures.

Fixture design principles
──────────────────────────
1. Every fixture that touches a real network resource is mocked.
   Tests must run offline with no credentials in the environment.

2. The `thread_id` and `tenant_id` fixtures are deterministic strings so
   UUID5-derived IDs are stable and can be asserted against.

3. The `initial_state` fixture calls `make_initial_state()` – the same
   factory used by production request handlers – so state shape divergence
   is caught immediately.

4. The `app_client` fixture overrides the `get_graph` FastAPI dependency
   with a test double, so route tests never touch real LangGraph state.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from rfp_responder.state import RFPState, WorkflowStatus, make_initial_state

# ─────────────────────────────────────────────────────────────────────────────
# Deterministic identity fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tenant_id() -> str:
    return "test-tenant-acme"


@pytest.fixture
def thread_id() -> str:
    return "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def questionnaire_id() -> str:
    return "rfp-acme-2024-q4"


# ─────────────────────────────────────────────────────────────────────────────
# State fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def raw_json_payload() -> dict[str, Any]:
    return {
        "format": "json",
        "questions": [
            {
                "question_text": "Do you support SAML SSO for enterprise customers?",
                "category": "Authentication",
                "control_id": "SOC2-CC6.1",
            },
            {
                "question_text": "Is data encrypted at rest using AES-256?",
                "category": "Encryption",
                "control_id": "SOC2-CC6.7",
            },
            {
                "question_text": "Do you perform automated vulnerability scanning?",
                "category": "Vulnerability Management",
            },
        ],
    }


@pytest.fixture
def initial_state(
    tenant_id: str,
    thread_id: str,
    questionnaire_id: str,
    raw_json_payload: dict,
) -> RFPState:
    return make_initial_state(
        tenant_id=tenant_id,
        thread_id=thread_id,
        questionnaire_id=questionnaire_id,
        raw_payload=raw_json_payload,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Mock external clients
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_settings_env(monkeypatch):
    """Inject dummy env vars so Settings() doesn't fail on missing secrets."""
    monkeypatch.setenv("OPENAI_API_KEY",      "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY",   "sk-ant-test")
    monkeypatch.setenv("NEO4J_PASSWORD",      "testpassword")
    monkeypatch.setenv("POSTGRES_DSN",        "postgresql+psycopg://user:pass@localhost:5432/test")
    monkeypatch.setenv("LANGCHAIN_API_KEY",   "ls__test")
    monkeypatch.setenv("AUTH_SECRET",         "test-secret-32-bytes-long-enough!")


@pytest.fixture
def mock_qdrant_client():
    with patch("rfp_responder.clients._qdrant") as mock:
        client = AsyncMock()
        mock.__class__ = type(client)
        yield client


@pytest.fixture
def mock_neo4j_driver():
    with patch("rfp_responder.clients._neo4j") as mock:
        driver = AsyncMock()
        mock.__class__ = type(driver)
        yield driver


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI test client with graph dependency override
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_graph():
    """
    A MagicMock that mimics the CompiledStateGraph interface.
    Tests configure return values per-scenario.
    """
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    graph.aget_state = AsyncMock()
    graph.aupdate_state = AsyncMock()
    return graph


@pytest.fixture
def app_client(mock_graph) -> TestClient:
    """
    FastAPI TestClient with:
    - get_graph dependency overridden to return mock_graph
    - lifespan disabled (we don't want real DB connections in tests)
    """
    from rfp_responder.app.main import app
    from rfp_responder.app.lifespan import get_graph

    app.dependency_overrides[get_graph] = lambda: mock_graph

    # Set app.state.graph so the dependency doesn't raise
    app.state.graph = mock_graph

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client

    app.dependency_overrides.clear()
