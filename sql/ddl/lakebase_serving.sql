-- ════════════════════════════════════════════════════════════════════
-- Capacity Command (Realtime) — Lakebase / Postgres serving schema
--
-- This is the OLTP serving layer the dashboard reads from. It is populated
-- by the Redox ingestion pipeline (backend/ingest), not a simulator.
--
-- {{SCHEMA}} is a placeholder. Apply with either:
--   • jobs/bootstrap_schema.py  (recommended — also seeds departments + beds)
--   • sed 's/{{SCHEMA}}/hospital_ops_lakebase/g' lakebase_serving.sql | psql "$DATABASE_URL"
-- ════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS {{SCHEMA}};
SET search_path TO {{SCHEMA}};

-- ── departments ──────────────────────────────────────────────────────
-- One row per physical care area. dept_id is the canonical key the whole
-- app keys on (ED, OR, ICU, TELE, MEDSURG, PEDS, ...). Seed from
-- config/departments.example.yaml so it matches how YOUR facility is laid out.
CREATE TABLE IF NOT EXISTS departments (
    dept_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    floor       INTEGER,
    total_beds  INTEGER NOT NULL DEFAULT 0
);

-- ── beds ─────────────────────────────────────────────────────────────
-- The bed master + live state. Status lifecycle:
--   available -> occupied (admit/transfer-in)
--   occupied  -> cleaning (discharge/transfer-out)
--   cleaning  -> available
--   available <-> blocked (operational hold)
CREATE TABLE IF NOT EXISTS beds (
    bed_id              TEXT PRIMARY KEY,
    dept_id             TEXT NOT NULL REFERENCES departments(dept_id),
    status              TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available','occupied','cleaning','blocked')),
    acuity_level        INTEGER,           -- 1 (highest) .. 5 (lowest); patient acuity in the bed
    admission_time      TIMESTAMPTZ,       -- when the current patient was admitted to this bed
    expected_discharge  TIMESTAMPTZ,
    blocked_reason      TEXT,
    previous_status     TEXT,              -- for the derived event feed
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at           TIMESTAMPTZ        -- set when a Delta-sync writes the row (pipeline lag metric)
);
CREATE INDEX IF NOT EXISTS idx_beds_dept   ON beds (dept_id);
CREATE INDEX IF NOT EXISTS idx_beds_status ON beds (status);

