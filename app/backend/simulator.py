"""In-process synthetic ADT simulator for portable demos.

When ``ENABLE_SIMULATION=true`` this drives a realistic, continuously moving
census WITHOUT a Redox feed and WITHOUT any external Databricks jobs. It
generates canonical ``AdtEvent`` objects (arrivals, admits, transfers,
discharges) and pushes them through the SAME applier the webhook uses, so
simulated state is indistinguishable from ingested state and the dashboard
updates live.

It honors the control plane written by the AdminPanel via ``/api/control``
(``speed_multiplier``, ``surge_multiplier``, ``simulated_hour``), so the
Fast Forward, ED Surge, and Peak Hour presets work against it. Start / Stop
from the UI pause and resume the loop, and Seed & Start reloads a clean census.

The arrival, acuity, admission, and discharge behavior reuse the same
research-calibrated constants as the seed (see ``lib/constants.py``).

This module is only started when ``ENABLE_SIMULATION=true``. In the default
realtime mode it is never imported into the run loop.
"""

from __future__ import annotations

import asyncio
import logging
import random

from config import SCHEMA
from db import db_pool
from ingest.applier import apply_event
from ingest.demo_seed import _FIRST, _LAST, seed_demo
from ingest.location_map import LocationMap
from ingest.models import AdtEvent, EventType, LocationRec, PatientRec
from ingest.util import now_utc
from lib.constants import (
    ADMISSION_PROB_BY_ESI,
    DEPARTMENTS,
    DEPT_OCCUPANCY_TARGET,
    DOW_MULTIPLIER,
    ESI_WEIGHTS_DAY,
    ESI_WEIGHTS_NIGHT,
    ESI_WEIGHTS_SURGE,
    HOURLY_LAMBDA,
    INPATIENT_DEPTS,
    P_CLEAN_DONE,
    P_DISCHARGE,
)

logger = logging.getLogger(__name__)

# ── Pacing ──────────────────────────────────────────────────────────────
# Real seconds between simulation steps.
BASE_TICK_SEC = 2.0
# At speed 1x, one simulated hour elapses every this many real seconds. This is
# what makes 1x a gentle trickle and Fast Forward (60x) a lively board.
SECONDS_PER_SIM_HOUR = 120.0
# How aggressively waiting ED patients convert to admits each step.
ADMIT_PACE = 0.25
# Per-step ED discharge pace for low-acuity arrivals that won't be admitted.
ED_DISCHARGE_PACE = 0.10
# The constants file expresses bed-state probabilities per 10s cycle.
_CYCLE_SCALE = BASE_TICK_SEC / 10.0

# Per-department bed capacity, for occupancy-aware discharge pacing.
_CAPACITY = {d["dept_id"]: d["total_beds"] for d in DEPARTMENTS}


def _esi_weights(hour: int, surge: float) -> list[float]:
    if surge > 1.5:
        return ESI_WEIGHTS_SURGE
    return ESI_WEIGHTS_DAY if 8 <= hour < 20 else ESI_WEIGHTS_NIGHT


def _discharge_hour_weight(hour: int) -> float:
    """Relative discharge propensity by hour. Peaks early/mid afternoon, near
    zero overnight. Mirrors the triangular(10, 15, 20) curve in the constants."""
    if hour < 8 or hour >= 21:
        return 0.1
    if 11 <= hour <= 16:
        return 1.0
    return 0.5


def _sample_poisson(mean: float, rng: random.Random) -> int:
    """Knuth's algorithm. Fine for the small means used here."""
    if mean <= 0:
        return 0
    import math

    limit = math.exp(-mean)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= limit:
            return k - 1


def _admit_dept(esi: int, rng: random.Random) -> str:
    """Route an admit to an inpatient unit by acuity. Higher acuity skews to
    ICU/TELE. If the chosen unit is full the applier admits with no bed, which
    correctly shows up as an ED boarder."""
    if esi <= 2:
        return rng.choices(["ICU", "TELE", "MEDSURG"], weights=[55, 30, 15])[0]
    if esi == 3:
        return rng.choices(["TELE", "MEDSURG", "PEDS"], weights=[35, 55, 10])[0]
    return rng.choices(["MEDSURG", "PEDS"], weights=[85, 15])[0]


def _make_patient(rng: random.Random, n: int) -> PatientRec:
    return PatientRec(
        mrn=f"{1000000 + n:07d}",
        name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
        age=rng.randint(18, 95),
        gender=rng.choice(["M", "F"]),
        ed_visits_6mo=rng.choices([0, 1, 2, 3, 4], weights=[50, 25, 15, 7, 3])[0],
    )


