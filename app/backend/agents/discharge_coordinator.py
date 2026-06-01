# Capacity Command — Discharge Coordinator Agent
#
# LangGraph state machine that:
#   1. Queries discharge candidates via DBSQL MCP stored procedure
#   2. Runs discharge_prioritizer UC Function to rank by readiness
#   3. Produces actionable discharge plan with staffing suggestions
#
# Data flow: query_discharge_candidates -> discharge_prioritizer -> plan

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from uc_functions.discharge_prioritizer import discharge_prioritizer
from agents.mlflow_tracing import trace_agent_run, AGENT_DISCHARGE

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────

@dataclass
class DischargeState:
    """LangGraph state for discharge coordination."""
    # Input
    dept_filter: Optional[str] = None  # None = all departments
    top_n: int = 10

    # Gathered data
    candidates_raw: list[dict] = field(default_factory=list)
    current_hour: int = 12

    # Analysis results
    prioritized: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)

    # Control
    error: Optional[str] = None


# ── Node functions ───────────────────────────────────────────────────────

async def fetch_discharge_candidates(
    state: DischargeState, conn: Any, schema: str
) -> DischargeState:
    """Node 1: Query discharge candidates (mirrors query_discharge_candidates stored proc)."""
    try:
        # Get simulation clock for current hour
        clock_query = f"SELECT COALESCE(clock, NOW()) AS ref FROM {schema}.snapshot_meta WHERE id = 1"
        clock_row = await conn.fetchrow(clock_query)
        if clock_row:
            ref_time = clock_row["ref"]
            state.current_hour = ref_time.hour if hasattr(ref_time, "hour") else 12

        # Query patients with predicted discharge
        if state.dept_filter:
            query = f"""
                SELECT p.patient_id, p.mrn, p.name, p.age, p.bed_id, p.dept_id,
                       p.lace_score, p.lace_risk, p.length_of_stay_days,
                       p.acuity_score, p.predicted_discharge_time,
                       p.discharge_reason, p.confidence_score,
                       EXTRACT(EPOCH FROM (p.predicted_discharge_time - $2)) / 3600.0
                           AS hours_until_discharge
                FROM {schema}.patients p
                WHERE p.predicted_discharge_time IS NOT NULL
                  AND p.bed_id IS NOT NULL
                  AND p.dept_id = $1
                ORDER BY p.predicted_discharge_time ASC, p.lace_score ASC
            """
            ref = clock_row["ref"] if clock_row else datetime.now()
            rows = await conn.fetch(query, state.dept_filter, ref)
        else:
            query = f"""
                SELECT p.patient_id, p.mrn, p.name, p.age, p.bed_id, p.dept_id,
                       p.lace_score, p.lace_risk, p.length_of_stay_days,
                       p.acuity_score, p.predicted_discharge_time,
                       p.discharge_reason, p.confidence_score,
                       EXTRACT(EPOCH FROM (p.predicted_discharge_time - $1)) / 3600.0
                           AS hours_until_discharge
                FROM {schema}.patients p
                WHERE p.predicted_discharge_time IS NOT NULL
                  AND p.bed_id IS NOT NULL
                ORDER BY p.predicted_discharge_time ASC, p.lace_score ASC
            """
            ref = clock_row["ref"] if clock_row else datetime.now()
            rows = await conn.fetch(query, ref)

        state.candidates_raw = []
        for r in rows:
            row_dict = dict(r)
            # Convert timestamps to ISO strings
            if row_dict.get("predicted_discharge_time") is not None:
                row_dict["predicted_discharge_time"] = (
                    row_dict["predicted_discharge_time"].isoformat()
                )
            # Ensure numeric types
            row_dict["hours_until_discharge"] = float(row_dict.get("hours_until_discharge") or 24)
            row_dict["lace_score"] = int(row_dict.get("lace_score") or 5)
            row_dict["acuity_score"] = int(row_dict.get("acuity_score") or 3)
            row_dict["length_of_stay_days"] = float(row_dict.get("length_of_stay_days") or 1)
            row_dict["confidence_score"] = float(row_dict.get("confidence_score") or 0.5)
            state.candidates_raw.append(row_dict)

    except Exception as e:
        logger.error("fetch_discharge_candidates failed: %s", e)
        state.error = f"Discharge candidates query failed: {e}"
    return state


