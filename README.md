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
| Simulator | Always on (external Databricks jobs) | In-process, off by default, behind `ENABLE_SIMULATION` (no jobs needed) |
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

## Run as a live demo (simulation mode)

For a self-contained demo with no Redox feed and no external jobs, set
`ENABLE_SIMULATION=true`. The app then runs a built-in synthetic ADT generator
in-process. It seeds a realistic census on startup and keeps it moving:
new ED arrivals, admits to beds, transfers, and discharges flow continuously
through the SAME applier the webhook uses, so the dashboard updates live and the
copilot answers against real-looking state.

```bash
cp .env.example .env
# in .env, set: ENABLE_SIMULATION=true
python jobs/bootstrap_schema.py
./scripts/run_local.sh &
open http://localhost:8000
```

The dashboard's controls drive the simulator directly:

- **Seed & Start** reloads a clean census.
- **Start / Stop** resume and pause the generator.
- **Fast Forward / ED Surge / Peak Hour** presets adjust the rate and time of day.
  These post to `/api/control`, which the simulator reads each cycle.

The arrival rate, acuity mix, admission probability, and discharge timing all
come from the research-calibrated constants in `app/backend/lib/constants.py`,
so the synthetic stream behaves like a real community hospital. The generator
self-balances around each unit's target occupancy, so the board stays busy but
does not run away. Set `ENABLE_SIMULATION=false` (the default) to return to
realtime mode, where data arrives only from the ingest webhook.

> The original demo simulator ran as external Databricks Jobs. If you set both
> `SEED_JOB_ID` and `SIMULATE_JOB_ID` (with `ENABLE_SIMULATION=true`), the
> lifecycle controls drive those jobs instead. With no job ids set, the portable
> in-process simulator above is used.

## Deploy to Databricks

See [docs/DEPLOY.md](docs/DEPLOY.md). In short: provision Lakebase and a serving
endpoint, run `bootstrap_schema.py`, fill in `app.yaml` from the template, deploy
the app, then point a Redox destination at `https://<app-url>/api/ingest/redox`.

## Connecting Redox

See [docs/REDOX_INTEGRATION.md](docs/REDOX_INTEGRATION.md) for the destination
setup, the verification handshake, signature verification, and the full
field-by-field mapping spec.

## Adapt this to your environment

Everything that ties the app to a specific hospital, EHR, or workspace is config,
not code. To stand this up for a new site, work through this checklist. None of it
requires touching the dashboard or the agents.

1. **Site name.** Set `SITE_NAME` (env / `app.yaml`). It is the only place the
   facility is named.
2. **Bed master.** Copy `config/departments.example.yaml` to
   `config/departments.yaml` and list your real units and bed counts. This seeds
   the canonical bed inventory. Set `AUTO_CREATE_BEDS=true` to let beds appear from
   ingest instead of pre-seeding.
3. **Unit mapping.** Copy `config/location_map.example.yaml` to
   `config/location_map.yaml` and map your EHR's unit / location codes to the
   canonical departments. This is what turns "5E" or "MICU-A" into a department the
   dashboard understands.
4. **Serving schema.** `sql/ddl/lakebase_serving.sql` is the canonical Postgres
   schema. Run `jobs/bootstrap_schema.py` to create and seed it. Change
   `LAKEBASE_SCHEMA` if you want a different schema name.
5. **Catalog and bronze.** Set `DELTA_CATALOG`, `DELTA_BRONZE_SCHEMA`, and
   `WAREHOUSE_ID` in `app.yaml` for durable raw landing, or set `BRONZE_SINK=local`
   / `none` to skip it.
6. **Ingestion source.** Today the webhook accepts Redox Data Model and FHIR R4/R5.
   To accept a different integration engine, add a parser under
   `app/backend/ingest/parsers/` that emits the canonical `AdtEvent` and register it
   in `normalize.py`. Nothing downstream of `AdtEvent` changes, the applier, serving
   tables, dashboard, and copilot are source-agnostic.
7. **Copilot model.** Set `SERVING_ENDPOINT` to any Foundation Model API chat
   endpoint. No code change to swap models.
8. **App config and secrets.** Copy `app/app.yaml.example` to `app/app.yaml`, fill
   `PGUSER` with the app's service principal id, and store
   `REDOX_VERIFICATION_TOKEN` / `REDOX_SIGNATURE_SECRET` as app secrets rather than
   inline values.

`app/app.yaml`, `.env`, `config/departments.yaml`, and `config/location_map.yaml`
are git-ignored on purpose, so your workspace ids, service principal, and site
layout never get committed. Commit only the `.example` templates.

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
