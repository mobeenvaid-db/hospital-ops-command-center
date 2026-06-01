"""TDD tests for lib.los — written first, run to verify RED."""
import random
import pytest

from lib.los import generate_los_hours, get_acuity_multiplier


class TestGenerateLosHours:
    """Tests for generate_los_hours(dept_id, esi_level, rng)."""

    def test_icu_esi_3_generates_values_centered_around_72h(self):
        """ICU ESI 3 generates values centered around 72h (use seeded random)."""
        rng = random.Random(42)
        samples = [generate_los_hours("ICU", 3, rng) for _ in range(1000)]
        median = sorted(samples)[500]
        assert 50 <= median <= 100

    def test_peds_esi_5_generates_shorter_stays(self):
        """PEDS ESI 5 generates shorter stays (median near 12h = 24 * 0.5)."""
        rng = random.Random(42)
        samples = [generate_los_hours("PEDS", 5, rng) for _ in range(1000)]
        median = sorted(samples)[500]
        # PEDS base median ~24h, ESI 5 multiplier 0.5 -> ~12h
        assert 8 <= median <= 20

    def test_esi_1_multiplier_doubles_base_los(self):
        """ESI 1 multiplier doubles the base LOS."""
        rng = random.Random(123)
        samples_esi3 = [generate_los_hours("ICU", 3, rng) for _ in range(500)]
        rng2 = random.Random(123)
        samples_esi1 = [generate_los_hours("ICU", 1, rng2) for _ in range(500)]
        median_esi3 = sorted(samples_esi3)[250]
        median_esi1 = sorted(samples_esi1)[250]
        # ESI 1 should be roughly 2x ESI 3
        assert median_esi1 > median_esi3 * 1.5

    def test_minimum_los_always_at_least_4_hours(self):
        """Minimum LOS is always >= 4 hours (even with low acuity in PEDS)."""
        rng = random.Random(999)
        for _ in range(500):
            los = generate_los_hours("PEDS", 5, rng)
            assert los >= 4

    def test_unknown_department_raises_value_error(self):
        """Unknown department raises ValueError."""
        with pytest.raises(ValueError):
            generate_los_hours("UNKNOWN", 3)


class TestGetAcuityMultiplier:
    """Tests for get_acuity_multiplier(esi_level)."""

    def test_esi_1_returns_2_0(self):
        """Acuity multiplier for ESI 1 returns 2.0."""
        assert get_acuity_multiplier(1) == 2.0

    def test_esi_3_returns_1_0(self):
        """Acuity multiplier for ESI 3 returns 1.0."""
        assert get_acuity_multiplier(3) == 1.0

    def test_esi_5_returns_0_5(self):
        """Acuity multiplier for ESI 5 returns 0.5."""
        assert get_acuity_multiplier(5) == 0.5

    def test_invalid_esi_raises_value_error(self):
        """Invalid ESI raises ValueError."""
        with pytest.raises(ValueError):
            get_acuity_multiplier(0)
        with pytest.raises(ValueError):
            get_acuity_multiplier(6)
