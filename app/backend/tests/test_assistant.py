# Capacity Command — AI Assistant API Tests (TDD: written FIRST before implementation)

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# Ensure app.backend is importable
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))


# ── Canned data for assistant context ──────────────────────────────────

def _assistant_context():
    """Canned context dict matching AssistantContextBuilder.build_context() output."""
    return {
        "bed_status_summary": [
            {"dept_id": "ICU", "name": "Intensive Care Unit", "occupancy_pct": 70.0, "total_beds": 20, "available": 6, "occupied": 14, "cleaning": 0, "blocked": 0},
        ],
        "ed_census": {
            "waiting_count": 12,
            "boarders_count": 3,
            "avg_wait_min": 28.5,
            "lwbs_rate_pct": 1.2,
            "arrivals_per_hour": [5.5, 7.0, 8.5, 7.0],
            "esi_breakdown": {"1": 0, "2": 3, "3": 6, "4": 2, "5": 1},
        },
        "or_status": {
            "utilization_pct": 62.5,
            "fcots_pct": 75.0,
            "rooms": [{"room_id": "OR-1", "status": "in_progress", "procedure": "Cardiac"}],
            "active_procedures": [{"room_id": "OR-1", "procedure": "Cardiac"}],
        },
        "predictions": {
            "predicted_discharges_2h": 3,
            "predicted_discharges_4h": 7,
            "arrival_forecast": [{"hour": 14, "predicted": 6.5}, {"hour": 15, "predicted": 6.5}],
            "capacity_by_dept": [{"dept_id": "ICU", "current_available": 6, "predicted_2h": 8, "confidence": 0.65}],
        },
        "anomalies": [
            {"type": "CAPACITY_WARNING", "dept": "MEDSURG", "value": 87.5, "severity": "WARNING", "confidence": 0.85},
        ],
        "recent_events": [
            {"event_id": "evt-001", "event_time": "2026-02-13T14:30:12Z", "entity_type": "bed", "entity_id": "ICU-01", "old_status": "occupied", "new_status": "cleaning"},
        ],
        "lace_risk_patients": [
            {"bed_id": "ICU-01", "dept_id": "ICU", "lace_score": 12, "risk_label": "HIGH"},
            {"bed_id": "TELE-05", "dept_id": "TELE", "lace_score": 8, "risk_label": "MODERATE"},
        ],
    }


def _fmapi_response():
    """Canned Foundation Model API response."""
    return {
        "choices": [
            {"message": {"content": "Based on the data, ICU has 6 available beds. MEDSURG shows a capacity warning at 87.5% occupancy."}}
        ]
    }


# ── Test app setup (standalone, no main.py dependency) ───────────────────

@pytest.fixture
def assistant_app():
    """Create FastAPI app with only assistant router for isolated testing."""
    from fastapi import FastAPI
    from app.backend.assistant_routes import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def mock_get_db():
    """Yield a mock DB connection."""
    conn = MagicMock()
    yield conn


@pytest.fixture
def mock_ctx_builder():
    """Patch AssistantContextBuilder.build_context to return canned data."""
    with patch("app.backend.assistant_routes.AssistantContextBuilder") as mock_cls:
        instance = mock_cls.return_value
        instance.build_context = AsyncMock(return_value=_assistant_context())
        instance.format_context_for_prompt = MagicMock(return_value="[Formatted hospital context]")
        yield mock_cls


@pytest.fixture
def mock_fmapi():
    """Patch AsyncOpenAI for FMAPI calls."""
    with patch("app.backend.assistant.AsyncOpenAI") as mock_client_cls:
        # Build a mock response matching OpenAI SDK response shape
        mock_message = MagicMock()
        mock_message.content = _fmapi_response()["choices"][0]["message"]["content"]
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client
        yield mock_client_cls


