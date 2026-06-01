# Capacity Command — Shared test fixtures

import os
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

# Ensure app.backend is importable
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))


# ── Canned data for mock DB ─────────────────────────────────────────────

def _header_metrics():
    return {
        "total_census": 87,
        "total_beds": 130,
        "ed_waiting": 12,
        "ed_boarders": 3,
        "avg_wait_min": 28.5,
        "beds_available": 43,
        "or_active": 5,
        "or_total": 8,
        "clock": "2026-02-13T14:30:00Z",
        "staleness_seconds": 12,
    }


def _dept_occupancy():
    return {"ICU": 70.0, "TELE": 68.0, "MEDSURG": 65.0, "PEDS": 50.0, "ED": 80.0}


def _beds_rows():
    return [
        {
            "bed_id": "ICU-01",
            "dept_id": "ICU",
            "dept_name": "Intensive Care Unit",
            "status": "occupied",
            "acuity": 2,
            "hours_in_bed": 14.5,
            "expected_discharge": "2026-02-13T18:00:00Z",
            "blocked_reason": None,
        },
        {
            "bed_id": "ICU-02",
            "dept_id": "ICU",
            "dept_name": "Intensive Care Unit",
            "status": "available",
            "acuity": None,
            "hours_in_bed": None,
            "expected_discharge": None,
            "blocked_reason": None,
        },
    ]


def _ed_metrics():
    return {
        "waiting_count": 12,
        "boarders_count": 3,
        "avg_wait_min": 28.5,
        "lwbs_rate_pct": 1.2,
        "arrivals_per_hour": [5.5, 7.0, 8.5, 7.0],
        "esi_breakdown": {"1": 0, "2": 3, "3": 6, "4": 2, "5": 1},
    }


def _or_metrics():
    return {
        "utilization_pct": 62.5,
        "fcots_pct": 75.0,
        "rooms": [
            {
                "room_id": "OR-1",
                "status": "in_progress",
                "procedure": "Cardiac",
                "surgeon_specialty": "Cardiothoracic",
                "elapsed_min": 45,
                "est_remaining_min": 75,
                "scheduled_start": "2026-02-13T08:00:00Z",
            },
        ],
        "active_procedures": [
            {"room_id": "OR-1", "procedure": "Cardiac", "surgeon_specialty": "Cardiothoracic"},
        ],
        "next_available_eta": "2026-02-13T15:30:00Z",
    }


def _forecast_data():
    return {
        "predicted_discharges_2h": 3,
        "predicted_discharges_4h": 7,
        "arrival_forecast": [
            {"hour": 14, "predicted": 6.5, "actual": 7},
            {"hour": 15, "predicted": 6.5, "actual": None},
        ],
        "capacity_by_dept": [
            {"dept_id": "ICU", "current_available": 6, "predicted_2h": 8, "confidence": 0.65},
        ],
        "anomalies": [
            {
                "type": "CAPACITY_WARNING",
                "dept": "MEDSURG",
                "value": 87.5,
                "severity": "WARNING",
                "confidence": 0.85,
            },
        ],
    }


def _events_rows():
    return [
        {
            "event_id": "evt-001",
            "event_time": "2026-02-13T14:30:12Z",
            "entity_type": "bed",
            "entity_id": "ICU-01",
            "old_status": "occupied",
            "new_status": "cleaning",
        },
    ], "2026-02-13T14:30:12Z"


def _dept_metrics_rows():
    return [
        {
            "dept_id": "ICU",
            "name": "Intensive Care Unit",
            "occupancy_pct": 70.0,
            "total_beds": 20,
            "available": 6,
            "occupied": 14,
            "cleaning": 0,
            "blocked": 0,
        },
    ]


def _patient_details():
    return {
        "patient_id": "PAT-00001",
        "mrn": "MRN1234567",
        "name": "Jane Smith",
        "age": 72,
        "gender": "F",
        "arrival_id": "ED-001",
        "bed_id": "ICU-01",
        "dept_id": "ICU",
        "length_of_stay_days": 3.2,
        "acuity_score": 2,
        "comorbidity_count": 4,
        "ed_visits_6mo": 2,
        "lace_score": 12,
        "lace_risk": "HIGH",
        "predicted_discharge_time": "2026-03-07T14:00:00Z",
        "discharge_reason": "Home",
        "confidence_score": 0.78,
        "patient_admission_time": "2026-03-03T10:15:00Z",
        "patient_updated_at": "2026-03-06T12:00:00Z",
        "esi_level": 2,
        "arrival_time": "2026-03-03T09:45:00Z",
        "disposition": "admitted",
        "wait_minutes": 30,
    }