class Simulator:
    """Background ADT generator. Singleton; see module-level ``simulator``."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._resume = asyncio.Event()
        self._resume.set()  # running by default once started
        self._reseed = False
        self._counter = 0
        self._rng = random.Random()
        self._locmap = LocationMap.load()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self, schema: str = SCHEMA) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(schema))
        logger.info("Simulator background task started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ── UI control hooks ─────────────────────────────────────────────────
    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def request_reseed(self) -> None:
        self._reseed = True

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and self._resume.is_set()

    # ── main loop ─────────────────────────────────────────────────────────
    async def _run(self, schema: str) -> None:
        await self._init_counter(schema)
        while True:
            try:
                await self._resume.wait()
                await self._step(schema)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad step must not kill the loop
                logger.exception("Simulator step failed")
            await asyncio.sleep(BASE_TICK_SEC)

    async def _init_counter(self, schema: str) -> None:
        pool = await db_pool.get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            # seed an initial census if the board is empty, so it never starts blank
            count = await conn.fetchval(f"SELECT count(*) FROM {schema}.patients")
            if not count:
                await seed_demo(conn, schema, self._locmap)
            mx = await conn.fetchval(
                f"SELECT max(mrn) FROM {schema}.patients WHERE mrn ~ '^[0-9]+$'"
            )
            try:
                self._counter = max(self._counter, int(mx) - 1000000) if mx else 0
            except (TypeError, ValueError):
                self._counter = 0

    async def _read_control(self, conn, schema: str) -> tuple[float, float, int]:
        try:
            row = await conn.fetchrow(
                f"SELECT speed_multiplier, surge_multiplier, simulated_hour "
                f"FROM {schema}.simulation_control WHERE control_id = 1"
            )
            if row:
                return (
                    float(row["speed_multiplier"]),
                    float(row["surge_multiplier"]),
                    int(row["simulated_hour"]),
                )
        except Exception:  # noqa: BLE001
            pass
        return 1.0, 1.0, -1

    async def _step(self, schema: str) -> None:
        pool = await db_pool.get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            if self._reseed:
                self._reseed = False
                await seed_demo(conn, schema, self._locmap)
                return

            speed, surge, sim_hour = await self._read_control(conn, schema)
            speed = max(1.0, speed)
            now = now_utc()
            hour = sim_hour if 0 <= sim_hour <= 23 else now.hour
            dow = now.weekday()
            sim_dt_hours = (BASE_TICK_SEC / SECONDS_PER_SIM_HOUR) * speed

            await self._arrivals(conn, schema, hour, dow, surge, sim_dt_hours)
            await self._convert_ed(conn, schema, speed)
            await self._discharges(conn, schema, hour, speed)
            await self._clean_beds(conn, schema, speed)
            await self._maybe_transfer(conn, schema, speed)

            # keep the freshness signal live even on an empty step
            await conn.execute(
                f"UPDATE {schema}.snapshot_meta SET clock = now(), synced_at = now() WHERE id = 1"
            )

    # ── steps ─────────────────────────────────────────────────────────────
    async def _arrivals(self, conn, schema, hour, dow, surge, sim_dt_hours) -> None:
        rate = HOURLY_LAMBDA[hour] * DOW_MULTIPLIER[dow] * max(0.5, surge)
        n = _sample_poisson(rate * sim_dt_hours, self._rng)
        weights = _esi_weights(hour, surge)
        for _ in range(n):
            self._counter += 1
            esi = self._rng.choices([1, 2, 3, 4, 5], weights=weights)[0]
            ev = AdtEvent(
                event_type=EventType.ARRIVAL,
                event_time=now_utc(),
                patient=self._make_patient(self._rng, self._counter),
                patient_class="emergency",
                esi_level=esi,
                location=LocationRec(department="Emergency", raw="Emergency"),
                source_format="simulator",
                data_model="PatientAdmin",
                source_event_label="Arrival",
            )
            await apply_event(conn, schema, ev, self._locmap, auto_create_beds=False)

    def _make_patient(self, rng, n) -> PatientRec:
        return _make_patient(rng, n)

    async def _convert_ed(self, conn, schema, speed) -> None:
        rows = await conn.fetch(
            f"SELECT arrival_id, esi_level FROM {schema}.ed_arrivals "
            f"WHERE disposition = 'waiting' ORDER BY arrival_time LIMIT 30"
        )
        for r in rows:
            mrn = r["arrival_id"]
            esi = r["esi_level"] or 3
            admit_p = min(0.9, ADMISSION_PROB_BY_ESI.get(esi, 0.2) * ADMIT_PACE * speed)
            roll = self._rng.random()
            if roll < admit_p:
                dept = _admit_dept(esi, self._rng)
                await apply_event(
                    conn, schema,
                    AdtEvent(
                        event_type=EventType.ADMIT,
                        event_time=now_utc(),
                        patient=PatientRec(mrn=mrn),
                        patient_class="inpatient",
                        esi_level=esi,
                        location=LocationRec(department=dept, raw=dept),
                        source_format="simulator",
                        data_model="PatientAdmin",
                        source_event_label="Admit",
                    ),
                    self._locmap, auto_create_beds=False,
                )
            elif esi >= 4 and roll < admit_p + ED_DISCHARGE_PACE * speed:
                await apply_event(
                    conn, schema,
                    AdtEvent(
                        event_type=EventType.DISCHARGE,
                        event_time=now_utc(),
                        patient=PatientRec(mrn=mrn),
                        patient_class="emergency",
                        location=LocationRec(department="Emergency", raw="Emergency"),
                        source_format="simulator",
                        data_model="PatientAdmin",
                        source_event_label="Discharge",
                    ),
                    self._locmap, auto_create_beds=False,
                )

    async def _discharges(self, conn, schema, hour, speed) -> None:
        occ = {
            r["dept_id"]: r["n"]
            for r in await conn.fetch(
                f"SELECT dept_id, count(*) AS n FROM {schema}.patients "
                f"WHERE bed_id IS NOT NULL GROUP BY dept_id"
            )
        }
        hour_w = _discharge_hour_weight(hour)
        base = P_DISCHARGE * _CYCLE_SCALE * speed * hour_w
        rows = await conn.fetch(
            f"SELECT patient_id, dept_id FROM {schema}.patients WHERE bed_id IS NOT NULL"
        )
        for r in rows:
            dept = r["dept_id"]
            cap = _CAPACITY.get(dept, 0) * DEPT_OCCUPANCY_TARGET.get(dept, 0.75)
            occ_factor = 1.0
            if cap:
                occ_factor = min(2.5, max(0.3, occ.get(dept, 0) / cap))
            if self._rng.random() < base * occ_factor:
                await apply_event(
                    conn, schema,
                    AdtEvent(
                        event_type=EventType.DISCHARGE,
                        event_time=now_utc(),
                        patient=PatientRec(mrn=r["patient_id"]),
                        patient_class="inpatient",
                        source_format="simulator",
                        data_model="PatientAdmin",
                        source_event_label="Discharge",
                    ),
                    self._locmap, auto_create_beds=False,
                )

    async def _clean_beds(self, conn, schema, speed) -> None:
        n_cleaning = await conn.fetchval(
            f"SELECT count(*) FROM {schema}.beds WHERE status = 'cleaning'"
        )
        if not n_cleaning:
            return
        expected = n_cleaning * P_CLEAN_DONE * _CYCLE_SCALE * speed
        n_done = int(expected) + (1 if self._rng.random() < (expected - int(expected)) else 0)
        if n_done <= 0:
            return
        await conn.execute(
            f"""
            UPDATE {schema}.beds SET previous_status = status, status = 'available',
                   last_updated = now(), synced_at = now()
            WHERE bed_id IN (
                SELECT bed_id FROM {schema}.beds WHERE status = 'cleaning'
                ORDER BY random() LIMIT {int(n_done)}
            )
            """
        )

    async def _maybe_transfer(self, conn, schema, speed) -> None:
        if self._rng.random() > min(0.4, 0.08 * speed):
            return
        row = await conn.fetchrow(
            f"SELECT patient_id, dept_id FROM {schema}.patients "
            f"WHERE bed_id IS NOT NULL ORDER BY random() LIMIT 1"
        )
        if not row:
            return
        choices = [d for d in INPATIENT_DEPTS if d != row["dept_id"]]
        if not choices:
            return
        dept = self._rng.choice(choices)
        await apply_event(
            conn, schema,
            AdtEvent(
                event_type=EventType.TRANSFER,
                event_time=now_utc(),
                patient=PatientRec(mrn=row["patient_id"]),
                patient_class="inpatient",
                location=LocationRec(department=dept, raw=dept),
                source_format="simulator",
                data_model="PatientAdmin",
                source_event_label="Transfer",
            ),
            self._locmap, auto_create_beds=False,
        )


# Module-level singleton used by main.lifespan and lifecycle_routes.
simulator = Simulator()