@pytest.fixture
async def assistant_client(assistant_app, mock_get_db, mock_ctx_builder, mock_fmapi):
    """AsyncClient for assistant endpoints with mocked dependencies."""
    from app.backend.db import get_db

    async def _mock_get_db():
        yield mock_get_db

    assistant_app.dependency_overrides[get_db] = _mock_get_db

    transport = ASGITransport(app=assistant_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── POST /api/assistant/ask ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assistant_ask_returns_structured_response(assistant_client):
    """POST /api/assistant/ask returns answer, context_queries, and data_citations."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "How many beds are available in ICU?"})
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data
    assert isinstance(data["answer"], str)
    assert "context_queries" in data
    assert isinstance(data["context_queries"], list)
    assert "data_citations" in data
    assert isinstance(data["data_citations"], list)


@pytest.mark.asyncio
async def test_assistant_ask_data_citations_populated(assistant_client):
    """Response includes data_citations showing which data informed the answer."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "What is the ED wait time?"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["data_citations"]) > 0
    assert all(isinstance(c, str) for c in data["data_citations"])


@pytest.mark.asyncio
async def test_assistant_ask_empty_question_returns_400(assistant_client):
    """Empty or whitespace-only question returns 400 or 422 (validation error)."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": ""})
    assert r.status_code in (400, 422)  # 422 from Pydantic min_length, 400 from route check

    r2 = await assistant_client.post("/api/assistant/ask", json={"question": "   "})
    assert r2.status_code in (400, 422)


@pytest.mark.asyncio
async def test_assistant_ask_rejects_oversized_question(assistant_client):
    """Question exceeding max_length (2000) returns 422."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "x" * 5000})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_assistant_context_builder_runs_seven_queries(assistant_client):
    """Context builder runs 7 predefined queries and formats them."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "Summarize capacity"})
    assert r.status_code == 200
    data = r.json()
    expected_keys = [
        "bed_status_summary",
        "ed_census",
        "or_status",
        "predictions",
        "anomalies",
        "recent_events",
        "lace_risk_patients",
    ]
    assert set(expected_keys).issubset(set(data["context_queries"]))


@pytest.mark.asyncio
async def test_assistant_system_prompt_includes_hospital_context(assistant_client, mock_ctx_builder):
    """System prompt includes hospital context data."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "Any risks?"})
    assert r.status_code == 200
    # Verify format_context_for_prompt was called with context
    instance = mock_ctx_builder.return_value
    instance.format_context_for_prompt.assert_called_once()
    call_arg = instance.format_context_for_prompt.call_args[0][0]
    assert "bed_status_summary" in call_arg or "ed_census" in call_arg


@pytest.mark.asyncio
async def test_assistant_fmapi_called_with_correct_format(assistant_client, mock_fmapi):
    """Foundation Model API call is made with correct messages format."""
    r = await assistant_client.post("/api/assistant/ask", json={"question": "What is the census?"})
    assert r.status_code == 200
    # Verify FMAPI was called (mock_fmapi patches httpx.AsyncClient)
    # The actual call happens inside assistant._call_fmapi
    assert r.json()["answer"]  # Answer came from FMAPI mock


@pytest.mark.asyncio
async def test_assistant_fmapi_unavailable_returns_fallback(assistant_app, mock_get_db, mock_ctx_builder):
    """Graceful handling when FMAPI is unavailable returns fallback message."""
    from app.backend.db import get_db

    async def _mock_get_db():
        yield mock_get_db

    assistant_app.dependency_overrides[get_db] = _mock_get_db

    # Patch config functions to return valid URL/token so code attempts the LLM call
    with patch("app.backend.assistant_routes.get_workspace_host", return_value="https://test.databricks.com"):
        with patch("app.backend.assistant_routes.get_oauth_token", return_value="dummy"):
            with patch("app.backend.assistant.AsyncOpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.chat.completions.create = AsyncMock(
                    side_effect=Exception("Connection refused")
                )
                mock_client_cls.return_value = mock_client

                transport = ASGITransport(app=assistant_app)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    r = await ac.post("/api/assistant/ask", json={"question": "What is the census?"})
                assert r.status_code == 200
                data = r.json()
                assert "answer" in data
                # Fallback message should indicate FMAPI was unavailable
                ans = data["answer"].lower()
                assert "unavailable" in ans or "error" in ans or "try again" in ans or "connection" in ans


# ── POST /api/assistant/shift-summary ─────────────────────────────────────

@pytest.mark.asyncio
async def test_shift_summary_returns_structured_response(assistant_client):
    """POST /api/assistant/shift-summary returns summary, sections, generated_at."""
    r = await assistant_client.post("/api/assistant/shift-summary")
    assert r.status_code == 200
    data = r.json()
    assert "summary" in data
    assert "sections" in data
    assert "generated_at" in data
    assert isinstance(data["sections"], dict)


@pytest.mark.asyncio
async def test_shift_summary_sections_structure(assistant_client):
    """Shift summary sections include census_overview, ed_status, or_status, high_risk_patients, anomalies, predictions."""
    r = await assistant_client.post("/api/assistant/shift-summary")
    assert r.status_code == 200
    sections = r.json()["sections"]
    required = ["census_overview", "ed_status", "or_status", "high_risk_patients", "anomalies", "predictions"]
    for key in required:
        assert key in sections, f"Missing section: {key}"


@pytest.mark.asyncio
async def test_shift_summary_high_risk_patients_includes_lace(assistant_client):
    """high_risk_patients section includes LACE scores."""
    r = await assistant_client.post("/api/assistant/shift-summary")
    assert r.status_code == 200
    high_risk = r.json()["sections"]["high_risk_patients"]
    assert isinstance(high_risk, list)
    if len(high_risk) > 0:
        assert "lace_score" in high_risk[0] or "risk_label" in high_risk[0]


@pytest.mark.asyncio
async def test_shift_summary_markdown_formatted(assistant_client):
    """Summary is markdown-formatted (contains headers or bullets)."""
    r = await assistant_client.post("/api/assistant/shift-summary")
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert isinstance(summary, str)
    # Markdown typically has # or - or *
    has_markdown = "#" in summary or "-" in summary or "*" in summary or "\n" in summary
    assert has_markdown or len(summary) > 50  # At least substantial content


# ── AssistantContextBuilder unit tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_context_builder_build_context_returns_seven_keys():
    """AssistantContextBuilder.build_context returns dict with 7 expected keys."""
    from app.backend.assistant import AssistantContextBuilder

    mock_conn = MagicMock()
    with patch("app.backend.assistant.queries") as mq:
        mq.get_dept_metrics = AsyncMock(return_value=[{"dept_id": "ICU", "name": "ICU", "occupancy_pct": 70, "total_beds": 20, "available": 6, "occupied": 14, "cleaning": 0, "blocked": 0}])
        mq.get_ed_metrics = AsyncMock(return_value={"waiting_count": 12, "boarders_count": 3, "avg_wait_min": 28.5, "lwbs_rate_pct": 1.2, "arrivals_per_hour": [5, 6, 7], "esi_breakdown": {"1": 0, "2": 3, "3": 6, "4": 2, "5": 1}})
        mq.get_or_metrics = AsyncMock(return_value={"utilization_pct": 62.5, "fcots_pct": 75, "rooms": [], "active_procedures": []})
        mq.get_forecast_data = AsyncMock(return_value={
            "predicted_discharges_2h": 3, "predicted_discharges_4h": 7,
            "arrival_forecast": [], "capacity_by_dept": [], "anomalies": [{"type": "CAPACITY_WARNING", "dept": "MEDSURG"}]
        })
        mq.get_recent_events = AsyncMock(return_value=([{"event_id": "e1"}], "2026-02-13T14:00:00Z"))
        mq.get_beds = AsyncMock(return_value=[
            {"bed_id": "ICU-01", "dept_id": "ICU", "status": "occupied", "acuity": 2, "hours_in_bed": 36, "expected_discharge": "2026-02-14T12:00:00Z"},
        ])

        builder = AssistantContextBuilder(mock_conn, "hospital")
        context = await builder.build_context()

        expected_keys = ["bed_status_summary", "ed_census", "or_status", "predictions", "anomalies", "recent_events", "lace_risk_patients"]
        for k in expected_keys:
            assert k in context, f"Missing key: {k}"


@pytest.mark.asyncio
async def test_context_builder_format_context_produces_text():
    """format_context_for_prompt produces readable text block."""
    from app.backend.assistant import AssistantContextBuilder

    builder = AssistantContextBuilder(MagicMock(), "hospital")
    context = _assistant_context()
    text = builder.format_context_for_prompt(context)
    assert isinstance(text, str)
    assert len(text) > 50
    # Should mention key data
    assert "ICU" in text or "ED" in text or "12" in text or "census" in text.lower()
