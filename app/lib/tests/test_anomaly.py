"""TDD tests for lib.anomaly — written first, run to verify RED."""
import pytest

from lib.anomaly import detect_anomalies, compute_system_status


def _normal_state() -> dict:
    """State with all metrics within normal thresholds."""
    return {
        "dept_occupancy": {"ICU": 70.0, "MEDSURG": 65.0, "TELE": 68.0, "PEDS": 50.0},
        "ed_avg_wait_min": 20.0,
        "ed_boarders_count": 2,
        "avg_boarding_hours": 2.0,
        "lwbs_rate_pct": 1.5,
        "or_overruns": [],
        "blocked_beds_pct": 5.0,
    }


class TestDetectAnomalies:
    """Tests for detect_anomalies(state)."""

    def test_all_normal_state_returns_empty_anomaly_list(self):
        """All normal state returns empty anomaly list."""
        result = detect_anomalies(_normal_state())
        assert result == []

    def test_icu_at_92_percent_returns_capacity_critical(self):
        """ICU at 92% occupancy returns CAPACITY_CRITICAL anomaly."""
        state = _normal_state()
        state["dept_occupancy"] = {"ICU": 92.5, "MEDSURG": 65.0}
        result = detect_anomalies(state)
        assert len(result) >= 1
        critical = [a for a in result if a["type"] == "CAPACITY_CRITICAL"]
        assert len(critical) == 1
        assert critical[0]["dept"] == "ICU"
        assert critical[0]["value"] == 92.5
        assert critical[0]["severity"] == "CRITICAL"
        assert critical[0]["confidence"] == 0.95

    def test_icu_at_87_percent_returns_capacity_warning_not_critical(self):
        """ICU at 87% returns CAPACITY_WARNING (not critical)."""
        state = _normal_state()
        state["dept_occupancy"] = {"ICU": 87.0, "MEDSURG": 65.0}
        result = detect_anomalies(state)
        critical = [a for a in result if a["type"] == "CAPACITY_CRITICAL"]
        warning = [a for a in result if a["type"] == "CAPACITY_WARNING"]
        assert len(critical) == 0
        assert len(warning) == 1
        assert warning[0]["dept"] == "ICU"
        assert warning[0]["severity"] == "WARNING"
        assert warning[0]["confidence"] == 0.85

    def test_ed_wait_at_75_min_returns_ed_wait_high(self):
        """ED wait at 75 min returns ED_WAIT_HIGH."""
        state = _normal_state()
        state["ed_avg_wait_min"] = 75.0
        result = detect_anomalies(state)
        high = [a for a in result if a["type"] == "ED_WAIT_HIGH"]
        assert len(high) == 1
        assert high[0]["value"] == 75.0
        assert high[0]["severity"] == "WARNING"
        assert high[0]["confidence"] == 0.90

    def test_8_boarders_returns_ed_boarding_surge(self):
        """8 boarders returns ED_BOARDING_SURGE."""
        state = _normal_state()
        state["ed_boarders_count"] = 8
        result = detect_anomalies(state)
        surge = [a for a in result if a["type"] == "ED_BOARDING_SURGE"]
        assert len(surge) == 1
        assert surge[0]["value"] == 8
        assert surge[0]["severity"] == "WARNING"
        assert surge[0]["confidence"] == 0.90

    def test_avg_boarding_hours_5_returns_boarding_hours_critical(self):
        """avg_boarding_hours=5 returns BOARDING_HOURS_CRITICAL."""
        state = _normal_state()
        state["avg_boarding_hours"] = 5.0
        result = detect_anomalies(state)
        critical = [a for a in result if a["type"] == "BOARDING_HOURS_CRITICAL"]
        assert len(critical) == 1
        assert critical[0]["value"] == 5.0
        assert critical[0]["severity"] == "CRITICAL"
        assert critical[0]["confidence"] == 0.95

    def test_lwbs_6_percent_returns_lwbs_critical_not_warning(self):
        """LWBS 6% returns LWBS_CRITICAL (not warning)."""
        state = _normal_state()
        state["lwbs_rate_pct"] = 6.0
        result = detect_anomalies(state)
        critical = [a for a in result if a["type"] == "LWBS_CRITICAL"]
        warning = [a for a in result if a["type"] == "LWBS_WARNING"]
        assert len(critical) == 1
        assert len(warning) == 0
        assert critical[0]["severity"] == "CRITICAL"
        assert critical[0]["confidence"] == 0.90

    def test_lwbs_4_percent_returns_lwbs_warning_not_critical(self):
        """LWBS 4% returns LWBS_WARNING (not critical)."""
        state = _normal_state()
        state["lwbs_rate_pct"] = 4.0
        result = detect_anomalies(state)
        critical = [a for a in result if a["type"] == "LWBS_CRITICAL"]
        warning = [a for a in result if a["type"] == "LWBS_WARNING"]
        assert len(critical) == 0
        assert len(warning) == 1
        assert warning[0]["severity"] == "WARNING"
        assert warning[0]["confidence"] == 0.80

    def test_or_overruns_only_or3_exceeds_threshold(self):
        """Two OR overruns: OR-3 (45 min) and OR-5 (20 min) returns one OR_OVERRUN for OR-3 only."""
        state = _normal_state()
        state["or_overruns"] = [
            {"room_id": "OR-3", "overrun_min": 45},
            {"room_id": "OR-5", "overrun_min": 20},
        ]
        result = detect_anomalies(state)
        overruns = [a for a in result if a["type"] == "OR_OVERRUN"]
        assert len(overruns) == 1
        assert overruns[0]["dept"] == "OR-3" or overruns[0].get("room_id") == "OR-3"
        assert overruns[0]["value"] == 45
        assert overruns[0]["severity"] == "WARNING"
        assert overruns[0]["confidence"] == 0.85

    def test_blocked_beds_12_percent_returns_blocked_beds_high(self):
        """blocked_beds_pct=12 returns BLOCKED_BEDS_HIGH."""
        state = _normal_state()
        state["blocked_beds_pct"] = 12.0
        result = detect_anomalies(state)
        high = [a for a in result if a["type"] == "BLOCKED_BEDS_HIGH"]
        assert len(high) == 1
        assert high[0]["value"] == 12.0
        assert high[0]["severity"] == "WARNING"
        assert high[0]["confidence"] == 0.80

    def test_multiple_anomalies_fire_simultaneously(self):
        """Multiple anomalies can fire simultaneously."""
        state = _normal_state()
        state["dept_occupancy"] = {"ICU": 92.0, "MEDSURG": 87.0}
        state["ed_avg_wait_min"] = 75.0
        state["ed_boarders_count"] = 8
        result = detect_anomalies(state)
        types = [a["type"] for a in result]
        assert "CAPACITY_CRITICAL" in types
        assert "CAPACITY_WARNING" in types
        assert "ED_WAIT_HIGH" in types
        assert "ED_BOARDING_SURGE" in types
        assert len(result) >= 4