def prioritize_discharges(state: DischargeState) -> DischargeState:
    """Node 2: Run discharge_prioritizer UC Function."""
    if state.error:
        return state

    result_json = discharge_prioritizer(
        patients_json=json.dumps(state.candidates_raw),
        current_hour=state.current_hour,
        top_n=state.top_n,
    )
    state.prioritized = json.loads(result_json)
    return state


def build_discharge_plan(state: DischargeState) -> DischargeState:
    """Node 3: Synthesize actionable discharge plan."""
    if state.error:
        state.plan = {"status": "error", "error": state.error, "candidates": []}
        return state

    candidates = state.prioritized.get("candidates", [])
    summary = state.prioritized.get("summary", {})
    total = state.prioritized.get("total_evaluated", 0)

    ready = summary.get("ready", 0)
    likely = summary.get("likely", 0)

    # Determine urgency
    if ready >= 5:
        status = "HIGH_VOLUME"
        headline = f"{ready} patients ready for discharge — coordinate transport and follow-up"
    elif ready >= 2:
        status = "MODERATE"
        headline = f"{ready} patients ready, {likely} likely — begin discharge workflows"
    elif ready >= 1:
        status = "NORMAL"
        headline = f"{ready} patient ready for discharge"
    else:
        status = "LOW"
        headline = "No patients currently flagged as discharge-ready"

    # Group candidates by department for staffing suggestions
    dept_counts: dict[str, int] = {}
    for c in candidates:
        if c.get("readiness") in ("READY", "LIKELY"):
            dept = c.get("dept_id", "UNKNOWN")
            dept_counts[dept] = dept_counts.get(dept, 0) + 1

    staffing = []
    for dept, count in sorted(dept_counts.items(), key=lambda x: -x[1]):
        if count >= 3:
            staffing.append({
                "dept": dept,
                "action": f"Assign dedicated discharge coordinator — {count} pending discharges",
            })
        elif count >= 1:
            staffing.append({
                "dept": dept,
                "action": f"Monitor {count} pending discharge(s)",
            })

    # High-LACE risk warnings (patients with LACE >= 10 who are being discharged)
    high_risk_discharges = [
        c for c in candidates
        if c.get("readiness") in ("READY", "LIKELY") and c.get("lace_score", 0) >= 10
    ]

    state.plan = {
        "status": status,
        "headline": headline,
        "summary": {
            "total_evaluated": total,
            "ready": ready,
            "likely": likely,
            "possible": summary.get("possible", 0),
            "not_ready": summary.get("not_ready", 0),
            "high_lace_risk_discharges": len(high_risk_discharges),
        },
        "candidates": candidates,
        "high_risk_warnings": [
            {
                "patient_id": c.get("patient_id"),
                "bed_id": c.get("bed_id"),
                "dept_id": c.get("dept_id"),
                "lace_score": c.get("lace_score"),
                "warning": f"LACE {c.get('lace_score', 0)}: High readmission risk — ensure follow-up scheduled",
            }
            for c in high_risk_discharges
        ],
        "staffing_suggestions": staffing,
    }
    return state


# ── Agent class ──────────────────────────────────────────────────────────

class DischargeCoordinator:
    """LangGraph agent that coordinates discharges using UC Functions + DBSQL MCP.

    Graph: fetch_discharge_candidates -> prioritize_discharges -> build_discharge_plan
    """

    def __init__(self, conn: Any, schema: str):
        self.conn = conn
        self.schema = schema

    @trace_agent_run(AGENT_DISCHARGE, "discharge_coordination")
    async def run(
        self,
        dept_filter: Optional[str] = None,
        top_n: int = 10,
    ) -> dict:
        """Execute the discharge coordination graph.

        Parameters
        ----------
        dept_filter : str, optional
            If set, only analyze this department.
        top_n : int
            Number of top candidates to return (default: 10).

        Returns
        -------
        dict
            Discharge plan with status, headline, summary, candidates,
            high_risk_warnings, and staffing_suggestions.
        """
        state = DischargeState(dept_filter=dept_filter, top_n=top_n)

        state = await fetch_discharge_candidates(state, self.conn, self.schema)
        state = prioritize_discharges(state)
        state = build_discharge_plan(state)

        return state.plan
