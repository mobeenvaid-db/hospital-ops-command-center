-- ════════════════════════════════════════════════════════════════════
-- Capacity Command (Realtime) — Delta bronze raw landing
--
-- Every raw Redox payload is appended here BEFORE it is parsed/mapped into
-- the Lakebase serving tables. This gives you:
--   • a durable, immutable audit of exactly what the EHR sent,
--   • replay (re-run the mapper over history after a logic fix),
--   • a debugging trail when a message doesn't map cleanly.
--
-- Run on a SQL warehouse / cluster. Set the catalog + schema to match
-- DELTA_CATALOG / DELTA_BRONZE_SCHEMA in your .env.
-- ════════════════════════════════════════════════════════════════════

CREATE CATALOG IF NOT EXISTS ${catalog};
CREATE SCHEMA IF NOT EXISTS ${catalog}.${bronze_schema};

-- Raw, append-only. payload is stored as a string (and parsed VARIANT) so
-- nothing is lost even if the shape is unexpected.
CREATE TABLE IF NOT EXISTS ${catalog}.${bronze_schema}.redox_events_raw (
    ingest_id        STRING,               -- uuid we assign on receipt
    received_at      TIMESTAMP,            -- when our endpoint received it
    source_format    STRING,               -- redox-datamodel | fhir | scheduling
    data_model       STRING,               -- Meta.DataModel (PatientAdmin, ...)
    event_type       STRING,               -- Meta.EventType (Arrival, Discharge, ...)
    redox_message_id STRING,               -- Meta.Message.ID (idempotency key)
    source_name      STRING,               -- Meta.Source.Name (sending facility/app)
    payload          STRING,               -- exact raw JSON body
    payload_variant  VARIANT,              -- parsed for ad-hoc SQL (Databricks Runtime 15.3+)
    headers          MAP<STRING, STRING>,  -- selected request headers (verification, signature)
    processed        BOOLEAN,              -- did the mapper apply it to Lakebase?
    process_error    STRING
)
USING DELTA
PARTITIONED BY (DATE(received_at))
TBLPROPERTIES (
    delta.enableChangeDataFeed = true,
    delta.autoOptimize.optimizeWrite = true,
    delta.autoOptimize.autoCompact = true
);

-- Optional: a normalized silver view of canonical AdtEvents, useful for
-- analytics and for the compute_predictions job. The ingestion service can
-- also write here directly; otherwise build it with a streaming job over
-- redox_events_raw using the same parsers in backend/ingest.
CREATE TABLE IF NOT EXISTS ${catalog}.${bronze_schema}.adt_events_silver (
    event_id          STRING,
    received_at       TIMESTAMP,
    event_time        TIMESTAMP,
    event_type        STRING,               -- canonical: arrival|admit|transfer|discharge|update|cancel
    mrn               STRING,
    patient_id        STRING,
    encounter_id      STRING,
    patient_class     STRING,               -- emergency|inpatient|outpatient|observation
    dept_id           STRING,
    bed_id            STRING,
    facility          STRING,
    raw_location      STRING,
    esi_level         INT,
    source_format     STRING,
    redox_message_id  STRING
)
USING DELTA
PARTITIONED BY (DATE(received_at));
