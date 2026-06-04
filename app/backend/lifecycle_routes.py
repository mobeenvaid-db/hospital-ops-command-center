"""Lifecycle control routes.

Dual-mode:

  • Simulation mode (ENABLE_SIMULATION=true AND SEED_JOB_ID/SIMULATE_JOB_ID set):
    drives the original Databricks seed/simulate Jobs.

  • Realtime mode (default): there is no simulator. "Seed & Start" instead loads
    a realistic census through the real ingestion applier (see ingest.demo_seed),
    so the demo dashboard can be populated without a live Redox feed. Status is
    derived from data freshness. start/stop are no-ops that report realtime mode.

Always mounted, so the prebuilt frontend's controls never hit an unmounted route.
"""

import asyncio
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from config import ENABLE_SIMULATION, SCHEMA
from db import get_db

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])

SEED_JOB_ID = os.environ.get("SEED_JOB_ID")
SIMULATE_JOB_ID = os.environ.get("SIMULATE_JOB_ID")
SEED_JOB_NAME = "capacity-command-seed"
SIMULATE_JOB_NAME = "capacity-command-simulate"
SEED_POLL_INTERVAL_SEC = 5
SEED_TIMEOUT_SEC = 300

# Data is considered "live" if the last applied event is within this window.
FRESH_WINDOW_SEC = 120


def _jobs_mode() -> bool:
    return bool(ENABLE_SIMULATION and SEED_JOB_ID and SIMULATE_JOB_ID)


def _insim_mode() -> bool:
    """In-process simulator: ENABLE_SIMULATION on, but no external seed/simulate
    jobs configured. This is the portable demo path (no Redox, no Databricks jobs)."""
    return bool(ENABLE_SIMULATION and not (SEED_JOB_ID and SIMULATE_JOB_ID))


async def _staleness_seconds(conn) -> Optional[float]:
    if conn is None:
        return None
    try:
        val = await conn.fetchval(
            f"SELECT EXTRACT(EPOCH FROM (now() - clock)) FROM {SCHEMA}.snapshot_meta WHERE id = 1"
        )
        return float(val) if val is not None else None
    except Exception:
        return None


# ── In-process simulation handlers (portable demo) ──────────────────────

async def _insim_status(conn):
    from simulator import simulator
    staleness = await _staleness_seconds(conn)
    running = simulator.is_running
    return {
        "is_running": running,
        "run_id": None,
        "state": "RUNNING" if running else "PAUSED",
        "staleness_seconds": int(staleness) if staleness is not None else None,
        "mode": "simulation",
        "message": "Simulation running — synthetic ADT feed"
                   if running else "Simulation paused — click Start to resume",
    }


async def _insim_start(conn):
    from simulator import simulator
    simulator.resume()
    return {"success": True, "mode": "simulation", "is_running": True,
            "message": "Simulation resumed"}


async def _insim_stop(conn):
    from simulator import simulator
    simulator.pause()
    return {"success": True, "mode": "simulation", "is_running": False,
            "message": "Simulation paused"}


async def _insim_seed(conn):
    from simulator import simulator
    summary = await _realtime_seed(conn)   # clean census now, through the applier
    simulator.resume()                     # make sure the loop is live afterward
    summary["mode"] = "simulation"
    summary["message"] = (
        f"Census reseeded: {summary.get('patients_admitted', 0)} inpatients, "
        f"{summary.get('ed_waiting', 0)} in the ED — simulation running"
    )
    return summary


# ── Realtime (default) handlers ─────────────────────────────────────────

async def _realtime_status(conn):
    staleness = await _staleness_seconds(conn)
    is_running = staleness is not None and staleness < FRESH_WINDOW_SEC
    return {
        "is_running": is_running,
        "run_id": None,
        "state": "RUNNING" if is_running else "PAUSED",
        "staleness_seconds": int(staleness) if staleness is not None else None,
        "mode": "realtime",
        "message": "Live data from ingestion" if is_running
                   else "No recent events — click Seed & Start to load demo data, or connect a Redox feed",
    }


async def _realtime_seed(conn):
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    from ingest.demo_seed import seed_demo
    from ingest.location_map import LocationMap
    summary = await seed_demo(conn, SCHEMA, LocationMap.load())
    return {
        "success": True,
        "mode": "realtime",
        "message": f"Loaded demo census: {summary['patients_admitted']} inpatients, "
                   f"{summary['ed_waiting']} in the ED",
        "seed_run_id": None,
        "simulate_run_id": None,
        **summary,
    }


# ── Jobs (simulation) helpers — original behavior ───────────────────────

