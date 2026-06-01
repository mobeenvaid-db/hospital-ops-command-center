"""Small shared helpers for the ingestion parsers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a tz-aware datetime (UTC if naive).

    Accepts trailing 'Z', offsets, or date-only strings. Returns None on failure.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # date-only fallback
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def age_from_dob(dob: Any, ref: Optional[datetime] = None) -> Optional[int]:
    """Whole-year age from a date of birth string/datetime."""
    d = parse_dt(dob)
    if d is None:
        return None
    ref = ref or now_utc()
    years = ref.year - d.year - ((ref.month, ref.day) < (d.month, d.day))
    return years if 0 <= years < 150 else None


def normalize_sex(value: Any) -> Optional[str]:
    """Normalize a sex/gender token to 'M' / 'F' / 'O'."""
    if not value:
        return None
    v = str(value).strip().upper()
    if v in ("M", "MALE"):
        return "M"
    if v in ("F", "FEMALE"):
        return "F"
    return "O"


def clean(value: Any) -> Optional[str]:
    """Trim a string; return None for empty/whitespace."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None
