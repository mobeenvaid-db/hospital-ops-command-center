"""Format detection + dispatch to the right parser.

Looks at the payload shape (not a header) so it works regardless of how the
sender labels things:

  • a Redox Data Model body has Meta.DataModel + a Patient object
  • a FHIR body has resourceType == "Encounter" or "Bundle"
"""

from __future__ import annotations

from typing import Optional

from .models import AdtEvent
from .parsers import fhir, redox_datamodel


class UnsupportedPayload(ValueError):
    """Raised when no parser recognizes the payload."""


def detect_format(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if redox_datamodel.can_parse(payload):
        return "redox-datamodel"
    if fhir.can_parse(payload):
        return "fhir"
    return None


def normalize(payload: dict) -> AdtEvent:
    """Parse any supported payload into a canonical AdtEvent."""
    fmt = detect_format(payload)
    if fmt == "redox-datamodel":
        return redox_datamodel.parse(payload)
    if fmt == "fhir":
        return fhir.parse(payload)
    raise UnsupportedPayload(
        "Payload is neither a Redox Data Model (Meta.DataModel + Patient) "
        "nor a FHIR Encounter/Bundle."
    )
