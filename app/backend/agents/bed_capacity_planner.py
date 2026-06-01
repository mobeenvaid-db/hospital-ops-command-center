# Capacity Command — Bed Capacity Planner Agent
#
# LangGraph state machine that:
#   1. Queries live bed/forecast data via DBSQL MCP stored procedures
#   2. Runs bed_gap_calculator UC Function per department
#   3. Synthesizes cross-department capacity plan with recommendations
#
# Data flow: query_forecast + bed state -> bed_gap_calculator -> plan

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from uc_functions.bed_gap_calculator import bed_gap_calculator
from agents.mlflow_tracing import trace_agent_run, AGENT_BED_CAPACITY

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────

@dataclass
class BedCapacityState:
    """LangGraph state for bed capacity planning."""
    # Input
    dept_filter: Optional[str] = None  # None = all departments

    # Gathered data (populated by query nodes)
    bed_data: list[dict] = field(default_factory=list)
    forecast_data: list[dict] = field(default_factory=list)
    discharge_scores: dict[str, list[float]] = field(default_factory=dict)

    # Analysis results (populated by compute nodes)
    dept_gaps: list[dict] = field(default_factory=list)
    plan: dict = field(default_factory=dict)

    # Control
    error: Optional[str] = None


# ── Node functions ───────────────────────────────────────────────────────

async def fetch_bed_status(state: BedCapacityState, conn: Any, schema: str) -> BedCapacityState:
    """Node 1: Query current bed status from Lakebase."""
    try:
        query = f"""
            SELECT dept_id, bed_id, status, acuity, hours_in_bed,
                   predicted_discharge_time, confidence_score, lace_score
            FROM {schema}.beds
            ORDER BY dept_id, bed_id
        """
        rows = await conn.fetch(query)
        state.bed_data = [dict(r) for r in rows]

        # Build discharge score arrays per department
        for r in state.bed_data:
            if r.get("status") == "occupied" and r.get("confidence_score") is not None:
                dept = r.get("dept_id", "UNKNOWN")
                state.discharge_scores.setdefault(dept, []).append(
                    float(r["confidence_score"])
                )
    except Exception as e:
        logger.error("fetch_bed_status failed: %s", e)
        state.error = f"Bed query failed: {e}"
    return state


async def fetch_forecast(state: BedCapacityState, conn: Any, schema: str) -> BedCapacityState:
    """Node 2: Query arrival forecast from DBSQL MCP stored procedure."""
    if state.error:
        return state
    try:
        # Mirrors: SELECT * FROM hospital_ops_lakebase.query_forecast(4)
        query = f"""
            SELECT pred_type, dept_id, predicted_value, confidence
            FROM {schema}.predictions
            WHERE pred_type IN ('arrival', 'capacity')
              AND predicted_time <= (
                  SELECT COALESCE(clock, NOW()) FROM {schema}.snapshot_meta WHERE id = 1
              ) + INTERVAL '2 hours'
              AND predicted_time >= (
                  SELECT COALESCE(clock, NOW()) FROM {schema}.snapshot_meta WHERE id = 1
              )
            ORDER BY pred_type, predicted_time
        """
        rows = await conn.fetch(query)
        state.forecast_data = [dict(r) for r in rows]
    except Exception as e:
        logger.error("fetch_forecast failed: %s", e)
        state.error = f"Forecast query failed: {e}"
    return state


