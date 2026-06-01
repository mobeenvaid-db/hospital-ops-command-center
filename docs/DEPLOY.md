# Deploy to Databricks

This deploys the app as a Databricks App backed by Lakebase, the Foundation
Model API, and a SQL warehouse for the Delta bronze landing.

## Prerequisites

- A Databricks workspace with Databricks Apps and Lakebase enabled
- The Databricks CLI authenticated to that workspace
- A SQL warehouse (for bronze inserts)
- A Redox account with a destination you can point at the app

## 1. Provision the serving database (Lakebase)

Create a Lakebase instance and a database named `hospital_ops` (or your choice,
matching `PGDATABASE`). Note the instance so you can wire it as the app's
`database` resource.

## 2. Create the bronze tables (Delta)

Set the catalog and schema, then run `sql/ddl/delta_bronze.sql` on a warehouse or
cluster. Substitute the variables, for example:

```sql
-- replace ${catalog} and ${bronze_schema} with your values, or run via a
-- notebook widget. Default schema name: hospital_ops_bronze
```

## 3. Bootstrap the serving schema

From a machine with network access to Lakebase and the PG environment set:

```bash
export PGHOST=<lakebase-host> PGPORT=5432 PGDATABASE=hospital_ops
export PGUSER=<app-service-principal-uuid> LAKEBASE_SCHEMA=hospital_ops_lakebase
export PGSSLMODE=require
python jobs/bootstrap_schema.py --departments config/departments.yaml
```

This creates the schema and seeds departments and beds from your config. Auth
uses a Databricks OAuth token automatically when `PGPASSWORD` is not set.

## 4. Configure the app

```bash
cp app/app.yaml.example app/app.yaml
```

Fill in the placeholders:

- `PGUSER`: the app service principal UUID (from the app details page)
- `DELTA_CATALOG`, `WAREHOUSE_ID`: for the bronze sink
- `SITE_NAME`: your facility name

Create the ingestion secrets and reference them with `valueFrom`:

```bash
databricks secrets create-scope capacity-command
databricks secrets put-secret capacity-command redox-verification-token
databricks secrets put-secret capacity-command redox-signature-secret
```

Keep `ENABLE_SIMULATION=false` and `REQUIRE_INGEST_AUTH=true`.

## 5. Deploy

```bash
databricks apps deploy capacity-command --source-code-path app
```

Wire the `database` and `serving-endpoint` resources to your Lakebase instance
and FMAPI endpoint in the app configuration, and grant the app service principal
read and write on the `hospital_ops_lakebase` schema and read on the bronze
tables.

## 6. Connect Redox

In Redox, create a destination with URL
`https://<your-app-url>/api/ingest/redox` and set the verification token to the
value you stored as `redox-verification-token`. Complete the verification
handshake, then send a test message. Confirm it lands:

```bash
curl https://<your-app-url>/api/ingest/health
```

and check `ingest_log` and the dashboard. See
[REDOX_INTEGRATION.md](REDOX_INTEGRATION.md) for the full setup and mapping.

## 7. Schedule analytics (optional)

Create a Databricks Job that runs `jobs/compute_predictions.py` every few minutes
to populate the forecast panel from real data. The job needs the same PG
environment as the bootstrap step.

## Smoke test in this workspace

For a quick validation in a development workspace, you can run the local flow
against a Lakebase instance by exporting the PG variables and using
`scripts/send_samples.sh` against the deployed app URL.
