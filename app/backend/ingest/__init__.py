"""Realtime ingestion: Redox (Data Model JSON + FHIR) -> canonical -> Lakebase.

Importing this package is dependency-light (no fastapi / asyncpg) so the parser
and mapping logic can be imported and unit-tested in isolation. The FastAPI
router lives in `ingest.redox_routes` and is imported by main.py directly.
"""

from .applier import apply_event
from .models import AdtEvent, EventType
from .normalize import normalize

__all__ = ["normalize", "apply_event", "AdtEvent", "EventType"]
