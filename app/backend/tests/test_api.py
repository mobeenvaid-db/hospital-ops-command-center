# Capacity Command — API Tests (TDD: written FIRST before implementation)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


# ── GET /api/health ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_200(client):
    """GET /api/health returns 200 OK."""
    r = await client.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_response_shape(client):
    """GET /api/health returns status, database, pool_size keys."""
    r = await client.get("/api/health")
    data = r.json()
    required = ["status", "database", "pool_size"]
    for key in required:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_health_degraded_when_no_db(client):
    """When pool is None (demo mode), status is degraded."""
    from app.backend import db
    # Ensure we're in demo mode (no PGHOST) — get_pool returns None
    with patch.object(db.db_pool, "get_pool", new_callable=AsyncMock, return_value=None):
        r = await client.get("/api/health")
    data = r.json()
    assert data["status"] == "degraded"
    assert data["database"] == "disconnected"
    assert data["pool_size"] == 0


@pytest.mark.asyncio
async def test_health_healthy_when_db_connected(client):
    """When pool exists, status is healthy and pool_size is returned."""
    from app.backend import db
    mock_pool = MagicMock()
    mock_pool.get_size = MagicMock(return_value=5)
    with patch.object(db.db_pool, "get_pool", new_callable=AsyncMock, return_value=mock_pool):
        r = await client.get("/api/health")
    data = r.json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"
    assert data["pool_size"] == 5


# ── GET /api/header ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_header_returns_200(client):
    """GET /api/header returns 200 OK."""
    r = await client.get("/api/header")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_header_response_shape(client):
    """GET /api/header returns all required keys."""
    r = await client.get("/api/header")
    data = r.json()
    required = [
        "system_status", "total_census", "total_beds", "ed_waiting", "ed_boarders",
        "avg_wait_min", "beds_available", "or_active", "or_total",
        "clock", "staleness_seconds",
    ]
    for key in required:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_header_system_status_valid(client):
    """system_status is one of NORMAL, CONSTRAINED, CRITICAL."""
    r = await client.get("/api/header")
    data = r.json()
    assert data["system_status"] in ("NORMAL", "CONSTRAINED", "CRITICAL")


@pytest.mark.asyncio
async def test_header_numeric_types(client):
    """Header numeric fields have correct types."""
    r = await client.get("/api/header")
    data = r.json()
    assert isinstance(data["total_census"], int)
    assert isinstance(data["total_beds"], int)
    assert isinstance(data["ed_waiting"], int)
    assert isinstance(data["ed_boarders"], int)
    assert isinstance(data["avg_wait_min"], (int, float))
    assert isinstance(data["beds_available"], int)
    assert isinstance(data["or_active"], int)
    assert isinstance(data["or_total"], int)
    assert isinstance(data["staleness_seconds"], (int, float))


@pytest.mark.asyncio
async def test_header_clock_iso8601(client):
    """clock is ISO 8601 format string."""
    r = await client.get("/api/header")
    data = r.json()
    assert "T" in data["clock"] and "Z" in data["clock"]


# ── GET /api/beds ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_beds_returns_200(client):
    """GET /api/beds returns 200 OK."""
    r = await client.get("/api/beds")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_beds_response_shape(client):
    """GET /api/beds returns departments with beds."""
    r = await client.get("/api/beds")
    data = r.json()
    assert "departments" in data
    assert isinstance(data["departments"], list)


@pytest.mark.asyncio
async def test_beds_department_structure(client):
    """Each department has dept_id, name, beds."""
    r = await client.get("/api/beds")
    data = r.json()
    for dept in data["departments"]:
        assert "dept_id" in dept
        assert "name" in dept
        assert "beds" in dept
        assert isinstance(dept["beds"], list)


@pytest.mark.asyncio
async def test_beds_bed_structure(client):
    """Each bed has bed_id, status, acuity, hours_in_bed, expected_discharge, blocked_reason."""
    r = await client.get("/api/beds")
    data = r.json()
    bed_keys = ["bed_id", "status", "acuity", "hours_in_bed", "expected_discharge", "blocked_reason"]
    for dept in data["departments"]:
        for bed in dept["beds"]:
            for key in bed_keys:
                assert key in bed, f"Bed missing key: {key}"
            assert bed["status"] in ("available", "occupied", "cleaning", "blocked")


# ── GET /api/ed ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ed_returns_200(client):
    """GET /api/ed returns 200 OK."""
    r = await client.get("/api/ed")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_ed_response_shape(client):
    """GET /api/ed returns all required keys."""
    r = await client.get("/api/ed")
    data = r.json()
    required = ["waiting_count", "boarders_count", "avg_wait_min", "lwbs_rate_pct", "arrivals_per_hour", "esi_breakdown"]
    for key in required:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_ed_esi_breakdown(client):
    """esi_breakdown has keys 1-5."""
    r = await client.get("/api/ed")
    data = r.json()
    for i in range(1, 6):
        assert str(i) in data["esi_breakdown"]
    assert isinstance(data["arrivals_per_hour"], list)


