# Capacity Command — Simulation Control API routes
#
# Control Plane Pattern: AdminPanel buttons write to Lakebase simulation_control table.
# The background simulation notebook reads these values every cycle and adjusts behavior.
# NO notebook execution API calls - the simulation runs continuously in the background.

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from config import SCHEMA
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["control"])

DEFAULTS = {
    "speed_multiplier": 1.0,
    "surge_multiplier": 1.0,
    "simulated_hour": -1,
}

# Demo mode in-memory state (used when conn is None)
_demo_params: dict[str, float | int] | None = None


class SimControlRequest(BaseModel):
    speed_multiplier: float = Field(1.0, ge=1.0, le=120.0)
    surge_multiplier: float = Field(1.0, ge=0.5, le=5.0)
    simulated_hour: int = Field(-1, ge=-1, le=23)


class SimControlResponse(BaseModel):
    speed_multiplier: float
    surge_multiplier: float
    simulated_hour: int
    updated_at: str


class SimControlPartialRequest(BaseModel):
    """Partial update request - only specified fields are updated (PATCH semantics)."""
    speed_multiplier: float | None = Field(None, ge=1.0, le=120.0)
    surge_multiplier: float | None = Field(None, ge=0.5, le=5.0)
    simulated_hour: int | None = Field(None, ge=-1, le=23)


# ── Routes ───────────────────────────────────────────────────────────────

@router.get("")
async def get_control(conn=Depends(get_db)) -> SimControlResponse:
    """Read current simulation control values from Lakebase."""
    global _demo_params

    if conn is None:
        # Demo mode: return in-memory params if set, otherwise defaults
        params = _demo_params if _demo_params is not None else DEFAULTS
        return SimControlResponse(
            **params,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    try:
        row = await conn.fetchrow(
            f"SELECT speed_multiplier, surge_multiplier, simulated_hour, updated_at "
            f"FROM {SCHEMA}.simulation_control WHERE control_id = 1"
        )
    except Exception:
        return SimControlResponse(
            **DEFAULTS,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    if row is None:
        return SimControlResponse(
            **DEFAULTS,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    return SimControlResponse(
        speed_multiplier=float(row["speed_multiplier"]),
        surge_multiplier=float(row["surge_multiplier"]),
        simulated_hour=int(row["simulated_hour"]),
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


@router.post("")
async def update_control(req: SimControlRequest, conn=Depends(get_db)) -> SimControlResponse:
    """Update simulation control values in Lakebase. Background simulation picks up changes within 10s."""
    global _demo_params

    if conn is not None:
        # Write params to Lakebase - running simulation will detect on next cycle
        await conn.execute(
            f"INSERT INTO {SCHEMA}.simulation_control "
            f"(control_id, speed_multiplier, surge_multiplier, simulated_hour, updated_at) "
            f"VALUES (1, $1, $2, $3, now()) "
            f"ON CONFLICT (control_id) DO UPDATE SET "
            f"speed_multiplier = $1, surge_multiplier = $2, simulated_hour = $3, updated_at = now()",
            req.speed_multiplier,
            req.surge_multiplier,
            req.simulated_hour,
        )
        logger.info(
            "Updated control: speed=%.1fx, surge=%.1fx, hour=%d",
            req.speed_multiplier,
            req.surge_multiplier,
            req.simulated_hour,
        )
    else:
        # Demo mode: store params in-memory
        _demo_params = {
            "speed_multiplier": req.speed_multiplier,
            "surge_multiplier": req.surge_multiplier,
            "simulated_hour": req.simulated_hour,
        }

    return await get_control(conn)


@router.patch("")
async def update_control_partial(req: SimControlPartialRequest, conn=Depends(get_db)) -> SimControlResponse:
    """Partial update - only updates specified fields (PATCH semantics). Allows combining scenarios."""
    global _demo_params

    # Get fields that were explicitly set in the request
    update_data = req.model_dump(exclude_unset=True)

    if not update_data:
        # No fields specified, just return current state
        return await get_control(conn)

    if conn is not None:
        # Build dynamic SQL UPDATE for only the specified fields
        set_clauses = []
        values = []

        for i, (key, value) in enumerate(update_data.items(), start=1):
            set_clauses.append(f"{key} = ${i}")
            values.append(value)

        set_string = ", ".join(set_clauses)

        query = (
            f"UPDATE {SCHEMA}.simulation_control "
            f"SET {set_string}, updated_at = now() "
            f"WHERE control_id = 1"
        )

        await conn.execute(query, *values)

        # Log what was updated
        updates = ", ".join(f"{k}={v}" for k, v in update_data.items())
        logger.info("Partial update: %s", updates)
    else:
        # Demo mode: merge with existing params
        if _demo_params is None:
            _demo_params = DEFAULTS.copy()
        _demo_params.update(update_data)

    return await get_control(conn)


# ── Preset Scenarios ────────────────────────────────────────────────────


@router.post("/preset/normal")
async def preset_normal(conn=Depends(get_db)) -> SimControlResponse:
    """Normal Operations: Full reset to baseline (1x speed, 1x surge, real-time)."""
    return await update_control_partial(
        SimControlPartialRequest(speed_multiplier=1.0, surge_multiplier=1.0, simulated_hour=-1), conn
    )


@router.post("/preset/fast-forward")
async def preset_fast_forward(conn=Depends(get_db)) -> SimControlResponse:
    """Fast Forward: 60x time-lapse (preserves simulated_hour if set)."""
    return await update_control_partial(
        SimControlPartialRequest(speed_multiplier=60.0, surge_multiplier=1.0), conn
    )


@router.post("/preset/surge")
async def preset_surge(conn=Depends(get_db)) -> SimControlResponse:
    """ED Surge: 4x arrivals, 12x speed (preserves simulated_hour if set)."""
    return await update_control_partial(
        SimControlPartialRequest(speed_multiplier=12.0, surge_multiplier=4.0), conn
    )


@router.post("/preset/peak-hour")
async def preset_peak_hour(conn=Depends(get_db)) -> SimControlResponse:
    """Peak Hour: Jump to 2 PM (preserves speed/surge if set)."""
    return await update_control_partial(
        SimControlPartialRequest(simulated_hour=14), conn
    )
