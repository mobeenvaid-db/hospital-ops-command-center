# Capacity Command — Query logic tests (TDD: tests written FIRST)

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.backend.queries import get_forecast_data, get_patient_details, get_bed_details


class MockRecord:
    """Dict-like mock for asyncpg Record."""

    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()


# ── get_forecast_data: discharge aggregation by time window ──────────────


@pytest.mark.asyncio
async def test_forecast_counts_discharges_by_time_window():
    """Discharge predictions with pred_type='discharge' are counted by 2h/4h windows."""
    now = datetime.now(timezone.utc)
    # 2 within 2h, 1 between 2h-4h, 1 beyond 4h
    rows = [
        MockRecord({
            "pred_type": "discharge",
            "dept_id": "ICU",
            "predicted_time": now + timedelta(hours=1),
            "predicted_value": 0.85,
            "confidence": 0.9,
        }),
        MockRecord({
            "pred_type": "discharge",
            "dept_id": "TELE",
            "predicted_time": now + timedelta(hours=1.5),
            "predicted_value": 0.72,
            "confidence": 0.8,
        }),
        MockRecord({
            "pred_type": "discharge",
            "dept_id": "MEDSURG",
            "predicted_time": now + timedelta(hours=3),
            "predicted_value": 0.65,
            "confidence": 0.75,
        }),
        MockRecord({
            "pred_type": "discharge",
            "dept_id": "PEDS",
            "predicted_time": now + timedelta(hours=5),
            "predicted_value": 0.5,
            "confidence": 0.7,
        }),
    ]
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(side_effect=[rows, []])

    result = await get_forecast_data(mock_conn, "hospital_ops")

    assert result["predicted_discharges_2h"] == 2
    assert result["predicted_discharges_4h"] == 3


@pytest.mark.asyncio
async def test_forecast_handles_ed_arrival_pred_type():
    """ed_arrival records produce arrival_forecast entries."""
    dt = datetime(2026, 2, 15, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        MockRecord({
            "pred_type": "ed_arrival",
            "dept_id": "ED",
            "predicted_time": dt,
            "predicted_value": 6.5,
            "confidence": 0.8,
        }),
    ]
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(side_effect=[rows, []])

    result = await get_forecast_data(mock_conn, "hospital_ops")

    assert len(result["arrival_forecast"]) == 1
    assert result["arrival_forecast"][0]["hour"] == 14
    assert result["arrival_forecast"][0]["predicted"] == 6.5


@pytest.mark.asyncio
async def test_forecast_handles_empty_predictions():
    """Conn with no rows returns defaults."""
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(side_effect=[[], []])

    result = await get_forecast_data(mock_conn, "hospital_ops")

    assert result["predicted_discharges_2h"] == 0
    assert result["predicted_discharges_4h"] == 0
    assert result["arrival_forecast"] == []
    assert result["capacity_by_dept"] == []
    assert result["anomalies"] == []


# ── get_patient_details: patient lookup by bed_id ──────────────────────────


def _make_patient_record():
    """Create a MockRecord simulating a patient row joined with ed_arrivals."""
    return MockRecord({
        "patient_id": "PAT-00042",
        "mrn": "MRN7654321",
        "name": "John Doe",
        "age": 68,
        "gender": "M",
        "arrival_id": "ED-100",
        "bed_id": "ICU-05",
        "dept_id": "ICU",
        "length_of_stay_days": 2.5,
        "acuity_score": 2,
        "comorbidity_count": 3,
        "ed_visits_6mo": 1,
        "lace_score": 10,
        "lace_risk": "HIGH",
        "predicted_discharge_time": datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc),
        "discharge_reason": "Home",
        "confidence_score": 0.82,
        "admission_time": datetime(2026, 3, 4, 8, 30, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc),
        "esi_level": 2,
        "arrival_time": datetime(2026, 3, 4, 7, 45, 0, tzinfo=timezone.utc),
        "disposition": "admitted",
        "wait_minutes": 45,
    })


@pytest.mark.asyncio
async def test_get_patient_details_returns_patient_for_occupied_bed():
    """get_patient_details returns patient demographics, LACE, discharge for a bed."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=_make_patient_record())

    result = await get_patient_details(mock_conn, "hospital_ops", "ICU-05")

    assert result is not None
    assert result["patient_id"] == "PAT-00042"
    assert result["mrn"] == "MRN7654321"
    assert result["name"] == "John Doe"
    assert result["age"] == 68
    assert result["gender"] == "M"
    assert result["lace_score"] == 10
    assert result["lace_risk"] == "HIGH"
    assert result["esi_level"] == 2
    assert result["disposition"] == "admitted"


@pytest.mark.asyncio
async def test_get_patient_details_returns_none_for_empty_bed():
    """get_patient_details returns None when no patient is in the bed."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    result = await get_patient_details(mock_conn, "hospital_ops", "ICU-99")

    assert result is None


