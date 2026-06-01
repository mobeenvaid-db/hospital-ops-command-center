#!/usr/bin/env python3
"""End-to-end verification of the ingestion pipeline against local Postgres.

Drives the REAL parsers + applier (backend/ingest) over the sample Redox/FHIR
payloads, then runs the actual dashboard read queries (backend/queries.py) to
prove the ingested state shows up correctly.

It connects through a tiny `psql` shim (PsqlConn) that implements just the
asyncpg surface the applier/queries use (execute / fetchval / fetchrow / fetch /
transaction). This lets us validate the genuine SQL against a real database
without the asyncpg wheel.

Requires: a running local Postgres + `psql` on PATH. No Python deps beyond the
stdlib (+ PyYAML for the location map).

Usage:
  PGDATABASE=capcmd_test LAKEBASE_SCHEMA=hospital_ops_lakebase \
    python scripts/verify_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "app" / "backend"
sys.path.insert(0, str(BACKEND))

from ingest.applier import apply_event  # noqa: E402
from ingest.location_map import LocationMap  # noqa: E402
from ingest.normalize import normalize  # noqa: E402
import queries  # noqa: E402

SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "hospital_ops_lakebase")
SAMPLES = REPO / "samples"

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
_failures = 0


def check(label, got, expected):
    global _failures
    ok = got == expected
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}: got={got} expected={expected}")
    if not ok:
        _failures += 1


def _fmt(arg) -> str:
    """Render a Python value as a Postgres SQL literal."""
    if arg is None:
        return "NULL"
    if isinstance(arg, bool):
        return "TRUE" if arg else "FALSE"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, datetime):
        return "'" + arg.isoformat() + "'"
    return "'" + str(arg).replace("'", "''") + "'"


def _subst(query: str, args) -> str:
    # Replace $N from highest index down so $10 doesn't clobber $1.
    out = query
    for i in range(len(args), 0, -1):
        out = out.replace(f"${i}", _fmt(args[i - 1]))
    return out


SEP = "\x1f"


class PsqlConn:
    """Minimal asyncpg-compatible connection backed by the psql CLI."""

    def __init__(self, db, schema):
        self.db = db
        self.schema = schema
        self._env = dict(os.environ, PGOPTIONS=f"-c search_path={schema}")

    def _psql(self, sql, tuples):
        cmd = ["psql", "-d", self.db, "-X", "-q", "-v", "ON_ERROR_STOP=1",
               "-P", "footer=off", "-A", "-F", SEP]
        if tuples:
            cmd.append("-t")
        cmd += ["-c", sql]
        res = subprocess.run(cmd, capture_output=True, text=True, env=self._env)
        if res.returncode != 0:
            raise RuntimeError(f"psql error:\n{res.stderr}\nSQL: {sql[:500]}")
        return res.stdout

    async def execute(self, query, *args):
        self._psql(_subst(query, args), tuples=True)

    async def fetchval(self, query, *args):
        out = self._psql(_subst(query, args), tuples=True).strip()
        if not out:
            return None
        return _coerce(out.splitlines()[0].split(SEP)[0])

    async def fetchrow(self, query, *args):
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def fetch(self, query, *args):
        out = self._psql(_subst(query, args), tuples=False)
        lines = [ln for ln in out.splitlines() if ln != ""]
        if not lines:
            return []
        header = lines[0].split(SEP)
        return [{h: _coerce(v) for h, v in zip(header, ln.split(SEP))} for ln in lines[1:]]

    def transaction(self):
        return _NoTxn()


class _NoTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _coerce(v):
    if v == "" or v is None:
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


async def main():
    db = os.environ.get("PGDATABASE", "capcmd_test")
    conn = PsqlConn(db, SCHEMA)
    locmap = LocationMap.load(str(REPO / "config" / "location_map.example.yaml"))

    print("\n── Applying ADT event stream (Redox + FHIR) ──")
    seq = [
        "redox_01_arrival.json",
        "redox_02_admit.json",
        "redox_03_transfer.json",
        "fhir_admit_bundle.json",
    ]
    for name in seq:
        payload = json.loads((SAMPLES / name).read_text())
        event = normalize(payload)
        result = await apply_event(conn, SCHEMA, event, locmap, auto_create_beds=True)
        print(f"  applied {name:28s} -> {event.event_type.value:9s} {result}")

    print("\n── Dashboard reads after admits ──")
    header = await queries.get_header_metrics(conn, SCHEMA)
    # Maria admitted ICU then transferred to TELE (1 bed), James admitted ICU (1 bed) => census 2
    check("total_census", header["total_census"], 2)

    dept_metrics = {d["dept_id"]: d for d in await queries.get_dept_metrics(conn, SCHEMA)}
    check("ICU occupied", dept_metrics["ICU"]["occupied"], 1)
    check("TELE occupied", dept_metrics["TELE"]["occupied"], 1)

    beds = await queries.get_beds(conn, SCHEMA)
    maria_bed = await conn.fetchval(
        f"SELECT bed_id FROM {SCHEMA}.patients WHERE mrn = '0000001234'")
    maria_dept = await conn.fetchval(
        f"SELECT dept_id FROM {SCHEMA}.patients WHERE mrn = '0000001234'")
    check("Maria moved to TELE", maria_dept, "TELE")

    lace = await conn.fetchval(
        f"SELECT lace_risk FROM {SCHEMA}.patients WHERE mrn = '0000007788'")
    print(f"  James LACE risk computed from real record: {lace}")
    check("LACE computed (non-null)", lace is not None, True)

    print("\n── Applying discharges ──")
    for name in ["redox_04_discharge.json", "fhir_discharge_bundle.json"]:
        event = normalize(json.loads((SAMPLES / name).read_text()))
        result = await apply_event(conn, SCHEMA, event, locmap, auto_create_beds=True)
        print(f"  applied {name:28s} -> {event.event_type.value:9s} {result}")

    header2 = await queries.get_header_metrics(conn, SCHEMA)
    check("total_census after discharge", header2["total_census"], 0)
    # Vacated beds go to 'cleaning': ICU-001 (freed by Maria's transfer),
    # TELE-001 and "ICU Bed 4" (freed by the two discharges) => 3.
    cleaning = await conn.fetchval(
        f"SELECT COUNT(*) FROM {SCHEMA}.beds WHERE status = 'cleaning'")
    check("vacated beds cleaning", cleaning, 3)

    print("\n── Idempotency / event log ──")
    log_n = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.event_log")
    check("event_log populated", log_n > 0, True)

    print()
    if _failures:
        print(f"{RED}{_failures} check(s) failed{RESET}")
        sys.exit(1)
    print(f"{GREEN}All end-to-end checks passed.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
