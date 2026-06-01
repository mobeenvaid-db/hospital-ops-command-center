# Capacity Command — Discharge Prediction (Heuristic Scoring + Time-of-Day)
# TDD: Tests written first in tests/test_discharge.py

from lib.constants import DISCHARGE_HOUR_MULTIPLIER, DOW_DISCHARGE_MULTIPLIER


def _base_score(hours_remaining: float) -> float:
    """Base discharge probability from hours remaining."""
    if hours_remaining <= 0:
        return 0.95
    if hours_remaining <= 2:
        return 0.80
    if hours_remaining <= 4:
        return 0.60
    if hours_remaining <= 8:
        return 0.35
    return 0.10


def discharge_score(hours_remaining: float, acuity: int, hour: int = 12) -> float:
    """Return discharge probability score 0.0-1.0.

    Args:
        hours_remaining: Hours until expected discharge (<= 0 means overdue).
        acuity: ESI acuity level 1-5. Levels 1-2 reduce score by 15% (less predictable).
        hour: Current hour 0-23. Applies time-of-day discharge curve.
              Defaults to 12 (noon) -- callers should pass the real current hour.
    """
    score = _base_score(hours_remaining)
    if acuity <= 2:
        score *= 0.85
    score *= DISCHARGE_HOUR_MULTIPLIER[hour]
    return max(0.0, min(1.0, score))


def get_discharge_multiplier(hour: int, day_of_week: int) -> float:
    """Return combined discharge probability multiplier."""
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= day_of_week <= 6:
        raise ValueError("day_of_week must be 0-6")
    return DISCHARGE_HOUR_MULTIPLIER[hour] * DOW_DISCHARGE_MULTIPLIER[day_of_week]


def count_predicted_discharges(beds: list[dict], threshold: float = 0.6) -> int:
    """Return count of beds where discharge_score > threshold.

    Args:
        beds: List of bed dicts, each requiring keys:
              - "hours_remaining" (float): hours until expected discharge
              - "acuity" (int): ESI acuity level 1-5
              - "hour" (int): current hour 0-23
        threshold: Minimum score to count as predicted discharge (default 0.6).
    """
    count = 0
    for bed in beds:
        score = discharge_score(
            hours_remaining=bed["hours_remaining"],
            acuity=bed["acuity"],
            hour=bed["hour"],
        )
        if score > threshold:
            count += 1
    return count
