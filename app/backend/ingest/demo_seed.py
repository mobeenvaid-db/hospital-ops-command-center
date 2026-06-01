"""Demo seed for realtime mode.

In a real deployment, data arrives from Redox and there is no "seed". For demos
and for this validation deployment (no live feed connected), the dashboard's
"Seed & Start" button calls this to load a realistic census by pushing a batch
of synthetic ADT events through the SAME applier the webhook uses. So the seed
exercises the real ingestion path rather than writing rows behind its back.

It first clears transactional state so repeated seeds give a clean snapshot.
"""

from __future__ import annotations

import random
from datetime import timedelta

from lib.constants import DEPARTMENTS, DEPT_OCCUPANCY_TARGET

from .applier import apply_event
from .location_map import LocationMap
from .models import AdtEvent, EventType, LocationRec, PatientRec
from .util import now_utc

_FIRST = ["Maria", "James", "Linda", "Robert", "Patricia", "John", "Jennifer", "Michael",
          "Susan", "David", "Karen", "Richard", "Nancy", "Joseph", "Betty", "Thomas",
          "Sandra", "Charles", "Ashley", "Daniel", "Emily", "Carlos", "Aisha", "Wei"]
_LAST = ["Alvarez", "Okafor", "Smith", "Johnson", "Nguyen", "Patel", "Garcia", "Brown",
         "Lee", "Martinez", "Davis", "Rodriguez", "Wilson", "Anderson", "Thomas", "Khan",
         "OBrien", "Cohen", "Ferreira", "Yamamoto"]


def _patient(rng: random.Random, n: int) -> PatientRec:
    return PatientRec(
        mrn=f"{1000000 + n:07d}",
        name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
        age=rng.randint(18, 95),
        gender=rng.choice(["M", "F"]),
        ed_visits_6mo=rng.choices([0, 1, 2, 3, 4], weights=[50, 25, 15, 7, 3])[0],
    )


async def seed_demo(conn, schema: str, locmap: LocationMap) -> dict:
    """Load a realistic census via the applier. Returns a summary dict."""
    rng = random.Random(42)
    now = now_utc()

    # 1. clear transactional state for a clean snapshot (beds back to available).
    # DELETE (not TRUNCATE) so the app's role needs only DML privileges.
    for tbl in ("patients", "ed_arrivals", "event_log", "ingest_log"):
        await conn.execute(f"DELETE FROM {schema}.{tbl}")
    await conn.execute(
        f"UPDATE {schema}.beds SET status='available', acuity_level=NULL, "
        f"admission_time=NULL, previous_status=NULL, blocked_reason=NULL"
    )

    n = 0
    admits = 0
    # 2. admit inpatients to hit each department's target occupancy
    inpatient = [d for d in DEPARTMENTS if d["dept_id"] in DEPT_OCCUPANCY_TARGET and d["dept_id"] != "ED"]
    for dept in inpatient:
        dept_id = dept["dept_id"]
        target = DEPT_OCCUPANCY_TARGET.get(dept_id, 0.7)
        count = round(dept["total_beds"] * target)
        for _ in range(count):
            n += 1
            esi = rng.choices([1, 2, 3, 4], weights=[10, 35, 45, 10])[0]
            hours_ago = rng.uniform(6, 120)
            ev = AdtEvent(
                event_type=EventType.ADMIT,
                event_time=now - timedelta(hours=hours_ago),
                patient=_patient(rng, n),
                patient_class="inpatient",
                esi_level=esi,
                location=LocationRec(department=dept_id, raw=dept_id),
                source_format="demo-seed",
                data_model="PatientAdmin",
                source_event_label="Admit",
            )
            res = await apply_event(conn, schema, ev, locmap, auto_create_beds=False)
            if res.get("bed_id"):
                admits += 1

    # 3. a handful of ED arrivals still waiting
    ed_waiting = 0
    for _ in range(rng.randint(8, 14)):
        n += 1
        esi = rng.choices([1, 2, 3, 4, 5], weights=[2, 25, 50, 18, 5])[0]
        mins_ago = rng.uniform(5, 180)
        ev = AdtEvent(
            event_type=EventType.ARRIVAL,
            event_time=now - timedelta(minutes=mins_ago),
            patient=_patient(rng, n),
            patient_class="emergency",
            esi_level=esi,
            location=LocationRec(department="Emergency", raw="Emergency"),
            source_format="demo-seed",
            data_model="PatientAdmin",
            source_event_label="Arrival",
        )
        await apply_event(conn, schema, ev, locmap, auto_create_beds=False)
        ed_waiting += 1

    return {"patients_admitted": admits, "ed_waiting": ed_waiting, "total_events": n}
