# Redox Integration Guide

This describes how to connect a Redox destination to the ingestion webhook, how
requests are authenticated, and exactly how each field maps into the serving
tables.

## 1. Endpoint

The app exposes one ingestion endpoint:

```
POST https://<your-app-url>/api/ingest/redox
GET  https://<your-app-url>/api/ingest/redox     (verification handshake)
GET  https://<your-app-url>/api/ingest/health    (status)
```

Configure this as the destination URL in Redox. The endpoint accepts both the
Redox Data Model JSON (PatientAdmin) and Redox FHIR (R4 or R5). It detects the
shape of each payload and parses accordingly, so you do not need separate
endpoints per flavor.

## 2. Authentication

Two mechanisms are supported and you can use either or both.

**Verification handshake (setup time).** When you add the destination, Redox
sends a GET with `verification-token` and `challenge` query parameters. The app
echoes the challenge only when the token matches `REDOX_VERIFICATION_TOKEN`.

**Per-request auth.** Each POST is authenticated by one of:

- the shared `verification-token` header equal to `REDOX_VERIFICATION_TOKEN`, or
- a `Redox-Signature` header containing the base64 HMAC-SHA256 of the raw request
  body, keyed by `REDOX_SIGNATURE_SECRET`.

Set `REQUIRE_INGEST_AUTH=true` in production. If no credential is configured the
app logs a warning and accepts the request, which is convenient for local
development only.

Store both secrets as Databricks App secrets and reference them with `valueFrom`
in `app.yaml`. Do not inline them.

## 3. What gets stored

Every payload is written verbatim to the Delta bronze table
`redox_events_raw` before any mapping, giving you a durable, replayable audit of
exactly what the EHR sent. Mapping then upserts into the Lakebase serving tables.
A per-message row is recorded in `ingest_log` for idempotency and observability.
Re-delivered messages (same `Meta.Message.ID`) are a no-op.

If the database is unreachable the endpoint returns 503. Mapping errors for a
single message are captured in `ingest_log` and returned in the response, but do
not fail the rest of a batch and do not raise to Redox (which would otherwise
retry the whole delivery).

## 4. Event mapping

### 4.1 Event type

| Redox PatientAdmin `EventType` | FHIR `Encounter.status` | Canonical | Effect |
| --- | --- | --- | --- |
| Arrival, Registration, PreAdmit | planned, arrived, triaged | arrival | create patient, open ED arrival (no bed) |
| Admit, AdmitVisit | in-progress | admit | occupy a bed, close ED arrival |
| Transfer | onleave | transfer | free old bed, occupy new bed |
| Discharge | finished, discharged, completed | discharge | free bed (to cleaning), close arrival |
| Update, VisitUpdate, PatientUpdate | on-hold | update | refresh demographics and ESI |
| Cancel* | cancelled, entered-in-error, discontinued | cancel | logged, see section 6 |

An admit to a bed that differs from the patient's current bed is treated as a
transfer. This makes FHIR behave correctly, since FHIR has no distinct transfer
trigger and signals a move with a new active location.

### 4.2 Patient and visit fields

| Canonical field | Redox Data Model | FHIR |
| --- | --- | --- |
| mrn (patient key) | `Patient.Identifiers[IDType=MR].ID` | `Patient.identifier[type.coding.code=MR].value` |
| name | `Patient.Demographics.First/Last` | `Patient.name[0]` given + family |
| age | derived from `Demographics.DOB` | derived from `Patient.birthDate` |
| gender | `Demographics.Sex` → M/F/O | `Patient.gender` → M/F/O |
| encounter_id | `Visit.VisitNumber` | `Encounter.id` or identifier |
| patient_class | `Visit.PatientClass` | `Encounter.class` code (EMER/IMP/AMB/OBSENC) |
| esi_level | `Visit.AcuityLevel`/`ESI`/`TriageLevel` | `Encounter.priority.coding.code` (1-5) |

### 4.3 Location to department

The dashboard keys on canonical department ids (ED, ICU, TELE, MEDSURG, PEDS, OR).
EHRs send local unit names, so `config/location_map.yaml` translates them.

The mapper looks at, in priority order:

1. Redox `Visit.Location.Department`, then `.Facility`
2. FHIR `Encounter.location[].location.display`, or the resolved `Location.name`
   and its `physicalType` (a `bd` resolves the bed and walks `partOf` to the ward)

Matching is case-insensitive: exact `match` entries first, then `contains`
keywords, then the `class_defaults` for the patient class, then `default`.

If the EHR sends a specific room or bed (Redox `Location.Room`/`Bed`, or a FHIR
`Location` of `physicalType` `bd`), that value is used as the `bed_id`. Otherwise
the mapper allocates the next available bed in the resolved department. With
`AUTO_CREATE_BEDS=true` it will create a bed it has not seen before, which is
handy when you do not have a clean bed master to seed.

## 5. Tables written

| Table | When | Notes |
| --- | --- | --- |
| `patients` | every event | upsert by MRN, LACE computed here |
| `beds` | admit, transfer, discharge | status occupied / cleaning, acuity, admission_time |
| `ed_arrivals` | arrival, admit, discharge | disposition lifecycle, boarder detection |
| `event_log` | every event | per-entity audit for the bed drill-down |
| `ingest_log` | every message | idempotency + status |
| `snapshot_meta` | every event | `clock` stamped so staleness reflects the live feed |
| `redox_events_raw` (Delta) | every message | raw payload, replay source |

## 6. Cancellations

Cancellation reversal is intentionally conservative. A `CancelDischarge` is
treated as a re-admit to the resolved or last bed if it is free. Other cancels
(`CancelAdmit`, `CancelTransfer`, and so on) are recorded in `event_log` and
`ingest_log` but are not auto-reversed, because a correct reversal depends on
local workflow. Extend `applier._on_cancel` if your facility needs full reversal.

## 7. OR and Scheduling

ADT does not carry surgical schedules. To populate the OR panel, connect a Redox
Scheduling destination and add a parser under `ingest/parsers` that emits the
`or_schedule` shape. The ingestion framework already routes by data model, so
this is additive and does not touch the ADT path.
