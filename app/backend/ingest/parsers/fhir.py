"""Parser: FHIR R4/R5 -> canonical AdtEvent.

Consumes either a single Encounter resource or a Bundle that contains an
Encounter plus the Patient / Location resources it references. Handles the
R4 vs R5 differences that matter for ADT:

  • Encounter.class is a Coding in R4 but a list of CodeableConcept in R5.
  • Encounter.status codes differ (R4: arrived/in-progress/finished;
    R5: in-progress/discharged/completed/...). Both are mapped below.

Redox's FHIR delivery wraps resources in a Bundle, so Bundle handling is the
common path.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import AdtEvent, EventType, LocationRec, PatientRec
from ..util import age_from_dob, clean, normalize_sex, now_utc, parse_dt

# Encounter.status (R4 + R5) -> canonical EventType
_STATUS_MAP = {
    "planned": EventType.ARRIVAL,
    "arrived": EventType.ARRIVAL,
    "triaged": EventType.ARRIVAL,
    "in-progress": EventType.ADMIT,
    "on-hold": EventType.UPDATE,
    "onleave": EventType.TRANSFER,
    "discharged": EventType.DISCHARGE,   # R5
    "finished": EventType.DISCHARGE,     # R4
    "completed": EventType.DISCHARGE,    # R5
    "cancelled": EventType.CANCEL,
    "discontinued": EventType.CANCEL,    # R5
    "entered-in-error": EventType.CANCEL,
}

# Encounter.class code -> canonical patient_class
_CLASS_MAP = {
    "EMER": "emergency",
    "IMP": "inpatient",
    "ACUTE": "inpatient",
    "NONAC": "inpatient",
    "OBSENC": "observation",
    "AMB": "outpatient",
    "SS": "outpatient",
    "PRENC": "outpatient",
}


def can_parse(payload: dict) -> bool:
    rt = payload.get("resourceType")
    return rt in ("Encounter", "Bundle")


def _find_encounter(payload: dict) -> Optional[dict]:
    if payload.get("resourceType") == "Encounter":
        return payload
    if payload.get("resourceType") == "Bundle":
        for entry in payload.get("entry") or []:
            res = entry.get("resource") or {}
            if res.get("resourceType") == "Encounter":
                return res
    return None


def _index_bundle(payload: dict) -> dict[str, dict]:
    """Map 'ResourceType/id' and fullUrl -> resource, for reference resolution."""
    index: dict[str, dict] = {}
    if payload.get("resourceType") != "Bundle":
        return index
    for entry in payload.get("entry") or []:
        res = entry.get("resource") or {}
        rt, rid = res.get("resourceType"), res.get("id")
        if rt and rid:
            index[f"{rt}/{rid}"] = res
        full = entry.get("fullUrl")
        if full:
            index[full] = res
    return index


def _resolve(ref: Optional[str], index: dict[str, dict]) -> Optional[dict]:
    if not ref:
        return None
    return index.get(ref) or index.get(ref.split("/")[-1])


def _patient_class(encounter: dict) -> Optional[str]:
    cls = encounter.get("class")
    code = None
    if isinstance(cls, dict):                        # R4: Coding
        code = cls.get("code")
    elif isinstance(cls, list) and cls:              # R5: [CodeableConcept]
        for coding in (cls[0].get("coding") or []):
            if coding.get("code"):
                code = coding["code"]
                break
    return _CLASS_MAP.get((code or "").upper()) if code else None


def _patient_rec(patient: Optional[dict], event_time) -> PatientRec:
    if not patient:
        return PatientRec()
    name = None
    names = patient.get("name") or []
    if names:
        n = names[0]
        given = " ".join(n.get("given") or [])
        family = n.get("family") or ""
        name = (f"{given} {family}").strip() or n.get("text")
    return PatientRec(
        mrn=_fhir_mrn(patient),
        patient_id=clean(patient.get("id")),
        name=clean(name),
        age=age_from_dob(patient.get("birthDate"), event_time),
        gender=normalize_sex(patient.get("gender")),
    )


def _fhir_mrn(patient: dict) -> Optional[str]:
    idents = patient.get("identifier") or []
    for ident in idents:
        for coding in ((ident.get("type") or {}).get("coding") or []):
            if (coding.get("code") or "").upper() in ("MR", "MRN"):
                return clean(ident.get("value"))
    return clean(idents[0].get("value")) if idents else None


def _location(encounter: dict, index: dict[str, dict]) -> LocationRec:
    """Use the most specific active location on the encounter."""
    locs = encounter.get("location") or []
    if not locs:
        return LocationRec()
    # Prefer an entry whose status is 'active'/missing; take the last (most recent).
    chosen = None
    for entry in locs:
        if entry.get("status") in (None, "active"):
            chosen = entry
    chosen = chosen or locs[-1]

    loc_ref = chosen.get("location") or {}
    display = clean(loc_ref.get("display"))
    resource = _resolve(loc_ref.get("reference"), index)

    name = display
    bed = None
    department = None
    facility = None
    if resource:
        name = name or clean(resource.get("name"))
        ptype = _physical_type(resource)
        if ptype == "bd":
            bed = clean(resource.get("name")) or name
            department = _ancestor_name(resource, index)
        elif ptype in ("wa", "wd", "lvl"):
            department = clean(resource.get("name")) or name
        elif ptype in ("bu", "si"):
            facility = clean(resource.get("name")) or name
        else:
            department = clean(resource.get("name")) or name
    else:
        department = name

    raw = name or department or facility
    return LocationRec(facility=facility, department=department, room=None, bed=bed, raw=raw)


def _physical_type(resource: dict) -> Optional[str]:
    for coding in ((resource.get("physicalType") or {}).get("coding") or []):
        if coding.get("code"):
            return coding["code"].lower()
    return None


def _ancestor_name(resource: dict, index: dict[str, dict]) -> Optional[str]:
    """Walk Location.partOf once to find the containing unit/ward name."""
    parent = _resolve((resource.get("partOf") or {}).get("reference"), index)
    return clean(parent.get("name")) if parent else None


def _esi(encounter: dict) -> Optional[int]:
    """ESI/triage acuity, if carried on Encounter.priority."""
    for coding in ((encounter.get("priority") or {}).get("coding") or []):
        code = coding.get("code")
        try:
            n = int(str(code).strip())
            if 1 <= n <= 5:
                return n
        except (ValueError, TypeError):
            continue
    return None


def parse(payload: dict) -> AdtEvent:
    index = _index_bundle(payload)
    encounter = _find_encounter(payload) or {}

    subj_ref = (encounter.get("subject") or {}).get("reference")
    patient = _resolve(subj_ref, index)
    # single-resource Encounter with a contained patient
    if patient is None:
        for c in encounter.get("contained") or []:
            if c.get("resourceType") == "Patient":
                patient = c
                break

    period = encounter.get("period") or {}
    status = (encounter.get("status") or "").lower()
    event_type = _STATUS_MAP.get(status, EventType.UNKNOWN)

    # event_time: end for discharge, else start, else now
    if event_type == EventType.DISCHARGE:
        event_time = parse_dt(period.get("end")) or parse_dt(period.get("start")) or now_utc()
    else:
        event_time = parse_dt(period.get("start")) or now_utc()

    visit_id = None
    for ident in encounter.get("identifier") or []:
        visit_id = clean(ident.get("value"))
        if visit_id:
            break

    # NOTE: do NOT use the encounter/visit identifier as the idempotency key.
    # It is stable across the whole stay (admit and discharge share it), so it
    # is an encounter id, not a per-message id. Idempotency for FHIR comes from
    # a per-delivery header when present (see redox_routes); otherwise it is
    # disabled and the applier's state-idempotency handles re-delivery.
    return AdtEvent(
        event_type=event_type,
        event_time=event_time,
        patient=_patient_rec(patient, event_time),
        encounter_id=clean(encounter.get("id")) or visit_id,
        patient_class=_patient_class(encounter),
        esi_level=_esi(encounter),
        location=_location(encounter, index),
        source_format="fhir",
        data_model="Encounter",
        source_event_label=status,
        redox_message_id=None,
    )
