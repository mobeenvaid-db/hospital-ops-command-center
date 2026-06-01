# Capacity Command — Agent Routes tests
#
# Tests for /api/agents/* endpoints.

import os
import sys
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))


def _mock_capabilities():
    return [
        {
            "agent_id": "bed_capacity_planner",
            "name": "Bed Capacity Planner",
            "description": "Analyzes bed capacity gaps.",
            "triggers": ["bed", "capacity"],
        },
        {
            "agent_id": "or_utilization_optimizer",
            "name": "OR Utilization Optimizer",
            "description": "Optimizes OR scheduling.",
            "triggers": ["OR", "surgery"],
        },
        {
            "agent_id": "discharge_coordinator",
            "name": "Discharge Coordinator",
            "description": "Prioritizes discharges.",
            "triggers": ["discharge", "LACE"],
        },
        {
            "agent_id": "anomaly_pattern_detector",
            "name": "Anomaly Pattern Detector",
            "description": "Detects anomaly patterns.",
            "triggers": ["anomaly", "pattern"],
        },
    ]


@pytest.fixture
async def make_agent_client():
    """Factory that patches AgentClient and yields (AsyncClient, mock_instance)."""
    mock_instance = MagicMock()
    mock_instance.query = AsyncMock()
    mock_instance.health_check = AsyncMock()

    # Patch both possible import paths (bare module and package path)
    with patch("agent_client.AgentClient") as MockCls1, \
         patch.dict("sys.modules", {}):
        MockCls1.return_value = mock_instance
        MockCls1.get_capabilities = MagicMock(return_value=_mock_capabilities())

        # Also patch on the agent_routes module directly
        import agent_routes
        original_cls = agent_routes.AgentClient
        agent_routes.AgentClient = MockCls1

        from app.backend.main import app
        from app.backend.db import get_db

        async def mock_get_db_override():
            yield MagicMock()

        app.dependency_overrides[get_db] = mock_get_db_override
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, mock_instance

        app.dependency_overrides.clear()
        agent_routes.AgentClient = original_cls


# ── /api/agents/capabilities ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_capabilities_returns_all_agents(make_agent_client):
    ac, _ = make_agent_client
    resp = await ac.get("/api/agents/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 4
    ids = [a["agent_id"] for a in data]
    assert "bed_capacity_planner" in ids
    assert "anomaly_pattern_detector" in ids


@pytest.mark.asyncio
async def test_capabilities_have_triggers(make_agent_client):
    ac, _ = make_agent_client
    resp = await ac.get("/api/agents/capabilities")
    data = resp.json()
    for agent in data:
        assert "triggers" in agent
        assert isinstance(agent["triggers"], list)
        assert len(agent["triggers"]) > 0


# ── /api/agents/status ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_ready(make_agent_client):
    ac, mock = make_agent_client
    mock.health_check.return_value = {
        "status": "ready",
        "endpoint": "capacity-command-mas",
        "state": "READY",
    }
    resp = await ac.get("/api/agents/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["endpoint"] == "capacity-command-mas"


@pytest.mark.asyncio
async def test_status_unreachable(make_agent_client):
    ac, mock = make_agent_client
    mock.health_check.return_value = {
        "status": "unreachable",
        "endpoint": "capacity-command-mas",
        "error": "Connection refused",
    }
    resp = await ac.get("/api/agents/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unreachable"


# ── /api/agents/query ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_routes_to_bed_capacity(make_agent_client):
    ac, mock = make_agent_client
    mock.query.return_value = {
        "answer": "ICU has 3 available beds. Predicted gap: -2 in 2 hours.",
        "agent_used": "bed_capacity_planner",
        "tool_calls": [
            {"function": "bed_gap_calculator", "args": {"dept_id": "ICU"}, "result": None},
        ],
        "reasoning": "Query mentions beds and capacity.",
        "plan": {"status": "WARNING"},
    }

    resp = await ac.post("/api/agents/query", json={"query": "ICU bed capacity?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_used"] == "bed_capacity_planner"
    assert "ICU" in data["answer"]
    assert len(data["tool_calls"]) == 1


@pytest.mark.asyncio
async def test_query_empty_returns_422(make_agent_client):
    ac, _ = make_agent_client
    resp = await ac.post("/api/agents/query", json={"query": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_query_with_context(make_agent_client):
    ac, mock = make_agent_client
    mock.query.return_value = {
        "answer": "OR-3 is in use.",
        "agent_used": "or_utilization_optimizer",
        "tool_calls": [],
        "reasoning": "OR query.",
        "plan": {},
    }

    resp = await ac.post(
        "/api/agents/query",
        json={"query": "OR-3 status?", "context": {"dept": "OR"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_used"] == "or_utilization_optimizer"


@pytest.mark.asyncio
async def test_query_error_response(make_agent_client):
    ac, mock = make_agent_client
    mock.query.return_value = {
        "answer": "MAS endpoint unavailable after 3 attempts: timeout",
        "agent_used": "error",
        "tool_calls": [],
        "reasoning": "",
        "plan": {},
    }

    resp = await ac.post("/api/agents/query", json={"query": "Check beds"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_used"] == "error"
    assert "unavailable" in data["answer"]