class TestComputeSystemStatus:
    """Tests for compute_system_status(state)."""

    def test_all_normal_state_returns_normal(self):
        """All normal state returns NORMAL status."""
        assert compute_system_status(_normal_state()) == "NORMAL"

    def test_one_dept_at_82_percent_returns_constrained(self):
        """System status with one dept at 82% = CONSTRAINED."""
        state = _normal_state()
        state["dept_occupancy"] = {"ICU": 82.0, "MEDSURG": 65.0}
        assert compute_system_status(state) == "CONSTRAINED"

    def test_dept_at_92_percent_returns_critical(self):
        """Dept at 92% (above constrained_max) returns CRITICAL."""
        state = _normal_state()
        state["dept_occupancy"] = {"ICU": 92.0, "MEDSURG": 65.0}
        assert compute_system_status(state) == "CRITICAL"

    def test_ed_wait_75_min_returns_critical(self):
        """ED wait 75 min (above constrained_max_ed_wait 60) returns CRITICAL."""
        state = _normal_state()
        state["ed_avg_wait_min"] = 75.0
        assert compute_system_status(state) == "CRITICAL"

    def test_ed_boarders_6_returns_constrained(self):
        """ed_boarders_count > 5 returns CONSTRAINED."""
        state = _normal_state()
        state["ed_boarders_count"] = 6
        assert compute_system_status(state) == "CONSTRAINED"