async def _get_job_id(job_name, job_id_env):
    if job_id_env:
        try:
            return int(job_id_env)
        except (ValueError, TypeError):
            pass
    from config import get_workspace_client
    w = get_workspace_client()
    for job in w.jobs.list(name=job_name):
        if job.settings and job.settings.name == job_name:
            return job.job_id
    return None


async def _get_active_run(job_id):
    from config import get_workspace_client
    w = get_workspace_client()
    for run in w.jobs.list_runs(job_id=job_id, active_only=True, limit=1):
        return {
            "run_id": run.run_id,
            "state": run.state.life_cycle_state.value if run.state and run.state.life_cycle_state else None,
            "state_message": run.state.state_message if run.state else None,
        }
    return None


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(conn=Depends(get_db)):
    if _insim_mode():
        return await _insim_status(conn)
    if not _jobs_mode():
        return await _realtime_status(conn)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        return {"error": "Simulate job not found.", "is_running": False}
    active_run = await _get_active_run(simulate_job_id)
    if active_run:
        return {"is_running": True, "run_id": active_run["run_id"],
                "state": active_run["state"], "message": active_run["state_message"]}
    staleness = await _staleness_seconds(conn)
    if staleness is not None and staleness < 30:
        return {"is_running": True, "run_id": None, "state": "RUNNING",
                "staleness_seconds": int(staleness), "message": "Running (staleness)"}
    return {"is_running": False, "run_id": None, "state": "TERMINATED",
            "staleness_seconds": int(staleness) if staleness is not None else None}


@router.post("/start")
async def start_simulation(conn=Depends(get_db)):
    if _insim_mode():
        return await _insim_start(conn)
    if not _jobs_mode():
        return {"success": True, "mode": "realtime",
                "message": "Realtime mode — data flows in from ingestion. Use Seed & Start to load demo data."}
    from config import get_workspace_client
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        raise HTTPException(status_code=404, detail="Simulate job not found.")
    if await _get_active_run(simulate_job_id):
        raise HTTPException(status_code=400, detail="Simulation already running")
    run = get_workspace_client().jobs.run_now(job_id=simulate_job_id)
    return {"success": True, "run_id": run.run_id, "message": "Simulation started"}


@router.post("/stop")
async def stop_simulation(conn=Depends(get_db)):
    if _insim_mode():
        return await _insim_stop(conn)
    if not _jobs_mode():
        return {"success": True, "mode": "realtime", "message": "Realtime mode — nothing to stop."}
    from config import get_workspace_client
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        raise HTTPException(status_code=404, detail="Simulate job not found.")
    active_run = await _get_active_run(simulate_job_id)
    if not active_run:
        raise HTTPException(status_code=400, detail="Simulation is not running")
    get_workspace_client().jobs.cancel_run(run_id=active_run["run_id"])
    return {"success": True, "run_id": active_run["run_id"], "message": "Simulation stopped"}


@router.post("/seed")
async def seed_and_restart(conn=Depends(get_db)):
    if _insim_mode():
        return await _insim_seed(conn)
    if not _jobs_mode():
        return await _realtime_seed(conn)

    from config import get_workspace_client
    w = get_workspace_client()
    seed_job_id = await _get_job_id(SEED_JOB_NAME, SEED_JOB_ID)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not seed_job_id or not simulate_job_id:
        raise HTTPException(status_code=404, detail="Jobs not found.")
    active_run = await _get_active_run(simulate_job_id)
    if active_run:
        w.jobs.cancel_run(run_id=active_run["run_id"])
        await asyncio.sleep(5)
    seed_run = w.jobs.run_now(job_id=seed_job_id)
    seed_run_id = seed_run.run_id
    start = time.time()
    while True:
        if time.time() - start > SEED_TIMEOUT_SEC:
            raise HTTPException(status_code=408, detail=f"Seed timeout (>{SEED_TIMEOUT_SEC}s)")
        run_state = w.jobs.get_run(seed_run_id)
        state = run_state.state.life_cycle_state.value if run_state.state and run_state.state.life_cycle_state else None
        if state == "TERMINATED":
            result_state = run_state.state.result_state.value if run_state.state and run_state.state.result_state else None
            if result_state != "SUCCESS":
                raise HTTPException(status_code=500, detail=f"Seed failed: {result_state}")
            break
        await asyncio.sleep(SEED_POLL_INTERVAL_SEC)
    simulate_run = w.jobs.run_now(job_id=simulate_job_id)
    return {"success": True, "message": "Seed complete, simulation restarted",
            "seed_run_id": seed_run_id, "simulate_run_id": simulate_run.run_id}
