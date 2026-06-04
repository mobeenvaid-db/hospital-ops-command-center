# Capacity Command — FastAPI backend
#
# Async-first backend using asyncpg + OAuth for Lakebase.
# Static files served from frontend dist/ (Vite default).

import asyncio
import os
from collections import defaultdict
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

import db
import queries
from config import SCHEMA, ENABLE_SIMULATION
from assistant_routes import router as assistant_router
from agent_routes import router as agent_router
from ingest.redox_routes import router as ingest_router
from lib.anomaly import compute_system_status


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start token refresh loop (no-op locally, runs in background for Databricks Apps)
    refresh_task = asyncio.create_task(db.db_pool.start_token_refresh_loop())
    # In simulation mode, run the in-process ADT generator so the dashboard shows
    # live, moving data with no Redox feed and no external jobs. Off by default.
    if ENABLE_SIMULATION:
        from simulator import simulator
        await simulator.start(SCHEMA)
    yield
    if ENABLE_SIMULATION:
        from simulator import simulator
        await simulator.stop()
    refresh_task.cancel()
    await db.db_pool.close()


app = FastAPI(title="Capacity Command API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Demo-only: restrict origins in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(assistant_router)
app.include_router(agent_router)
app.include_router(ingest_router)

# lifecycle + control are always mounted so the prebuilt frontend's status
# pings never fall through to the SPA catch-all (which would return HTML).
# They are realtime-aware: the simulator (seed/simulate jobs, surge/speed) is
# only driven when ENABLE_SIMULATION=true AND the job ids are set; otherwise
# they report realtime mode. The simulation UI itself is removed from the
# frontend bundle in realtime builds.
from control_routes import router as control_router
from lifecycle_routes import router as lifecycle_router
app.include_router(control_router)
app.include_router(lifecycle_router)


# ── Error handling ─────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    from fastapi.responses import JSONResponse
    # Catch asyncpg and psycopg2 errors
    exc_name = type(exc).__module__ + "." + type(exc).__qualname__
    if "asyncpg" in exc_name or "psycopg2" in exc_name:
        return JSONResponse(
            status_code=503,
            content={"error": "Database temporarily unavailable"},
        )
    return JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred"},
    )


# ── API Routes ─────────────────────────────────────────────────────────

@app.get("/api/debug/env")
async def api_debug_env():
    """Debug endpoint to check environment variables."""
    import os
    return {
        "PGHOST": os.environ.get("PGHOST", "NOT_SET"),
        "PGPORT": os.environ.get("PGPORT", "NOT_SET"),
        "PGDATABASE": os.environ.get("PGDATABASE", "NOT_SET"),
        "PGUSER": os.environ.get("PGUSER", "NOT_SET"),
        "LAKEBASE_DATABASE": os.environ.get("LAKEBASE_DATABASE", "NOT_SET"),
        "LAKEBASE_SCHEMA": os.environ.get("LAKEBASE_SCHEMA", "NOT_SET"),
        "DATABRICKS_APP_NAME": os.environ.get("DATABRICKS_APP_NAME", "NOT_SET"),
        "DATABRICKS_HOST": os.environ.get("DATABRICKS_HOST", "NOT_SET"),
    }

@app.get("/api/header")
async def api_header(conn=Depends(db.get_db)):
    header = await queries.get_header_metrics(conn, SCHEMA)
    dept_occ = await queries.get_dept_occupancy(conn, SCHEMA)
    state = {
        "dept_occupancy": dept_occ,
        "ed_avg_wait_min": header.get("avg_wait_min", 0),
        "ed_boarders_count": header.get("ed_boarders", 0),
    }
    system_status = compute_system_status(state)

    staleness = header["staleness_seconds"]
    # If staleness is 0 and total_census is also 0, data is likely missing
    data_available = not (staleness == 0 and header["total_census"] == 0 and header["total_beds"] == 0)
    # Treat null/zero staleness with no data as very stale
    if not data_available:
        staleness = 86400  # 24 hours — signals "no data"

    return {
        "system_status": system_status,
        "total_census": header["total_census"],
        "total_beds": header["total_beds"],
        "ed_waiting": header["ed_waiting"],
        "ed_boarders": header["ed_boarders"],
        "avg_wait_min": header["avg_wait_min"],
        "beds_available": header["beds_available"],
        "or_active": header["or_active"],
        "or_total": header["or_total"],
        "clock": header["clock"],
        "staleness_seconds": staleness,
        "data_available": data_available,
    }


