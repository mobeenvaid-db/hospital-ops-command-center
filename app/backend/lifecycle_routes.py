"""
Simulation lifecycle control routes.

Provides API endpoints for controlling simulation lifecycle via Databricks Jobs API:
- GET /api/lifecycle/status - Get current simulation state
- POST /api/lifecycle/start - Start simulation if not running
- POST /api/lifecycle/stop - Stop running simulation
- POST /api/lifecycle/seed - 3-step orchestration: stop → seed → restart
"""

import asyncio
import os
import time
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from config import get_workspace_client
from db import get_db


router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])

# Job IDs from environment variables (set in app.yaml)
# Fallback to job name lookup if env vars not set
SEED_JOB_ID = os.environ.get("SEED_JOB_ID")
SIMULATE_JOB_ID = os.environ.get("SIMULATE_JOB_ID")

# Legacy: job names for lookup if IDs not provided
SEED_JOB_NAME = "capacity-command-seed"
SIMULATE_JOB_NAME = "capacity-command-simulate"

# Polling configuration
SEED_POLL_INTERVAL_SEC = 5
SEED_TIMEOUT_SEC = 300  # 5 minutes


class SimulationStatus:
    """Current simulation state."""

    def __init__(
        self,
        is_running: bool,
        run_id: Optional[int] = None,
        state: Optional[str] = None,
        sim_clock: Optional[str] = None,
        staleness_seconds: Optional[int] = None,
        cycle: Optional[int] = None,
    ):
        self.is_running = is_running
        self.run_id = run_id
        self.state = state
        self.sim_clock = sim_clock
        self.staleness_seconds = staleness_seconds
        self.cycle = cycle


async def _get_job_id(job_name: str, job_id_env: Optional[str] = None) -> Optional[int]:
    """
    Get job ID by name or from environment variable.

    If job_id_env is provided and set, use it directly (avoids permission issues).
    Otherwise, fall back to looking up by name.
    """
    # Use hardcoded ID from env var if available
    if job_id_env:
        try:
            return int(job_id_env)
        except (ValueError, TypeError):
            pass

    # Fallback: lookup by name (requires SP to have permission to view jobs)
    w = get_workspace_client()
    jobs = w.jobs.list(name=job_name)
    for job in jobs:
        if job.settings and job.settings.name == job_name:
            return job.job_id
    return None


async def _get_active_run(job_id: int) -> Optional[dict]:
    """Get active run for a job (PENDING or RUNNING state)."""
    w = get_workspace_client()
    runs = w.jobs.list_runs(job_id=job_id, active_only=True, limit=1)
    for run in runs:
        return {
            "run_id": run.run_id,
            "state": run.state.life_cycle_state.value if run.state and run.state.life_cycle_state else None,
            "state_message": run.state.state_message if run.state else None,
        }
    return None


async def _get_snapshot_staleness(conn) -> Optional[int]:
    """Get staleness in seconds from snapshot_meta.clock."""
    if conn is None:
        return None
    try:
        row = await conn.fetchrow(
            "SELECT clock FROM snapshot_meta ORDER BY clock DESC LIMIT 1"
        )
        if row and row["clock"]:
            now = datetime.now(UTC)
            clock = row["clock"]
            if clock.tzinfo is None:
                # Assume UTC if naive
                from datetime import timezone
                clock = clock.replace(tzinfo=timezone.utc)
            staleness = (now - clock).total_seconds()
            return int(staleness)
    except Exception:
        pass
    return None


@router.get("/status")
async def get_status(conn=Depends(get_db)):
    """
    Get current simulation state.

    Returns hybrid status: Jobs API (primary) + staleness check (fallback).
    """
    # Get job ID (from env var or lookup by name)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        return {
            "error": "Simulate job not found. Set SIMULATE_JOB_ID env var in app.yaml.",
            "is_running": False,
        }

    # Check for active run (definitive source)
    active_run = await _get_active_run(simulate_job_id)
    if active_run:
        return {
            "is_running": True,
            "run_id": active_run["run_id"],
            "state": active_run["state"],
            "message": active_run["state_message"],
        }

    # Fallback: check staleness
    staleness = await _get_snapshot_staleness(conn)
    if staleness is not None and staleness < 30:
        # Data is fresh, simulation likely running but Jobs API didn't find it
        return {
            "is_running": True,
            "run_id": None,
            "state": "RUNNING",
            "staleness_seconds": staleness,
            "message": "Running (detected via staleness check)",
        }

    return {
        "is_running": False,
        "run_id": None,
        "state": "TERMINATED",
        "staleness_seconds": staleness,
    }


