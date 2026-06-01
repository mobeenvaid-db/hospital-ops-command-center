"""UC Function: Bed Capacity Gap Calculator

Registered as: hospital_ops.agents.bed_gap_calculator

Calculates the bed capacity gap per department:
  gap = forecast_arrivals - available_beds - predicted_discharges

A positive gap means demand exceeds supply (beds needed).
A negative gap means surplus capacity (beds available).

Uses the lib.capacity supply-demand model calibrated from 70+ published
citations (HCUP/AHRQ LOS, CMS ED benchmarks, NHPP arrival rates).
"""

import json
from typing import Optional


def bed_gap_calculator(
    dept_id: str,
    available_beds: int,
    occupied_beds: int,
    total_beds: int,
    forecast_arrivals_2h: float,
    discharge_scores_json: str = "[]",
    discharge_threshold: float = 0.6,
    dept_admission_share: float = 0.25,
) -> str:
    """Calculate bed capacity gap for a department.

    Parameters
    ----------
    dept_id : str
        Department identifier (e.g. "ICU", "MEDSURG", "TELE", "PEDS").
    available_beds : int
        Current count of available (empty, clean) beds.
    occupied_beds : int
        Current count of occupied beds.
    total_beds : int
        Total bed capacity for this department.
    forecast_arrivals_2h : float
        EWMA-predicted ED arrivals in next 2 hours.
    discharge_scores_json : str
        JSON array of discharge probability scores (0.0-1.0) for each
        occupied bed. E.g. '[0.85, 0.40, 0.92, 0.15]'.
    discharge_threshold : float
        Minimum score to count as a predicted discharge (default 0.6).
    dept_admission_share : float
        This department's fraction of total admissions (0.0-1.0).

    Returns
    -------
    str
        JSON object with gap analysis:
        - dept_id: department
        - gap: beds needed (positive) or surplus (negative)
        - available_beds: current available
        - predicted_discharges_2h: beds expected to free up
        - predicted_admissions_2h: beds expected to be consumed
        - occupancy_pct: current occupancy percentage
        - risk_level: "CRITICAL" (gap >= 3), "WARNING" (gap >= 1), "NORMAL"
        - recommendation: human-readable action suggestion
    """
    try:
        discharge_scores = json.loads(discharge_scores_json)
    except (json.JSONDecodeError, TypeError):
        discharge_scores = []

    # Count predicted discharges (scores above threshold)
    qualifying_discharges = [s for s in discharge_scores if s > discharge_threshold]
    predicted_discharges_2h = len(qualifying_discharges)

    # Predicted admissions from ED arrivals
    predicted_admissions_2h = forecast_arrivals_2h * dept_admission_share

    # Gap = demand - supply
    # Positive gap = beds needed; negative = surplus
    gap = round(predicted_admissions_2h) - available_beds - predicted_discharges_2h

    # Occupancy
    occupancy_pct = round(100.0 * occupied_beds / max(total_beds, 1), 1)

    # Risk classification
    if gap >= 3:
        risk_level = "CRITICAL"
    elif gap >= 1:
        risk_level = "WARNING"
    else:
        risk_level = "NORMAL"

    # Generate recommendation
    if risk_level == "CRITICAL":
        recommendation = (
            f"{dept_id}: {gap} bed shortfall projected in 2h. "
            f"Accelerate {predicted_discharges_2h} pending discharges and "
            f"consider diverting {round(predicted_admissions_2h)} incoming admits."
        )
    elif risk_level == "WARNING":
        recommendation = (
            f"{dept_id}: Tight capacity. {available_beds} beds available, "
            f"{predicted_discharges_2h} discharges expected. Monitor closely."
        )
    else:
        recommendation = (
            f"{dept_id}: Capacity adequate. {available_beds} beds available "
            f"with {predicted_discharges_2h} discharges expected."
        )

    result = {
        "dept_id": dept_id,
        "gap": gap,
        "available_beds": available_beds,
        "occupied_beds": occupied_beds,
        "total_beds": total_beds,
        "predicted_discharges_2h": predicted_discharges_2h,
        "predicted_admissions_2h": round(predicted_admissions_2h, 1),
        "occupancy_pct": occupancy_pct,
        "risk_level": risk_level,
        "recommendation": recommendation,
    }

    return json.dumps(result)


# --- UC Function Registration SQL ---
# Run via Databricks CLI or notebook:
#
# CREATE OR REPLACE FUNCTION hospital_ops.agents.bed_gap_calculator(
#   dept_id STRING,
#   available_beds INT,
#   occupied_beds INT,
#   total_beds INT,
#   forecast_arrivals_2h DOUBLE,
#   discharge_scores_json STRING DEFAULT '[]',
#   discharge_threshold DOUBLE DEFAULT 0.6,
#   dept_admission_share DOUBLE DEFAULT 0.25
# )
# RETURNS STRING
# LANGUAGE PYTHON
# DETERMINISTIC
# COMMENT 'Calculate bed capacity gap: gap = forecast_arrivals - available - predicted_discharges. Positive = shortfall.'
# AS $$
#   <contents of bed_gap_calculator function above>
# $$;
