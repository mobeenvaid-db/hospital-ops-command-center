# Capacity Command — Anomaly Pattern Detector Agent
#
# LangGraph state machine that:
#   1. Queries recent anomaly events via DBSQL MCP stored procedure
#   2. Runs anomaly_pattern_matcher UC Function to find patterns
#   3. Produces incident report with escalation recommendations
#
# Data flow: query_recent_anomalies -> anomaly_pattern_matcher -> report

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from uc_functions.anomaly_pattern_matcher import anomaly_pattern_matcher
from agents.mlflow_tracing import trace_agent_run, AGENT_ANOMALY

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────

@dataclass
class AnomalyState:
    """LangGraph state for anomaly pattern detection."""
    # Input
    lookback_hours: int = 4
    recurrence_threshold: int = 2

    # Gathered data
    events_raw: list[dict] = field(default_factory=list)

    # Analysis results
    patterns: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)

    # Control
    error: Optional[str] = None


# ── Node functions ───────────────────────────────────────────────────────

async def fetch_anomalies(state: AnomalyState, conn: Any, schema: str) -> AnomalyState:
    """Node 1: Query recent anomalies (mirrors query_recent_anomalies stored proc)."""
    try:
        query = f"""
            WITH sim AS (
                SELECT COALESCE(clock, NOW()) AS ref
                FROM {schema}.snapshot_meta WHERE id = 1
            )
            SELECT
                pred_id AS event_id,
                dept_id AS type,
                dept_id AS dept,
                predicted_value AS value,
                CASE WHEN confidence >= 0.8 THEN 'CRITICAL' ELSE 'WARNING' END AS severity,
                confidence,
                generated_at AS event_time
            FROM {schema}.predictions
            WHERE pred_type = 'anomaly'
              AND generated_at >= (SELECT ref FROM sim) - INTERVAL '{state.lookback_hours} hours'
            ORDER BY generated_at DESC
        """
        rows = await conn.fetch(query)
        state.events_raw = []
        for r in rows:
            row_dict = dict(r)
            # Convert timestamps to ISO strings
            if row_dict.get("event_time") is not None:
                row_dict["event_time"] = row_dict["event_time"].isoformat()
            row_dict["value"] = float(row_dict.get("value") or 0)
            row_dict["confidence"] = float(row_dict.get("confidence") or 0)
            state.events_raw.append(row_dict)
    except Exception as e:
        logger.error("fetch_anomalies failed: %s", e)
        state.error = f"Anomaly query failed: {e}"
    return state


def detect_patterns(state: AnomalyState) -> AnomalyState:
    """Node 2: Run anomaly_pattern_matcher UC Function."""
    if state.error:
        return state

    result_json = anomaly_pattern_matcher(
        events_json=json.dumps(state.events_raw),
        window_hours=state.lookback_hours,
        recurrence_threshold=state.recurrence_threshold,
    )
    state.patterns = json.loads(result_json)
    return state


def build_report(state: AnomalyState) -> AnomalyState:
    """Node 3: Synthesize anomaly pattern report with escalation guidance."""
    if state.error:
        state.report = {"status": "error", "error": state.error, "incidents": []}
        return state

    patterns = state.patterns
    summary = patterns.get("summary", {})
    total_events = summary.get("total_events", 0)
    pattern_count = summary.get("pattern_count", 0)
    escalations = patterns.get("escalations", [])
    correlations = patterns.get("correlations", [])
    recurring = patterns.get("recurring_patterns", [])
    hotspots = patterns.get("hotspots", [])
    recommendations = patterns.get("recommendations", [])

    # Determine overall severity
    has_critical_escalation = any(
        e.get("critical_count", 0) > 0 for e in escalations
    )
    has_critical_recurring = any(
        p.get("max_severity") == "CRITICAL" for p in recurring
    )

    if has_critical_escalation:
        status = "CRITICAL"
        headline = "Active escalation detected — surge protocol recommended"
    elif has_critical_recurring or correlations:
        status = "WARNING"
        headline = f"{pattern_count} anomaly patterns detected — investigate root cause"
    elif total_events > 0:
        status = "MONITOR"
        headline = f"{total_events} anomaly events in last {state.lookback_hours}h, no systemic patterns"
    else:
        status = "CLEAR"
        headline = f"No anomalies in last {state.lookback_hours}h"

    # Build incident objects from patterns
    incidents = []
    for esc in escalations:
        incidents.append({
            "type": "ESCALATION",
            "severity": "CRITICAL",
            "dept": esc.get("dept"),
            "detail": (
                f"{esc.get('warning_count', 0)} warnings escalated to "
                f"{esc.get('critical_count', 0)} criticals"
            ),
        })
    for corr in correlations:
        incidents.append({
            "type": "CORRELATION",
            "severity": corr.get("combined_severity", "WARNING"),
            "dept": "SYSTEM",
            "detail": f"{corr['pair'][0]} + {corr['pair'][1]}: {corr.get('explanation', '')}",
        })
    for rec_pattern in recurring:
        if rec_pattern.get("trend") == "ESCALATING":
            incidents.append({
                "type": "RECURRING_ESCALATING",
                "severity": rec_pattern.get("max_severity", "WARNING"),
                "dept": rec_pattern.get("dept"),
                "detail": (
                    f"{rec_pattern.get('type')} occurred {rec_pattern.get('count')}x "
                    f"({rec_pattern.get('trend')})"
                ),
            })

    state.report = {
        "status": status,
        "headline": headline,
        "summary": {
            "total_events": total_events,
            "pattern_count": pattern_count,
            "recurring_count": summary.get("recurring_count", 0),
            "correlation_count": summary.get("correlation_count", 0),
            "escalation_count": summary.get("escalation_count", 0),
            "lookback_hours": state.lookback_hours,
        },
        "incidents": incidents,
        "hotspots": hotspots,
        "recommendations": recommendations,
        "raw_events_count": len(state.events_raw),
    }
    return state


# ── Agent class ──────────────────────────────────────────────────────────

class AnomalyPatternDetector:
    """LangGraph agent that detects anomaly patterns using UC Functions + DBSQL MCP.

    Graph: fetch_anomalies -> detect_patterns -> build_report
    """

    def __init__(self, conn: Any, schema: str):
        self.conn = conn
        self.schema = schema

    @trace_agent_run(AGENT_ANOMALY, "anomaly_detection")
    async def run(
        self,
        lookback_hours: int = 4,
        recurrence_threshold: int = 2,
    ) -> dict:
        """Execute the anomaly pattern detection graph.

        Parameters
        ----------
        lookback_hours : int
            How far back to look for anomalies (default: 4 hours).
        recurrence_threshold : int
            Minimum occurrences to flag as recurring (default: 2).

        Returns
        -------
        dict
            Anomaly report with status, headline, summary, incidents,
            hotspots, and recommendations.
        """
        state = AnomalyState(
            lookback_hours=lookback_hours,
            recurrence_threshold=recurrence_threshold,
        )

        state = await fetch_anomalies(state, self.conn, self.schema)
        state = detect_patterns(state)
        state = build_report(state)

        return state.report