def compute_gaps(state: BedCapacityState) -> BedCapacityState:
    """Node 3: Run bed_gap_calculator UC Function for each department."""
    if state.error:
        return state

    # Aggregate bed data by department
    dept_stats: dict[str, dict] = {}
    for bed in state.bed_data:
        dept = bed.get("dept_id", "UNKNOWN")
        if state.dept_filter and dept != state.dept_filter:
            continue
        stats = dept_stats.setdefault(dept, {
            "total": 0, "occupied": 0, "available": 0,
        })
        stats["total"] += 1
        if bed.get("status") == "occupied":
            stats["occupied"] += 1
        elif bed.get("status") == "available":
            stats["available"] += 1

    # Get total forecast arrivals (sum of arrival predictions in 2h window)
    total_arrivals_2h = sum(
        float(f.get("predicted_value", 0))
        for f in state.forecast_data
        if f.get("pred_type") == "arrival"
    )

    # Admission share heuristic: distribute proportionally by dept bed count
    total_beds = sum(d["total"] for d in dept_stats.values()) or 1

    for dept, stats in dept_stats.items():
        admission_share = stats["total"] / total_beds
        discharge_scores = state.discharge_scores.get(dept, [])

        result_json = bed_gap_calculator(
            dept_id=dept,
            available_beds=stats["available"],
            occupied_beds=stats["occupied"],
            total_beds=stats["total"],
            forecast_arrivals_2h=total_arrivals_2h,
            discharge_scores_json=json.dumps(discharge_scores),
            discharge_threshold=0.6,
            dept_admission_share=admission_share,
        )
        state.dept_gaps.append(json.loads(result_json))

    # Sort by gap descending (most critical first)
    state.dept_gaps.sort(key=lambda x: x.get("gap", 0), reverse=True)
    return state


def build_plan(state: BedCapacityState) -> BedCapacityState:
    """Node 4: Synthesize capacity plan from gap analysis."""
    if state.error:
        state.plan = {"status": "error", "error": state.error, "actions": []}
        return state

    critical = [g for g in state.dept_gaps if g.get("risk_level") == "CRITICAL"]
    warning = [g for g in state.dept_gaps if g.get("risk_level") == "WARNING"]
    normal = [g for g in state.dept_gaps if g.get("risk_level") == "NORMAL"]

    # System-level status
    if critical:
        system_status = "CRITICAL"
    elif warning:
        system_status = "WARNING"
    else:
        system_status = "NORMAL"

    # Build action items
    actions = []
    for gap in critical:
        actions.append({
            "priority": "IMMEDIATE",
            "dept": gap["dept_id"],
            "action": gap["recommendation"],
            "gap": gap["gap"],
        })
    for gap in warning:
        actions.append({
            "priority": "MONITOR",
            "dept": gap["dept_id"],
            "action": gap["recommendation"],
            "gap": gap["gap"],
        })

    # Total capacity metrics
    total_available = sum(g.get("available_beds", 0) for g in state.dept_gaps)
    total_occupied = sum(g.get("occupied_beds", 0) for g in state.dept_gaps)
    total_beds = sum(g.get("total_beds", 0) for g in state.dept_gaps)
    total_discharges_2h = sum(g.get("predicted_discharges_2h", 0) for g in state.dept_gaps)

    state.plan = {
        "status": system_status,
        "summary": {
            "total_beds": total_beds,
            "occupied": total_occupied,
            "available": total_available,
            "occupancy_pct": round(100 * total_occupied / max(total_beds, 1), 1),
            "predicted_discharges_2h": total_discharges_2h,
            "departments_critical": len(critical),
            "departments_warning": len(warning),
            "departments_normal": len(normal),
        },
        "department_gaps": state.dept_gaps,
        "actions": actions,
    }
    return state


# ── Agent class ──────────────────────────────────────────────────────────

class BedCapacityPlanner:
    """LangGraph agent that plans bed capacity using UC Functions + DBSQL MCP.

    Graph: fetch_bed_status -> fetch_forecast -> compute_gaps -> build_plan
    """

    def __init__(self, conn: Any, schema: str):
        self.conn = conn
        self.schema = schema

    @trace_agent_run(AGENT_BED_CAPACITY, "capacity_plan")
    async def run(self, dept_filter: Optional[str] = None) -> dict:
        """Execute the bed capacity planning graph.

        Parameters
        ----------
        dept_filter : str, optional
            If set, only analyze this department (e.g. "ICU").
            If None, analyze all departments.

        Returns
        -------
        dict
            Capacity plan with status, summary, department_gaps, and actions.
        """
        state = BedCapacityState(dept_filter=dept_filter)

        # Execute graph nodes sequentially (each depends on prior state)
        state = await fetch_bed_status(state, self.conn, self.schema)
        state = await fetch_forecast(state, self.conn, self.schema)
        state = compute_gaps(state)
        state = build_plan(state)

        return state.plan
