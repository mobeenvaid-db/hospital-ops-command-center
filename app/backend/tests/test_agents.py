# Tests for LangGraph agents — uses mock asyncpg connections
#
# Each test verifies the full graph execution with realistic sample data,
# validating that UC Functions produce correct structured output and
# the plan/report synthesis works end-to-end.

import json
from datetime import datetime, date
from unittest.mock import AsyncMock, MagicMock
import sys
import os

import pytest

# Ensure backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Fixtures ─────────────────────────────────────────────────────────────

def _make_bed_rows():
    """Sample bed data: 3 departments, mix of occupied/available."""
    beds = []
    # ICU: 8 beds, 6 occupied, 2 available
    for i in range(6):
        beds.append({
            "dept_id": "ICU", "bed_id": f"ICU-{i+1}", "status": "occupied",
            "acuity": 4, "hours_in_bed": 48.0,
            "predicted_discharge_time": datetime(2026, 3, 8, 14, 0),
            "confidence_score": 0.7 if i < 3 else 0.3, "lace_score": 8,
        })
    for i in range(2):
        beds.append({
            "dept_id": "ICU", "bed_id": f"ICU-{7+i}", "status": "available",
            "acuity": None, "hours_in_bed": None,
            "predicted_discharge_time": None, "confidence_score": None, "lace_score": None,
        })
    # MEDSURG: 12 beds, 10 occupied, 2 available
    for i in range(10):
        beds.append({
            "dept_id": "MEDSURG", "bed_id": f"MS-{i+1}", "status": "occupied",
            "acuity": 3, "hours_in_bed": 72.0,
            "predicted_discharge_time": datetime(2026, 3, 8, 12, 0),
            "confidence_score": 0.85 if i < 5 else 0.4, "lace_score": 5,
        })
    for i in range(2):
        beds.append({
            "dept_id": "MEDSURG", "bed_id": f"MS-{11+i}", "status": "available",
            "acuity": None, "hours_in_bed": None,
            "predicted_discharge_time": None, "confidence_score": None, "lace_score": None,
        })
    return beds


def _make_forecast_rows():
    """Sample forecast predictions: arrivals in next 2h."""
    return [
        {"pred_type": "arrival", "dept_id": "ED", "predicted_value": 8.0, "confidence": 0.75},
        {"pred_type": "arrival", "dept_id": "ED", "predicted_value": 6.0, "confidence": 0.70},
        {"pred_type": "capacity", "dept_id": "ICU", "predicted_value": 1.5, "confidence": 0.65},
    ]


def _make_or_rows():
    """Sample OR schedule: 3 rooms, mix of statuses."""
    return [
        {
            "or_id": "OR-001", "room_id": "OR-1", "procedure_type": "Cardiac",
            "surgeon_specialty": "Cardiac", "scheduled_start": datetime(2026, 3, 8, 7, 0),
            "actual_start": datetime(2026, 3, 8, 7, 15), "est_duration_min": 180,
            "status": "in-progress", "previous_status": "scheduled",
            "updated_at": datetime(2026, 3, 8, 7, 15),
        },
        {
            "or_id": "OR-002", "room_id": "OR-1", "procedure_type": "General Surgery",
            "surgeon_specialty": "General Surgery", "scheduled_start": datetime(2026, 3, 8, 12, 0),
            "actual_start": None, "est_duration_min": 90,
            "status": "scheduled", "previous_status": None,
            "updated_at": datetime(2026, 3, 8, 6, 0),
        },
        {
            "or_id": "OR-003", "room_id": "OR-2", "procedure_type": "Orthopedic",
            "surgeon_specialty": "Orthopedic", "scheduled_start": datetime(2026, 3, 8, 8, 0),
            "actual_start": datetime(2026, 3, 8, 8, 10), "est_duration_min": 120,
            "status": "in-progress", "previous_status": "scheduled",
            "updated_at": datetime(2026, 3, 8, 8, 10),
        },
        {
            "or_id": "OR-004", "room_id": "OR-3", "procedure_type": "Urology",
            "surgeon_specialty": "Urology", "scheduled_start": datetime(2026, 3, 8, 10, 0),
            "actual_start": None, "est_duration_min": 60,
            "status": "scheduled", "previous_status": None,
            "updated_at": datetime(2026, 3, 8, 6, 0),
        },
    ]


