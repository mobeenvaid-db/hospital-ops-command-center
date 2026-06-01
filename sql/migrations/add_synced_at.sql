-- Migration: add synced_at columns used by the /api/pipeline lag metric.
-- Safe to run repeatedly. Only needed if you created the serving tables from
-- an older schema that predates synced_at (the current lakebase_serving.sql
-- already includes these columns).
--
-- synced_at is stamped by the Delta->Lakebase sync (or by the ingest service
-- when writing directly) so the dashboard can show end-to-end pipeline lag:
--     lag = synced_at - last_write
-- {{SCHEMA}} is a placeholder (see lakebase_serving.sql header).
SET search_path TO {{SCHEMA}};

ALTER TABLE beds          ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
ALTER TABLE ed_arrivals   ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
ALTER TABLE or_schedule   ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
ALTER TABLE snapshot_meta ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
