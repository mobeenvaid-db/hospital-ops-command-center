"""Parser: Redox Data Model JSON (PatientAdmin) -> canonical AdtEvent.

Redox normalizes the source EHR's HL7v2/FHIR and delivers JSON shaped like:

    {
      "Meta": {"DataModel": "PatientAdmin", "EventType": "Arrival",
               "EventDateTime": "...", "Source": {"Name": "..."},
               "Message": {"ID": 123}},
      "Patient": {"Identifiers": [{"ID": "...", "IDType": "MR"}],
                  "Demographics": {"FirstName": "...", "LastName": "...",
                                   "DOB": "...", "Sex": "Male"}},
      "Visit":   {"VisitNumber": "...", "PatientClass": "Inpatient",
                  "Location": {"Facility": "...", "Department": "3N",
                               "Room": "136", "Bed": "B"}}
    }

Reference: https://docs.redoxengine.com/ (PatientAdmin data model).
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import AdtEvent, EventType, LocationRec, PatientRec
from ..util import age_from_dob, clean, normalize_sex, now_utc, parse_dt

# Redox PatientAdmin EventType -> canonical EventType
_EVENT_MAP = {
    "Arrival": EventType.ARRIVAL,
    "Registration": EventType.ARRIVAL,
    "PreAdmit": EventType.ARRIVAL,
    "PreAdmitVisit": EventType.ARRIVAL,
    "Admit": EventType.ADMIT,
    "AdmitVisit": EventType.ADMIT,
    "Transfer": EventType.TRANSFER,
    "Discharge": EventType.DISCHARGE,
    "Update": EventType.UPDATE,
    "VisitUpdate": EventType.UPDATE,
    "PatientUpdate": EventType.UPDATE,
}


def can_parse(payload: dict) -> bool:
    meta = payload.get("Meta") or {}
    return bool(meta.get("DataModel")) and "Patient" in payload


def data_model(payload: dict) -> Optional[str]:
    return (payload.get("Meta") or {}).get("DataModel")


def _map_event_type(raw: Optional[str]) -> EventType:
    if not raw:
        return EventType.UNKNOWN
    if raw in _EVENT_MAP:
        return _EVENT_MAP[raw]
    if raw.lower().startswith("cancel"):
        return EventType.CANCEL
    return EventType.UNKNOWN


def _mrn(patient: dict) -> Optional[str]:
    """Pull the medical record number from Patient.Identifiers."""
    for ident in patient.get("Identifiers") or []:
        idtype = (ident.get("IDType") or "").upper()
        if idtype in ("MR", "MRN", "MEDICALRECORD", "MEDICAL RECORD NUMBER"):
            return clean(ident.get("ID"))
    # fall back to the first identifier
    idents = patient.get("Identifiers") or []
    return clean(idents[0].get("ID")) if idents else None


def _patient(patient: dict, event_time) -> PatientRec:
    demo = patient.get("Demographics") or {}
    first = clean(demo.get("FirstName")) or ""
    last = clean(demo.get("LastName")) or ""
    name = (f"{first} {last}").strip() or None
    return PatientRec(
        mrn=_mrn(patient),
        patient_id=_source_patient_id(patient),
        name=name,
        age=age_from_dob(demo.get("DOB"), event_time),
        gender=normalize_sex(demo.get("Sex")),
    )


def _source_patient_id(patient: dict) -> Optional[str]:
    """A non-MR identifier (EHR/enterprise id) if present, else None."""
    for ident in patient.get("Identifiers") or []:
        idtype = (ident.get("IDType") or "").upper()
        if idtype in ("EHRID", "EPI", "EPIC", "FHIR", "PI", "PT"):
            return clean(ident.get("ID"))
    return None


def _location(loc: dict) -> LocationRec:
    if not loc:
        return LocationRec()
    facility = clean(loc.get("Facility"))
    department = clean(loc.get("Department"))
    room = clean(loc.get("Room"))
    bed = clean(loc.get("Bed"))
    raw = department or facility or " ".join(p for p in (facility, room, bed) if p) or None
    return LocationRec(facility=facility, department=department, room=room, bed=bed, raw=raw)


def parse(payload: dict) -> AdtEvent:
    meta = payload.get("Meta") or {}
    visit = payload.get("Visit") or {}
    patient = payload.get("Patient") or {}

    event_time = parse_dt(meta.get("EventDateTime")) or parse_dt(visit.get("VisitDateTime")) or now_utc()
    raw_event = meta.get("EventType")

    msg = meta.get("Message") or {}
    source = meta.get("Source") or {}

    return AdtEvent(
        event_type=_map_event_type(raw_event),
        event_time=event_time,
        patient=_patient(patient, event_time),
        encounter_id=clean(visit.get("VisitNumber")),
        patient_class=clean(visit.get("PatientClass")),
        esi_level=_esi(visit),
        location=_location(visit.get("Location") or {}),
        prior_location=_location(visit.get("PreviousLocation") or {}),
        source_format="redox-datamodel",
        data_model=meta.get("DataModel"),
        source_event_label=raw_event,
        redox_message_id=clean(msg.get("ID")),
        source_name=clean(source.get("Name")),
    )


def _esi(visit: dict) -> Optional[int]:
    """ESI is rarely in PatientAdmin; check a couple of known spots."""
    for key in ("AcuityLevel", "ESI", "TriageLevel"):
        val = visit.get(key)
        if val is not None:
            try:
                return int(str(val).strip())
            except (ValueError, TypeError):
                pass
    return None
