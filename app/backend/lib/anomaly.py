# Capacity Command — Threshold-Based Anomaly Detection
# TDD: Tests written first in tests/test_anomaly.py

from lib.constants import (
    OCCUPANCY_CRITICAL,
    OCCUPANCY_WARNING,
    ED_WAIT_HIGH_MIN,
    BOARDING_CRITICAL_HOURS,
    BOARDING_SURGE_COUNT,
    LWBS_WARNING_PCT,
    LWBS_CRITICAL_PCT,
    OR_OVERRUN_THRESHOLD_MIN,
    BLOCKED_BEDS_HIGH_PCT,
    SYSTEM_STATUS_THRESHOLDS,
)


def detect_anomalies(state: dict) -> list[dict]:
    """Detect anomalies from hospital state metrics. Returns list of anomaly dicts."""
    anomalies: list[dict] = []

    dept_occupancy = state.get("dept_occupancy", {})
    for dept, pct in dept_occupancy.items():
        if pct > OCCUPANCY_CRITICAL:
            anomalies.append({
                "type": "CAPACITY_CRITICAL",
                "dept": dept,
                "value": pct,
                "confidence": 0.95,
                "severity": "CRITICAL",
            })
        elif pct > OCCUPANCY_WARNING:
            anomalies.append({
                "type": "CAPACITY_WARNING",
                "dept": dept,
                "value": pct,
                "confidence": 0.85,
                "severity": "WARNING",
            })

    ed_avg_wait_min = state.get("ed_avg_wait_min", 0)
    if ed_avg_wait_min > ED_WAIT_HIGH_MIN:
        anomalies.append({
            "type": "ED_WAIT_HIGH",
            "dept": "ED",
            "value": ed_avg_wait_min,
            "confidence": 0.90,
            "severity": "WARNING",
        })

    ed_boarders_count = state.get("ed_boarders_count", 0)
    if ed_boarders_count > BOARDING_SURGE_COUNT:
        anomalies.append({
            "type": "ED_BOARDING_SURGE",
            "dept": "ED",
            "value": ed_boarders_count,
            "confidence": 0.90,
            "severity": "WARNING",
        })

    avg_boarding_hours = state.get("avg_boarding_hours", 0)
    if avg_boarding_hours > BOARDING_CRITICAL_HOURS:
        anomalies.append({
            "type": "BOARDING_HOURS_CRITICAL",
            "dept": "ED",
            "value": avg_boarding_hours,
            "confidence": 0.95,
            "severity": "CRITICAL",
        })

    lwbs_rate_pct = state.get("lwbs_rate_pct", 0)
    if lwbs_rate_pct > LWBS_CRITICAL_PCT:
        anomalies.append({
            "type": "LWBS_CRITICAL",
            "dept": "ED",
            "value": lwbs_rate_pct,
            "confidence": 0.90,
            "severity": "CRITICAL",
        })
    elif lwbs_rate_pct > LWBS_WARNING_PCT:
        anomalies.append({
            "type": "LWBS_WARNING",
            "dept": "ED",
            "value": lwbs_rate_pct,
            "confidence": 0.80,
            "severity": "WARNING",
        })

    or_overruns = state.get("or_overruns", [])
    for overrun in or_overruns:
        overrun_min = overrun.get("overrun_min", 0)
        if overrun_min > OR_OVERRUN_THRESHOLD_MIN:
            room_id = overrun.get("room_id", "OR")
            anomalies.append({
                "type": "OR_OVERRUN",
                "dept": room_id,
                "value": overrun_min,
                "confidence": 0.85,
                "severity": "WARNING",
            })

    blocked_beds_pct = state.get("blocked_beds_pct", 0)
    if blocked_beds_pct > BLOCKED_BEDS_HIGH_PCT:
        anomalies.append({
            "type": "BLOCKED_BEDS_HIGH",
            "dept": "HOSPITAL",
            "value": blocked_beds_pct,
            "confidence": 0.80,
            "severity": "WARNING",
        })

    return anomalies


def compute_system_status(state: dict) -> str:
    """Compute system status: NORMAL, CONSTRAINED, or CRITICAL."""
    th = SYSTEM_STATUS_THRESHOLDS
    constrained_occ = th["constrained_max_occupancy"]
    constrained_ed = th["constrained_max_ed_wait"]
    normal_occ = th["normal_max_occupancy"]
    normal_ed = th["normal_max_ed_wait"]
    constrained_boarders = th["constrained_max_boarders"]

    dept_occupancy = state.get("dept_occupancy", {})
    max_occupancy = max(dept_occupancy.values(), default=0)
    ed_avg_wait_min = state.get("ed_avg_wait_min", 0)
    ed_boarders_count = state.get("ed_boarders_count", 0)

    if max_occupancy > constrained_occ or ed_avg_wait_min > constrained_ed:
        return "CRITICAL"
    if max_occupancy > normal_occ or ed_avg_wait_min > normal_ed or ed_boarders_count > constrained_boarders:
        return "CONSTRAINED"
    return "NORMAL"