@app.get("/api/beds")
async def api_beds(conn=Depends(db.get_db)):
    rows = await queries.get_beds(conn, SCHEMA)
    by_dept: dict[str, list] = defaultdict(list)
    for r in rows:
        dept_id = r.get("dept_id", "")
        by_dept[dept_id].append({
            "bed_id": r.get("bed_id"),
            "status": r.get("status", "available"),
            "acuity": r.get("acuity"),
            "hours_in_bed": r.get("hours_in_bed"),
            "expected_discharge": _to_iso(r.get("expected_discharge")),
            "blocked_reason": r.get("blocked_reason"),
        })
    departments = []
    seen = set()
    for r in rows:
        dept_id = r.get("dept_id")
        if dept_id and dept_id not in seen:
            seen.add(dept_id)
            departments.append({
                "dept_id": dept_id,
                "name": r.get("dept_name", dept_id),
                "beds": by_dept.get(dept_id, []),
            })
    return {"departments": departments}


class BlockBedRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=200)
    operator: str = Field("operator", max_length=100)


@app.post("/api/beds/{bed_id}/block")
async def api_block_bed(bed_id: str, body: BlockBedRequest, conn=Depends(db.get_db)):
    """Block a bed with a given reason. Operator action."""
    result = await queries.block_bed(conn, SCHEMA, bed_id, reason=body.reason, operator=body.operator)
    if not result.get("success"):
        error = result.get("error", "Failed to block bed")
        if "not found" in error.lower():
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=400, detail=error)
    # Log operator write (non-blocking, best-effort)
    await queries.log_operator_write(
        conn, SCHEMA, action="block", entity_id=bed_id,
        operator=body.operator, latency_ms=result.get("write_latency_ms", 0),
    )
    return result


@app.post("/api/beds/{bed_id}/mark-ready")
async def api_mark_bed_ready(bed_id: str, conn=Depends(db.get_db)):
    """Mark a blocked bed as ready (unblock it). Operator action."""
    result = await queries.mark_bed_ready(conn, SCHEMA, bed_id, operator="operator")
    if not result.get("success"):
        error = result.get("error", "Failed to mark bed ready")
        if "not found" in error.lower():
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=400, detail=error)
    # Log operator write (non-blocking, best-effort)
    await queries.log_operator_write(
        conn, SCHEMA, action="unblock", entity_id=bed_id,
        operator="operator", latency_ms=result.get("write_latency_ms", 0),
    )
    return result


