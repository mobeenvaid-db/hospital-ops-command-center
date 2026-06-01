"""TDD tests for lib.capacity — written first, run to verify RED."""
import pytest

from lib.capacity import forecast_capacity, forecast_department_capacity


class TestForecastCapacity:
    """Tests for forecast_capacity(current_available, predicted_discharges, predicted_admissions)."""

    def test_5_available_plus_3_discharges_minus_2_admissions_equals_6(self):
        """5 available + 3 discharges - 2 admissions = 6."""
        beds, confidence = forecast_capacity(
            current_available=5,
            predicted_discharges=3,
            predicted_admissions=2.0,
        )
        assert beds == 6
        assert confidence == 0.70

    def test_0_available_plus_0_discharges_minus_5_admissions_equals_negative_5(self):
        """0 available + 0 discharges - 5 admissions = -5 (negative = over capacity)."""
        beds, confidence = forecast_capacity(
            current_available=0,
            predicted_discharges=0,
            predicted_admissions=5.0,
        )
        assert beds == -5
        assert confidence == 0.70

    def test_rounds_admissions_before_subtraction(self):
        """predicted_admissions is rounded before subtraction."""
        beds, _ = forecast_capacity(
            current_available=10,
            predicted_discharges=2,
            predicted_admissions=2.7,
        )
        assert beds == 10 + 2 - 3  # round(2.7) = 3
        assert beds == 9


class TestForecastDepartmentCapacity:
    """Tests for forecast_department_capacity(...)."""

    def test_scores_above_threshold_count_as_predicted_discharges(self):
        """Scores [0.8, 0.7, 0.3, 0.1] at threshold 0.6 -> 2 predicted discharges."""
        result = forecast_department_capacity(
            dept_id="ICU",
            available=5,
            discharge_scores=[0.8, 0.7, 0.3, 0.1],
            ed_forecast=0,
            dept_admission_share=0.0,
            discharge_threshold=0.6,
        )
        assert result["predicted_discharges_2h"] == 2
        assert result["dept_id"] == "ICU"
        assert result["current_available"] == 5

    def test_ed_forecast_and_dept_share_calculate_admissions(self):
        """ed_forecast=6, dept_share=0.25 -> admissions = 6*2*0.25 = 3."""
        result = forecast_department_capacity(
            dept_id="MEDSURG",
            available=10,
            discharge_scores=[],
            ed_forecast=6.0,
            dept_admission_share=0.25,
        )
        assert result["predicted_admissions_2h"] == pytest.approx(3.0, rel=1e-6)
        assert result["predicted_capacity_2h"] == 10 + 0 - 3  # 7

    def test_empty_scores_list_works_zero_discharges(self):
        """Empty scores list works (0 discharges)."""
        result = forecast_department_capacity(
            dept_id="TELE",
            available=8,
            discharge_scores=[],
            ed_forecast=0,
            dept_admission_share=0.0,
        )
        assert result["predicted_discharges_2h"] == 0
        assert result["predicted_capacity_2h"] == 8
        assert result["confidence"] == pytest.approx(0.70 * 0.5, rel=1e-6)  # 0.5 when none qualify

    def test_confidence_is_0_70_times_avg_of_qualifying_scores(self):
        """Confidence is 0.70 * avg of qualifying scores."""
        result = forecast_department_capacity(
            dept_id="ICU",
            available=5,
            discharge_scores=[0.8, 0.7, 0.3, 0.1],
            ed_forecast=0,
            dept_admission_share=0.0,
            discharge_threshold=0.6,
        )
        # Qualifying: 0.8, 0.7. Avg = 0.75. Confidence = 0.70 * 0.75 = 0.525
        assert result["confidence"] == pytest.approx(0.525, rel=1e-6)

    def test_predicted_capacity_formula(self):
        """predicted_capacity_2h = available + predicted_discharges_2h - round(predicted_admissions_2h)."""
        result = forecast_department_capacity(
            dept_id="PEDS",
            available=4,
            discharge_scores=[0.9, 0.85],
            ed_forecast=5.0,
            dept_admission_share=0.2,
            discharge_threshold=0.6,
        )
        # predicted_discharges_2h = 2
        # predicted_admissions_2h = 5 * 2 * 0.2 = 2.0
        # predicted_capacity_2h = 4 + 2 - 2 = 4
        assert result["predicted_discharges_2h"] == 2
        assert result["predicted_admissions_2h"] == pytest.approx(2.0, rel=1e-6)
        assert result["predicted_capacity_2h"] == 4