@pytest.mark.asyncio
async def test_get_patient_details_returns_none_when_conn_is_none():
    """get_patient_details returns None in demo mode (conn=None)."""
    result = await get_patient_details(None, "hospital_ops", "ICU-01")
    assert result is None


@pytest.mark.asyncio
async def test_get_patient_details_returns_none_on_exception():
    """get_patient_details returns None if the query raises an exception."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(side_effect=Exception("connection lost"))

    result = await get_patient_details(mock_conn, "hospital_ops", "ICU-05")

    assert result is None


@pytest.mark.asyncio
async def test_get_patient_details_lace_components():
    """get_patient_details includes all LACE score components."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=_make_patient_record())

    result = await get_patient_details(mock_conn, "hospital_ops", "ICU-05")

    assert result["length_of_stay_days"] == 2.5
    assert result["acuity_score"] == 2
    assert result["comorbidity_count"] == 3
    assert result["ed_visits_6mo"] == 1
    assert result["lace_score"] == 10


@pytest.mark.asyncio
async def test_get_patient_details_discharge_prediction():
    """get_patient_details includes discharge prediction fields."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=_make_patient_record())

    result = await get_patient_details(mock_conn, "hospital_ops", "ICU-05")

    assert result["discharge_reason"] == "Home"
    assert result["confidence_score"] == 0.82
    # predicted_discharge_time should be serialized to ISO
    assert "2026-03-07" in str(result["predicted_discharge_time"])


# ── get_bed_details: enriched bed drill-down with patient data ─────────────


@pytest.mark.asyncio
async def test_get_bed_details_returns_patient_section():
    """get_bed_details includes patient data from the patients table."""
    bed_record = MockRecord({
        "bed_id": "ICU-05", "dept_id": "ICU", "status": "occupied",
        "acuity_level": 2, "admission_time": datetime(2026, 3, 4, 8, 30, tzinfo=timezone.utc),
        "expected_discharge": None, "blocked_reason": None,
        "previous_status": "available", "last_updated": datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
    })
    patient_record = _make_patient_record()
    dept_record = MockRecord({
        "dept_id": "ICU", "name": "Intensive Care Unit", "floor": 3, "total_beds": 20,
    })

    mock_conn = MagicMock()
    # fetchrow calls: bed, patient, dept
    mock_conn.fetchrow = AsyncMock(side_effect=[bed_record, patient_record, dept_record])
    # fetch call: events
    mock_conn.fetch = AsyncMock(return_value=[])

    result = await get_bed_details(mock_conn, "hospital_ops", "ICU-05")

    assert "error" not in result
    assert result["bed"]["bed_id"] == "ICU-05"
    assert result["patient"] is not None
    assert result["patient"]["name"] == "John Doe"
    assert result["patient"]["lace_score"] == 10
    assert result["patient"]["lace_risk"] == "HIGH"
    assert "timing" in result
    assert result["timing"]["total_ms"] >= 0


@pytest.mark.asyncio
async def test_get_bed_details_patient_none_for_empty_bed():
    """get_bed_details returns patient=None when bed has no patient."""
    bed_record = MockRecord({
        "bed_id": "ICU-99", "dept_id": "ICU", "status": "available",
        "acuity_level": None, "admission_time": None,
        "expected_discharge": None, "blocked_reason": None,
        "previous_status": None, "last_updated": datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc),
    })
    dept_record = MockRecord({
        "dept_id": "ICU", "name": "Intensive Care Unit", "floor": 3, "total_beds": 20,
    })

    mock_conn = MagicMock()
    # fetchrow: bed=found, patient=None, dept=found
    mock_conn.fetchrow = AsyncMock(side_effect=[bed_record, None, dept_record])
    mock_conn.fetch = AsyncMock(return_value=[])

    result = await get_bed_details(mock_conn, "hospital_ops", "ICU-99")

    assert "error" not in result
    assert result["patient"] is None


@pytest.mark.asyncio
async def test_get_bed_details_demo_mode_includes_patient():
    """get_bed_details in demo mode (conn=None) includes a synthetic patient."""
    result = await get_bed_details(None, "hospital_ops", "ICU-01")

    assert result["patient"] is not None
    assert result["patient"]["name"] == "Demo Patient"
    assert result["patient"]["lace_score"] == 9
    assert result["patient"]["lace_risk"] == "MODERATE"
    assert "timing" in result


@pytest.mark.asyncio
async def test_get_bed_details_not_found():
    """get_bed_details returns error dict when bed_id doesn't exist."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    result = await get_bed_details(mock_conn, "hospital_ops", "INVALID-999")

    assert "error" in result
    assert "not found" in result["error"].lower()