@router.post("/start")
async def start_simulation():
    """Start simulation if not running."""
    # Get job ID (from env var or lookup by name)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        raise HTTPException(
            status_code=404,
            detail="Simulate job not found. Set SIMULATE_JOB_ID env var in app.yaml.",
        )

    # Check if already running
    active_run = await _get_active_run(simulate_job_id)
    if active_run:
        raise HTTPException(
            status_code=400,
            detail=f"Simulation already running (run_id={active_run['run_id']})",
        )

    # Submit new run
    w = get_workspace_client()
    run = w.jobs.run_now(job_id=simulate_job_id)

    return {
        "success": True,
        "run_id": run.run_id,
        "message": "Simulation started (cluster starting, ~2 min)",
    }


@router.post("/stop")
async def stop_simulation():
    """Stop running simulation."""
    # Get job ID (from env var or lookup by name)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)
    if not simulate_job_id:
        raise HTTPException(
            status_code=404,
            detail="Simulate job not found. Set SIMULATE_JOB_ID env var in app.yaml.",
        )

    # Find active run
    active_run = await _get_active_run(simulate_job_id)
    if not active_run:
        raise HTTPException(
            status_code=400,
            detail="Simulation is not running",
        )

    # Cancel run
    w = get_workspace_client()
    w.jobs.cancel_run(run_id=active_run["run_id"])

    return {
        "success": True,
        "run_id": active_run["run_id"],
        "message": "Simulation stopped",
    }


@router.post("/seed")
async def seed_and_restart(conn=Depends(get_db)):
    """
    3-step orchestrated seed operation:
    1. Cancel active simulate job (if running)
    2. Submit seed job → poll until TERMINATED
    3. Submit simulate job to restart

    This endpoint blocks for ~30-90s until seed completes.
    """
    w = get_workspace_client()

    # Get job IDs (from env vars or lookup by name)
    seed_job_id = await _get_job_id(SEED_JOB_NAME, SEED_JOB_ID)
    simulate_job_id = await _get_job_id(SIMULATE_JOB_NAME, SIMULATE_JOB_ID)

    if not seed_job_id or not simulate_job_id:
        raise HTTPException(
            status_code=404,
            detail="Jobs not found. Set SEED_JOB_ID and SIMULATE_JOB_ID env vars in app.yaml.",
        )

    # Step 1: Stop simulation if running
    active_run = await _get_active_run(simulate_job_id)
    if active_run:
        w.jobs.cancel_run(run_id=active_run["run_id"])
        await asyncio.sleep(5)  # Give it time to stop

    # Step 2: Submit seed job and poll
    seed_run = w.jobs.run_now(job_id=seed_job_id)
    seed_run_id = seed_run.run_id

    start_time = time.time()
    while True:
        # Check timeout
        elapsed = time.time() - start_time
        if elapsed > SEED_TIMEOUT_SEC:
            raise HTTPException(
                status_code=408,
                detail=f"Seed timeout (>{SEED_TIMEOUT_SEC}s)",
            )

        # Poll run state
        run_state = w.jobs.get_run(seed_run_id)
        state = run_state.state.life_cycle_state.value if run_state.state and run_state.state.life_cycle_state else None

        if state == "TERMINATED":
            # Check if successful
            result_state = run_state.state.result_state.value if run_state.state and run_state.state.result_state else None
            if result_state != "SUCCESS":
                raise HTTPException(
                    status_code=500,
                    detail=f"Seed failed with state: {result_state}",
                )
            break

        # Continue polling
        await asyncio.sleep(SEED_POLL_INTERVAL_SEC)

    # Step 3: Restart simulation
    simulate_run = w.jobs.run_now(job_id=simulate_job_id)

    return {
        "success": True,
        "message": "Seed complete, simulation restarted",
        "seed_run_id": seed_run_id,
        "simulate_run_id": simulate_run.run_id,
    }
