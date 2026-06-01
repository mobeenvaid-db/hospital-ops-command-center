# Capacity Command — ED Arrival Logic (NHPP + EWMA Forecast)
# TDD: Tests written first in tests/test_arrivals.py

from lib.constants import (
    HOURLY_LAMBDA,
    DOW_MULTIPLIER,
    ESI_WEIGHTS_DAY,
    ESI_WEIGHTS_NIGHT,
    ESI_WEIGHTS_SURGE,
    ADMISSION_PROB_BY_ESI,
)


def get_arrival_lambda(hour: int, day_of_week: int, surge: float = 1.0) -> float:
    """Returns the NHPP arrival rate for a given hour (0-23) and day (0=Mon, 6=Sun)."""
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    if not 0 <= day_of_week <= 6:
        raise ValueError(f"day_of_week must be 0-6, got {day_of_week}")
    return HOURLY_LAMBDA[hour] * DOW_MULTIPLIER[day_of_week] * surge


def get_esi_weights(hour: int, surge: float = 1.0) -> list[float]:
    """Returns ESI weight vector based on time of day. Weights sum to ~1.0."""
    if surge > 1.5:
        raw = list(ESI_WEIGHTS_SURGE)
    elif 8 <= hour <= 19:
        raw = list(ESI_WEIGHTS_DAY)
    else:
        raw = list(ESI_WEIGHTS_NIGHT)
    total = sum(raw)
    return [w / total for w in raw] if total > 0 else raw


def get_admission_probability(esi_level: int) -> float:
    """Returns admission probability for ESI level 1-5."""
    if esi_level not in ADMISSION_PROB_BY_ESI:
        raise ValueError(f"esi_level must be 1-5, got {esi_level}")
    return ADMISSION_PROB_BY_ESI[esi_level]


def ewma_forecast(
    actual_last_hour: float, base_rate: float, alpha: float = 0.3
) -> tuple[float, float]:
    """Returns (forecast_next_hour, confidence)."""
    forecast = alpha * actual_last_hour + (1 - alpha) * base_rate
    deviation = abs(actual_last_hour - base_rate) / max(base_rate, 1)
    confidence = max(0.50, min(0.95, 0.80 - 0.3 * deviation))
    return (forecast, confidence)
