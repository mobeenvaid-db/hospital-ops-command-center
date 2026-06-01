"""UC Function: OR Slot Optimizer

Registered as: hospital_ops.agents.or_slot_optimizer

Suggests optimal OR schedule changes to:
  - Minimize gaps between cases (idle time)
  - Maximize room utilization
  - Prioritize by procedure revenue tier and urgency

Uses heuristic scheduling: sorts by priority, fills gaps greedily,
flags overruns that cascade into subsequent slots.
"""

import json
from datetime import datetime


# Revenue tiers by specialty (relative, not actual $)
_REVENUE_TIER = {
    "Cardiac": 3,
    "Neuro": 3,
    "Thoracic Surgery": 3,
    "Orthopedic": 2,
    "General Surgery": 2,
    "Plastics": 2,
    "Urology": 1,
    "GI / Endoscopy": 1,
}


def or_slot_optimizer(
    schedule_json: str,
    rooms_total: int = 8,
    target_utilization_pct: float = 85.0,
    turnover_min: int = 30,
) -> str:
    """Suggest OR schedule optimizations.

    Parameters
    ----------
    schedule_json : str
        JSON array of OR cases, each with:
        - or_id: str
        - room_id: str
        - procedure_type: str
        - surgeon_specialty: str
        - scheduled_start: str (ISO timestamp)
        - actual_start: str (ISO timestamp, null if not started)
        - est_duration_min: int
        - status: str ("scheduled", "in-progress", "turnover", "complete")
    rooms_total : int
        Total OR rooms available (default 8).
    target_utilization_pct : float
        Target utilization percentage (default 85%).
    turnover_min : int
        Expected turnover time between cases in minutes (default 30).

    Returns
    -------
    str
        JSON object with:
        - current_utilization_pct: current utilization
        - target_utilization_pct: target
        - gap_pct: utilization gap to close
        - room_analysis: per-room breakdown
        - suggestions: list of optimization recommendations
        - overrun_alerts: cases running over estimated duration
    """
    try:
        schedule = json.loads(schedule_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "Invalid schedule_json", "suggestions": []})

    # Group by room
    rooms: dict[str, list[dict]] = {}
    for case in schedule:
        rid = case.get("room_id", "UNKNOWN")
        rooms.setdefault(rid, []).append(case)

    # Analyze each room
    room_analysis = []
    overrun_alerts = []
    suggestions = []
    active_rooms = 0
    total_utilized_min = 0
    total_available_min = 0

    for room_id in sorted(rooms.keys()):
        cases = rooms[room_id]
        in_progress = [c for c in cases if c.get("status") == "in-progress"]
        scheduled = [c for c in cases if c.get("status") == "scheduled"]
        completed = [c for c in cases if c.get("status") == "complete"]
        turnover = [c for c in cases if c.get("status") == "turnover"]

        is_active = len(in_progress) > 0
        if is_active:
            active_rooms += 1

        # Calculate utilized time
        room_utilized = sum(c.get("est_duration_min", 0) for c in completed + in_progress)
        room_scheduled = sum(c.get("est_duration_min", 0) for c in scheduled)
        room_total = room_utilized + room_scheduled + len(cases) * turnover_min
        total_utilized_min += room_utilized
        total_available_min += max(room_total, 480)  # assume 8h block minimum

        # Detect overruns
        for c in in_progress:
            actual_start = c.get("actual_start")
            est_duration = c.get("est_duration_min", 0)
            if actual_start and est_duration:
                try:
                    start_dt = datetime.fromisoformat(
                        actual_start.replace("Z", "+00:00")
                    )
                    now = datetime.now(start_dt.tzinfo) if start_dt.tzinfo else datetime.now()
                    elapsed = (now - start_dt).total_seconds() / 60
                    overrun = elapsed - est_duration
                    if overrun > 15:
                        overrun_alerts.append({
                            "room_id": room_id,
                            "or_id": c.get("or_id"),
                            "procedure": c.get("procedure_type", ""),
                            "overrun_min": round(overrun),
                            "impact": f"Delays next case by ~{round(overrun)} min",
                        })
                except (ValueError, TypeError):
                    pass

        # Detect gaps (scheduled cases with no predecessor)
        gap_min = 0
        if not is_active and not turnover and scheduled:
            gap_min = 30  # estimated idle gap
            revenue_tier = max(
                (_REVENUE_TIER.get(c.get("procedure_type", ""), 1) for c in scheduled),
                default=1,
            )
            suggestions.append({
                "type": "FILL_GAP",
                "room_id": room_id,
                "gap_min": gap_min,
                "priority": revenue_tier,
                "suggestion": (
                    f"Room {room_id} idle with {len(scheduled)} pending case(s). "
                    f"Consider moving up next case to reduce gap."
                ),
            })

        # Detect underutilized rooms
        if not cases or (not is_active and not scheduled):
            suggestions.append({
                "type": "UNDERUTILIZED",
                "room_id": room_id,
                "gap_min": 480,
                "priority": 1,
                "suggestion": (
                    f"Room {room_id} has no scheduled cases. "
                    f"Available for add-on cases or block release."
                ),
            })

        room_analysis.append({
            "room_id": room_id,
            "active": is_active,
            "in_progress": len(in_progress),
            "scheduled": len(scheduled),
            "completed": len(completed),
            "turnover": len(turnover),
            "utilized_min": room_utilized,
            "scheduled_min": room_scheduled,
        })

    # Overall utilization
    current_util = round(
        100.0 * active_rooms / max(rooms_total, 1), 1
    )
    gap_pct = round(max(0, target_utilization_pct - current_util), 1)

    # Add overrun cascade suggestions
    for alert in overrun_alerts:
        suggestions.append({
            "type": "OVERRUN_CASCADE",
            "room_id": alert["room_id"],
            "gap_min": alert["overrun_min"],
            "priority": 3,
            "suggestion": (
                f"Room {alert['room_id']}: {alert['procedure']} overrun by "
                f"{alert['overrun_min']} min. "
                f"Consider reassigning next case to available room."
            ),
        })

    # Sort suggestions by priority (highest first)
    suggestions.sort(key=lambda x: x.get("priority", 0), reverse=True)

    result = {
        "current_utilization_pct": current_util,
        "target_utilization_pct": target_utilization_pct,
        "gap_pct": gap_pct,
        "active_rooms": active_rooms,
        "rooms_total": rooms_total,
        "room_analysis": room_analysis,
        "suggestions": suggestions,
        "overrun_alerts": overrun_alerts,
    }

    return json.dumps(result)
