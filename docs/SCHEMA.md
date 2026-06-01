# Serving Schema Reference

Defined in `sql/ddl/lakebase_serving.sql`. All tables live in the schema named by
`LAKEBASE_SCHEMA` (default `hospital_ops_lakebase`).

## departments
The physical care areas. Seed from `config/departments.yaml`.

| column | type | notes |
| --- | --- | --- |
| dept_id | text PK | canonical key (ED, ICU, TELE, MEDSURG, PEDS, OR) |
| name | text | display name |
| floor | int | |
| total_beds | int | capacity, used for occupancy math |

## beds
Bed master plus live status.

| column | type | notes |
| --- | --- | --- |
| bed_id | text PK | |
| dept_id | text FK | |
| status | text | available, occupied, cleaning, blocked |
| acuity_level | int | 1 (highest) to 5, of the current patient |
| admission_time | timestamptz | when current patient was placed |
| expected_discharge | timestamptz | |
| blocked_reason | text | |
| previous_status | text | for the derived event feed |
| last_updated | timestamptz | |
| synced_at | timestamptz | pipeline lag metric |

## ed_arrivals
One row per ED encounter. A boarder is an admitted patient (bed_assigned and
triage_complete set) still waiting in the ED.

| column | type | notes |
| --- | --- | --- |
| arrival_id | text PK | encounter id |
| esi_level | int | Emergency Severity Index 1 to 5 |
| arrival_time | timestamptz | |
| disposition | text | waiting, admitted, discharged, transferred, lwbs |
| wait_minutes | numeric | |
| bed_assigned | text | bed id once assigned |
| triage_complete | timestamptz | |
| previous_disposition | text | |
| updated_at, synced_at | timestamptz | |

## patients
Inpatient roster, readmission risk, and discharge prediction.

| column | type | notes |
| --- | --- | --- |
| patient_id | text PK | MRN-based key |
| mrn, name, age, gender | | demographics |
| arrival_id | text | links to ed_arrivals |
| bed_id | text | current bed, null after discharge |
| dept_id | text | |
| length_of_stay_days | numeric | computed at discharge |
| acuity_score | int | |
| comorbidity_count, ed_visits_6mo | int | LACE inputs |
| lace_score | int | computed on ingest |
| lace_risk | text | LOW, MODERATE, HIGH |
| predicted_discharge_time | timestamptz | from the analytics job |
| discharge_reason | text | |
| confidence_score | numeric | |
| admission_time, updated_at | timestamptz | |

## or_schedule
Operating-room cases. Not populated by ADT. Fed by an optional Scheduling feed.

| column | type | notes |
| --- | --- | --- |
| or_id | text PK | |
| room_id | text | OR-1 to OR-8 |
| procedure_type, surgeon_specialty | text | |
| status | text | scheduled, in-progress, turnover, complete, cancelled |
| scheduled_start, actual_start | timestamptz | |
| est_duration_min | int | |
| previous_status, updated_at, synced_at | | |

## predictions
Forecast rows from `jobs/compute_predictions.py`.

| column | type | notes |
| --- | --- | --- |
| pred_type | text | discharge, ed_arrival, capacity, anomaly |
| dept_id | text | |
| predicted_time | timestamptz | |
| predicted_value | numeric | |
| confidence | numeric | |
| generated_at | timestamptz | |

## event_log
Per-entity audit trail, indexed by entity_id for the bed drill-down.

| column | type | notes |
| --- | --- | --- |
| event_id | text PK | |
| entity_type | text | bed, patient, ed, or |
| entity_id | text | |
| event_time | timestamptz | |
| old_status, new_status | text | |
| detail | jsonb | |
| source | text | redox, scheduling, operator, analytics |

## ingest_log
Per-message ingestion ledger (realtime). The unique index on redox_message_id
provides idempotency.

| column | type | notes |
| --- | --- | --- |
| redox_message_id | text | unique when not null |
| source_format | text | redox-datamodel, fhir, scheduling |
| data_model, event_type | text | |
| status | text | applied, skipped, error |
| detail | text | |
| received_at | timestamptz | |

## snapshot_meta
Single-row freshness marker. `clock` is the timestamp of the last applied event.

## operator_writes
Audit of operator actions taken from the dashboard (block and unblock bed).

## simulation_control
Demo only. Used when `ENABLE_SIMULATION=true`. The realtime pipeline never
writes to it.
