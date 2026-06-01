"""TDD tests for lib.discharge — written first, run to verify RED."""
import pytest

from lib.discharge import (
    discharge_score,
    get_discharge_multiplier,
    count_predicted_discharges,
)


class TestDischargeScore:
    """Tests for discharge_score(hours_remaining, acuity, hour)."""

    def test_overdue_patient_at_peak_hour_gets_score_near_0_95(self):
        """Overdue patient (hours_remaining=-1) at peak hour (14) gets score ~0.95."""
        result = discharge_score(hours_remaining=-1, acuity=3, hour=14)
        assert result == pytest.approx(0.95, rel=1e-6)

    def test_overdue_patient_at_3am_gets_very_low_score(self):
        """Overdue patient at 3AM gets very low score (0.95 * 0.01 = ~0.0095)."""
        result = discharge_score(hours_remaining=-1, acuity=3, hour=3)
        assert result == pytest.approx(0.0095, rel=1e-6)

    def test_three_hours_remaining_esi_1_at_noon(self):
        """Patient with 3 hours remaining, ESI 1, at noon: 0.60 * 0.85 * 0.90 = ~0.459."""
        result = discharge_score(hours_remaining=3, acuity=1, hour=12)
        assert result == pytest.approx(0.459, rel=1e-6)

    def test_ten_hours_remaining_gets_base_0_10(self):
        """Patient with 10 hours remaining gets base 0.10."""
        result = discharge_score(hours_remaining=10, acuity=3, hour=12)
        assert result == pytest.approx(0.10 * 0.90, rel=1e-6)  # 0.10 * hour_multiplier

    def test_score_clamped_to_one(self):
        """Score is clamped to [0.0, 1.0]."""
        result = discharge_score(hours_remaining=-1, acuity=5, hour=14)
        assert result <= 1.0
        assert result >= 0.0


class TestGetDischargeMultiplier:
    """Tests for get_discharge_multiplier(hour, day_of_week)."""

    def test_weekend_sunday_discharge_multiplier(self):
        """Weekend Sunday discharge multiplier is 0.70 (at peak hour)."""
        result = get_discharge_multiplier(hour=14, day_of_week=6)
        assert result == pytest.approx(1.0 * 0.70, rel=1e-6)

    def test_saturday_3am_multiplier(self):
        """Saturday 3AM multiplier = 0.01 * 0.75 = 0.0075."""
        result = get_discharge_multiplier(hour=3, day_of_week=5)
        assert result == pytest.approx(0.0075, rel=1e-6)

    def test_peak_weekday_tuesday_15(self):
        """Peak weekday (Tuesday 15:00) = 1.00 * 1.0 = 1.0."""
        result = get_discharge_multiplier(hour=15, day_of_week=2)
        assert result == pytest.approx(1.0, rel=1e-6)

    def test_invalid_hour_raises_value_error(self):
        """Invalid hour raises ValueError."""
        with pytest.raises(ValueError):
            get_discharge_multiplier(hour=24, day_of_week=0)
        with pytest.raises(ValueError):
            get_discharge_multiplier(hour=-1, day_of_week=0)

    def test_invalid_day_of_week_raises_value_error(self):
        """Invalid day_of_week raises ValueError."""
        with pytest.raises(ValueError):
            get_discharge_multiplier(hour=12, day_of_week=7)
        with pytest.raises(ValueError):
            get_discharge_multiplier(hour=12, day_of_week=-1)


class TestCountPredictedDischarges:
    """Tests for count_predicted_discharges(beds, threshold)."""

    def test_three_beds_only_overdue_exceeds_threshold(self):
        """count_predicted_discharges: 1 overdue, 1 at 3hrs, 1 at 12hrs = 1 (only overdue exceeds 0.6)."""
        beds = [
            {"hours_remaining": -1, "acuity": 3, "hour": 14},
            {"hours_remaining": 3, "acuity": 1, "hour": 14},
            {"hours_remaining": 12, "acuity": 3, "hour": 14},
        ]
        result = count_predicted_discharges(beds, threshold=0.6)
        assert result == 1

    def test_empty_beds_returns_zero(self):
        """Empty beds list returns 0."""
        assert count_predicted_discharges([]) == 0

    def test_custom_threshold(self):
        """Custom threshold filters correctly."""
        beds = [
            {"hours_remaining": 1, "acuity": 3, "hour": 14},
        ]
        # 1hr remaining: base 0.80, at 14: 0.80 * 1.0 = 0.80
        assert count_predicted_discharges(beds, threshold=0.5) == 1
        assert count_predicted_discharges(beds, threshold=0.9) == 0
