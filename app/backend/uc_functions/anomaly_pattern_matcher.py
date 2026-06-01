"""UC Function: Anomaly Pattern Matcher

Registered as: hospital_ops.agents.anomaly_pattern_matcher

Detects patterns in anomaly events:
  - Recurring issues (same type in same dept within time window)
  - Correlated failures (multiple anomaly types co-occurring)
  - Escalation patterns (warning -> critical progression)

Uses threshold-based detection aligned with lib.anomaly constants.
"""

import json
from collections import Counter, defaultdict


# Anomaly type severity ordering
_SEVERITY_ORDER = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}

# Known correlation pairs (anomalies that often co-occur)
_CORRELATED_PAIRS = [
    ("CAPACITY_CRITICAL", "ED_BOARDING_SURGE"),
    ("CAPACITY_WARNING", "ED_WAIT_HIGH"),
    ("ED_BOARDING_SURGE", "BOARDING_HOURS_CRITICAL"),
    ("CAPACITY_CRITICAL", "BLOCKED_BEDS_HIGH"),
    ("OR_OVERRUN", "CAPACITY_WARNING"),
    ("LWBS_CRITICAL", "ED_WAIT_HIGH"),
]


def anomaly_pattern_matcher(
    events_json: str,
    window_hours: int = 4,
    recurrence_threshold: int = 2,
) -> str:
    """Detect patterns in anomaly events.

    Parameters
    ----------
    events_json : str
        JSON array of anomaly events, each with:
        - event_id: str
        - event_time: str (ISO timestamp)
        - type: str (anomaly type, e.g. "CAPACITY_CRITICAL")
        - dept: str (department or entity)
        - value: float (metric value that triggered the anomaly)
        - severity: str ("CRITICAL", "WARNING", "INFO")
        - confidence: float (0.0-1.0)
    window_hours : int
        Time window in hours for pattern detection (default 4).
    recurrence_threshold : int
        Minimum occurrences to flag as recurring (default 2).

    Returns
    -------
    str
        JSON object with:
        - recurring_patterns: repeated anomaly types per dept
        - correlations: co-occurring anomaly pairs detected
        - escalations: warning-to-critical progressions
        - hotspots: departments with highest anomaly density
        - summary: overall pattern analysis
        - recommendations: actionable suggestions
    """
    try:
        events = json.loads(events_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "Invalid events_json", "patterns": []})

    if not events:
        return json.dumps({
            "recurring_patterns": [],
            "correlations": [],
            "escalations": [],
            "hotspots": [],
            "summary": {"total_events": 0, "pattern_count": 0},
            "recommendations": [],
        })

    # --- Recurring patterns: same type+dept appearing multiple times ---
    type_dept_counts: dict[str, int] = Counter()
    type_dept_events: dict[str, list] = defaultdict(list)
    for e in events:
        key = f"{e.get('type', '')}|{e.get('dept', '')}"
        type_dept_counts[key] += 1
        type_dept_events[key].append(e)

    recurring_patterns = []
    for key, count in type_dept_counts.items():
        if count >= recurrence_threshold:
            atype, dept = key.split("|", 1)
            sample_events = type_dept_events[key]
            max_severity = max(
                (_SEVERITY_ORDER.get(e.get("severity", "INFO"), 0) for e in sample_events),
                default=0,
            )
            severity_label = {3: "CRITICAL", 2: "WARNING"}.get(max_severity, "INFO")
            recurring_patterns.append({
                "type": atype,
                "dept": dept,
                "count": count,
                "max_severity": severity_label,
                "trend": "ESCALATING" if count > recurrence_threshold * 2 else "RECURRING",
            })

    # --- Correlations: co-occurring anomaly types ---
    active_types = set(e.get("type", "") for e in events)
    correlations = []
    for type_a, type_b in _CORRELATED_PAIRS:
        if type_a in active_types and type_b in active_types:
            correlations.append({
                "pair": [type_a, type_b],
                "explanation": _correlation_explanation(type_a, type_b),
                "combined_severity": "CRITICAL",
            })

    # --- Escalations: warning -> critical in same dept ---
    dept_severities: dict[str, list[str]] = defaultdict(list)
    for e in events:
        dept = e.get("dept", "")
        sev = e.get("severity", "INFO")
        dept_severities[dept].append(sev)

    escalations = []
    for dept, sevs in dept_severities.items():
        has_warning = "WARNING" in sevs
        has_critical = "CRITICAL" in sevs
        if has_warning and has_critical:
            escalations.append({
                "dept": dept,
                "pattern": "WARNING -> CRITICAL",
                "warning_count": sevs.count("WARNING"),
                "critical_count": sevs.count("CRITICAL"),
            })

    # --- Hotspots: departments ranked by anomaly density ---
    dept_counts = Counter(e.get("dept", "") for e in events)
    hotspots = [
        {"dept": dept, "anomaly_count": count, "pct_of_total": round(100.0 * count / len(events), 1)}
        for dept, count in dept_counts.most_common(5)
    ]

    # --- Recommendations ---
    recommendations = []
    for pattern in recurring_patterns:
        if pattern["max_severity"] == "CRITICAL":
            recommendations.append({
                "priority": "HIGH",
                "target": pattern["dept"],
                "action": (
                    f"Recurring {pattern['type']} in {pattern['dept']} "
                    f"({pattern['count']}x). Investigate root cause immediately."
                ),
            })
    for corr in correlations:
        recommendations.append({
            "priority": "HIGH",
            "target": "SYSTEM",
            "action": (
                f"Correlated anomalies detected: {corr['pair'][0]} + {corr['pair'][1]}. "
                f"{corr['explanation']}"
            ),
        })
    for esc in escalations:
        recommendations.append({
            "priority": "CRITICAL",
            "target": esc["dept"],
            "action": (
                f"{esc['dept']}: Escalation detected "
                f"({esc['warning_count']} warnings, {esc['critical_count']} criticals). "
                f"Activate surge protocol."
            ),
        })

    recommendations.sort(
        key=lambda x: {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1}.get(x["priority"], 0),
        reverse=True,
    )

    result = {
        "recurring_patterns": recurring_patterns,
        "correlations": correlations,
        "escalations": escalations,
        "hotspots": hotspots,
        "summary": {
            "total_events": len(events),
            "pattern_count": len(recurring_patterns) + len(correlations) + len(escalations),
            "recurring_count": len(recurring_patterns),
            "correlation_count": len(correlations),
            "escalation_count": len(escalations),
        },
        "recommendations": recommendations,
    }

    return json.dumps(result)


def _correlation_explanation(type_a: str, type_b: str) -> str:
    """Return human-readable explanation for correlated anomaly pairs."""
    explanations = {
        ("CAPACITY_CRITICAL", "ED_BOARDING_SURGE"):
            "Full beds cause ED boarding backup. Prioritize discharges.",
        ("CAPACITY_WARNING", "ED_WAIT_HIGH"):
            "Tight capacity extends ED wait times. Monitor bed turnover.",
        ("ED_BOARDING_SURGE", "BOARDING_HOURS_CRITICAL"):
            "Boarding surge duration exceeding Joint Commission 4h target.",
        ("CAPACITY_CRITICAL", "BLOCKED_BEDS_HIGH"):
            "Blocked beds worsening capacity crisis. Unblock maintenance beds.",
        ("OR_OVERRUN", "CAPACITY_WARNING"):
            "OR overruns delay post-op bed turnover, reducing capacity.",
        ("LWBS_CRITICAL", "ED_WAIT_HIGH"):
            "High LWBS rate driven by long wait times. Add ED staffing.",
    }
    return explanations.get(
        (type_a, type_b),
        explanations.get((type_b, type_a), "Co-occurring anomalies indicate systemic stress."),
    )