@app.get("/api/beds/{bed_id}/details")
async def api_bed_details(bed_id: str, conn=Depends(db.get_db)):
    """
    OLTP drill-down: Get complete bed context via indexed point lookups.

    Lakebase: B-tree indexes on bed_id, bed_assigned, entity_id → direct row access (milliseconds)
    SQL Warehouse: Delta Parquet files have no row indexes → must scan tables (seconds)

    This is why you use Lakebase for operational apps (OLTP) and Delta for analytics (OLAP).

    Response includes a patient section with demographics, LACE readmission risk,
    and discharge predictions from the patients table.
    """
    result = await queries.get_bed_details(conn, SCHEMA, bed_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Structure the patient section for the frontend
    raw_patient = result.get("patient")
    if raw_patient:
        result["patient"] = {
            "patient_id": raw_patient.get("patient_id"),
            "mrn": raw_patient.get("mrn"),
            "name": raw_patient.get("name"),
            "age": raw_patient.get("age"),
            "gender": raw_patient.get("gender"),
            "lace": {
                "score": raw_patient.get("lace_score"),
                "risk": raw_patient.get("lace_risk"),
                "length_of_stay_days": raw_patient.get("length_of_stay_days"),
                "acuity_score": raw_patient.get("acuity_score"),
                "comorbidity_count": raw_patient.get("comorbidity_count"),
                "ed_visits_6mo": raw_patient.get("ed_visits_6mo"),
            },
            "discharge": {
                "predicted_time": _to_iso(raw_patient.get("predicted_discharge_time")),
                "reason": raw_patient.get("discharge_reason"),
                "confidence": raw_patient.get("confidence_score"),
            },
            "arrival": {
                "arrival_id": raw_patient.get("arrival_id"),
                "esi_level": raw_patient.get("esi_level"),
                "arrival_time": _to_iso(raw_patient.get("arrival_time")),
                "disposition": raw_patient.get("disposition"),
                "wait_minutes": raw_patient.get("wait_minutes"),
            },
            "admission_time": _to_iso(
                raw_patient.get("patient_admission_time")
                or raw_patient.get("admission_time")
            ),
        }

    return result


@app.get("/api/ed")
async def api_ed(conn=Depends(db.get_db)):
    data = await queries.get_ed_metrics(conn, SCHEMA)
    esi = data.get("esi_breakdown", {})
    for i in range(1, 6):
        if str(i) not in esi:
            esi[str(i)] = 0
    return {
        "waiting_count": data.get("waiting_count", 0),
        "boarders_count": data.get("boarders_count", 0),
        "avg_wait_min": data.get("avg_wait_min", 0),
        "lwbs_rate_pct": data.get("lwbs_rate_pct", 0),
        "arrivals_per_hour": data.get("arrivals_per_hour", [0, 0, 0, 0]),
        "esi_breakdown": esi,
    }


@app.get("/api/or")
async def api_or(conn=Depends(db.get_db)):
    data = await queries.get_or_metrics(conn, SCHEMA)
    rooms = data.get("rooms", [])
    for r in rooms:
        if "scheduled_start" in r and r["scheduled_start"] and not isinstance(r["scheduled_start"], str):
            r["scheduled_start"] = _to_iso(r["scheduled_start"])
    return {
        "utilization_pct": data.get("utilization_pct", 0),
        "fcots_pct": data.get("fcots_pct", 0),
        "rooms": rooms,
        "active_procedures": data.get("active_procedures", []),
        "next_available_eta": _to_iso(data.get("next_available_eta")) or "",
    }


@app.get("/api/forecast")
async def api_forecast(conn=Depends(db.get_db)):
    data = await queries.get_forecast_data(conn, SCHEMA)
    return {
        "predicted_discharges_2h": data.get("predicted_discharges_2h", 0),
        "predicted_discharges_4h": data.get("predicted_discharges_4h", 0),
        "arrival_forecast": data.get("arrival_forecast", []),
        "capacity_by_dept": data.get("capacity_by_dept", []),
        "anomalies": data.get("anomalies", []),
    }


@app.get("/api/events")
async def api_events(limit: int = Query(50, ge=1, le=500), conn=Depends(db.get_db)):
    events, last_time = await queries.get_recent_events(conn, SCHEMA, limit=limit)
    if events and not last_time:
        last_time = events[0].get("event_time")
    return {
        "events": events,
        "last_event_time": last_time or "",
    }


@app.get("/api/pipeline")
async def api_pipeline(conn=Depends(db.get_db)):
    """Data flow: last write (Delta), last in Lakebase (synced_at), lag. Pipeline staleness from snapshot_meta."""
    data = await queries.get_pipeline_status(conn, SCHEMA)
    last_op_write = await queries.get_last_operator_write(conn, SCHEMA)
    op_write_count = await queries.get_operator_write_count(conn, SCHEMA)
    return {
        "pipeline_ts": _to_iso(data.get("pipeline_ts")),
        "pipeline_lag_seconds": data.get("pipeline_lag_sec", 999999.0),
        "tables": [
            {
                "table": r.get("tbl"),
                "rows": r.get("rows", 0),
                "last_write_delta": _to_iso(r.get("last_write_delta")),
                "last_in_lakebase": _to_iso(r.get("last_in_lakebase")),
                "lag_seconds": round(float(r.get("lag_sec") or 0), 1),
            }
            for r in data.get("tables", [])
        ],
        "now": _to_iso(datetime.now(timezone.utc)),
        "last_operator_write": last_op_write,
        "operator_write_count_1h": op_write_count,
    }


@app.get("/api/metrics")
async def api_metrics(conn=Depends(db.get_db)):
    rows = await queries.get_dept_metrics(conn, SCHEMA)
    return {
        "departments": [
            {
                "dept_id": r.get("dept_id"),
                "name": r.get("name", r.get("dept_id", "")),
                "occupancy_pct": float(r.get("occupancy_pct", 0)),
                "total_beds": r.get("total_beds", 0),
                "available": r.get("available", 0),
                "occupied": r.get("occupied", 0),
                "cleaning": r.get("cleaning", 0),
                "blocked": r.get("blocked", 0),
            }
            for r in rows
        ],
    }


@app.get("/api/health")
async def api_health():
    """Health check: always 200. Returns healthy/degraded based on DB pool."""
    pool = await db.db_pool.get_pool()
    if pool is None:
        return {"status": "degraded", "database": "disconnected", "pool_size": 0}
    size = pool.get_size()
    test_result = None
    test_error = None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT COUNT(*) as cnt FROM {SCHEMA}.departments"
            )
            test_result = dict(row) if row else "no row"
    except Exception as e:
        test_error = f"{type(e).__name__}: {e}"

    return {
        "status": "healthy",
        "database": "connected",
        "pool_size": size,
        "pgdatabase": os.environ.get("PGDATABASE", "(not set)"),
        "lakebase_database": os.environ.get("LAKEBASE_DATABASE", "(not set)"),
        "lakebase_schema": os.environ.get("LAKEBASE_SCHEMA", "(not set)"),
        "schema_in_use": SCHEMA,
        "demo_mode": db.db_pool.is_demo_mode,
        "test_query": test_result,
        "test_error": test_error,
    }