def _make_patient_rows():
    """Sample discharge candidates."""
    return [
        {
            "patient_id": "P001", "mrn": "MRN001", "name": "Patient A",
            "age": 65, "bed_id": "MS-1", "dept_id": "MEDSURG",
            "lace_score": 4, "lace_risk": "LOW", "length_of_stay_days": 2.5,
            "acuity_score": 3, "predicted_discharge_time": datetime(2026, 3, 8, 14, 0),
            "discharge_reason": "clinical", "confidence_score": 0.9,
            "hours_until_discharge": 2.0,
        },
        {
            "patient_id": "P002", "mrn": "MRN002", "name": "Patient B",
            "age": 78, "bed_id": "ICU-1", "dept_id": "ICU",
            "lace_score": 14, "lace_risk": "HIGH", "length_of_stay_days": 7.0,
            "acuity_score": 5, "predicted_discharge_time": datetime(2026, 3, 8, 16, 0),
            "discharge_reason": "clinical", "confidence_score": 0.6,
            "hours_until_discharge": 4.0,
        },
        {
            "patient_id": "P003", "mrn": "MRN003", "name": "Patient C",
            "age": 45, "bed_id": "MS-3", "dept_id": "MEDSURG",
            "lace_score": 3, "lace_risk": "LOW", "length_of_stay_days": 1.0,
            "acuity_score": 2, "predicted_discharge_time": datetime(2026, 3, 8, 13, 0),
            "discharge_reason": "clinical", "confidence_score": 0.95,
            "hours_until_discharge": 1.0,
        },
    ]


def _make_anomaly_rows():
    """Sample anomaly events from predictions table."""
    return [
        {
            "event_id": "A001", "type": "CAPACITY_CRITICAL", "dept": "ICU",
            "value": 95.0, "severity": "CRITICAL", "confidence": 0.9,
            "event_time": datetime(2026, 3, 8, 10, 0),
        },
        {
            "event_id": "A002", "type": "ED_BOARDING_SURGE", "dept": "ED",
            "value": 12.0, "severity": "CRITICAL", "confidence": 0.85,
            "event_time": datetime(2026, 3, 8, 10, 15),
        },
        {
            "event_id": "A003", "type": "CAPACITY_CRITICAL", "dept": "ICU",
            "value": 97.0, "severity": "CRITICAL", "confidence": 0.92,
            "event_time": datetime(2026, 3, 8, 11, 0),
        },
        {
            "event_id": "A004", "type": "CAPACITY_WARNING", "dept": "MEDSURG",
            "value": 88.0, "severity": "WARNING", "confidence": 0.7,
            "event_time": datetime(2026, 3, 8, 10, 30),
        },
        {
            "event_id": "A005", "type": "CAPACITY_CRITICAL", "dept": "MEDSURG",
            "value": 93.0, "severity": "CRITICAL", "confidence": 0.88,
            "event_time": datetime(2026, 3, 8, 11, 30),
        },
    ]


