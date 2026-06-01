#!/usr/bin/env bash
# Run the app locally against a local Postgres (stands in for Lakebase).
#
#   1. loads .env (copy from .env.example first)
#   2. ensures the database + schema exist and are seeded
#   3. starts the FastAPI server with the built frontend
#
# Prereqs: local Postgres running, and `pip install -r app/requirements.txt`.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
else
  echo "No .env found — copy .env.example to .env first." >&2; exit 1
fi

: "${PGDATABASE:=hospital_ops}"
: "${LAKEBASE_SCHEMA:=hospital_ops_lakebase}"

echo "→ ensuring database $PGDATABASE exists"
createdb "$PGDATABASE" 2>/dev/null || true

echo "→ bootstrapping schema + bed master"
python jobs/bootstrap_schema.py

echo "→ starting server on http://localhost:8000"
cd app
exec uvicorn serve:app --host 0.0.0.0 --port 8000 --app-dir backend
