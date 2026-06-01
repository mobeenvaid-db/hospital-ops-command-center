# Architecture

## Components

**Ingestion (new).** A FastAPI router at `/api/ingest/redox` receives Redox
deliveries. It authenticates the request, lands the raw payload to Delta bronze,
normalizes it into a canonical `AdtEvent`, and applies that event to the Postgres
serving tables in a transaction. The mapping logic lives entirely in
`backend/ingest`.

**Serving layer.** Databricks Lakebase (managed Postgres) holds the operational
state the dashboard reads. It is an OLTP store with B-tree indexes for fast point
lookups (the bed drill-down hits indexed rows in milliseconds). The schema is in
`sql/ddl/lakebase_serving.sql`.

**Bronze landing.** Every raw payload is appended to a Delta table. This is the
system of record for what the EHR sent and the source for replay. The sink is
pluggable: Delta on Databricks, or a local NDJSON file for development.

**Dashboard API.** The existing FastAPI endpoints (`/api/header`, `/api/beds`,
`/api/ed`, `/api/or`, `/api/forecast`, ...) read from the serving tables. Their
response contracts are unchanged, so the prebuilt React frontend keeps working.

**Copilot and agents.** The assistant (`/api/assistant`) and multi-agent
supervisor (`/api/agents`) call the Databricks Foundation Model API. They read
the same serving tables for context.

**Analytics (optional).** `jobs/compute_predictions.py` reads the live serving
tables and writes the `predictions` table on a schedule. This replaces the
simulator's prediction feed with forecasts derived from real data.

## Data flow

1. EHR emits HL7v2 or FHIR. Redox normalizes and delivers JSON to the webhook.
2. The webhook verifies the request and writes the raw body to bronze.
3. The payload is parsed to an `AdtEvent` (one model for both wire formats).
4. The applier upserts patients, occupies or frees beds, updates ED arrivals,
   writes the event log, and stamps `snapshot_meta.clock`.
5. The dashboard polls the read API and reflects the new state.

## Why one canonical event

Redox can deliver either its Data Model JSON or FHIR R4/R5, and a given site may
switch over time. Rather than spread format knowledge across the mapping, each
format has a small parser that produces a single `AdtEvent`. The applier and all
serving-table logic depend only on `AdtEvent`. Adding a new source (for example
Redox Scheduling for the OR panel) means writing one parser, not editing the
mapping.

## Realtime versus simulation

The simulator (control and lifecycle routes, plus the external seed and simulate
jobs) is demo-only and mounted only when `ENABLE_SIMULATION=true`. In a real
deployment it is off, and the serving tables are driven entirely by ingestion.
`snapshot_meta.clock` is set to the wall clock on each applied event, so the
dashboard's staleness signal reflects how recently real data arrived.

## Failure behavior

- Database unreachable: the webhook returns 503 and Redox retries.
- Bronze write fails: logged, never blocks the serving upsert.
- One message in a batch fails to map: recorded in `ingest_log` as an error,
  the rest of the batch still applies.
- Duplicate delivery (same message id): skipped via the `ingest_log` unique index.
- Expired Lakebase OAuth token: the connection pool refreshes and retries once.