def _bed_details_response():
    return {
        "bed": {
            "bed_id": "ICU-01",
            "dept_id": "ICU",
            "status": "occupied",
            "acuity_level": 2,
            "admission_time": "2026-03-03T10:15:00Z",
            "expected_discharge": "2026-03-07T14:00:00Z",
            "blocked_reason": None,
        },
        "patient": _patient_details(),
        "events": [
            {
                "event_id": "evt-001",
                "event_time": "2026-03-03T10:15:00Z",
                "entity_type": "bed",
                "entity_id": "ICU-01",
                "old_status": "available",
                "new_status": "occupied",
            },
        ],
        "department": {"dept_id": "ICU", "name": "Intensive Care Unit", "floor": 3, "total_beds": 20},
        "timing": {
            "bed_ms": 2,
            "patient_ms": 3,
            "events_ms": 4,
            "dept_ms": 1,
            "total_ms": 10,
            "sql_warehouse_est_ms": 8000,
        },
    }


# ── Mock DB connection ─────────────────────────────────────────────────

class MockConnection:
    """Mock DB connection that returns canned data."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return MagicMock()


def _mock_get_db():
    """Yield a mock connection for dependency injection."""
    conn = MagicMock()
    yield conn


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_queries():
    """Patch queries module to return canned data (async mocks for asyncpg queries)."""
    with patch("app.backend.main.queries") as mq:
        mq.get_header_metrics = AsyncMock(return_value=_header_metrics())
        mq.get_dept_occupancy = AsyncMock(return_value=_dept_occupancy())
        mq.get_beds = AsyncMock(return_value=_beds_rows())
        mq.get_ed_metrics = AsyncMock(return_value=_ed_metrics())
        mq.get_or_metrics = AsyncMock(return_value=_or_metrics())
        mq.get_forecast_data = AsyncMock(return_value=_forecast_data())
        events, last_time = _events_rows()
        mq.get_recent_events = AsyncMock(return_value=(events, last_time))
        mq.get_dept_metrics = AsyncMock(return_value=_dept_metrics_rows())
        mq.mark_bed_ready = AsyncMock(return_value={
            "success": True,
            "bed_id": "ICU-01",
            "status": "available",
            "blocked": False,
            "unblocked_at": "2026-03-06T14:33:00Z",
            "unblocked_by": "operator",
            "write_latency_ms": 198,
        })
        mq.block_bed = AsyncMock(return_value={
            "success": True,
            "bed_id": "MED-12",
            "previous_status": "available",
            "status": "blocked",
            "blocked_reason": "Infection Control",
            "blocked_at": "2026-03-06T14:32:01Z",
            "blocked_by": "charge_nurse",
            "write_latency_ms": 312,
        })
        mq.log_operator_write = AsyncMock(return_value=None)
        mq.get_last_operator_write = AsyncMock(return_value=None)
        mq.get_operator_write_count = AsyncMock(return_value=0)
        mq.get_patient_details = AsyncMock(return_value=_patient_details())
        mq.get_bed_details = AsyncMock(return_value=_bed_details_response())
        yield mq


@pytest.fixture
async def client(mock_queries):
    """AsyncClient for testing FastAPI app."""
    from app.backend.main import app
    from app.backend.db import get_db

    async def mock_get_db_override():
        yield MagicMock()

    app.dependency_overrides[get_db] = mock_get_db_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def mock_queries_db_error():
    """Patch queries module to simulate database errors (asyncpg.PostgresError)."""
    err = asyncpg.PostgresError("connection refused")
    with patch("app.backend.main.queries") as mq:
        mq.get_header_metrics = AsyncMock(side_effect=err)
        mq.get_dept_occupancy = AsyncMock(side_effect=err)
        mq.get_beds = AsyncMock(side_effect=err)
        mq.get_ed_metrics = AsyncMock(side_effect=err)
        mq.get_or_metrics = AsyncMock(side_effect=err)
        mq.get_forecast_data = AsyncMock(side_effect=err)
        mq.get_recent_events = AsyncMock(side_effect=err)
        mq.get_dept_metrics = AsyncMock(side_effect=err)
        mq.mark_bed_ready = AsyncMock(side_effect=err)
        mq.block_bed = AsyncMock(side_effect=err)
        mq.log_operator_write = AsyncMock(return_value=None)
        mq.get_last_operator_write = AsyncMock(return_value=None)
        mq.get_operator_write_count = AsyncMock(return_value=0)
        mq.get_patient_details = AsyncMock(side_effect=err)
        mq.get_bed_details = AsyncMock(side_effect=err)
        yield mq


@pytest.fixture
async def client_with_db_error(mock_queries_db_error):
    """AsyncClient that simulates database errors (queries raise asyncpg.PostgresError)."""
    from app.backend.main import app
    from app.backend.db import get_db

    async def mock_get_db_override():
        yield MagicMock()

    app.dependency_overrides[get_db] = mock_get_db_override
    # raise_app_exceptions=False so we get the 503 response instead of the exception
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