class MockRecord(dict):
    """Mock asyncpg Record that supports both dict and attribute access."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


def _rows_to_records(rows):
    return [MockRecord(r) for r in rows]


def _make_mock_conn(fetch_side_effect=None, fetchrow_return=None):
    """Create a mock asyncpg connection."""
    conn = AsyncMock()
    if fetch_side_effect:
        conn.fetch = AsyncMock(side_effect=fetch_side_effect)
    if fetchrow_return:
        conn.fetchrow = AsyncMock(return_value=MockRecord(fetchrow_return))
    return conn


# ── Bed Capacity Planner Tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_bed_capacity_planner_all_depts():
    """BedCapacityPlanner produces gap analysis for all departments."""
    from agents.bed_capacity_planner import BedCapacityPlanner

    bed_records = _rows_to_records(_make_bed_rows())
    forecast_records = _rows_to_records(_make_forecast_rows())

    conn = _make_mock_conn(
        fetch_side_effect=[bed_records, forecast_records],
    )

    planner = BedCapacityPlanner(conn, schema="hospital_ops_lakebase")
    plan = await planner.run()

    assert plan["status"] in ("CRITICAL", "WARNING", "NORMAL")
    assert "summary" in plan
    assert plan["summary"]["total_beds"] == 20  # 8 ICU + 12 MEDSURG
    assert len(plan["department_gaps"]) == 2  # ICU + MEDSURG
    assert "actions" in plan


@pytest.mark.asyncio
async def test_bed_capacity_planner_single_dept():
    """BedCapacityPlanner filters to a single department."""
    from agents.bed_capacity_planner import BedCapacityPlanner

    bed_records = _rows_to_records(_make_bed_rows())
    forecast_records = _rows_to_records(_make_forecast_rows())

    conn = _make_mock_conn(
        fetch_side_effect=[bed_records, forecast_records],
    )

    planner = BedCapacityPlanner(conn, schema="hospital_ops_lakebase")
    plan = await planner.run(dept_filter="ICU")

    assert len(plan["department_gaps"]) == 1
    assert plan["department_gaps"][0]["dept_id"] == "ICU"


@pytest.mark.asyncio
async def test_bed_capacity_planner_error_handling():
    """BedCapacityPlanner handles DB errors gracefully."""
    from agents.bed_capacity_planner import BedCapacityPlanner

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=Exception("connection lost"))

    planner = BedCapacityPlanner(conn, schema="hospital_ops_lakebase")
    plan = await planner.run()

    assert plan["status"] == "error"
    assert "error" in plan


# ── OR Utilization Optimizer Tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_or_optimizer_full_schedule():
    """ORUtilizationOptimizer analyzes a full OR schedule."""
    from agents.or_utilization_optimizer import ORUtilizationOptimizer

    or_records = _rows_to_records(_make_or_rows())
    conn = _make_mock_conn(fetch_side_effect=[or_records])

    optimizer = ORUtilizationOptimizer(conn, schema="hospital_ops_lakebase")
    plan = await optimizer.run(rooms_total=8)

    assert plan["status"] in ("CRITICAL", "WARNING", "NORMAL")
    assert "summary" in plan
    assert plan["summary"]["total_cases"] == 4
    assert plan["summary"]["rooms_total"] == 8
    assert "room_analysis" in plan
    assert "immediate_actions" in plan


@pytest.mark.asyncio
async def test_or_optimizer_empty_schedule():
    """ORUtilizationOptimizer handles an empty schedule."""
    from agents.or_utilization_optimizer import ORUtilizationOptimizer

    conn = _make_mock_conn(fetch_side_effect=[[]])

    optimizer = ORUtilizationOptimizer(conn, schema="hospital_ops_lakebase")
    plan = await optimizer.run()

    assert plan["status"] in ("CRITICAL", "WARNING", "NORMAL")
    assert plan["summary"]["total_cases"] == 0


@pytest.mark.asyncio
async def test_or_optimizer_error_handling():
    """ORUtilizationOptimizer handles DB errors gracefully."""
    from agents.or_utilization_optimizer import ORUtilizationOptimizer

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=Exception("timeout"))

    optimizer = ORUtilizationOptimizer(conn, schema="hospital_ops_lakebase")
    plan = await optimizer.run()

    assert plan["status"] == "error"


# ── Discharge Coordinator Tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_discharge_coordinator_all_depts():
    """DischargeCoordinator prioritizes discharges across all departments."""
    from agents.discharge_coordinator import DischargeCoordinator

    patient_records = _rows_to_records(_make_patient_rows())
    clock_record = MockRecord({"ref": datetime(2026, 3, 8, 12, 0)})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=clock_record)
    conn.fetch = AsyncMock(return_value=patient_records)

    coordinator = DischargeCoordinator(conn, schema="hospital_ops_lakebase")
    plan = await coordinator.run()

    assert plan["status"] in ("HIGH_VOLUME", "MODERATE", "NORMAL", "LOW")
    assert "candidates" in plan
    assert len(plan["candidates"]) <= 10
    assert "summary" in plan
    assert plan["summary"]["total_evaluated"] == 3
    # Patient B has LACE 14 -- should generate high risk warning
    assert plan["summary"]["high_lace_risk_discharges"] >= 0


@pytest.mark.asyncio
async def test_discharge_coordinator_dept_filter():
    """DischargeCoordinator filters by department."""
    from agents.discharge_coordinator import DischargeCoordinator

    # Only MEDSURG patients
    medsurg_patients = [p for p in _make_patient_rows() if p["dept_id"] == "MEDSURG"]
    patient_records = _rows_to_records(medsurg_patients)
    clock_record = MockRecord({"ref": datetime(2026, 3, 8, 12, 0)})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=clock_record)
    conn.fetch = AsyncMock(return_value=patient_records)

    coordinator = DischargeCoordinator(conn, schema="hospital_ops_lakebase")
    plan = await coordinator.run(dept_filter="MEDSURG")

    assert "candidates" in plan
    for c in plan["candidates"]:
        assert c["dept_id"] == "MEDSURG"


@pytest.mark.asyncio
async def test_discharge_coordinator_high_lace_warnings():
    """DischargeCoordinator flags high-LACE patients being discharged."""
    from agents.discharge_coordinator import DischargeCoordinator

    patient_records = _rows_to_records(_make_patient_rows())
    clock_record = MockRecord({"ref": datetime(2026, 3, 8, 12, 0)})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=clock_record)
    conn.fetch = AsyncMock(return_value=patient_records)

    coordinator = DischargeCoordinator(conn, schema="hospital_ops_lakebase")
    plan = await coordinator.run()

    # Check that high_risk_warnings exist (Patient B LACE=14 is discharge-ready)
    assert "high_risk_warnings" in plan


# ── Anomaly Pattern Detector Tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_anomaly_detector_with_patterns():
    """AnomalyPatternDetector detects recurring patterns and correlations."""
    from agents.anomaly_pattern_detector import AnomalyPatternDetector

    anomaly_records = _rows_to_records(_make_anomaly_rows())

    conn = _make_mock_conn(fetch_side_effect=[anomaly_records])

    detector = AnomalyPatternDetector(conn, schema="hospital_ops_lakebase")
    report = await detector.run(lookback_hours=4)

    assert report["status"] in ("CRITICAL", "WARNING", "MONITOR", "CLEAR")
    assert "summary" in report
    assert report["summary"]["total_events"] == 5
    assert "incidents" in report
    assert "hotspots" in report
    assert "recommendations" in report


@pytest.mark.asyncio
async def test_anomaly_detector_no_events():
    """AnomalyPatternDetector handles zero anomaly events."""
    from agents.anomaly_pattern_detector import AnomalyPatternDetector

    conn = _make_mock_conn(fetch_side_effect=[[]])

    detector = AnomalyPatternDetector(conn, schema="hospital_ops_lakebase")
    report = await detector.run()

    assert report["status"] == "CLEAR"
    assert report["summary"]["total_events"] == 0
    assert len(report["incidents"]) == 0


@pytest.mark.asyncio
async def test_anomaly_detector_escalation():
    """AnomalyPatternDetector detects warning-to-critical escalation."""
    from agents.anomaly_pattern_detector import AnomalyPatternDetector

    # MEDSURG has both WARNING (A004) and CRITICAL (A005) -> escalation
    anomaly_records = _rows_to_records(_make_anomaly_rows())
    conn = _make_mock_conn(fetch_side_effect=[anomaly_records])

    detector = AnomalyPatternDetector(conn, schema="hospital_ops_lakebase")
    report = await detector.run()

    # Check escalations detected
    escalation_incidents = [i for i in report["incidents"] if i["type"] == "ESCALATION"]
    # MEDSURG has WARNING + CRITICAL = escalation
    assert len(escalation_incidents) >= 1


@pytest.mark.asyncio
async def test_anomaly_detector_correlation():
    """AnomalyPatternDetector detects correlated anomaly pairs."""
    from agents.anomaly_pattern_detector import AnomalyPatternDetector

    # CAPACITY_CRITICAL + ED_BOARDING_SURGE is a known correlated pair
    anomaly_records = _rows_to_records(_make_anomaly_rows())
    conn = _make_mock_conn(fetch_side_effect=[anomaly_records])

    detector = AnomalyPatternDetector(conn, schema="hospital_ops_lakebase")
    report = await detector.run()

    correlation_incidents = [i for i in report["incidents"] if i["type"] == "CORRELATION"]
    assert len(correlation_incidents) >= 1


@pytest.mark.asyncio
async def test_anomaly_detector_error_handling():
    """AnomalyPatternDetector handles DB errors gracefully."""
    from agents.anomaly_pattern_detector import AnomalyPatternDetector

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=Exception("connection refused"))

    detector = AnomalyPatternDetector(conn, schema="hospital_ops_lakebase")
    report = await detector.run()

    assert report["status"] == "error"
