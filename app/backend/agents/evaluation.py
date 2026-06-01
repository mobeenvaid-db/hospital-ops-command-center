# MLflow Evaluation Framework for Capacity Command Agents
#
# 40 test scenarios (10 per agent) covering:
#   - Happy path with realistic data
#   - Edge cases (empty data, single department, max capacity)
#   - Error handling (DB failures, malformed data)
#   - Boundary conditions (thresholds, filters)
#
# Metrics tracked per scenario:
#   - accuracy: does the result match expected status/structure?
#   - latency_ms: execution time
#   - tool_calls: number of UC Function invocations
#   - success_rate: did the agent complete without error?
#
# Usage:
#   python -m agents.evaluation          # run all scenarios
#   python -m agents.evaluation --agent bed_capacity_planner  # single agent

import asyncio
import json
import logging
import sys
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Optional
from unittest.mock import AsyncMock

# Ensure backend modules importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import mlflow
    MLFLOW_AVAILABLE = True
except ImportError:
    MLFLOW_AVAILABLE = False

from agents.mlflow_tracing import setup_mlflow_tracing, EXPERIMENT_NAME

logger = logging.getLogger(__name__)

SCHEMA = "hospital_ops_lakebase"


# ── Mock helpers ─────────────────────────────────────────────────────────

