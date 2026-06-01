"""UC Function: Discharge Prioritizer

Registered as: hospital_ops.agents.discharge_prioritizer

Ranks patients by discharge readiness using a composite score:
  - Discharge probability (time-based from lib.discharge)
  - LACE readmission risk (lower = safer to discharge)
  - Length of stay (longer stay = higher priority to free bed)
  - Predicted discharge time proximity

Returns a prioritized list for charge nurses and bed managers.
"""

import json
from datetime import datetime


# Inline discharge scoring (mirrors lib.discharge logic to be self-contained in UC)
_DISCHARGE_HOUR_MULTIPLIER = [
    0.02, 0.02, 0.01, 0.01, 0.01, 0.02,  # 00-05
    0.05, 0.10, 0.20, 0.40, 0.60, 0.80,  # 06-11
    0.90, 0.95, 1.00, 1.00, 0.90, 0.80,  # 12-17
    0.60, 0.40, 0.20, 0.10, 0.05, 0.03,  # 18-23
]


def _base_score(hours_remaining: float) -> float:
    if hours_remaining <= 0:
        return 0.95
    if hours_remaining <= 2:
        return 0.80
    if hours_remaining <= 4:
        return 0.60
    if hours_remaining <= 8:
        return 0.35
    return 0.10


def _discharge_score(hours_remaining: float, acuity: int, hour: int = 12) -> float:
    score = _base_score(hours_remaining)
    if acuity <= 2:
        score *= 0.85
    score *= _DISCHARGE_HOUR_MULTIPLIER[min(max(hour, 0), 23)]
    return max(0.0, min(1.0, score))


def discharge_prioritizer(
    patients_json: str,
    current_hour: int = 12,
    top_n: int = 10,
) -> str:
    """Rank patients by discharge readiness.

    Parameters
    ----------
    patients_json : str
        JSON array of patient objects, each with:
        - patient_id: str
        - bed_id: str
        - dept_id: str
        - name: str
        - lace_score: int (0-19)
        - lace_risk: str ("LOW", "MODERATE", "HIGH")
        - length_of_stay_days: float
        - acuity_score: int (1-5)
        - hours_until_discharge: float (hours until expected discharge, negative = overdue)
        - predicted_discharge_time: str (ISO timestamp, optional)
        - confidence_score: float (0.0-1.0, optional)
    current_hour : int
        Current hour 0-23 for time-of-day discharge curve.
    top_n : int
        Number of top candidates to return (default 10).

    Returns
    -------
    str
        JSON object with:
        - candidates: ranked list of discharge-ready patients
        - summary: counts by risk tier
        - total_evaluated: number of patients scored
    """
    try:
        patients = json.loads(patients_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "Invalid patients_json", "candidates": []})

    scored = []
    for p in patients:
        hours_remaining = p.get("hours_until_discharge", 24.0)
        acuity = p.get("acuity_score", 3)
        lace_score = p.get("lace_score", 5)
        los_days = p.get("length_of_stay_days", 1.0)
        confidence = p.get("confidence_score", 0.5)

        # Discharge probability score (0-1)
        d_score = _discharge_score(hours_remaining, acuity, current_hour)

        # LACE penalty: higher LACE = less safe to discharge
        # Normalize LACE (0-19) to penalty (0.0-0.3)
        lace_penalty = min(lace_score, 19) / 19.0 * 0.3

        # LOS bonus: longer stay = higher priority to free bed (up to 0.15)
        los_bonus = min(los_days / 10.0, 0.15)

        # Confidence boost from prediction model
        conf_boost = (confidence or 0.5) * 0.1

        # Composite priority score
        priority_score = round(d_score - lace_penalty + los_bonus + conf_boost, 3)

        # Classify readiness
        if d_score >= 0.7 and lace_score <= 6:
            readiness = "READY"
        elif d_score >= 0.5:
            readiness = "LIKELY"
        elif d_score >= 0.3:
            readiness = "POSSIBLE"
        else:
            readiness = "NOT_READY"

        scored.append({
            "patient_id": p.get("patient_id", ""),
            "bed_id": p.get("bed_id", ""),
            "dept_id": p.get("dept_id", ""),
            "name": p.get("name", ""),
            "priority_score": priority_score,
            "discharge_probability": round(d_score, 3),
            "lace_score": lace_score,
            "lace_risk": p.get("lace_risk", "LOW"),
            "length_of_stay_days": round(los_days, 1),
            "hours_until_discharge": round(hours_remaining, 1),
            "readiness": readiness,
            "predicted_discharge_time": p.get("predicted_discharge_time"),
        })

    # Sort by priority score descending
    scored.sort(key=lambda x: x["priority_score"], reverse=True)

    # Summary counts
    ready_count = sum(1 for s in scored if s["readiness"] == "READY")
    likely_count = sum(1 for s in scored if s["readiness"] == "LIKELY")
    possible_count = sum(1 for s in scored if s["readiness"] == "POSSIBLE")

    result = {
        "candidates": scored[:top_n],
        "summary": {
            "ready": ready_count,
            "likely": likely_count,
            "possible": possible_count,
            "not_ready": len(scored) - ready_count - likely_count - possible_count,
        },
        "total_evaluated": len(scored),
    }

    return json.dumps(result)