def _to_iso(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if hasattr(val, "isoformat"):
        s = val.isoformat()
        return s.replace("+00:00", "Z") if "+00:00" in s else (s + "Z" if "Z" not in s else s)
    return str(val)


# ── Static files (React build from Vite) ── MUST come AFTER API routes ──
# Vite outputs to dist/ by default; we also support build/ as fallback.
_frontend_dir = Path(__file__).parent.parent / "frontend"
_dist_path = _frontend_dir / "dist"
_build_path = _frontend_dir / "build"
_static_dir = _dist_path if _dist_path.exists() else _build_path


if _static_dir.exists():
    # Serve /assets/* for Vite hashed files
    _assets_dir = _static_dir / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    # Serve remaining static files (favicon, etc.) but NOT as SPA catch-all
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static-root")

    # No-cache headers for index.html so redeploys (e.g. a repatched bundle)
    # are always picked up. Hashed /assets/* are safe to cache long-term.
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    def _index_response():
        return FileResponse(str(_static_dir / "index.html"), headers=_NO_CACHE)

    # SPA catch-all: return index.html for any non-API, non-asset route
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Catch-all route for SPA — serves index.html for client-side routing."""
        file_path = (_static_dir / full_path).resolve()
        static_root = _static_dir.resolve()
        try:
            file_path.relative_to(static_root)
        except ValueError:
            return _index_response()
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return _index_response()