class MockRecord(dict):
    """Mock asyncpg Record."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


def _records(rows: list[dict]) -> list[MockRecord]:
    return [MockRecord(r) for r in rows]


def _mock_conn(fetch_side_effect=None, fetchrow_return=None):
    conn = AsyncMock()
    if fetch_side_effect is not None:
        conn.fetch = AsyncMock(side_effect=fetch_side_effect)
    else:
        conn.fetch = AsyncMock(return_value=[])
    if fetchrow_return is not None:
        conn.fetchrow = AsyncMock(return_value=MockRecord(fetchrow_return))
    else:
        conn.fetchrow = AsyncMock(return_value=None)
    return conn


# ── Test data generators ────────────────────────────────────────────────

def _bed_rows(
    depts: dict[str, tuple[int, int]] | None = None,
) -> list[dict]:
    """Generate bed data. depts = {"ICU": (occupied, available), ...}"""
    if depts is None:
        depts = {"ICU": (6, 2), "MEDSURG": (10, 2)}
    beds = []
    for dept, (occ, avail) in depts.items():
        for i in range(occ):
            beds.append({
                "dept_id": dept, "bed_id": f"{dept}-{i+1}", "status": "occupied",
                "acuity": 4, "hours_in_bed": 48.0,
                "predicted_discharge_time": datetime(2026, 3, 8, 14, 0),
                "confidence_score": 0.7 if i % 2 == 0 else 0.3,
                "lace_score": 8,
            })
        for i in range(avail):
            beds.append({
                "dept_id": dept, "bed_id": f"{dept}-A{i+1}", "status": "available",
                "acuity": None, "hours_in_bed": None,
                "predicted_discharge_time": None, "confidence_score": None,
                "lace_score": None,
            })
    return beds


def _forecast_rows(arrivals: float = 14.0) -> list[dict]:
    return [
        {"pred_type": "arrival", "dept_id": "ED", "predicted_value": arrivals * 0.6, "confidence": 0.75},
        {"pred_type": "arrival", "dept_id": "ED", "predicted_value": arrivals * 0.4, "confidence": 0.70},
        {"pred_type": "capacity", "dept_id": "ICU", "predicted_value": 1.5, "confidence": 0.65},
    ]


def _or_rows(count: int = 4) -> list[dict]:
    cases = []
    rooms = ["OR-1", "OR-2", "OR-3", "OR-4"]
    statuses = ["in-progress", "scheduled", "in-progress", "scheduled"]
    specialties = ["Cardiac", "General Surgery", "Orthopedic", "Urology"]
    for i in range(min(count, len(rooms))):
        cases.append({
            "or_id": f"OR-{i+1:03d}", "room_id": rooms[i],
            "procedure_type": specialties[i], "surgeon_specialty": specialties[i],
            "scheduled_start": datetime(2026, 3, 8, 7 + i * 2, 0),
            "actual_start": datetime(2026, 3, 8, 7 + i * 2, 15) if statuses[i] == "in-progress" else None,
            "est_duration_min": 120 + i * 30,
            "status": statuses[i], "previous_status": "scheduled" if statuses[i] != "scheduled" else None,
            "updated_at": datetime(2026, 3, 8, 7 + i * 2, 15),
        })
    return cases


def _patient_rows(count: int = 5, high_lace: bool = True) -> list[dict]:
    patients = []
    for i in range(count):
        lace = 14 if (i == 1 and high_lace) else 3 + i
        patients.append({
            "patient_id": f"P{i+1:03d}", "mrn": f"MRN{i+1:03d}",
            "name": f"Patient {chr(65 + i)}", "age": 45 + i * 10,
            "bed_id": f"MS-{i+1}", "dept_id": "MEDSURG" if i % 2 == 0 else "ICU",
            "lace_score": lace, "lace_risk": "HIGH" if lace >= 10 else "LOW",
            "length_of_stay_days": 1.0 + i * 0.5, "acuity_score": 2 + (i % 3),
            "predicted_discharge_time": datetime(2026, 3, 8, 12 + (i % 12), 0),
            "discharge_reason": "clinical", "confidence_score": 0.9 - i * 0.1,
            "hours_until_discharge": 1.0 + i,
        })
    return patients


def _anomaly_rows(count: int = 5, include_escalation: bool = True) -> list[dict]:
    events = []
    types = ["CAPACITY_CRITICAL", "ED_BOARDING_SURGE", "CAPACITY_CRITICAL",
             "CAPACITY_WARNING", "CAPACITY_CRITICAL"]
    depts = ["ICU", "ED", "ICU", "MEDSURG", "MEDSURG"]
    sevs = ["CRITICAL", "CRITICAL", "CRITICAL", "WARNING", "CRITICAL"]
    for i in range(min(count, len(types))):
        events.append({
            "event_id": f"A{i+1:03d}", "type": types[i], "dept": depts[i],
            "value": 90.0 + i * 2, "severity": sevs[i],
            "confidence": 0.85 + i * 0.02,
            "event_time": datetime(2026, 3, 8, 10 + (i * 15) // 60, (i * 15) % 60),
        })
    return events


# ── Scenario definitions ────────────────────────────────────────────────

@dataclass
class Scenario:
    """A single evaluation scenario."""
    id: str
    agent: str
    description: str
    conn_factory: Any  # callable returning mock conn
    run_kwargs: dict = field(default_factory=dict)
    expected_status: Optional[str] = None
    expected_keys: list[str] = field(default_factory=list)
    expected_min_items: dict[str, int] = field(default_factory=dict)
    should_error: bool = False


@dataclass
class ScenarioResult:
    """Result of running a single scenario."""
    scenario_id: str
    agent: str
    success: bool
    accuracy: float  # 0.0-1.0
    latency_ms: float
    result_status: str
    checks_passed: int
    checks_total: int
    error: Optional[str] = None


def _build_scenarios() -> list[Scenario]:
    """Build all 40 evaluation scenarios."""
    scenarios = []

    # ── Bed Capacity Planner (10 scenarios) ──────────────────────────────

    # BCP-01: Normal multi-department analysis
    scenarios.append(Scenario(
        id="BCP-01", agent="bed_capacity_planner",
        description="Normal capacity analysis across ICU + MEDSURG",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_bed_rows()), _records(_forecast_rows())]
        ),
        expected_status=None,  # depends on data
        expected_keys=["status", "summary", "department_gaps", "actions"],
        expected_min_items={"department_gaps": 2},
    ))

    # BCP-02: Single department filter
    scenarios.append(Scenario(
        id="BCP-02", agent="bed_capacity_planner",
        description="Filter to ICU department only",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_bed_rows()), _records(_forecast_rows())]
        ),
        run_kwargs={"dept_filter": "ICU"},
        expected_keys=["status", "summary", "department_gaps"],
        expected_min_items={"department_gaps": 1},
    ))

    # BCP-03: High occupancy -> CRITICAL status
    scenarios.append(Scenario(
        id="BCP-03", agent="bed_capacity_planner",
        description="High occupancy triggers CRITICAL status",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[
                _records(_bed_rows({"ICU": (18, 0), "MEDSURG": (20, 0)})),
                _records(_forecast_rows(arrivals=20.0)),
            ]
        ),
        expected_status="CRITICAL",
        expected_keys=["status", "actions"],
    ))

    # BCP-04: Low occupancy -> NORMAL status
    scenarios.append(Scenario(
        id="BCP-04", agent="bed_capacity_planner",
        description="Low occupancy produces NORMAL status",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[
                _records(_bed_rows({"ICU": (2, 8), "MEDSURG": (3, 12)})),
                _records(_forecast_rows(arrivals=2.0)),
            ]
        ),
        expected_status="NORMAL",
        expected_keys=["status", "summary"],
    ))

    # BCP-05: Empty bed data
    scenarios.append(Scenario(
        id="BCP-05", agent="bed_capacity_planner",
        description="Empty bed data returns valid structure",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[[], _records(_forecast_rows())]
        ),
        expected_keys=["status", "summary", "department_gaps"],
    ))

    # BCP-06: No forecast data
    scenarios.append(Scenario(
        id="BCP-06", agent="bed_capacity_planner",
        description="No forecast data still produces plan",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_bed_rows()), []]
        ),
        expected_keys=["status", "summary", "department_gaps"],
    ))

    # BCP-07: Single bed department
    scenarios.append(Scenario(
        id="BCP-07", agent="bed_capacity_planner",
        description="Single department with 1 bed",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[
                _records(_bed_rows({"PEDS": (1, 0)})),
                _records(_forecast_rows(arrivals=3.0)),
            ]
        ),
        expected_keys=["status", "department_gaps"],
        expected_min_items={"department_gaps": 1},
    ))

    # BCP-08: Many departments
    scenarios.append(Scenario(
        id="BCP-08", agent="bed_capacity_planner",
        description="6 departments analyzed simultaneously",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[
                _records(_bed_rows({
                    "ICU": (6, 2), "MEDSURG": (10, 2), "TELE": (8, 1),
                    "PEDS": (4, 1), "OBS": (5, 2), "NEURO": (3, 1),
                })),
                _records(_forecast_rows(arrivals=18.0)),
            ]
        ),
        expected_keys=["status", "department_gaps"],
        expected_min_items={"department_gaps": 6},
    ))

    # BCP-09: DB connection error
    scenarios.append(Scenario(
        id="BCP-09", agent="bed_capacity_planner",
        description="Database connection error handled gracefully",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[Exception("connection lost")]
        ),
        expected_status="error",
        should_error=False,  # should handle internally
    ))

    # BCP-10: Non-existent department filter
    scenarios.append(Scenario(
        id="BCP-10", agent="bed_capacity_planner",
        description="Non-existent department filter returns empty gaps",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_bed_rows()), _records(_forecast_rows())]
        ),
        run_kwargs={"dept_filter": "NONEXISTENT"},
        expected_keys=["status", "department_gaps"],
    ))

    # ── OR Utilization Optimizer (10 scenarios) ──────────────────────────

    # OPT-01: Full schedule analysis
    scenarios.append(Scenario(
        id="OPT-01", agent="or_utilization_optimizer",
        description="Full OR schedule with 4 cases across 3 rooms",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(4))]
        ),
        run_kwargs={"rooms_total": 8},
        expected_keys=["status", "summary", "room_analysis", "immediate_actions"],
    ))

    # OPT-02: Empty schedule
    scenarios.append(Scenario(
        id="OPT-02", agent="or_utilization_optimizer",
        description="Empty OR schedule (no cases today)",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[[]]
        ),
        expected_keys=["status", "summary"],
    ))

    # OPT-03: All rooms active
    scenarios.append(Scenario(
        id="OPT-03", agent="or_utilization_optimizer",
        description="All rooms active with in-progress cases",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records([
                {"or_id": f"OR-{i+1:03d}", "room_id": f"OR-{i+1}",
                 "procedure_type": "Cardiac", "surgeon_specialty": "Cardiac",
                 "scheduled_start": datetime(2026, 3, 8, 7, 0),
                 "actual_start": datetime(2026, 3, 8, 7, 10),
                 "est_duration_min": 120, "status": "in-progress",
                 "previous_status": "scheduled",
                 "updated_at": datetime(2026, 3, 8, 7, 10)}
                for i in range(8)
            ])]
        ),
        run_kwargs={"rooms_total": 8},
        expected_keys=["status", "summary"],
    ))

    # OPT-04: Low utilization target
    scenarios.append(Scenario(
        id="OPT-04", agent="or_utilization_optimizer",
        description="Low utilization target (50%) easily met",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(4))]
        ),
        run_kwargs={"rooms_total": 4, "target_utilization_pct": 50.0},
        expected_keys=["status", "summary"],
    ))

    # OPT-05: High utilization target
    scenarios.append(Scenario(
        id="OPT-05", agent="or_utilization_optimizer",
        description="High utilization target (95%) hard to meet",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(2))]
        ),
        run_kwargs={"rooms_total": 8, "target_utilization_pct": 95.0},
        expected_keys=["status", "summary"],
    ))

    # OPT-06: Specific date filter
    scenarios.append(Scenario(
        id="OPT-06", agent="or_utilization_optimizer",
        description="Specific target date analysis",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(3))]
        ),
        run_kwargs={"target_date": date(2026, 3, 8)},
        expected_keys=["status", "summary", "room_analysis"],
    ))

    # OPT-07: Single room schedule
    scenarios.append(Scenario(
        id="OPT-07", agent="or_utilization_optimizer",
        description="Single room with 1 case",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(1))]
        ),
        run_kwargs={"rooms_total": 1},
        expected_keys=["status", "room_analysis"],
    ))

    # OPT-08: DB error
    scenarios.append(Scenario(
        id="OPT-08", agent="or_utilization_optimizer",
        description="Database timeout handled gracefully",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[Exception("timeout")]
        ),
        expected_status="error",
    ))

    # OPT-09: Many rooms available
    scenarios.append(Scenario(
        id="OPT-09", agent="or_utilization_optimizer",
        description="20 rooms with sparse utilization",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_or_rows(2))]
        ),
        run_kwargs={"rooms_total": 20},
        expected_keys=["status", "summary"],
    ))

    # OPT-10: All cases completed
    scenarios.append(Scenario(
        id="OPT-10", agent="or_utilization_optimizer",
        description="All cases completed for the day",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records([
                {"or_id": f"OR-{i+1:03d}", "room_id": f"OR-{i+1}",
                 "procedure_type": "General Surgery", "surgeon_specialty": "General Surgery",
                 "scheduled_start": datetime(2026, 3, 8, 7, 0),
                 "actual_start": datetime(2026, 3, 8, 7, 5),
                 "est_duration_min": 90, "status": "complete",
                 "previous_status": "in-progress",
                 "updated_at": datetime(2026, 3, 8, 9, 0)}
                for i in range(4)
            ])]
        ),
        expected_keys=["status", "summary"],
    ))

    # ── Discharge Coordinator (10 scenarios) ─────────────────────────────

    clock = {"ref": datetime(2026, 3, 8, 12, 0)}

    # DC-01: Normal discharge prioritization
    scenarios.append(Scenario(
        id="DC-01", agent="discharge_coordinator",
        description="Prioritize 5 discharge candidates",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(5))],
            fetchrow_return=clock,
        ),
        expected_keys=["status", "headline", "summary", "candidates"],
    ))

    # DC-02: Department filter
    scenarios.append(Scenario(
        id="DC-02", agent="discharge_coordinator",
        description="Filter discharges to ICU only",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records([p for p in _patient_rows(5) if p["dept_id"] == "ICU"])],
            fetchrow_return=clock,
        ),
        run_kwargs={"dept_filter": "ICU"},
        expected_keys=["status", "candidates"],
    ))

    # DC-03: High LACE risk warnings
    scenarios.append(Scenario(
        id="DC-03", agent="discharge_coordinator",
        description="High-LACE patients flagged in warnings",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(5, high_lace=True))],
            fetchrow_return=clock,
        ),
        expected_keys=["status", "high_risk_warnings"],
    ))

    # DC-04: No discharge candidates
    scenarios.append(Scenario(
        id="DC-04", agent="discharge_coordinator",
        description="No patients ready for discharge",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[[]],
            fetchrow_return=clock,
        ),
        expected_status="LOW",
        expected_keys=["status", "headline"],
    ))

    # DC-05: Large patient volume
    scenarios.append(Scenario(
        id="DC-05", agent="discharge_coordinator",
        description="20 patients evaluated, top 10 returned",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(20, high_lace=False))],
            fetchrow_return=clock,
        ),
        run_kwargs={"top_n": 10},
        expected_keys=["status", "candidates", "summary"],
    ))

    # DC-06: Custom top_n
    scenarios.append(Scenario(
        id="DC-06", agent="discharge_coordinator",
        description="Return top 3 candidates only",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(10, high_lace=False))],
            fetchrow_return=clock,
        ),
        run_kwargs={"top_n": 3},
        expected_keys=["status", "candidates"],
    ))

    # DC-07: All patients high LACE
    scenarios.append(Scenario(
        id="DC-07", agent="discharge_coordinator",
        description="All patients have high LACE scores",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records([
                {**p, "lace_score": 15, "lace_risk": "HIGH"}
                for p in _patient_rows(5, high_lace=False)
            ])],
            fetchrow_return=clock,
        ),
        expected_keys=["status", "high_risk_warnings"],
    ))

    # DC-08: DB error
    scenarios.append(Scenario(
        id="DC-08", agent="discharge_coordinator",
        description="Database connection refused",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[Exception("connection refused")],
            fetchrow_return=clock,
        ),
        expected_status="error",
    ))

    # DC-09: Staffing suggestions generated
    scenarios.append(Scenario(
        id="DC-09", agent="discharge_coordinator",
        description="Multiple discharges trigger staffing suggestions",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(8, high_lace=False))],
            fetchrow_return=clock,
        ),
        expected_keys=["status", "staffing_suggestions"],
    ))

    # DC-10: Single patient
    scenarios.append(Scenario(
        id="DC-10", agent="discharge_coordinator",
        description="Single patient discharge evaluation",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_patient_rows(1, high_lace=False))],
            fetchrow_return=clock,
        ),
        expected_keys=["status", "candidates", "summary"],
    ))

    # ── Anomaly Pattern Detector (10 scenarios) ──────────────────────────

    # APD-01: Multi-pattern detection
    scenarios.append(Scenario(
        id="APD-01", agent="anomaly_pattern_detector",
        description="Detect recurring, correlated, and escalation patterns",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(5))]
        ),
        expected_keys=["status", "headline", "summary", "incidents", "hotspots"],
    ))

    # APD-02: No anomalies
    scenarios.append(Scenario(
        id="APD-02", agent="anomaly_pattern_detector",
        description="Clean system with no anomalies",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[[]]
        ),
        expected_status="CLEAR",
        expected_keys=["status", "headline"],
    ))

    # APD-03: Single event (below recurrence threshold)
    scenarios.append(Scenario(
        id="APD-03", agent="anomaly_pattern_detector",
        description="Single anomaly event (no patterns)",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(1))]
        ),
        expected_keys=["status", "summary"],
    ))

    # APD-04: Escalation detection
    scenarios.append(Scenario(
        id="APD-04", agent="anomaly_pattern_detector",
        description="WARNING to CRITICAL escalation in MEDSURG",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(5, include_escalation=True))]
        ),
        expected_keys=["status", "incidents"],
    ))

    # APD-05: Custom lookback window
    scenarios.append(Scenario(
        id="APD-05", agent="anomaly_pattern_detector",
        description="Short lookback window (1 hour)",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(3))]
        ),
        run_kwargs={"lookback_hours": 1},
        expected_keys=["status", "summary"],
    ))

    # APD-06: High recurrence threshold
    scenarios.append(Scenario(
        id="APD-06", agent="anomaly_pattern_detector",
        description="High recurrence threshold filters out sparse patterns",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(5))]
        ),
        run_kwargs={"recurrence_threshold": 5},
        expected_keys=["status", "summary"],
    ))

    # APD-07: Large event volume
    scenarios.append(Scenario(
        id="APD-07", agent="anomaly_pattern_detector",
        description="50 anomaly events analyzed for patterns",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(5) * 10)]
        ),
        expected_keys=["status", "summary", "incidents", "hotspots"],
    ))

    # APD-08: DB error
    scenarios.append(Scenario(
        id="APD-08", agent="anomaly_pattern_detector",
        description="Database connection error handled",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[Exception("connection refused")]
        ),
        expected_status="error",
    ))

    # APD-09: Correlation-only (no recurrence)
    scenarios.append(Scenario(
        id="APD-09", agent="anomaly_pattern_detector",
        description="Two correlated anomaly types without recurrence",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records([
                {"event_id": "X1", "type": "CAPACITY_CRITICAL", "dept": "ICU",
                 "value": 95.0, "severity": "CRITICAL", "confidence": 0.9,
                 "event_time": datetime(2026, 3, 8, 10, 0)},
                {"event_id": "X2", "type": "ED_BOARDING_SURGE", "dept": "ED",
                 "value": 12.0, "severity": "CRITICAL", "confidence": 0.85,
                 "event_time": datetime(2026, 3, 8, 10, 5)},
            ])]
        ),
        expected_keys=["status", "summary"],
    ))

    # APD-10: Long lookback window
    scenarios.append(Scenario(
        id="APD-10", agent="anomaly_pattern_detector",
        description="24-hour lookback window analysis",
        conn_factory=lambda: _mock_conn(
            fetch_side_effect=[_records(_anomaly_rows(5))]
        ),
        run_kwargs={"lookback_hours": 24},
        expected_keys=["status", "summary"],
    ))

    return scenarios


# ── Scenario runner ──────────────────────────────────────────────────────

def _get_agent(name: str, conn, schema: str):
    """Instantiate agent by name."""
    if name == "bed_capacity_planner":
        from agents.bed_capacity_planner import BedCapacityPlanner
        return BedCapacityPlanner(conn, schema)
    elif name == "or_utilization_optimizer":
        from agents.or_utilization_optimizer import ORUtilizationOptimizer
        return ORUtilizationOptimizer(conn, schema)
    elif name == "discharge_coordinator":
        from agents.discharge_coordinator import DischargeCoordinator
        return DischargeCoordinator(conn, schema)
    elif name == "anomaly_pattern_detector":
        from agents.anomaly_pattern_detector import AnomalyPatternDetector
        return AnomalyPatternDetector(conn, schema)
    else:
        raise ValueError(f"Unknown agent: {name}")


async def _run_scenario(scenario: Scenario) -> ScenarioResult:
    """Execute a single evaluation scenario and check results."""
    conn = scenario.conn_factory()
    agent = _get_agent(scenario.agent, conn, SCHEMA)

    checks_passed = 0
    checks_total = 0
    error_msg = None

    start = time.perf_counter()
    try:
        result = await agent.run(**scenario.run_kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000
        success = True
        result_status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"

        # Check expected status
        if scenario.expected_status:
            checks_total += 1
            if result_status == scenario.expected_status:
                checks_passed += 1

        # Check expected keys
        for key in scenario.expected_keys:
            checks_total += 1
            if isinstance(result, dict) and key in result:
                checks_passed += 1

        # Check minimum item counts
        for key, min_count in scenario.expected_min_items.items():
            checks_total += 1
            if isinstance(result, dict) and isinstance(result.get(key), list):
                if len(result[key]) >= min_count:
                    checks_passed += 1

        # If error was expected, verify it was handled
        if scenario.should_error:
            checks_total += 1
            if result_status == "error":
                checks_passed += 1

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        success = False
        result_status = "exception"
        error_msg = str(e)

    # Default: if no specific checks, just verify the agent didn't crash
    if checks_total == 0:
        checks_total = 1
        if success:
            checks_passed = 1

    accuracy = checks_passed / max(checks_total, 1)

    return ScenarioResult(
        scenario_id=scenario.id,
        agent=scenario.agent,
        success=success,
        accuracy=accuracy,
        latency_ms=round(elapsed_ms, 2),
        result_status=result_status,
        checks_passed=checks_passed,
        checks_total=checks_total,
        error=error_msg,
    )


# ── Main evaluation runner ──────────────────────────────────────────────

async def run_evaluation(
    agent_filter: Optional[str] = None,
    log_to_mlflow: bool = True,
) -> dict:
    """Run all evaluation scenarios and log results.

    Parameters
    ----------
    agent_filter : str, optional
        If set, only run scenarios for this agent.
    log_to_mlflow : bool
        Whether to log results to MLflow (default True).

    Returns
    -------
    dict
        Evaluation summary with per-agent and overall metrics.
    """
    scenarios = _build_scenarios()
    if agent_filter:
        scenarios = [s for s in scenarios if s.agent == agent_filter]

    if log_to_mlflow and MLFLOW_AVAILABLE:
        setup_mlflow_tracing()

    results: list[ScenarioResult] = []
    parent_run = None

    if log_to_mlflow and MLFLOW_AVAILABLE:
        parent_run = mlflow.start_run(
            run_name=f"evaluation_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )

    try:
        for scenario in scenarios:
            result = await _run_scenario(scenario)
            results.append(result)

            # Log individual scenario to MLflow
            if log_to_mlflow and MLFLOW_AVAILABLE:
                with mlflow.start_run(
                    run_name=f"eval_{scenario.id}",
                    nested=True,
                ):
                    mlflow.set_tags({
                        "scenario_id": scenario.id,
                        "agent_name": scenario.agent,
                        "description": scenario.description,
                        "eval_type": "scenario",
                    })
                    mlflow.log_metrics({
                        "accuracy": result.accuracy,
                        "latency_ms": result.latency_ms,
                        "success": 1 if result.success else 0,
                        "checks_passed": result.checks_passed,
                        "checks_total": result.checks_total,
                    })
                    if result.error:
                        mlflow.set_tag("error", result.error[:500])
    finally:
        if parent_run:
            # Log aggregate metrics
            _log_aggregate_metrics(results)
            mlflow.end_run()

    return _build_report(results)


def _log_aggregate_metrics(results: list[ScenarioResult]):
    """Log aggregate metrics to the parent MLflow run."""
    if not MLFLOW_AVAILABLE or not results:
        return

    total = len(results)
    successes = sum(1 for r in results if r.success)
    avg_accuracy = sum(r.accuracy for r in results) / total
    avg_latency = sum(r.latency_ms for r in results) / total

    mlflow.log_metrics({
        "overall_success_rate": successes / total,
        "overall_accuracy": round(avg_accuracy, 3),
        "overall_avg_latency_ms": round(avg_latency, 2),
        "total_scenarios": total,
        "total_passed": successes,
    })

    # Per-agent metrics
    agents = set(r.agent for r in results)
    for agent in agents:
        agent_results = [r for r in results if r.agent == agent]
        agent_total = len(agent_results)
        agent_success = sum(1 for r in agent_results if r.success)
        agent_acc = sum(r.accuracy for r in agent_results) / agent_total
        agent_lat = sum(r.latency_ms for r in agent_results) / agent_total
        prefix = agent.replace("_", ".")
        mlflow.log_metrics({
            f"{prefix}.success_rate": agent_success / agent_total,
            f"{prefix}.accuracy": round(agent_acc, 3),
            f"{prefix}.avg_latency_ms": round(agent_lat, 2),
            f"{prefix}.scenarios": agent_total,
        })


def _build_report(results: list[ScenarioResult]) -> dict:
    """Build evaluation summary report."""
    total = len(results)
    successes = sum(1 for r in results if r.success)
    avg_accuracy = sum(r.accuracy for r in results) / max(total, 1)
    avg_latency = sum(r.latency_ms for r in results) / max(total, 1)

    # Per-agent breakdown
    agents = {}
    for r in results:
        if r.agent not in agents:
            agents[r.agent] = {"passed": 0, "total": 0, "accuracy_sum": 0.0,
                               "latency_sum": 0.0, "scenarios": []}
        agents[r.agent]["total"] += 1
        agents[r.agent]["accuracy_sum"] += r.accuracy
        agents[r.agent]["latency_sum"] += r.latency_ms
        if r.success:
            agents[r.agent]["passed"] += 1
        agents[r.agent]["scenarios"].append({
            "id": r.scenario_id,
            "success": r.success,
            "accuracy": r.accuracy,
            "latency_ms": r.latency_ms,
            "status": r.result_status,
            "checks": f"{r.checks_passed}/{r.checks_total}",
            "error": r.error,
        })

    agent_summaries = {}
    for agent, data in agents.items():
        agent_summaries[agent] = {
            "success_rate": round(data["passed"] / max(data["total"], 1), 3),
            "avg_accuracy": round(data["accuracy_sum"] / max(data["total"], 1), 3),
            "avg_latency_ms": round(data["latency_sum"] / max(data["total"], 1), 2),
            "passed": data["passed"],
            "total": data["total"],
            "scenarios": data["scenarios"],
        }

    # Failures
    failures = [
        {"id": r.scenario_id, "agent": r.agent, "error": r.error, "status": r.result_status}
        for r in results if not r.success
    ]

    return {
        "overall": {
            "success_rate": round(successes / max(total, 1), 3),
            "avg_accuracy": round(avg_accuracy, 3),
            "avg_latency_ms": round(avg_latency, 2),
            "total_scenarios": total,
            "passed": successes,
            "failed": total - successes,
        },
        "agents": agent_summaries,
        "failures": failures,
    }


# ── CLI entrypoint ──────────────────────────────────────────────────────

async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Run agent evaluation scenarios")
    parser.add_argument("--agent", type=str, default=None,
                        help="Filter to a specific agent")
    parser.add_argument("--no-mlflow", action="store_true",
                        help="Skip MLflow logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    report = await run_evaluation(
        agent_filter=args.agent,
        log_to_mlflow=not args.no_mlflow,
    )

    # Print summary
    overall = report["overall"]
    print(f"\n{'='*60}")
    print(f"EVALUATION REPORT — {overall['total_scenarios']} scenarios")
    print(f"{'='*60}")
    print(f"  Success Rate:  {overall['success_rate']*100:.1f}%")
    print(f"  Avg Accuracy:  {overall['avg_accuracy']*100:.1f}%")
    print(f"  Avg Latency:   {overall['avg_latency_ms']:.1f} ms")
    print(f"  Passed:        {overall['passed']}/{overall['total_scenarios']}")
    print()

    for agent, data in report["agents"].items():
        print(f"  {agent}:")
        print(f"    Pass: {data['passed']}/{data['total']}  "
              f"Accuracy: {data['avg_accuracy']*100:.1f}%  "
              f"Latency: {data['avg_latency_ms']:.1f} ms")

    if report["failures"]:
        print(f"\n  FAILURES ({len(report['failures'])}):")
        for f in report["failures"]:
            print(f"    {f['id']} ({f['agent']}): {f['error'] or f['status']}")

    print(f"{'='*60}\n")

    if MLFLOW_AVAILABLE and not args.no_mlflow:
        print(f"Results logged to MLflow experiment: {EXPERIMENT_NAME}")

    return report


if __name__ == "__main__":
    asyncio.run(_main())