# ── GET /api/or ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_or_returns_200(client):
    """GET /api/or returns 200 OK."""
    r = await client.get("/api/or")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_or_response_shape(client):
    """GET /api/or returns utilization, rooms, fcots, active_procedures, next_available_eta."""
    r = await client.get("/api/or")
    data = r.json()
    required = ["utilization_pct", "fcots_pct", "rooms", "active_procedures", "next_available_eta"]
    for key in required:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_or_room_structure(client):
    """Each room has room_id, status, procedure, surgeon_specialty, elapsed_min, est_remaining_min, scheduled_start."""
    r = await client.get("/api/or")
    data = r.json()
    room_keys = ["room_id", "status", "procedure", "surgeon_specialty", "elapsed_min", "est_remaining_min", "scheduled_start"]
    for room in data["rooms"]:
        for key in room_keys:
            assert key in room, f"Room missing key: {key}"


# ── GET /api/forecast ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_forecast_returns_200(client):
    """GET /api/forecast returns 200 OK."""
    r = await client.get("/api/forecast")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_forecast_response_shape(client):
    """GET /api/forecast returns predictions, arrival_forecast, capacity_by_dept, anomalies."""
    r = await client.get("/api/forecast")
    data = r.json()
    required = ["predicted_discharges_2h", "predicted_discharges_4h", "arrival_forecast", "capacity_by_dept", "anomalies"]
    for key in required:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_forecast_arrival_forecast_items(client):
    """arrival_forecast items have hour, predicted, actual."""
    r = await client.get("/api/forecast")
    data = r.json()
    for item in data["arrival_forecast"]:
        assert "hour" in item
        assert "predicted" in item
        assert "actual" in item  # may be null


@pytest.mark.asyncio
async def test_forecast_capacity_by_dept_items(client):
    """capacity_by_dept items have dept_id, current_available, predicted_2h, confidence."""
    r = await client.get("/api/forecast")
    data = r.json()
    for item in data["capacity_by_dept"]:
        assert "dept_id" in item
        assert "current_available" in item
        assert "predicted_2h" in item
        assert "confidence" in item


@pytest.mark.asyncio
async def test_forecast_anomalies_structure(client):
    """anomalies items have type, dept, value, severity, confidence."""
    r = await client.get("/api/forecast")
    data = r.json()
    for a in data["anomalies"]:
        assert "type" in a
        assert "dept" in a
        assert "value" in a
        assert "severity" in a
        assert "confidence" in a


# ── GET /api/events ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_events_returns_200(client):
    """GET /api/events returns 200 OK."""
    r = await client.get("/api/events")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_events_response_shape(client):
    """GET /api/events returns events list and last_event_time."""
    r = await client.get("/api/events")
    data = r.json()
    assert "events" in data
    assert "last_event_time" in data
    assert isinstance(data["events"], list)


@pytest.mark.asyncio
async def test_events_item_structure(client):
    """Each event has event_id, event_time, entity_type, entity_id, old_status, new_status."""
    r = await client.get("/api/events")
    data = r.json()
    event_keys = ["event_id", "event_time", "entity_type", "entity_id", "old_status", "new_status"]
    for evt in data["events"]:
        for key in event_keys:
            assert key in evt, f"Event missing key: {key}"


@pytest.mark.asyncio
async def test_events_accepts_limit_param(client):
    """GET /api/events?limit=10 returns 200."""
    r = await client.get("/api/events?limit=10")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_events_invalid_limit_returns_error(client):
    """GET /api/events?limit=invalid returns 422 or 400."""
    r = await client.get("/api/events?limit=invalid")
    assert r.status_code in (400, 422)


# ── GET /api/metrics ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_returns_200(client):
    """GET /api/metrics returns 200 OK."""
    r = await client.get("/api/metrics")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_metrics_response_shape(client):
    """GET /api/metrics returns dept_metrics (departments) with occupancy, beds counts."""
    r = await client.get("/api/metrics")
    data = r.json()
    assert "departments" in data
    assert isinstance(data["departments"], list)


@pytest.mark.asyncio
async def test_metrics_dept_structure(client):
    """Each dept has dept_id, name, occupancy_pct, total_beds, available, occupied, cleaning, blocked."""
    r = await client.get("/api/metrics")
    data = r.json()
    dept_keys = ["dept_id", "name", "occupancy_pct", "total_beds", "available", "occupied", "cleaning", "blocked"]
    for dept in data["departments"]:
        for key in dept_keys:
            assert key in dept, f"Dept missing key: {key}"


