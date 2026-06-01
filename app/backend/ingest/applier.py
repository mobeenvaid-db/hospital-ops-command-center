"""Apply a canonical AdtEvent to the Lakebase serving tables.

This is the only place that mutates serving state from ingestion. It is written
to be idempotent and robust to out-of-order / partial messages:

  • patients are keyed by MRN (stable across a stay)
  • an ADMIT to a bed that differs from the patient's current bed is treated as
    a transfer (frees the old bed) — this makes FHIR (no explicit transfer
    trigger) behave correctly
  • discharges free the bed even if we never saw the admit

LACE readmission risk is computed here from the real record on every event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timezone
from typing import Any, Optional

from lib.lace import compute_lace_score

from .location_map import LocationMap
from .models import AdtEvent, EventType

logger = logging.getLogger(__name__)


def patient_key(event: AdtEvent) -> Optional[str]:
    p = event.patient
    return p.mrn or p.patient_id or event.encounter_id


async def apply_event(
    conn: Any,
    schema: str,
    event: AdtEvent,
    locmap: LocationMap,
    auto_create_beds: bool = False,
) -> dict:
    """Apply one event. Returns a small result dict for the ingest log."""
    pid = patient_key(event)
    if pid is None:
        return {"status": "skipped", "detail": "no patient identifier", "event_type": event.event_type.value}

    handler = {
        EventType.ARRIVAL: _on_arrival,
        EventType.ADMIT: _on_admit,
        EventType.TRANSFER: _on_transfer,
        EventType.DISCHARGE: _on_discharge,
        EventType.UPDATE: _on_update,
        EventType.CANCEL: _on_cancel,
    }.get(event.event_type)

    if handler is None:
        return {"status": "skipped", "detail": f"unhandled event {event.source_event_label}",
                "event_type": event.event_type.value}

    result = await handler(conn, schema, event, locmap, pid, auto_create_beds)
    await _touch_clock(conn, schema, event)
    result.setdefault("status", "applied")
    result.setdefault("event_type", event.event_type.value)
    result.setdefault("patient_id", pid)
    return result


# ── event handlers ────────────────────────────────────────────────────

async def _on_arrival(conn, schema, event, locmap, pid, auto_create):
    dept = locmap.resolve_dept(event)
    await _upsert_patient(conn, schema, event, pid, dept_id=dept, bed_id=None)
    arrival_id = event.encounter_id or pid
    await conn.execute(
        f"""
        INSERT INTO {schema}.ed_arrivals
            (arrival_id, esi_level, arrival_time, disposition, triage_complete, updated_at)
        VALUES ($1, $2, $3, 'waiting', $4, now())
        ON CONFLICT (arrival_id) DO UPDATE SET
            esi_level = COALESCE(EXCLUDED.esi_level, {schema}.ed_arrivals.esi_level),
            previous_disposition = {schema}.ed_arrivals.disposition,
            updated_at = now()
        """,
        arrival_id,
        event.esi_level,
        event.event_time,
        event.event_time if event.esi_level is not None else None,
    )
    await conn.execute(
        f"UPDATE {schema}.patients SET arrival_id = $1, updated_at = now() WHERE patient_id = $2",
        arrival_id, pid,
    )
    await _log_event(conn, schema, "ed", arrival_id, None, "waiting", event)
    return {"dept_id": dept, "arrival_id": arrival_id}


async def _on_admit(conn, schema, event, locmap, pid, auto_create):
    dept = locmap.resolve_dept(event)
    current_bed = await _current_bed(conn, schema, pid)
    target_bed = await _resolve_bed(conn, schema, event, dept, auto_create)

    if current_bed and target_bed and current_bed != target_bed:
        # admit to a different bed == transfer
        await _free_bed(conn, schema, current_bed, event)

    if target_bed:
        await _occupy_bed(conn, schema, target_bed, dept, event)

    await _upsert_patient(conn, schema, event, pid, dept_id=dept, bed_id=target_bed,
                          admission_time=event.event_time)

    # link / close out any ED arrival for this patient
    arrival_id = event.encounter_id or pid
    await conn.execute(
        f"""
        UPDATE {schema}.ed_arrivals
        SET bed_assigned = $2,
            triage_complete = COALESCE(triage_complete, $3),
            previous_disposition = disposition,
            disposition = 'admitted',
            updated_at = now()
        WHERE arrival_id = $1
        """,
        arrival_id, target_bed, event.event_time,
    )
    await _log_event(conn, schema, "bed", target_bed or "(none)", "available", "occupied", event)
    return {"dept_id": dept, "bed_id": target_bed}


async def _on_transfer(conn, schema, event, locmap, pid, auto_create):
    dept = locmap.resolve_dept(event)
    current_bed = await _current_bed(conn, schema, pid)
    target_bed = await _resolve_bed(conn, schema, event, dept, auto_create)

    if current_bed and current_bed != target_bed:
        await _free_bed(conn, schema, current_bed, event)
    if target_bed:
        await _occupy_bed(conn, schema, target_bed, dept, event)

    await _upsert_patient(conn, schema, event, pid, dept_id=dept, bed_id=target_bed)
    await _log_event(conn, schema, "bed", target_bed or "(none)", "available", "occupied", event)
    return {"dept_id": dept, "bed_id": target_bed, "from_bed": current_bed}


async def _on_discharge(conn, schema, event, locmap, pid, auto_create):
    current_bed = await _current_bed(conn, schema, pid)
    if current_bed:
        await _free_bed(conn, schema, current_bed, event)
    await conn.execute(
        f"""
        UPDATE {schema}.patients
        SET bed_id = NULL,
            discharge_reason = COALESCE(discharge_reason, 'discharged'),
            length_of_stay_days = CASE
                WHEN admission_time IS NOT NULL
                THEN ROUND(EXTRACT(EPOCH FROM ($2::timestamptz - admission_time)) / 86400.0, 2)
                ELSE length_of_stay_days END,
            updated_at = now()
        WHERE patient_id = $1
        """,
        pid, event.event_time,
    )
    arrival_id = event.encounter_id or pid
    await conn.execute(
        f"""
        UPDATE {schema}.ed_arrivals
        SET previous_disposition = disposition, disposition = 'discharged', updated_at = now()
        WHERE arrival_id = $1 AND disposition IN ('waiting', 'admitted')
        """,
        arrival_id,
    )
    await _log_event(conn, schema, "bed", current_bed or "(none)", "occupied", "cleaning", event)
    return {"bed_id": current_bed}


async def _on_update(conn, schema, event, locmap, pid, auto_create):
    dept = locmap.resolve_dept(event)
    current_bed = await _current_bed(conn, schema, pid)
    await _upsert_patient(conn, schema, event, pid, dept_id=dept, bed_id=current_bed)
    # keep ESI fresh on an open ED arrival
    if event.esi_level is not None:
        arrival_id = event.encounter_id or pid
        await conn.execute(
            f"UPDATE {schema}.ed_arrivals SET esi_level = $2, updated_at = now() WHERE arrival_id = $1",
            arrival_id, event.esi_level,
        )
    await _log_event(conn, schema, "patient", pid, None, "update", event)
    return {"dept_id": dept, "bed_id": current_bed}


async def _on_cancel(conn, schema, event, locmap, pid, auto_create):
    # Cancellation reversal is intentionally conservative: we record it but do
    # not undo derived state automatically. See docs/REDOX_INTEGRATION.md.
    label = (event.source_event_label or "").lower()
    if "discharge" in label:
        # cancel-discharge -> re-admit to the resolved/last bed if free
        return await _on_admit(conn, schema, event, locmap, pid, auto_create)
    await _log_event(conn, schema, "patient", pid, None, "cancel", event)
    return {"status": "skipped", "detail": f"cancel ({event.source_event_label}) logged, not reversed"}


# ── helpers ───────────────────────────────────────────────────────────

async def _current_bed(conn, schema, pid) -> Optional[str]:
    return await conn.fetchval(
        f"SELECT bed_id FROM {schema}.patients WHERE patient_id = $1", pid
    )


async def _resolve_bed(conn, schema, event, dept, auto_create) -> Optional[str]:
    """Pick the bed_id: explicit from the EHR, else next available in dept."""
    explicit = LocationMap.explicit_bed_id(event.location)
    if explicit:
        await _ensure_dept(conn, schema, dept)
        exists = await conn.fetchval(f"SELECT 1 FROM {schema}.beds WHERE bed_id = $1", explicit)
        if not exists:
            if auto_create:
                await conn.execute(
                    f"INSERT INTO {schema}.beds (bed_id, dept_id, status) VALUES ($1, $2, 'available') "
                    f"ON CONFLICT (bed_id) DO NOTHING",
                    explicit, dept,
                )
            else:
                logger.warning("Bed %s not in master and auto_create_beds off; allocating in %s", explicit, dept)
                return await _next_available(conn, schema, dept, auto_create)
        return explicit
    return await _next_available(conn, schema, dept, auto_create)


async def _next_available(conn, schema, dept, auto_create) -> Optional[str]:
    await _ensure_dept(conn, schema, dept)
    bed = await conn.fetchval(
        f"SELECT bed_id FROM {schema}.beds WHERE dept_id = $1 AND status = 'available' "
        f"ORDER BY bed_id LIMIT 1",
        dept,
    )
    if bed:
        return bed
    if auto_create:
        new_id = f"{dept}-{uuid.uuid4().hex[:6]}"
        await conn.execute(
            f"INSERT INTO {schema}.beds (bed_id, dept_id, status) VALUES ($1, $2, 'available')",
            new_id, dept,
        )
        return new_id
    return None  # patient becomes a boarder (no bed)


async def _ensure_dept(conn, schema, dept) -> None:
    await conn.execute(
        f"INSERT INTO {schema}.departments (dept_id, name, total_beds) VALUES ($1, $1, 0) "
        f"ON CONFLICT (dept_id) DO NOTHING",
        dept,
    )


async def _occupy_bed(conn, schema, bed_id, dept, event) -> None:
    await conn.execute(
        f"""
        UPDATE {schema}.beds
        SET previous_status = status,
            status = 'occupied',
            dept_id = $2,
            acuity_level = COALESCE($3, acuity_level),
            admission_time = $4,
            blocked_reason = NULL,
            last_updated = now(),
            synced_at = now()
        WHERE bed_id = $1
        """,
        bed_id, dept, event.esi_level, event.event_time,
    )


async def _free_bed(conn, schema, bed_id, event) -> None:
    await conn.execute(
        f"""
        UPDATE {schema}.beds
        SET previous_status = status,
            status = 'cleaning',
            acuity_level = NULL,
            admission_time = NULL,
            last_updated = now(),
            synced_at = now()
        WHERE bed_id = $1
        """,
        bed_id,
    )


async def _upsert_patient(conn, schema, event, pid, dept_id, bed_id, admission_time=None) -> None:
    p = event.patient
    lace_score, lace_risk = _lace(event, admission_time)
    await conn.execute(
        f"""
        INSERT INTO {schema}.patients
            (patient_id, mrn, name, age, gender, dept_id, bed_id,
             acuity_score, ed_visits_6mo, comorbidity_count,
             lace_score, lace_risk, admission_time, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, now())
        ON CONFLICT (patient_id) DO UPDATE SET
            mrn = COALESCE(EXCLUDED.mrn, {schema}.patients.mrn),
            name = COALESCE(EXCLUDED.name, {schema}.patients.name),
            age = COALESCE(EXCLUDED.age, {schema}.patients.age),
            gender = COALESCE(EXCLUDED.gender, {schema}.patients.gender),
            dept_id = COALESCE(EXCLUDED.dept_id, {schema}.patients.dept_id),
            bed_id = EXCLUDED.bed_id,
            acuity_score = COALESCE(EXCLUDED.acuity_score, {schema}.patients.acuity_score),
            lace_score = COALESCE(EXCLUDED.lace_score, {schema}.patients.lace_score),
            lace_risk = COALESCE(EXCLUDED.lace_risk, {schema}.patients.lace_risk),
            admission_time = COALESCE(EXCLUDED.admission_time, {schema}.patients.admission_time),
            updated_at = now()
        """,
        pid, p.mrn, p.name, p.age, p.gender, dept_id, bed_id,
        event.esi_level, p.ed_visits_6mo, p.comorbidity_count,
        lace_score, lace_risk, admission_time,
    )


def _lace(event: AdtEvent, admission_time) -> tuple[Optional[int], Optional[str]]:
    """Compute LACE from the event. Uses current LOS when an admit time is known."""
    los_days = 0.0
    at = admission_time or event.event_time
    if at:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        los_days = max(0.0, (event.event_time - at).total_seconds() / 86400.0)
    is_acute = (event.patient_class or "").lower() in ("emergency", "inpatient", "")
    esi = event.esi_level if event.esi_level in (1, 2, 3, 4, 5) else 3
    try:
        return compute_lace_score(los_days, is_acute, esi, event.patient.ed_visits_6mo)
    except ValueError:
        return None, None


async def _log_event(conn, schema, entity_type, entity_id, old, new, event) -> None:
    try:
        await conn.execute(
            f"""
            INSERT INTO {schema}.event_log
                (event_id, entity_type, entity_id, event_time, old_status, new_status, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (event_id) DO NOTHING
            """,
            uuid.uuid4().hex, entity_type, entity_id, event.event_time, old, new,
            event.source_format,
        )
    except Exception:  # noqa: BLE001 - event log is best-effort
        pass


async def _touch_clock(conn, schema, event) -> None:
    """Mark the serving layer fresh so the dashboard staleness signal is accurate."""
    try:
        await conn.execute(
            f"UPDATE {schema}.snapshot_meta SET clock = now(), synced_at = now() WHERE id = 1"
        )
    except Exception:  # noqa: BLE001
        pass
