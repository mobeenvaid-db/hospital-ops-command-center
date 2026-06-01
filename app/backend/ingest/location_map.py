"""Resolve an EHR location into a canonical (dept_id, bed_id) pair.

Driven by config/location_map.yaml (see LOCATION_MAP_PATH). Falls back to a
sensible built-in map so the service still runs if the file is missing.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .models import AdtEvent, LocationRec

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "class_defaults": {
        "emergency": "ED",
        "inpatient": "MEDSURG",
        "observation": "MEDSURG",
        "outpatient": "MEDSURG",
    },
    "default": "MEDSURG",
    "rules": [
        {"dept_id": "ED", "match": ["ED", "EMERGENCY"], "contains": ["EMERG"]},
        {"dept_id": "ICU", "match": ["ICU", "MICU", "SICU", "CCU"], "contains": ["INTENSIVE", "CRITICAL CARE"]},
        {"dept_id": "TELE", "match": ["TELE", "TELEMETRY", "PCU"], "contains": ["TELE", "PROGRESSIVE"]},
        {"dept_id": "PEDS", "match": ["PEDS", "PEDIATRICS", "PICU"], "contains": ["PED", "CHILD"]},
        {"dept_id": "OR", "match": ["OR", "SURGERY"], "contains": ["SURG", "OPERAT", "PERIOP"]},
        {"dept_id": "MEDSURG", "match": ["MEDSURG", "MED SURG", "MED/SURG"], "contains": ["MED", "SURG", "FLOOR"]},
    ],
}


class LocationMap:
    def __init__(self, config: dict):
        self._class_defaults = {k.lower(): v for k, v in (config.get("class_defaults") or {}).items()}
        self._default = config.get("default", "MEDSURG")
        self._rules = config.get("rules") or []

    # ── construction ──────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Optional[str] = None) -> "LocationMap":
        path = path or os.environ.get("LOCATION_MAP_PATH")
        if path and os.path.exists(path):
            try:
                import yaml  # lazy import; optional dependency
                with open(path) as f:
                    return cls(yaml.safe_load(f) or {})
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to load location map %s (%s); using defaults", path, e)
        return cls(_DEFAULT_CONFIG)

    # ── resolution ────────────────────────────────────────────────────
    def resolve_dept(self, event: AdtEvent) -> str:
        """Return the canonical dept_id for an event."""
        loc = event.location
        candidates = [loc.department, loc.facility, loc.raw]
        for value in candidates:
            dept = self._match_dept(value)
            if dept:
                return dept
        # no usable location -> class default
        pc = (event.patient_class or "").lower()
        if pc in self._class_defaults:
            return self._class_defaults[pc]
        return self._default

    def _match_dept(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        v = value.strip().upper()
        if not v:
            return None
        # 1. exact match
        for rule in self._rules:
            for m in rule.get("match") or []:
                if v == str(m).strip().upper():
                    return rule["dept_id"]
        # 2. substring/keyword
        for rule in self._rules:
            for c in rule.get("contains") or []:
                if str(c).strip().upper() in v:
                    return rule["dept_id"]
        return None

    @staticmethod
    def explicit_bed_id(location: LocationRec) -> Optional[str]:
        """If the EHR sent a specific room/bed, build a stable bed_id from it."""
        if location.bed and location.room:
            return f"{location.bed}".strip() if location.bed.count("-") else f"{location.room}-{location.bed}"
        if location.bed:
            return location.bed.strip()
        if location.room:
            return location.room.strip()
        return None