# ── Error-path: DB down (503) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_header_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/header returns 503."""
    r = await client_with_db_error.get("/api/header")
    assert r.status_code == 503
    assert r.json().get("error") == "Database temporarily unavailable"


@pytest.mark.asyncio
async def test_beds_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/beds returns 503."""
    r = await client_with_db_error.get("/api/beds")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_ed_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/ed returns 503."""
    r = await client_with_db_error.get("/api/ed")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_or_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/or returns 503."""
    r = await client_with_db_error.get("/api/or")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_forecast_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/forecast returns 503."""
    r = await client_with_db_error.get("/api/forecast")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_events_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/events returns 503."""
    r = await client_with_db_error.get("/api/events")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_metrics_returns_503_on_db_error(client_with_db_error):
    """When queries raise asyncpg.PostgresError, /api/metrics returns 503."""
    r = await client_with_db_error.get("/api/metrics")
    assert r.status_code == 503


# ── Security: Path traversal (SPA catch-all) ───────────────────────────────

@pytest.mark.asyncio
async def test_path_traversal_returns_index_html(client):
    """Path traversal (e.g. GET /../../etc/passwd) must NOT serve arbitrary files; return index.html."""
    # URL-encoded ../ to escape from app/frontend/dist to /etc/passwd (vulnerability)
    r = await client.get("/..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2fetc%2fpasswd")
    # Must not return /etc/passwd content (would contain "root:")
    assert "root:" not in r.text
    # Should return index.html (200) or 404, not the actual file
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "html" in r.text.lower() or "Capacity Command" in r.text


# ── POST /api/beds/{bed_id}/mark-ready ────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_bed_ready_success(client, mock_queries):
    """POST /api/beds/{bed_id}/mark-ready marks a blocked bed as ready."""
    # Mock successful mark_bed_ready response
    mock_queries.mark_bed_ready.return_value = {
        "success": True,
        "bed_id": "ICU-201",
        "status": "available",
        "blocked": False,
        "unblocked_at": "2026-03-03T18:35:00Z",
        "unblocked_by": "operator"
    }

    r = await client.post("/api/beds/ICU-201/mark-ready")

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["bed_id"] == "ICU-201"
    assert data["blocked"] is False
    assert "unblocked_at" in data


@pytest.mark.asyncio
async def test_mark_bed_ready_bed_not_found(client, mock_queries):
    """POST /api/beds/{bed_id}/mark-ready returns 404 if bed not found."""
    mock_queries.mark_bed_ready.return_value = {
        "success": False,
        "error": "Bed INVALID-999 not found"
    }

    r = await client.post("/api/beds/INVALID-999/mark-ready")

    assert r.status_code == 404
    data = r.json()
    assert "detail" in data
    assert "not found" in data["detail"].lower()


@pytest.mark.asyncio
async def test_mark_bed_ready_bed_not_blocked(client, mock_queries):
    """POST /api/beds/{bed_id}/mark-ready returns 400 if bed is not blocked."""
    mock_queries.mark_bed_ready.return_value = {
        "success": False,
        "error": "Bed ICU-201 is not blocked"
    }

    r = await client.post("/api/beds/ICU-201/mark-ready")

    assert r.status_code == 400
    data = r.json()
    assert "detail" in data
    assert "not blocked" in data["detail"].lower()


# ── POST /api/beds/{bed_id}/block ──────────────────────────────────────

@pytest.mark.asyncio
async def test_block_bed_success(client, mock_queries):
    """POST /api/beds/{bed_id}/block blocks an available bed with reason and latency."""
    mock_queries.block_bed.return_value = {
        "success": True,
        "bed_id": "MED-12",
        "previous_status": "available",
        "status": "blocked",
        "blocked_reason": "Infection Control",
        "blocked_at": "2026-03-06T14:32:01Z",
        "blocked_by": "charge_nurse",
        "write_latency_ms": 312,
    }

    r = await client.post(
        "/api/beds/MED-12/block",
        json={"reason": "Infection Control", "operator": "charge_nurse"},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["bed_id"] == "MED-12"
    assert data["status"] == "blocked"
    assert data["blocked_reason"] == "Infection Control"
    assert data["write_latency_ms"] > 0
    assert "blocked_at" in data


@pytest.mark.asyncio
async def test_block_bed_already_blocked(client, mock_queries):
    """POST /api/beds/{bed_id}/block returns 400 if bed is already blocked."""
    mock_queries.block_bed.return_value = {
        "success": False,
        "error": "Bed MED-12 is already blocked (reason: Staffing Shortage)",
    }

    r = await client.post(
        "/api/beds/MED-12/block",
        json={"reason": "Infection Control"},
    )

    assert r.status_code == 400
    data = r.json()
    assert "detail" in data
    assert "already blocked" in data["detail"].lower()


@pytest.mark.asyncio
async def test_block_bed_not_found(client, mock_queries):
    """POST /api/beds/{bed_id}/block returns 404 if bed does not exist."""
    mock_queries.block_bed.return_value = {
        "success": False,
        "error": "Bed INVALID-999 not found",
    }

    r = await client.post(
        "/api/beds/INVALID-999/block",
        json={"reason": "Maintenance"},
    )

    assert r.status_code == 404
    data = r.json()
    assert "detail" in data
    assert "not found" in data["detail"].lower()


@pytest.mark.asyncio
async def test_block_bed_response_has_latency(client, mock_queries):
    """Block response includes write_latency_ms field with a positive value."""
    mock_queries.block_bed.return_value = {
        "success": True,
        "bed_id": "TELE-05",
        "previous_status": "occupied",
        "status": "blocked",
        "blocked_reason": "Equipment Failure",
        "blocked_at": "2026-03-06T15:00:00Z",
        "blocked_by": "operator",
        "write_latency_ms": 287,
    }

    r = await client.post(
        "/api/beds/TELE-05/block",
        json={"reason": "Equipment Failure"},
    )

    assert r.status_code == 200
    data = r.json()
    assert "write_latency_ms" in data
    assert isinstance(data["write_latency_ms"], int)
    assert data["write_latency_ms"] > 0


@pytest.mark.asyncio
async def test_block_bed_missing_reason(client, mock_queries):
    """POST /api/beds/{bed_id}/block returns 422 if reason is missing."""
    r = await client.post("/api/beds/MED-12/block", json={})

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_block_bed_empty_reason(client, mock_queries):
    """POST /api/beds/{bed_id}/block returns 422 if reason is empty string."""
    r = await client.post("/api/beds/MED-12/block", json={"reason": ""})

    assert r.status_code == 422


# ── GET /api/beds/{bed_id}/details — patient data ────────────────────────


@pytest.mark.asyncio
async def test_bed_details_returns_200(client, mock_queries):
    """GET /api/beds/{bed_id}/details returns 200 OK."""
    r = await client.get("/api/beds/ICU-01/details")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_bed_details_has_patient_section(client, mock_queries):
    """GET /api/beds/{bed_id}/details includes structured patient section."""
    r = await client.get("/api/beds/ICU-01/details")
    data = r.json()
    assert "patient" in data
    assert "bed" in data
    assert "timing" in data


@pytest.mark.asyncio
async def test_bed_details_patient_structure(client, mock_queries):
    """Patient section has demographics, LACE, discharge, arrival sub-objects."""
    r = await client.get("/api/beds/ICU-01/details")
    data = r.json()
    patient = data["patient"]
    assert patient is not None
    # Demographics
    assert "patient_id" in patient
    assert "mrn" in patient
    assert "name" in patient
    assert "age" in patient
    assert "gender" in patient
    # LACE sub-object
    assert "lace" in patient
    lace = patient["lace"]
    assert "score" in lace
    assert "risk" in lace
    assert "length_of_stay_days" in lace
    assert "acuity_score" in lace
    assert "comorbidity_count" in lace
    assert "ed_visits_6mo" in lace
    # Discharge sub-object
    assert "discharge" in patient
    discharge = patient["discharge"]
    assert "predicted_time" in discharge
    assert "reason" in discharge
    assert "confidence" in discharge
    # Arrival sub-object
    assert "arrival" in patient
    arrival = patient["arrival"]
    assert "arrival_id" in arrival
    assert "esi_level" in arrival
    assert "disposition" in arrival


@pytest.mark.asyncio
async def test_bed_details_patient_values(client, mock_queries):
    """Patient section contains correct values from mock data."""
    r = await client.get("/api/beds/ICU-01/details")
    data = r.json()
    patient = data["patient"]
    assert patient["name"] == "Jane Smith"
    assert patient["age"] == 72
    assert patient["gender"] == "F"
    assert patient["lace"]["score"] == 12
    assert patient["lace"]["risk"] == "HIGH"
    assert patient["discharge"]["reason"] == "Home"


@pytest.mark.asyncio
async def test_bed_details_404_for_nonexistent_bed(client, mock_queries):
    """GET /api/beds/{bed_id}/details returns 404 for nonexistent bed."""
    mock_queries.get_bed_details.return_value = {"error": "Bed INVALID-999 not found"}
    r = await client.get("/api/beds/INVALID-999/details")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bed_details_timing_section(client, mock_queries):
    """GET /api/beds/{bed_id}/details includes timing metrics."""
    r = await client.get("/api/beds/ICU-01/details")
    data = r.json()
    timing = data["timing"]
    assert "bed_ms" in timing
    assert "patient_ms" in timing
    assert "total_ms" in timing
    assert "sql_warehouse_est_ms" in timing
