# Capacity Command — SQL queries (Lakebase or Delta)
#
# Tables: departments, beds, ed_arrivals, or_schedule,
#         dept_metrics, predictions, snapshot_meta
#
# All queries use schema-qualified table names via f-string schema.
# User-supplied values (like limit) use asyncpg $1 parameters.
# All functions are async; conn is SqlExecutor (or None in demo mode).

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sql_executor import SqlExecutor


async def get_header_metrics(conn: Any, schema: str) -> dict:
    """Header metrics from beds, ed_arrivals, or_schedule, snapshot_meta."""
    if conn is None:
        return _default_header_metrics()
    try:
        row = await conn.fetchrow(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {schema}.beds WHERE status = 'occupied')
                    AS total_census,
                (SELECT COUNT(*) FROM {schema}.beds)
                    AS total_beds,
                (SELECT COUNT(*) FROM {schema}.ed_arrivals WHERE disposition = 'waiting')
                    AS ed_waiting,
                (SELECT COUNT(*) FROM {schema}.ed_arrivals
                 WHERE disposition = 'waiting' AND bed_assigned IS NOT NULL
                   AND triage_complete IS NOT NULL)
                    AS ed_boarders,
                (SELECT GREATEST(0, COALESCE(AVG(wait_minutes), 0))
                 FROM {schema}.ed_arrivals WHERE disposition = 'waiting')
                    AS avg_wait_min,
                (SELECT COUNT(*) FROM {schema}.beds WHERE status = 'available')
                    AS beds_available,
                (SELECT COUNT(DISTINCT room_id) FROM {schema}.or_schedule
                 WHERE status = 'in-progress')
                    AS or_active,
                (SELECT COUNT(DISTINCT room_id) FROM {schema}.or_schedule)
                    AS or_total,
                (SELECT clock FROM {schema}.snapshot_meta WHERE id = 1)
                    AS clock,
                (SELECT COALESCE(
                    EXTRACT(EPOCH FROM (NOW() - MAX(last_updated))), 999999)
                 FROM {schema}.beds)
                    AS staleness_seconds
            """
        )
        if not row:
            return _default_header_metrics()
        return {
            "total_census": row["total_census"] or 0,
            "total_beds": row["total_beds"] or 0,
            "ed_waiting": row["ed_waiting"] or 0,
            "ed_boarders": row["ed_boarders"] or 0,
            "avg_wait_min": float(row["avg_wait_min"] or 0),
            "beds_available": row["beds_available"] or 0,
            "or_active": row["or_active"] or 0,
            "or_total": row["or_total"] or 0,
            "clock": _to_iso8601(row["clock"]) if row["clock"] else _now_iso8601(),
            "staleness_seconds": float(row["staleness_seconds"] or 0),
        }
    except Exception:
        return _default_header_metrics()


async def get_dept_occupancy(conn: Any, schema: str) -> dict[str, float]:
    """Department occupancy from beds table for system status computation."""
    if conn is None:
        return {}
    try:
        rows = await conn.fetch(
            f"""
            SELECT
                b.dept_id,
                ROUND(100.0 * COUNT(*) FILTER (WHERE b.status = 'occupied')
                      / NULLIF(COUNT(*), 0), 1) AS occupancy_pct
            FROM {schema}.beds b
            GROUP BY b.dept_id
            """
        )
        return {row["dept_id"]: float(row["occupancy_pct"] or 0) for row in rows}
    except Exception:
        return {}


async def get_beds(conn: Any, schema: str) -> list[dict]:
    """All beds joined with departments. Computes hours_in_bed from admission_time."""
    if conn is None:
        return []
    try:
        rows = await conn.fetch(
            f"""
            WITH sim AS (SELECT COALESCE(clock, NOW()) AS ref FROM {schema}.snapshot_meta WHERE id = 1)
            SELECT
                b.bed_id,
                b.dept_id,
                d.name AS dept_name,
                b.status,
                b.acuity_level AS acuity,
                CASE
                    WHEN b.admission_time IS NOT NULL
                    THEN ROUND(EXTRACT(EPOCH FROM ((SELECT ref FROM sim) - b.admission_time)) / 3600.0, 1)
                    ELSE NULL
                END AS hours_in_bed,
                b.expected_discharge,
                b.blocked_reason
            FROM {schema}.beds b
            LEFT JOIN {schema}.departments d ON b.dept_id = d.dept_id
            ORDER BY b.dept_id, b.bed_id
            """
        )
        return [_record_to_dict(row) for row in rows]
    except Exception:
        return []


async def get_ed_metrics(conn: Any, schema: str) -> dict:
    """ED metrics computed live from ed_arrivals table."""
    if conn is None:
        return _default_ed_metrics()
    try:
        row = await conn.fetchrow(
            f"""
            WITH sim AS (SELECT COALESCE(clock, NOW()) AS ref FROM {schema}.snapshot_meta WHERE id = 1),
            current_ed AS (
                SELECT *,
                    EXTRACT(EPOCH FROM ((SELECT ref FROM sim) - arrival_time)) / 60.0 AS live_wait_min
                FROM {schema}.ed_arrivals
            ),
            waiting AS (
                SELECT * FROM current_ed WHERE disposition = 'waiting'
            ),
            recent AS (
                SELECT * FROM current_ed
                WHERE arrival_time >= (SELECT ref FROM sim) - INTERVAL '4 hours'
            )
            SELECT
                (SELECT COUNT(*) FROM waiting) AS waiting_count,
                (SELECT COUNT(*) FROM waiting
                 WHERE bed_assigned IS NOT NULL AND triage_complete IS NOT NULL)
                    AS boarders_count,
                (SELECT GREATEST(0, COALESCE(AVG(live_wait_min), 0)) FROM waiting)
                    AS avg_wait_min,
                (SELECT ROUND(100.0 *
                    COUNT(*) FILTER (WHERE disposition = 'transferred' OR
                        (disposition = 'waiting' AND live_wait_min > 120))
                    / NULLIF(COUNT(*), 0), 1)
                 FROM current_ed
                 WHERE arrival_time >= (SELECT ref FROM sim) - INTERVAL '4 hours')
                    AS lwbs_rate_pct,
                (SELECT json_agg(cnt ORDER BY hr)
                 FROM (
                     SELECT
                         EXTRACT(HOUR FROM arrival_time) AS hr,
                         COUNT(*)::float AS cnt
                     FROM recent
                     GROUP BY EXTRACT(HOUR FROM arrival_time)
                     ORDER BY hr DESC
                     LIMIT 4
                 ) sub)
                    AS arrivals_per_hour,
                (SELECT json_object_agg(esi::text, cnt)
                 FROM (
                     SELECT esi_level AS esi, COUNT(*) AS cnt
                     FROM waiting
                     GROUP BY esi_level
                 ) sub)
                    AS esi_breakdown
            """
        )
        if not row:
            return _default_ed_metrics()

        # Parse JSON columns (asyncpg may return str or dict depending on codec)
        arrivals = row["arrivals_per_hour"]
        if isinstance(arrivals, str):
            arrivals = json.loads(arrivals)
        if not arrivals:
            arrivals = [0, 0, 0, 0]

        esi = row["esi_breakdown"]
        if isinstance(esi, str):
            esi = json.loads(esi)
        if not esi:
            esi = {}

        return {
            "waiting_count": row["waiting_count"] or 0,
            "boarders_count": row["boarders_count"] or 0,
            "avg_wait_min": float(row["avg_wait_min"] or 0),
            "lwbs_rate_pct": float(row["lwbs_rate_pct"] or 0),
            "arrivals_per_hour": arrivals,
            "esi_breakdown": esi,
        }
    except Exception:
        return _default_ed_metrics()


async def get_or_metrics(conn: Any, schema: str) -> dict:
    """OR metrics computed from or_schedule table."""
    if conn is None:
        return _default_or_metrics()
    try:
        summary = await conn.fetchrow(
            f"""
            WITH rooms AS (
                SELECT DISTINCT room_id FROM {schema}.or_schedule
            ),
            active AS (
                SELECT * FROM {schema}.or_schedule
                WHERE status IN ('in-progress', 'scheduled', 'turnover')
            )
            SELECT
                ROUND(100.0 * (SELECT COUNT(DISTINCT room_id) FROM active
                               WHERE status = 'in-progress')
                      / NULLIF((SELECT COUNT(*) FROM rooms), 0), 1)
                    AS utilization_pct,
                ROUND(100.0 * (
                    SELECT COUNT(*) FILTER (WHERE actual_start IS NOT NULL
                        AND actual_start <= scheduled_start + INTERVAL '15 minutes')
                    FROM (
                        SELECT DISTINCT ON (room_id) room_id, scheduled_start, actual_start
                        FROM {schema}.or_schedule
                        WHERE scheduled_start IS NOT NULL
                        ORDER BY room_id, scheduled_start ASC
                    ) first_cases
                ) / NULLIF((SELECT COUNT(*) FROM rooms), 0), 1)
                    AS fcots_pct
            """
        )
        utilization_pct = float(summary["utilization_pct"] or 0) if summary else 0.0
        fcots_pct = float(summary["fcots_pct"] or 0) if summary else 0.0

        # Fetch room details - always return all 8 rooms, show "available" when no active cases
        room_rows = await conn.fetch(
            f"""
            WITH sim AS (SELECT COALESCE(clock, NOW()) AS ref FROM {schema}.snapshot_meta WHERE id = 1),
            all_rooms AS (
                SELECT 'OR-' || generate_series(1, 8) AS room_id
            ),
            latest_cases AS (
                SELECT DISTINCT ON (room_id)
                    room_id,
                    status,
                    procedure_type,
                    surgeon_specialty,
                    actual_start,
                    est_duration_min,
                    scheduled_start
                FROM {schema}.or_schedule
                WHERE status IN ('in-progress', 'scheduled', 'turnover')
                ORDER BY room_id, CASE status
                    WHEN 'in-progress' THEN 1
                    WHEN 'turnover' THEN 2
                    WHEN 'scheduled' THEN 3
                    END
            )
            SELECT
                ar.room_id,
                COALESCE(lc.status, 'available') AS status,
                lc.procedure_type AS procedure,
                lc.surgeon_specialty,
                CASE WHEN lc.actual_start IS NOT NULL
                     THEN ROUND(EXTRACT(EPOCH FROM ((SELECT ref FROM sim) - lc.actual_start)) / 60.0)
                     ELSE NULL END AS elapsed_min,
                CASE WHEN lc.actual_start IS NOT NULL AND lc.est_duration_min IS NOT NULL
                     THEN GREATEST(0, lc.est_duration_min -
                          ROUND(EXTRACT(EPOCH FROM ((SELECT ref FROM sim) - lc.actual_start)) / 60.0))
                     ELSE NULL END AS est_remaining_min,
                lc.scheduled_start
            FROM all_rooms ar
            LEFT JOIN latest_cases lc ON ar.room_id = lc.room_id
            ORDER BY ar.room_id
            """
        )
        rooms = [_record_to_dict(r) for r in room_rows]

        active_procedures = [
            {
                "room_id": r.get("room_id"),
                "procedure": r.get("procedure"),
                "surgeon_specialty": r.get("surgeon_specialty"),
                "elapsed_min": r.get("elapsed_min"),
                "est_remaining_min": r.get("est_remaining_min"),
            }
            for r in rooms
            if r.get("status") == "in-progress"
        ]

        # Next available: earliest room that isn't in-progress
        next_eta = None
        for r in rooms:
            if r.get("status") != "in-progress" and r.get("scheduled_start"):
                next_eta = r["scheduled_start"]
                break

        return {
            "utilization_pct": utilization_pct,
            "fcots_pct": fcots_pct,
            "rooms": rooms,
            "active_procedures": active_procedures,
            "next_available_eta": next_eta,
        }
    except Exception:
        return _default_or_metrics()


async def get_forecast_data(conn: Any, schema: str) -> dict:
    """Aggregate predictions table (row-per-prediction) into dashboard shape."""
    if conn is None:
        return _default_forecast_data()
    try:
        rows = await conn.fetch(
            f"""
            SELECT pred_type, dept_id, predicted_time, predicted_value, confidence
            FROM {schema}.predictions
            ORDER BY generated_at DESC, pred_type
            """
        )

        # Current available beds per department (for capacity_by_dept)
        avail_rows = await conn.fetch(
            f"""
            SELECT dept_id, COUNT(*) AS available
            FROM {schema}.beds
            WHERE status = 'available'
            GROUP BY dept_id
            """
        )
        available_by_dept: dict[str, int] = {
            r["dept_id"]: r["available"] for r in avail_rows
        }

        discharges_2h = 0
        discharges_4h = 0
        arrival_forecast = []
        capacity_by_dept = []
        anomalies = []

        # Count discharge predictions by time window.
        # Confidence filtering happens at write time in the notebook (score > 0.3).
        # Use sim_clock instead of wall clock to match simulated time
        sim_row = await conn.fetchrow(
            f"SELECT clock FROM {schema}.snapshot_meta WHERE id = 1"
        )
        now = sim_row["clock"] if sim_row and sim_row["clock"] else datetime.now(timezone.utc)
        for r in rows:
            pt = r["pred_type"] or ""
            if pt == "discharge":
                pred_time = r["predicted_time"]
                if pred_time and pred_time <= now + timedelta(hours=2):
                    discharges_2h += 1
                if pred_time and pred_time <= now + timedelta(hours=4):
                    discharges_4h += 1
            elif pt == "ed_arrival":
                arrival_forecast.append({
                    "hour": _extract_hour(r["predicted_time"]),
                    "predicted": r["predicted_value"] or 0,
                    "actual": None,
                })
            elif pt == "capacity":
                dept = r["dept_id"] or ""
                capacity_by_dept.append({
                    "dept_id": dept,
                    "current_available": available_by_dept.get(dept, 0),
                    "predicted_2h": int(r["predicted_value"] or 0),
                    "confidence": r["confidence"] or 0.7,
                })
            elif pt == "anomaly":
                anomalies.append({
                    "type": r["dept_id"] or "UNKNOWN",
                    "dept": r["dept_id"] or "",
                    "value": r["predicted_value"] or 0,
                    "severity": "WARNING" if (r["confidence"] or 0) < 0.8 else "CRITICAL",
                    "confidence": r["confidence"] or 0,
                })

        return {
            "predicted_discharges_2h": discharges_2h,
            "predicted_discharges_4h": discharges_4h,
            "arrival_forecast": arrival_forecast,
            "capacity_by_dept": capacity_by_dept,
            "anomalies": anomalies,
        }
    except Exception:
        return _default_forecast_data()


async def get_recent_events(conn: Any, schema: str, limit: int = 50) -> tuple[list[dict], Optional[str]]:
    """Derive recent events from operational tables (beds, or_schedule, ed_arrivals).

    Instead of querying a dedicated event_log table (anti-pattern for Lakebase),
    we derive the event feed from recent state changes in the operational tables
    that legitimately belong in a low-latency serving layer.
    """
    if conn is None:
        return [], None
    try:
        rows = await conn.fetch(
            f"""
            WITH bed_events AS (
                SELECT
                    'bed-' || bed_id AS event_id,
                    last_updated AS event_time,
                    'bed' AS entity_type,
                    bed_id AS entity_id,
                    previous_status AS old_status,
                    status AS new_status
                FROM {schema}.beds
                WHERE last_updated IS NOT NULL
            ),
            or_events AS (
                SELECT
                    'or-' || or_id AS event_id,
                    updated_at AS event_time,
                    'or' AS entity_type,
                    room_id || ' — ' || COALESCE(procedure_type, 'procedure') AS entity_id,
                    previous_status AS old_status,
                    status AS new_status
                FROM {schema}.or_schedule
                WHERE updated_at IS NOT NULL
            ),
            ed_events AS (
                SELECT
                    'ed-' || arrival_id AS event_id,
                    updated_at AS event_time,
                    'ed' AS entity_type,
                    arrival_id AS entity_id,
                    previous_disposition AS old_status,
                    disposition AS new_status
                FROM {schema}.ed_arrivals
                WHERE updated_at IS NOT NULL
            ),
            all_events AS (
                SELECT * FROM bed_events
                UNION ALL
                SELECT * FROM or_events
                UNION ALL
                SELECT * FROM ed_events
            )
            SELECT event_id, event_time, entity_type, entity_id, old_status, new_status
            FROM all_events
            ORDER BY event_time DESC
            LIMIT $1
            """,
            limit,
        )
        events = [_record_to_dict(r) for r in rows]
        last_time = _to_iso8601(rows[0]["event_time"]) if rows and rows[0]["event_time"] else None
        return events, last_time
    except Exception:
        return [], None


async def get_pipeline_status(conn: Any, schema: str) -> dict:
    """Per-table: last write (Delta), last in Lakebase (synced_at), lag. Pipeline staleness = snapshot_meta.clock."""
    if conn is None:
        return {"pipeline_ts": None, "pipeline_lag_sec": 999999.0, "tables": []}
    try:
        # Pipeline staleness: snapshot_meta.clock written every cycle — if fresh, pipeline is working
        pipeline_row = await conn.fetchrow(
            f"SELECT max(clock) AS pipeline_ts FROM {schema}.snapshot_meta"
        )
        pipeline_ts = pipeline_row["pipeline_ts"] if pipeline_row else None
        pipeline_lag = 999999.0
        if pipeline_ts:
            lag_row = await conn.fetchrow(
                "SELECT EXTRACT(EPOCH FROM (NOW() - $1::timestamptz))::float AS lag",
                pipeline_ts,
            )
            pipeline_lag = float(lag_row["lag"]) if lag_row and lag_row["lag"] else 999999.0

        # Per-table: last_write (Delta), last_in_lakebase (synced_at), lag_sec = synced_at - last_write
        # synced_at set by trigger when Delta Sync writes; requires scripts/add_synced_at.sql migration
        rows = await conn.fetch(
            f"""
            SELECT 'beds' AS tbl, count(*) AS rows,
                   max(last_updated) AS last_write_delta,
                   max(synced_at) AS last_in_lakebase,
                   EXTRACT(EPOCH FROM (max(synced_at) - max(last_updated)))::float AS lag_sec
            FROM {schema}.beds
            UNION ALL
            SELECT 'ed_arrivals', count(*), max(updated_at), max(synced_at),
                   EXTRACT(EPOCH FROM (max(synced_at) - max(updated_at)))::float
            FROM {schema}.ed_arrivals
            UNION ALL
            SELECT 'or_schedule', count(*), max(updated_at), max(synced_at),
                   EXTRACT(EPOCH FROM (max(synced_at) - max(updated_at)))::float
            FROM {schema}.or_schedule
            UNION ALL
            SELECT 'snapshot_meta', count(*), max(clock), max(synced_at),
                   EXTRACT(EPOCH FROM (max(synced_at) - max(clock)))::float
            FROM {schema}.snapshot_meta
            ORDER BY tbl
            """
        )
        tables = [_record_to_dict(r) for r in rows]
        return {
            "pipeline_ts": pipeline_ts,
            "pipeline_lag_sec": round(pipeline_lag, 1),
            "tables": tables,
        }
    except Exception:
        # Fallback when synced_at not yet added (migration not run)
        pipeline_ts = None
        pipeline_lag = 999999.0
        try:
            rows = await conn.fetch(
                f"""
                SELECT 'beds' AS tbl, count(*) AS rows, max(last_updated) AS last_write_delta,
                       max(last_updated) AS last_in_lakebase, 0::float AS lag_sec
                FROM {schema}.beds
                UNION ALL
                SELECT 'ed_arrivals', count(*), max(updated_at), max(updated_at), 0::float
                FROM {schema}.ed_arrivals
                UNION ALL
                SELECT 'or_schedule', count(*), max(updated_at), max(updated_at), 0::float
                FROM {schema}.or_schedule
                UNION ALL
                SELECT 'snapshot_meta', count(*), max(clock), max(clock), 0::float
                FROM {schema}.snapshot_meta
                ORDER BY tbl
                """
            )
            return {
                "pipeline_ts": pipeline_ts,
                "pipeline_lag_sec": round(pipeline_lag, 1),
                "tables": [_record_to_dict(r) for r in rows],
            }
        except Exception:
            return {"pipeline_ts": None, "pipeline_lag_sec": 999999.0, "tables": []}


async def get_dept_metrics(conn: Any, schema: str) -> list[dict]:
    """Department metrics: compute from beds table with department join."""
    if conn is None:
        return []
    try:
        rows = await conn.fetch(
            f"""
            SELECT
                d.dept_id,
                d.name,
                ROUND(100.0 * COUNT(*) FILTER (WHERE b.status = 'occupied')
                      / NULLIF(d.total_beds, 0), 1) AS occupancy_pct,
                d.total_beds,
                COUNT(*) FILTER (WHERE b.status = 'available') AS available,
                COUNT(*) FILTER (WHERE b.status = 'occupied') AS occupied,
                COUNT(*) FILTER (WHERE b.status = 'cleaning') AS cleaning,
                COUNT(*) FILTER (WHERE b.status = 'blocked') AS blocked
            FROM {schema}.departments d
            LEFT JOIN {schema}.beds b ON d.dept_id = b.dept_id
            WHERE d.dept_id NOT IN ('OR')
            GROUP BY d.dept_id, d.name, d.total_beds
            ORDER BY d.dept_id
            """
        )
        return [_record_to_dict(row) for row in rows]
    except Exception:
        return []


# ── Helpers ───────────────────────────────────────────────────────────

def _record_to_dict(record) -> dict:
    """Convert an asyncpg Record to a plain dict with ISO timestamps."""
    d = {}
    for key in record.keys():
        val = record[key]
        if hasattr(val, "isoformat"):
            d[key] = val.isoformat().replace("+00:00", "Z") if val else None
        else:
            d[key] = val
    return d


def _to_iso8601(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        s = val.isoformat()
        return s.replace("+00:00", "Z") if "+00:00" in s else (s + "Z" if "Z" not in s else s)
    return str(val)


def _now_iso8601() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_hour(val: Any) -> int:
    """Extract hour from a timestamp or return 0."""
    if val is None:
        return 0
    if hasattr(val, "hour"):
        return val.hour
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).hour
    except Exception:
        return 0


def _default_header_metrics() -> dict:
    return {
        "total_census": 0,
        "total_beds": 0,
        "ed_waiting": 0,
        "ed_boarders": 0,
        "avg_wait_min": 0.0,
        "beds_available": 0,
        "or_active": 0,
        "or_total": 0,
        "clock": _now_iso8601(),
        "staleness_seconds": 0.0,
    }


def _default_ed_metrics() -> dict:
    return {
        "waiting_count": 0,
        "boarders_count": 0,
        "avg_wait_min": 0.0,
        "lwbs_rate_pct": 0.0,
        "arrivals_per_hour": [0, 0, 0, 0],
        "esi_breakdown": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
    }


def _default_or_metrics() -> dict:
    return {
        "utilization_pct": 0.0,
        "fcots_pct": 0.0,
        "rooms": [],
        "active_procedures": [],
        "next_available_eta": None,
    }


def _default_forecast_data() -> dict:
    return {
        "predicted_discharges_2h": 0,
        "predicted_discharges_4h": 0,
        "arrival_forecast": [],
        "capacity_by_dept": [],
        "anomalies": [],
    }


async def block_bed(conn: Any, schema: str, bed_id: str, reason: str, operator: str = "operator") -> dict:
    """
    Block a bed with a given reason.

    Updates: status='blocked', blocked_reason=reason, previous_status=current status
    Returns: updated bed state with write_latency_ms
    """
    import random
    import time

    if conn is None:
        # Demo mode: return synthetic success
        latency = random.randint(150, 400)
        return {
            "success": True,
            "bed_id": bed_id,
            "previous_status": "available",
            "status": "blocked",
            "blocked_reason": reason,
            "blocked_at": _now_iso8601(),
            "blocked_by": operator,
            "write_latency_ms": latency,
        }

    try:
        # Check if bed exists
        current = await conn.fetchrow(
            f"SELECT bed_id, status, blocked_reason FROM {schema}.beds WHERE bed_id = $1",
            bed_id
        )

        if not current:
            return {"success": False, "error": f"Bed {bed_id} not found"}

        if current["status"] == "blocked":
            return {
                "success": False,
                "error": f"Bed {bed_id} is already blocked (reason: {current['blocked_reason']})",
            }

        # Measure write latency
        start = time.perf_counter()
        result = await conn.fetchrow(
            f"""
            UPDATE {schema}.beds
            SET status = 'blocked',
                blocked_reason = $2,
                previous_status = status,
                last_updated = NOW()
            WHERE bed_id = $1 AND status != 'blocked'
            RETURNING bed_id, status, previous_status, blocked_reason, last_updated
            """,
            bed_id,
            reason,
        )
        write_latency_ms = round((time.perf_counter() - start) * 1000)

        if not result:
            return {"success": False, "error": f"Failed to block bed {bed_id}"}

        return {
            "success": True,
            "bed_id": result["bed_id"],
            "previous_status": result["previous_status"] or "available",
            "status": result["status"],
            "blocked_reason": result["blocked_reason"],
            "blocked_at": _to_iso8601(result["last_updated"]),
            "blocked_by": operator,
            "write_latency_ms": write_latency_ms,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def mark_bed_ready(conn: Any, schema: str, bed_id: str, operator: str = "operator") -> dict:
    """
    Mark a blocked bed as ready (unblock it).

    Updates: status='available', blocked_reason=NULL, previous_status='blocked'
    Returns: updated bed state with write_latency_ms
    """
    import random
    import time

    if conn is None:
        # Demo mode: return synthetic success
        latency = random.randint(150, 400)
        return {
            "success": True,
            "bed_id": bed_id,
            "status": "available",
            "blocked": False,
            "unblocked_at": _now_iso8601(),
            "unblocked_by": operator,
            "write_latency_ms": latency,
        }

    try:
        # Get current state
        current = await conn.fetchrow(
            f"SELECT bed_id, status FROM {schema}.beds WHERE bed_id = $1",
            bed_id
        )

        if not current:
            return {"success": False, "error": f"Bed {bed_id} not found"}

        if current["status"] != "blocked":
            return {"success": False, "error": f"Bed {bed_id} is not blocked (status: {current['status']})"}

        # Measure write latency
        start = time.perf_counter()
        result = await conn.fetchrow(
            f"""
            UPDATE {schema}.beds
            SET status = 'available',
                blocked_reason = NULL,
                previous_status = 'blocked',
                last_updated = NOW()
            WHERE bed_id = $1
            RETURNING bed_id, status, blocked_reason, last_updated
            """,
            bed_id
        )
        write_latency_ms = round((time.perf_counter() - start) * 1000)

        if not result:
            return {"success": False, "error": "Update failed"}

        return {
            "success": True,
            "bed_id": result["bed_id"],
            "status": result["status"],
            "blocked": False,
            "unblocked_at": _to_iso8601(result["last_updated"]),
            "unblocked_by": operator,
            "write_latency_ms": write_latency_ms,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def log_operator_write(conn: Any, schema: str, action: str, entity_id: str, operator: str, latency_ms: int) -> None:
    """Log an operator write action to the operator_writes audit table."""
    if conn is None:
        return
    try:
        await conn.execute(
            f"""
            INSERT INTO {schema}.operator_writes (action, entity_type, entity_id, operator, latency_ms)
            VALUES ($1, 'bed', $2, $3, $4)
            """,
            action,
            entity_id,
            operator,
            latency_ms,
        )
    except Exception:
        pass  # Non-critical: don't fail the main operation if audit logging fails


async def get_last_operator_write(conn: Any, schema: str) -> dict | None:
    """Get the most recent operator write for pipeline status enrichment."""
    if conn is None:
        return None
    try:
        row = await conn.fetchrow(
            f"""
            SELECT action, entity_id AS bed_id, latency_ms, written_at,
                   EXTRACT(EPOCH FROM (NOW() - written_at))::float AS seconds_ago
            FROM {schema}.operator_writes
            ORDER BY written_at DESC
            LIMIT 1
            """
        )
        if not row:
            return None
        return {
            "action": row["action"],
            "bed_id": row["bed_id"],
            "latency_ms": row["latency_ms"],
            "timestamp": _to_iso8601(row["written_at"]),
            "seconds_ago": round(float(row["seconds_ago"]), 1),
        }
    except Exception:
        return None


async def get_operator_write_count(conn: Any, schema: str) -> int:
    """Count operator writes in the last hour."""
    if conn is None:
        return 0
    try:
        row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS cnt
            FROM {schema}.operator_writes
            WHERE written_at >= NOW() - INTERVAL '1 hour'
            """
        )
        return row["cnt"] if row else 0
    except Exception:
        return 0