-- ── ed_arrivals ──────────────────────────────────────────────────────
-- One row per Emergency Department encounter. A "boarder" is an admitted
-- patient (bed_assigned + triage_complete) still physically waiting in the ED.
CREATE TABLE IF NOT EXISTS ed_arrivals (
    arrival_id           TEXT PRIMARY KEY,
    esi_level            INTEGER,          -- Emergency Severity Index 1..5
    arrival_time         TIMESTAMPTZ NOT NULL,
    disposition          TEXT NOT NULL DEFAULT 'waiting'
                         CHECK (disposition IN ('waiting','admitted','discharged','transferred','lwbs')),
    wait_minutes         NUMERIC,
    bed_assigned         TEXT,             -- bed_id once an inpatient bed is assigned
    triage_complete      TIMESTAMPTZ,      -- set when triage finishes
    previous_disposition TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ed_disposition ON ed_arrivals (disposition);
CREATE INDEX IF NOT EXISTS idx_ed_arrival_time ON ed_arrivals (arrival_time);

-- ── patients ─────────────────────────────────────────────────────────
-- Inpatient roster + readmission risk + discharge prediction.
-- LACE is computed on ingest (backend/ingest/applier) from the real record.
CREATE TABLE IF NOT EXISTS patients (
    patient_id               TEXT PRIMARY KEY,
    mrn                      TEXT,
    name                     TEXT,
    age                      INTEGER,
    gender                   TEXT,
    arrival_id               TEXT,         -- links to ed_arrivals when admitted via ED
    bed_id                   TEXT,         -- current bed (NULL once discharged)
    dept_id                  TEXT,
    length_of_stay_days      NUMERIC,
    acuity_score             INTEGER,
    comorbidity_count        INTEGER,
    ed_visits_6mo            INTEGER DEFAULT 0,
    lace_score               INTEGER,
    lace_risk                TEXT,         -- LOW | MODERATE | HIGH
    predicted_discharge_time TIMESTAMPTZ,
    discharge_reason         TEXT,
    confidence_score         NUMERIC,
    admission_time           TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_patients_bed     ON patients (bed_id);
CREATE INDEX IF NOT EXISTS idx_patients_mrn     ON patients (mrn);
CREATE INDEX IF NOT EXISTS idx_patients_arrival ON patients (arrival_id);

-- ── or_schedule ──────────────────────────────────────────────────────
-- Operating-room cases. NOT populated by ADT — fed by an optional Redox
-- Scheduling feed (see backend/ingest/parsers). Empty is fine; the OR
-- panel degrades gracefully.
CREATE TABLE IF NOT EXISTS or_schedule (
    or_id             TEXT PRIMARY KEY,
    room_id           TEXT NOT NULL,       -- e.g. OR-1 .. OR-8
    procedure_type    TEXT,
    surgeon_specialty TEXT,
    status            TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK (status IN ('scheduled','in-progress','turnover','complete','cancelled')),
    scheduled_start   TIMESTAMPTZ,
    actual_start      TIMESTAMPTZ,
    est_duration_min  INTEGER,
    previous_status   TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_or_room   ON or_schedule (room_id);
CREATE INDEX IF NOT EXISTS idx_or_status ON or_schedule (status);

-- ── predictions ──────────────────────────────────────────────────────
-- Forecast rows written by the optional analytics job (jobs/compute_predictions.py).
-- pred_type in ('discharge','ed_arrival','capacity','anomaly').
CREATE TABLE IF NOT EXISTS predictions (
    id              BIGSERIAL PRIMARY KEY,
    pred_type       TEXT NOT NULL,
    dept_id         TEXT,
    predicted_time  TIMESTAMPTZ,
    predicted_value NUMERIC,
    confidence      NUMERIC,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_predictions_type ON predictions (pred_type, generated_at DESC);

-- ── event_log ────────────────────────────────────────────────────────
-- Per-entity audit trail of state changes (bed/patient/ed). Indexed by
-- entity_id for the OLTP drill-down (GET /api/beds/{bed_id}/details).
CREATE TABLE IF NOT EXISTS event_log (
    event_id    TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,             -- bed | patient | ed | or
    entity_id   TEXT NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL DEFAULT now(),
    old_status  TEXT,
    new_status  TEXT,
    detail      JSONB,
    source      TEXT                       -- redox | scheduling | operator | analytics
);
CREATE INDEX IF NOT EXISTS idx_event_entity ON event_log (entity_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_event_time   ON event_log (event_time DESC);

-- ── operator_writes ──────────────────────────────────────────────────
-- Audit of operator actions taken from the dashboard (block/unblock bed).
CREATE TABLE IF NOT EXISTS operator_writes (
    id          BIGSERIAL PRIMARY KEY,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'bed',
    entity_id   TEXT NOT NULL,
    operator    TEXT NOT NULL DEFAULT 'operator',
    latency_ms  INTEGER,
    written_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_operator_writes_time ON operator_writes (written_at DESC);

-- ── snapshot_meta ────────────────────────────────────────────────────
-- Single-row freshness marker. clock = timestamp of the last applied event.
-- The app uses NOW() - clock as the "data staleness" signal. In realtime mode
-- the ingestion pipeline stamps clock on every applied event.
CREATE TABLE IF NOT EXISTS snapshot_meta (
    id        INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    clock     TIMESTAMPTZ,
    synced_at TIMESTAMPTZ
);
INSERT INTO snapshot_meta (id, clock) VALUES (1, now())
    ON CONFLICT (id) DO NOTHING;

-- ── ingest_log (realtime) ────────────────────────────────────────────
-- Lightweight per-payload ingestion ledger: idempotency + observability.
-- redox_message_id makes re-delivered Redox messages a no-op.
CREATE TABLE IF NOT EXISTS ingest_log (
    id               BIGSERIAL PRIMARY KEY,
    redox_message_id TEXT,
    source_format    TEXT,                 -- redox-datamodel | fhir | scheduling
    data_model       TEXT,                 -- PatientAdmin | Scheduling | ...
    event_type       TEXT,                 -- Arrival | Admit | Transfer | Discharge | ...
    status           TEXT NOT NULL,        -- applied | skipped | error
    detail           TEXT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_message
    ON ingest_log (redox_message_id) WHERE redox_message_id IS NOT NULL;

-- ── simulation_control (OPTIONAL — demo/simulator only) ──────────────
-- Only used when ENABLE_SIMULATION=true. Safe to leave in place; the
-- realtime pipeline never writes to it.
CREATE TABLE IF NOT EXISTS simulation_control (
    control_id       INTEGER PRIMARY KEY DEFAULT 1 CHECK (control_id = 1),
    speed_multiplier NUMERIC NOT NULL DEFAULT 1.0,
    surge_multiplier NUMERIC NOT NULL DEFAULT 1.0,
    simulated_hour   INTEGER NOT NULL DEFAULT -1,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO simulation_control (control_id) VALUES (1)
    ON CONFLICT (control_id) DO NOTHING;
