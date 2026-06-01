"""Canonical ingestion model.

Both wire formats — Redox Data Model JSON and FHIR R4/R5 — are normalized into
a single `AdtEvent`. The applier only ever sees `AdtEvent`, so adding a new
source format means writing one parser, not touching the mapping logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """Canonical ADT event semantics, independent of HL7 trigger codes."""

    ARRIVAL = "arrival"      # patient presents (ED registration / pre-admit)
    ADMIT = "admit"          # assigned an inpatient bed (A01)
    TRANSFER = "transfer"    # moved to a different bed/unit (A02)
    DISCHARGE = "discharge"  # left the facility (A03)
    UPDATE = "update"        # demographics / location update, no state change (A08)
    CANCEL = "cancel"        # cancellation of a prior event (A11/A12/A13)
    UNKNOWN = "unknown"


@dataclass
class PatientRec:
    """Patient demographics + readmission-risk inputs."""

    mrn: Optional[str] = None
    patient_id: Optional[str] = None       # source patient id if distinct from MRN
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    # LACE inputs (when available from the source)
    ed_visits_6mo: int = 0
    comorbidity_count: Optional[int] = None
    is_acute: bool = True


@dataclass
class LocationRec:
    """Where the patient is (or is going). Any field may be missing."""

    facility: Optional[str] = None
    department: Optional[str] = None   # raw unit/department name as sent by the EHR
    room: Optional[str] = None
    bed: Optional[str] = None
    raw: Optional[str] = None          # best single human-readable string for logging/mapping


@dataclass
class AdtEvent:
    """A single normalized admit/discharge/transfer event."""

    event_type: EventType
    event_time: datetime
    patient: PatientRec
    encounter_id: Optional[str] = None
    patient_class: Optional[str] = None      # emergency | inpatient | observation | outpatient
    esi_level: Optional[int] = None          # Emergency Severity Index, if triaged
    location: LocationRec = field(default_factory=LocationRec)
    prior_location: LocationRec = field(default_factory=LocationRec)  # for transfers
    # provenance
    source_format: str = "unknown"           # redox-datamodel | fhir
    data_model: Optional[str] = None         # PatientAdmin | Scheduling | ...
    source_event_label: Optional[str] = None # raw EventType / trigger code, for logging
    redox_message_id: Optional[str] = None
    source_name: Optional[str] = None
