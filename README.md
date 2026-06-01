# Capacity Command (Realtime)

A hospital Capacity Command Center that runs on **live ADT data from your EHR**,
delivered through **Redox**. It shows bed and census state, ED and OR status,
readmission risk, anomalies, and a Claude-powered operations copilot, all served
from a low-latency Postgres layer (Databricks Lakebase).

This is a portable, productized version of the original Capacity Command demo.
The demo invented its data with a simulator. This version ingests real
admit / transfer / discharge events over a webhook, lands them durably, and maps
them into the serving tables the dashboard reads.

## What changed versus the demo

| Area | Demo | Realtime |
| --- | --- | --- |
| Data source | Synthetic simulator (Databricks jobs) | Redox ADT/FHIR over a webhook |
| Schema | Created by an external seed job (not in repo) | Canonical DDL in `sql/ddl` |
| Config | Hardcoded job IDs and service principal | Env driven, `app.yaml.example` and `.env.example` |
| Raw data | None | Durable Delta bronze landing with replay |
| Simulator | Always on | Off by default, behind `ENABLE_SIMULATION` |
| Predictions | Simulator wrote them | Optional analytics job over real data |

## How it works

```
  Epic / Cerner / other EHR
            │  HL7v2 / FHIR
            ▼
        Redox  ──(normalizes)──►  HTTPS webhook delivery
                                         │  Redox Data Model JSON or FHIR R4/R5
                                         ▼
                         POST /api/ingest/redox  (FastAPI)
                                         │
            ┌────────────────────────────┼───────────────────────────┐
            ▼                            ▼                            ▼
   verify token/HMAC          land raw → Delta bronze        normalize → AdtEvent
                                                                      │
                                                          map to canonical state
                                                                      ▼
                                       Lakebase (Postgres) serving tables
                                       beds · patients · ed_arrivals · event_log
                                                                      ▼
                                       React dashboard + Claude copilot
```

Both Redox delivery flavors are supported. A thin adapter detects the payload
shape and routes it to the right parser, then everything flows through one
canonical `AdtEvent` and one applier, so the mapping logic is written once.

## Quickstart (local, against local Postgres)

```bash
cp .env.example .env                 # adjust PG* if needed
pip install -r app/requirements.txt
brew services start postgresql@16    # or any local Postgres

python jobs/bootstrap_schema.py      # create schema + seed bed master
./scripts/run_local.sh &             # start the app on :8000
./scripts/send_samples.sh            # feed it the sample ADT stream
open http://localhost:8000
```

To verify the full ingestion path without standing up the server (drives the
real parsers, applier, and dashboard queries against Postgres):

```bash
PGDATABASE=hospital_ops python scripts/verify_e2e.py
```

## Deploy to Databricks

See [docs/DEPLOY.md](docs/DEPLOY.md). In short: provision Lakebase and a serving
endpoint, run `bootstrap_schema.py`, fill in `app.yaml` from the template, deploy
the app, then point a Redox destination at `https://<app-url>/api/ingest/redox`.

## Connecting Redox

See [docs/REDOX_INTEGRATION.md](docs/REDOX_INTEGRATION.md) for the destination
setup, the verification handshake, signature verification, and the full
field-by-field mapping spec.

## Repo layout

```
app/
  app.yaml.example          Databricks App config template
  backend/
    ingest/                 Redox webhook + parsers + mapper (the new core)
      redox_routes.py        webhook: verify, land, normalize, apply
      normalize.py           format detection + dispatch
      parsers/               redox_datamodel.py, fhir.py
      models.py              canonical AdtEvent
      applier.py             AdtEvent → Lakebase serving-table mutations
      location_map.py        EHR unit → canonical department
      bronze.py              raw landing (Delta | local file)
      security.py            Redox verification + HMAC
    main.py, queries.py, ...  dashboard API (unchanged contract)
    control_routes.py         simulator (only mounted if ENABLE_SIMULATION)
  frontend/dist/            built React SPA (served as-is)
sql/
  ddl/lakebase_serving.sql  canonical Postgres serving schema
  ddl/delta_bronze.sql      raw bronze landing tables
config/
  departments.example.yaml  bed master bootstrap
  location_map.example.yaml  unit → department mapping
jobs/
  bootstrap_schema.py       create + seed the serving schema
  compute_predictions.py    optional analytics → predictions table
samples/                    example Redox + FHIR payloads
scripts/                    run_local.sh, send_samples.sh, verify_e2e.py
docs/                       ARCHITECTURE, REDOX_INTEGRATION, SCHEMA, DEPLOY
```

## A note on OR and predictions

ADT carries admit, transfer, and discharge. It does not carry surgical schedules
or forecasts, so:

- **OR panel** is fed by an optional Redox Scheduling feed. The ingestion
  framework is multi-data-model and the OR panel degrades gracefully (empty)
  until that feed is connected.
- **Predictions panel** is fed by `jobs/compute_predictions.py`, a scheduled job
  that derives discharge and arrival forecasts from the real serving tables.
  LACE readmission risk is computed on ingest from the real patient record.
