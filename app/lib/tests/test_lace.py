"""TDD tests for lib.lace — written first, run to verify RED."""
import pytest

from lib.lace import compute_lace_score, lace_risk_label


class TestComputeLaceScore:
    """Tests for compute_lace_score(los_days, is_acute, esi_level, ed_visits_6mo)."""

    def test_short_stay_not_acute_esi_5_zero_ed_visits(self):
        """Short stay (0.5 days), not acute, ESI 5, 0 ED visits = score 0, LOW."""
        score, risk = compute_lace_score(
            los_days=0.5, is_acute=False, esi_level=5, ed_visits_6mo=0
        )
        assert score == 0
        assert risk == "LOW"

    def test_long_stay_acute_esi_1_four_ed_visits(self):
        """Long stay (14 days), acute, ESI 1, 4 ED visits = 7+3+3+4 = 17, HIGH."""
        score, risk = compute_lace_score(
            los_days=14, is_acute=True, esi_level=1, ed_visits_6mo=4
        )
        assert score == 17
        assert risk == "HIGH"

    def test_three_days_acute_esi_2_one_ed_visit(self):
        """3 days, acute, ESI 2, 1 ED visit = 3+3+2+1 = 9, MODERATE."""
        score, risk = compute_lace_score(
            los_days=3, is_acute=True, esi_level=2, ed_visits_6mo=1
        )
        # L: 3.0 days -> <=3 -> 3, A: acute -> 3, C: ESI 2 -> 2, E: 1 -> 1
        assert score == 9
        assert risk == "MODERATE"

    def test_seven_days_acute_esi_1_ed_visits_capped(self):
        """7 days, acute, ESI 1, 5 ED visits = 5+3+3+4 = 15, HIGH (ed_visits capped at 4)."""
        score, risk = compute_lace_score(
            los_days=7, is_acute=True, esi_level=1, ed_visits_6mo=5
        )
        assert score == 15
        assert risk == "HIGH"

    def test_esi_4_comorbidity_proxy_zero(self):
        """ESI 4 comorbidity proxy = 0."""
        score, _ = compute_lace_score(
            los_days=0.5, is_acute=False, esi_level=4, ed_visits_6mo=0
        )
        # L=0, A=0, C=0, E=0 -> score 0
        assert score == 0

    def test_esi_1_comorbidity_proxy_three(self):
        """ESI 1 comorbidity proxy = 3."""
        score, _ = compute_lace_score(
            los_days=0.5, is_acute=False, esi_level=1, ed_visits_6mo=0
        )
        # L=0, A=0, C=3, E=0 -> score 3
        assert score == 3


class TestLaceLOSBoundaries:
    """Tests for LOS boundary values — exact day boundaries use <=."""

    def test_exactly_2_days_returns_l_score_2(self):
        """Exactly 2.0 days -> L=2 (inclusive upper bound)."""
        score, _ = compute_lace_score(los_days=2.0, is_acute=False, esi_level=5)
        assert score == 2  # L=2, A=0, C=0, E=0

    def test_exactly_3_days_returns_l_score_3(self):
        """Exactly 3.0 days -> L=3 (inclusive upper bound)."""
        score, _ = compute_lace_score(los_days=3.0, is_acute=False, esi_level=5)
        assert score == 3  # L=3, A=0, C=0, E=0

    def test_exactly_6_days_returns_l_score_4(self):
        """Exactly 6.0 days -> L=4 (inclusive upper bound)."""
        score, _ = compute_lace_score(los_days=6.0, is_acute=False, esi_level=5)
        assert score == 4  # L=4, A=0, C=0, E=0

    def test_exactly_13_days_returns_l_score_5(self):
        """Exactly 13.0 days -> L=5 (inclusive upper bound)."""
        score, _ = compute_lace_score(los_days=13.0, is_acute=False, esi_level=5)
        assert score == 5  # L=5, A=0, C=0, E=0

    def test_just_over_13_days_returns_l_score_7(self):
        """13.01 days -> L=7 (exceeds boundary)."""
        score, _ = compute_lace_score(los_days=13.01, is_acute=False, esi_level=5)
        assert score == 7  # L=7, A=0, C=0, E=0

    def test_exactly_1_day_returns_l_score_2(self):
        """Exactly 1.0 days -> L=2 (enters first bucket)."""
        score, _ = compute_lace_score(los_days=1.0, is_acute=False, esi_level=5)
        assert score == 2  # L=2, A=0, C=0, E=0


class TestLaceInputValidation:
    """Tests for input validation in compute_lace_score."""

    def test_esi_level_0_raises_value_error(self):
        """ESI level 0 (invalid) raises ValueError."""
        with pytest.raises(ValueError, match="esi_level must be 1-5"):
            compute_lace_score(los_days=1.0, is_acute=True, esi_level=0)

    def test_esi_level_6_raises_value_error(self):
        """ESI level 6 (invalid) raises ValueError."""
        with pytest.raises(ValueError, match="esi_level must be 1-5"):
            compute_lace_score(los_days=1.0, is_acute=True, esi_level=6)

    def test_negative_ed_visits_raises_value_error(self):
        """Negative ed_visits_6mo raises ValueError."""
        with pytest.raises(ValueError, match="ed_visits_6mo must be >= 0"):
            compute_lace_score(los_days=1.0, is_acute=True, esi_level=3, ed_visits_6mo=-1)


class TestLaceRiskLabel:
    """Tests for lace_risk_label(score)."""

    def test_high_risk(self):
        """Score > 10 returns HIGH."""
        assert lace_risk_label(11) == "HIGH"
        assert lace_risk_label(17) == "HIGH"

    def test_moderate_risk(self):
        """Score > 6 and <= 10 returns MODERATE."""
        assert lace_risk_label(7) == "MODERATE"
        assert lace_risk_label(10) == "MODERATE"

    def test_low_risk(self):
        """Score <= 6 returns LOW."""
        assert lace_risk_label(0) == "LOW"
        assert lace_risk_label(6) == "LOW"
