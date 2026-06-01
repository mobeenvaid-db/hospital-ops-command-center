"""TDD tests for lib.arrivals — written first, run to verify RED."""
import pytest

from lib.arrivals import (
    get_arrival_lambda,
    get_esi_weights,
    get_admission_probability,
    ewma_forecast,
)
from lib.constants import (
    HOURLY_LAMBDA,
    DOW_MULTIPLIER,
    ESI_WEIGHTS_DAY,
    ESI_WEIGHTS_NIGHT,
    ESI_WEIGHTS_SURGE,
)


class TestGetArrivalLambda:
    """Tests for get_arrival_lambda(hour, day_of_week, surge)."""

    def test_peak_hour_monday_returns_expected_rate(self):
        """Peak hour (10) Monday should return ~9.2 (8.0 * 1.15)."""
        result = get_arrival_lambda(hour=10, day_of_week=0, surge=1.0)
        assert result == pytest.approx(9.2, rel=1e-6)

    def test_nadir_hour_saturday_returns_expected_rate(self):
        """Nadir hour (3) Saturday should return ~0.85 (1.0 * 0.85)."""
        result = get_arrival_lambda(hour=3, day_of_week=5, surge=1.0)
        assert result == pytest.approx(0.85, rel=1e-6)

    def test_surge_2x_at_peak_doubles_rate(self):
        """Surge 2x at peak should double the rate."""
        base = get_arrival_lambda(hour=10, day_of_week=0, surge=1.0)
        surged = get_arrival_lambda(hour=10, day_of_week=0, surge=2.0)
        assert surged == pytest.approx(base * 2.0, rel=1e-6)

    def test_invalid_hour_raises_value_error(self):
        """Invalid hour (25) raises ValueError."""
        with pytest.raises(ValueError):
            get_arrival_lambda(hour=25, day_of_week=0)

    def test_invalid_day_of_week_raises_value_error(self):
        """Invalid day_of_week raises ValueError."""
        with pytest.raises(ValueError):
            get_arrival_lambda(hour=10, day_of_week=7)
        with pytest.raises(ValueError):
            get_arrival_lambda(hour=10, day_of_week=-1)


class TestGetEsiWeights:
    """Tests for get_esi_weights(hour, surge)."""

    def test_daytime_esi_weights_at_hour_12(self):
        """Daytime ESI weights at hour 12 should return ESI_WEIGHTS_DAY (normalized)."""
        result = get_esi_weights(hour=12, surge=1.0)
        expected = [w / sum(ESI_WEIGHTS_DAY) for w in ESI_WEIGHTS_DAY]
        assert result == pytest.approx(expected, rel=1e-6)

    def test_nighttime_esi_weights_at_hour_2(self):
        """Nighttime ESI weights at hour 2 should return ESI_WEIGHTS_NIGHT (normalized)."""
        result = get_esi_weights(hour=2, surge=1.0)
        expected = [w / sum(ESI_WEIGHTS_NIGHT) for w in ESI_WEIGHTS_NIGHT]
        assert result == pytest.approx(expected, rel=1e-6)

    def test_surge_over_1_5_overrides_to_surge_weights(self):
        """Surge > 1.5 overrides to ESI_WEIGHTS_SURGE (normalized)."""
        result = get_esi_weights(hour=12, surge=1.6)
        expected = [w / sum(ESI_WEIGHTS_SURGE) for w in ESI_WEIGHTS_SURGE]
        assert result == pytest.approx(expected, rel=1e-6)
        result_night = get_esi_weights(hour=2, surge=2.0)
        assert result_night == pytest.approx(expected, rel=1e-6)

    def test_esi_weights_sum_to_approximately_one(self):
        """ESI weights sum to approximately 1.0."""
        for hour in range(24):
            for surge in [1.0, 1.2, 1.6]:
                weights = get_esi_weights(hour=hour, surge=surge)
                assert sum(weights) == pytest.approx(1.0, abs=0.01)


class TestGetAdmissionProbability:
    """Tests for get_admission_probability(esi_level)."""

    def test_esi_1_returns_0_95(self):
        """Admission prob for ESI 1 = 0.95."""
        assert get_admission_probability(1) == 0.95

    def test_esi_5_returns_0_01(self):
        """Admission prob for ESI 5 = 0.01."""
        assert get_admission_probability(5) == 0.01

    def test_invalid_esi_0_raises_value_error(self):
        """Invalid ESI 0 raises ValueError."""
        with pytest.raises(ValueError):
            get_admission_probability(0)

    def test_invalid_esi_6_raises_value_error(self):
        """Invalid ESI 6 raises ValueError."""
        with pytest.raises(ValueError):
            get_admission_probability(6)


class TestEwmaForecast:
    """Tests for ewma_forecast(actual_last_hour, base_rate, alpha)."""

    def test_actual_equals_base_gives_confidence_near_0_80(self):
        """EWMA with actual==base gives confidence ~0.80."""
        forecast, confidence = ewma_forecast(actual_last_hour=10.0, base_rate=10.0, alpha=0.3)
        assert confidence == pytest.approx(0.80, abs=0.01)

    def test_actual_far_from_base_gives_lower_confidence(self):
        """EWMA with actual far from base gives lower confidence."""
        _, confidence_low = ewma_forecast(actual_last_hour=100.0, base_rate=10.0, alpha=0.3)
        _, confidence_base = ewma_forecast(actual_last_hour=10.0, base_rate=10.0, alpha=0.3)
        assert confidence_low < confidence_base

    def test_forecast_is_weighted_average_of_actual_and_base(self):
        """EWMA forecast is weighted average of actual and base."""
        actual, base, alpha = 15.0, 10.0, 0.3
        expected_forecast = alpha * actual + (1 - alpha) * base
        forecast, _ = ewma_forecast(actual_last_hour=actual, base_rate=base, alpha=alpha)
        assert forecast == pytest.approx(expected_forecast, rel=1e-6)

    def test_returns_tuple_of_two_floats(self):
        """ewma_forecast returns (forecast_next_hour, confidence)."""
        result = ewma_forecast(actual_last_hour=10.0, base_rate=10.0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], float)
        assert isinstance(result[1], float)

    def test_confidence_stays_in_bounds_for_extreme_deviation(self):
        """Confidence is clamped to [0.50, 0.95] even with extreme actual/base mismatch."""
        # Massive overshoot: actual 10x base
        _, conf_high = ewma_forecast(actual_last_hour=1000.0, base_rate=1.0)
        assert 0.50 <= conf_high <= 0.95

        # Zero actual, positive base
        _, conf_low = ewma_forecast(actual_last_hour=0.0, base_rate=100.0)
        assert 0.50 <= conf_low <= 0.95

        # Both zero (edge case)
        _, conf_zero = ewma_forecast(actual_last_hour=0.0, base_rate=0.0)
        assert 0.50 <= conf_zero <= 0.95
