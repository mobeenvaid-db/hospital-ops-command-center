"""Unit tests for the ingestion parsers, format detection, and location map.

Pure (no DB). Run from the repo root:  pytest app/backend/tests/test_ingest_parsers.py
"""

import json
import os
import sys

import pytest

# Make the flat backend modules (ingest, config, db, lib) importable.
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from ingest.location_map import LocationMap  # noqa: E402
from ingest.models import EventType  # noqa: E402
from ingest.normalize import detect_format, normalize  # noqa: E402
from ingest.parsers import fhir, redox_datamodel  # noqa: E402

SAMPLES = os.path.abspath(os.path.join(BACKEND, "..", "..", "samples"))


def load(name):
    with open(os.path.join(SAMPLES, name)) as f:
        return json.load(f)


# ── format detection ──────────────────────────────────────────────────

def test_detect_redox_datamodel():
    assert detect_format(load("redox_01_arrival.json")) == "redox-datamodel"


def test_detect_fhir_bundle():
    assert detect_format(load("fhir_admit_bundle.json")) == "fhir"


def test_detect_unknown():
    assert detect_format({"foo": "bar"}) is None


# ── Redox Data Model parser ───────────────────────────────────────────

def test_redox_arrival():
    e = normalize(load("redox_01_arrival.json"))
    assert e.event_type == EventType.ARRIVAL
    assert e.patient.mrn == "0000001234"
    assert e.patient.name == "Maria Alvarez"
    assert e.patient.gender == "F"
    assert e.patient_class == "Emergency"
    assert e.esi_level == 2
    assert e.location.department == "Emergency"
    assert e.redox_message_id == "1001"
    assert e.source_format == "redox-datamodel"
    # age computed from DOB 1958 relative to 2026 event time
    assert e.patient.age == 68


def test_redox_event_type_mapping():
    assert redox_datamodel.parse(load("redox_02_admit.json")).event_type == EventType.ADMIT
    assert redox_datamodel.parse(load("redox_03_transfer.json")).event_type == EventType.TRANSFER
    assert redox_datamodel.parse(load("redox_04_discharge.json")).event_type == EventType.DISCHARGE


def test_redox_cancel_maps_to_cancel():
    payload = load("redox_02_admit.json")
    payload["Meta"]["EventType"] = "CancelAdmit"
    assert redox_datamodel.parse(payload).event_type == EventType.CANCEL


def test_redox_mrn_prefers_mr_identifier():
    payload = load("redox_01_arrival.json")
    # EHRID should be ignored in favor of the MR identifier
    assert redox_datamodel.parse(payload).patient.mrn == "0000001234"


# ── FHIR parser (R4 + R5) ─────────────────────────────────────────────

def test_fhir_admit_bundle():
    e = normalize(load("fhir_admit_bundle.json"))
    assert e.event_type == EventType.ADMIT          # status "in-progress"
    assert e.patient.mrn == "0000007788"
    assert e.patient.name == "James Okafor"
    assert e.patient.gender == "M"
    assert e.patient_class == "inpatient"           # class code IMP
    assert e.esi_level == 3                          # from Encounter.priority
    # Location resolves the bed and walks partOf to the ward
    assert e.location.bed == "ICU Bed 4"
    assert e.location.department == "ICU"


def test_fhir_discharge_status():
    e = normalize(load("fhir_discharge_bundle.json"))
    assert e.event_type == EventType.DISCHARGE       # status "discharged" (R5)
    # discharge event_time uses period.end
    assert e.event_time.isoformat().startswith("2026-06-04")


def test_fhir_r4_class_coding():
    """R4 represents Encounter.class as a single Coding (not a list)."""
    payload = load("fhir_admit_bundle.json")
    enc = payload["entry"][0]["resource"]
    enc["class"] = {"system": "x", "code": "EMER"}   # R4 shape
    enc["status"] = "arrived"
    e = fhir.parse(payload)
    assert e.patient_class == "emergency"
    assert e.event_type == EventType.ARRIVAL


# ── location map ──────────────────────────────────────────────────────

@pytest.fixture
def locmap():
    return LocationMap.load(os.path.join(BACKEND, "..", "..", "config", "location_map.example.yaml"))


def test_location_map_exact_and_keyword(locmap):
    assert locmap._match_dept("ICU") == "ICU"
    assert locmap._match_dept("Telemetry") == "TELE"
    assert locmap._match_dept("Emergency") == "ED"
    # keyword fallback
    assert locmap._match_dept("MICU") == "ICU"


def test_location_map_resolve_uses_class_default(locmap):
    e = normalize(load("redox_01_arrival.json"))
    e.location.department = None
    e.location.facility = None
    e.location.raw = None
    assert locmap.resolve_dept(e) == "ED"   # PatientClass Emergency -> ED


def test_location_map_resolve_department(locmap):
    e = normalize(load("redox_02_admit.json"))
    assert locmap.resolve_dept(e) == "ICU"