async def get_patient_details(conn: Any, schema: str, bed_id: str) -> dict | None:
    """Get patient details for a given bed via indexed lookup on patients.bed_id.

    Returns demographics, LACE scores, and discharge predictions.
    Returns None if no patient is assigned to the bed.
    """
    if conn is None:
        return None
    try:
        row = await conn.fetchrow(
            f"""
            SELECT
                p.patient_id, p.mrn, p.name, p.age, p.gender,
                p.arrival_id, p.bed_id, p.dept_id,
                p.length_of_stay_days, p.acuity_score,
                p.comorbidity_count, p.ed_visits_6mo,
                p.lace_score, p.lace_risk,
                p.predicted_discharge_time, p.discharge_reason,
                p.confidence_score,
                p.admission_time, p.updated_at,
                e.esi_level, e.arrival_time, e.disposition, e.wait_minutes
            FROM {schema}.patients p
            LEFT JOIN {schema}.ed_arrivals e ON p.arrival_id = e.arrival_id
            WHERE p.bed_id = $1
            LIMIT 1
            """,
            bed_id,
        )
        if not row:
            return None
        return _record_to_dict(row)
    except Exception:
        return None


async def get_bed_details(conn: Any, schema: str, bed_id: str) -> dict:
    """
    OLTP drill-down: Get complete bed context via 4 indexed point lookups.

    Lakebase (Postgres): B-tree indexes → direct row access (milliseconds)
    SQL Warehouse (Delta): Parquet files → table scans (seconds, no row indexes)

    Returns bed details + assigned patient (ED arrival) + recent events + department.
    Measures individual query latency to prove OLTP vs OLAP performance difference.
    """
    import time
    import asyncio

    if conn is None:
        # Demo mode fallback
        return {
            "bed": {"bed_id": bed_id, "status": "occupied", "dept_id": "ICU"},
            "patient": {
                "patient_id": "demo-patient-001",
                "mrn": "MRN0000001",
                "name": "Demo Patient",
                "age": 65,
                "gender": "M",
                "lace_score": 9,
                "lace_risk": "MODERATE",
                "predicted_discharge_time": None,
                "discharge_reason": None,
                "confidence_score": None,
                "length_of_stay_days": 2.5,
                "acuity_score": 3,
                "comorbidity_count": 2,
                "ed_visits_6mo": 1,
            },
            "events": [],
            "department": {"dept_id": "ICU", "name": "Intensive Care Unit"},
            "timing": {
                "bed_ms": 5, "patient_ms": 8, "events_ms": 7, "dept_ms": 3,
                "total_ms": 23, "sql_warehouse_est_ms": 8000
            }
        }

    try:
        # Query 1: Bed details (indexed on bed_id PK)
        t0 = time.perf_counter()
        bed_row = await conn.fetchrow(
            f"SELECT * FROM {schema}.beds WHERE bed_id = $1", bed_id
        )
        bed_ms = round((time.perf_counter() - t0) * 1000)

        if not bed_row:
            return {"error": f"Bed {bed_id} not found"}

        bed = _record_to_dict(bed_row)
        dept_id = bed.get("dept_id")

        # Query 2: Assigned patient from patients table (indexed on patients.bed_id)
        # Joins with ed_arrivals for arrival context (ESI, wait time, disposition)
        t1 = time.perf_counter()
        patient_row = await conn.fetchrow(
            f"""
            SELECT
                p.patient_id, p.mrn, p.name, p.age, p.gender,
                p.arrival_id, p.bed_id, p.dept_id,
                p.length_of_stay_days, p.acuity_score,
                p.comorbidity_count, p.ed_visits_6mo,
                p.lace_score, p.lace_risk,
                p.predicted_discharge_time, p.discharge_reason,
                p.confidence_score,
                p.admission_time AS patient_admission_time,
                p.updated_at AS patient_updated_at,
                e.esi_level, e.arrival_time, e.disposition, e.wait_minutes
            FROM {schema}.patients p
            LEFT JOIN {schema}.ed_arrivals e ON p.arrival_id = e.arrival_id
            WHERE p.bed_id = $1
            LIMIT 1
            """,
            bed_id,
        )
        patient_ms = round((time.perf_counter() - t1) * 1000)
        patient = _record_to_dict(patient_row) if patient_row else None

        # Query 3: Recent events (indexed on event_log.entity_id)
        t2 = time.perf_counter()
        event_rows = await conn.fetch(
            f"""
            SELECT * FROM {schema}.event_log
            WHERE entity_id = $1
            ORDER BY event_time DESC
            LIMIT 10
            """,
            bed_id
        )
        events_ms = round((time.perf_counter() - t2) * 1000)
        events = [_record_to_dict(r) for r in event_rows]

        # Query 4: Department context (indexed on departments.dept_id PK)
        t3 = time.perf_counter()
        dept_row = await conn.fetchrow(
            f"SELECT * FROM {schema}.departments WHERE dept_id = $1", dept_id
        ) if dept_id else None
        dept_ms = round((time.perf_counter() - t3) * 1000)
        department = _record_to_dict(dept_row) if dept_row else None

        total_ms = bed_ms + patient_ms + events_ms + dept_ms

        # SQL Warehouse cold start estimate: 2s per query avg
        sql_warehouse_est_ms = 4 * 2000

        return {
            "bed": bed,
            "patient": patient,
            "events": events,
            "department": department,
            "timing": {
                "bed_ms": bed_ms,
                "patient_ms": patient_ms,
                "events_ms": events_ms,
                "dept_ms": dept_ms,
                "total_ms": total_ms,
                "sql_warehouse_est_ms": sql_warehouse_est_ms,
            }
        }

    except Exception as e:
        return {"error": str(e)}
