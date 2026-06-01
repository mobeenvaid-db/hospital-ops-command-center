# Capacity Command — Capacity Forecast (Supply-Demand Rollup)
# TDD: Tests written first in tests/test_capacity.py


def forecast_capacity(
    current_available: int,
    predicted_discharges: int,
    predicted_admissions: float,
) -> tuple[int, float]:
    """Forecast available beds. Returns (predicted_available_beds, confidence)."""
    predicted_available = current_available + predicted_discharges - round(predicted_admissions)
    confidence = 0.70
    return (predicted_available, confidence)


def forecast_department_capacity(
    dept_id: str,
    available: int,
    discharge_scores: list[float],
    ed_forecast: float,
    dept_admission_share: float,
    discharge_threshold: float = 0.6,
) -> dict:
    """Forecast department capacity for the next 2 hours.

    Uses a supply-demand model: beds freed by predicted discharges minus
    beds consumed by predicted admissions from the ED.

    Args:
        dept_id: Department identifier (e.g. "ICU", "MEDSURG").
        available: Current count of available beds in this department.
        discharge_scores: List of discharge probability scores (0.0-1.0)
                          for each occupied bed in this department.
        ed_forecast: EWMA-predicted ED arrivals per hour.
        dept_admission_share: This department's fraction of total admissions (0.0-1.0).
        discharge_threshold: Minimum score to count as a predicted discharge (default 0.6).

    Returns:
        Dict with keys: dept_id, current_available, predicted_discharges_2h,
        predicted_admissions_2h, predicted_capacity_2h, confidence.
    """
    qualifying = [s for s in discharge_scores if s > discharge_threshold]
    predicted_discharges_2h = len(qualifying)
    predicted_admissions_2h = ed_forecast * 2 * dept_admission_share
    predicted_capacity_2h = available + predicted_discharges_2h - round(predicted_admissions_2h)

    if qualifying:
        avg_qualifying = sum(qualifying) / len(qualifying)
        confidence = 0.70 * avg_qualifying
    else:
        confidence = 0.70 * 0.5

    return {
        "dept_id": dept_id,
        "current_available": available,
        "predicted_discharges_2h": predicted_discharges_2h,
        "predicted_admissions_2h": predicted_admissions_2h,
        "predicted_capacity_2h": predicted_capacity_2h,
        "confidence": confidence,
    }
