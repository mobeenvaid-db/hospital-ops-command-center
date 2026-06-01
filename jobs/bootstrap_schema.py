#!/usr/bin/env python3
"""Bootstrap the Lakebase serving schema and seed the bed master.

Idempotent. Run it once before pointing Redox at the app, and again any time
you change config/departments.yaml.

  1. applies sql/ddl/lakebase_serving.sql (with the {{SCHEMA}} placeholder
     replaced by LAKEBASE_SCHEMA)
  2. upserts departments + beds from config/departments.yaml

Connection comes from the standard PG* env vars (local Postgres or Lakebase).
Auth: PGPASSWORD if set, otherwise a Databricks OAuth token (when running on
Databricks / with a CLI profile).

Usage:
  python jobs/bootstrap_schema.py
  python jobs/bootstrap_schema.py --departments config/departments.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

REPO = Path(__file__).resolve().parent.parent
DDL = REPO / "sql" / "ddl" / "lakebase_serving.sql"


def _password() -> str:
    pw = os.environ.get("PGPASSWORD") or os.environ.get("LAKEBASE_PASSWORD")
    if pw:
        return pw
    # Fall back to a Databricks OAuth token when available.
    try:
        sys.path.insert(0, str(REPO / "app" / "backend"))
        from config import get_oauth_token  # type: ignore
        return get_oauth_token()
    except Exception:
        return ""


async def _connect() -> asyncpg.Connection:
    ssl = "require" if os.environ.get("PGSSLMODE") or os.environ.get("DATABRICKS_APP_NAME") else None
    return await asyncpg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ.get("PGDATABASE", "hospital_ops"),
        user=os.environ.get("PGUSER", os.environ.get("USER", "postgres")),
        password=_password() or None,
        ssl=ssl,
    )


async def apply_ddl(conn: asyncpg.Connection, schema: str) -> None:
    sql = DDL.read_text().replace("{{SCHEMA}}", schema)
    await conn.execute(sql)
    print(f"✓ schema {schema} created/verified")


async def seed_departments(conn: asyncpg.Connection, schema: str, cfg: dict) -> None:
    await conn.execute(f"SET search_path TO {schema}")
    depts = cfg.get("departments") or []
    dept_count = bed_count = 0
    for d in depts:
        await conn.execute(
            "INSERT INTO departments (dept_id, name, floor, total_beds) "
            "VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (dept_id) DO UPDATE SET name=EXCLUDED.name, "
            "floor=EXCLUDED.floor, total_beds=EXCLUDED.total_beds",
            d["dept_id"], d.get("name", d["dept_id"]), d.get("floor"), int(d.get("count", 0)),
        )
        dept_count += 1
        prefix = d.get("bed_prefix", d["dept_id"])
        for i in range(1, int(d.get("count", 0)) + 1):
            bed_id = f"{prefix}-{i:03d}"
            await conn.execute(
                "INSERT INTO beds (bed_id, dept_id, status) VALUES ($1,$2,'available') "
                "ON CONFLICT (bed_id) DO NOTHING",
                bed_id, d["dept_id"],
            )
            bed_count += 1
    print(f"✓ seeded {dept_count} departments, {bed_count} beds")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--departments", default=str(REPO / "config" / "departments.yaml"))
    args = ap.parse_args()

    schema = os.environ.get("LAKEBASE_SCHEMA", "hospital_ops_lakebase")

    dept_path = Path(args.departments)
    if not dept_path.exists():
        example = dept_path.with_suffix(".example.yaml")
        if example.exists():
            dept_path = example
            print(f"(using {example.name}; copy it to departments.yaml to customize)")
        else:
            sys.exit(f"departments config not found: {args.departments}")
    cfg = yaml.safe_load(dept_path.read_text()) or {}

    conn = await _connect()
    try:
        await apply_ddl(conn, schema)
        await seed_departments(conn, schema, cfg)
    finally:
        await conn.close()
    print("Bootstrap complete.")


if __name__ == "__main__":
    asyncio.run(main())
