# Capacity Command — OR Utilization Optimizer Agent
#
# LangGraph state machine that:
#   1. Queries live OR schedule via DBSQL MCP stored procedure
#   2. Runs or_slot_optimizer UC Function for schedule analysis
#   3. Produces optimization suggestions with priority ranking
#
# Data flow: query_or_schedule -> or_slot_optimizer -> optimization plan

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from uc_functions.or_slot_optimizer import or_slot_optimizer
from agents.mlflow_tracing import trace_agent_run, AGENT_OR_OPTIMIZER

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────

@dataclass
class OROptimizerState:
    """LangGraph state for OR utilization optimization."""
    # Input
    target_date: Optional[date] = None  # None = today
    rooms_total: int = 8
    target_utilization_pct: float = 85.0

    # Gathered data
    schedule_data: list[dict] = field(default_factory=list)

    # Analysis results
    optimization: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)

    # Control
    error: Optional[str] = None


# ── Node functions ───────────────────────────────────────────────────────

async def fetch_or_schedule(state: OROptimizerState, conn: Any, schema: str) -> OROptimizerState:
    """Node 1: Query OR schedule from Lakebase (mirrors query_or_schedule stored proc)."""
    try:
        query = f"""
            SELECT or_id, room_id, procedure_type, surgeon_specialty,
                   scheduled_start, actual_start, est_duration_min,
                   status, previous_status, updated_at
            FROM {schema}.or_schedule
            WHERE DATE(COALESCE(scheduled_start, actual_start, updated_at)) = $1
               OR status IN ('in-progress', 'turnover')
            ORDER BY room_id, COALESCE(scheduled_start, actual_start) ASC
        """
        target = state.target_date or date.today()
        rows = await conn.fetch(query, target)
        state.schedule_data = []
        for r in rows:
            row_dict = dict(r)
            # Convert timestamps to ISO strings for JSON serialization
            for ts_field in ("scheduled_start", "actual_start", "updated_at"):
                if row_dict.get(ts_field) is not None:
                    row_dict[ts_field] = row_dict[ts_field].isoformat()
            state.schedule_data.append(row_dict)
    except Exception as e:
        logger.error("fetch_or_schedule failed: %s", e)
        state.error = f"OR schedule query failed: {e}"
    return state


def analyze_schedule(state: OROptimizerState) -> OROptimizerState:
    """Node 2: Run or_slot_optimizer UC Function on the schedule."""
    if state.error:
        return state

    result_json = or_slot_optimizer(
        schedule_json=json.dumps(state.schedule_data),
        rooms_total=state.rooms_total,
        target_utilization_pct=state.target_utilization_pct,
        turnover_min=30,
    )
    state.optimization = json.loads(result_json)
    return state


def build_or_plan(state: OROptimizerState) -> OROptimizerState:
    """Node 3: Synthesize OR optimization plan from analysis."""
    if state.error:
        state.plan = {"status": "error", "error": state.error, "suggestions": []}
        return state

    opt = state.optimization
    current_util = opt.get("current_utilization_pct", 0)
    target_util = opt.get("target_utilization_pct", 85)
    gap_pct = opt.get("gap_pct", 0)
    suggestions = opt.get("suggestions", [])
    overruns = opt.get("overrun_alerts", [])

    # Determine system status
    if overruns or gap_pct > 20:
        status = "CRITICAL"
    elif gap_pct > 10:
        status = "WARNING"
    else:
        status = "NORMAL"

    # Categorize suggestions
    immediate_actions = [s for s in suggestions if s.get("priority", 0) >= 3]
    monitor_actions = [s for s in suggestions if s.get("priority", 0) < 3]

    state.plan = {
        "status": status,
        "summary": {
            "current_utilization_pct": current_util,
            "target_utilization_pct": target_util,
            "gap_pct": gap_pct,
            "active_rooms": opt.get("active_rooms", 0),
            "rooms_total": opt.get("rooms_total", state.rooms_total),
            "total_cases": len(state.schedule_data),
            "overrun_count": len(overruns),
            "suggestion_count": len(suggestions),
        },
        "room_analysis": opt.get("room_analysis", []),
        "immediate_actions": immediate_actions,
        "monitor_actions": monitor_actions,
        "overrun_alerts": overruns,
    }
    return state


# ── Agent class ──────────────────────────────────────────────────────────

class ORUtilizationOptimizer:
    """LangGraph agent that optimizes OR utilization using UC Functions + DBSQL MCP.

    Graph: fetch_or_schedule -> analyze_schedule -> build_or_plan
    """

    def __init__(self, conn: Any, schema: str):
        self.conn = conn
        self.schema = schema

    @trace_agent_run(AGENT_OR_OPTIMIZER, "or_optimization")
    async def run(
        self,
        target_date: Optional[date] = None,
        rooms_total: int = 8,
        target_utilization_pct: float = 85.0,
    ) -> dict:
        """Execute the OR optimization graph.

        Parameters
        ----------
        target_date : date, optional
            Date to analyze (default: today).
        rooms_total : int
            Total OR rooms available (default: 8).
        target_utilization_pct : float
            Target utilization percentage (default: 85%).

        Returns
        -------
        dict
            Optimization plan with status, summary, room_analysis,
            immediate_actions, monitor_actions, and overrun_alerts.
        """
        state = OROptimizerState(
            target_date=target_date,
            rooms_total=rooms_total,
            target_utilization_pct=target_utilization_pct,
        )

        state = await fetch_or_schedule(state, self.conn, self.schema)
        state = analyze_schedule(state)
        state = build_or_plan(state)

        return state.plan
