# Capacity Command — LACE Readmission Risk Scoring
# TDD: Tests written first in tests/test_lace.py


def _lace_los(los_days: float) -> int:
    """L (Length of Stay) component.

    Boundaries are inclusive on the upper end (per van Walraven et al.):
      <1d=0, 1-2d=2, >2-3d=3, >3-6d=4, >6-13d=5, >13d=7.
    """
    if los_days < 1:
        return 0
    if los_days <= 2:
        return 2
    if los_days <= 3:
        return 3
    if los_days <= 6:
        return 4
    if los_days <= 13:
        return 5
    return 7


def _lace_acuity(is_acute: bool) -> int:
    """A (Acuity) component: acute=3, not acute=0."""
    return 3 if is_acute else 0


def _lace_comorbidity(esi_level: int) -> int:
    """C (Comorbidity proxy): max(0, 4 - esi_level)."""
    return max(0, 4 - esi_level)


def _lace_ed_visits(ed_visits_6mo: int) -> int:
    """E (ED visits) component: min(ed_visits_6mo, 4)."""
    return min(ed_visits_6mo, 4)


def compute_lace_score(
    los_days: float,
    is_acute: bool,
    esi_level: int,
    ed_visits_6mo: int = 0,
) -> tuple[int, str]:
    """Return (total_score, risk_level). Risk: >10=HIGH, >6=MODERATE, else LOW.

    Args:
        los_days: Length of stay in days (>= 0).
        is_acute: Whether the admission was acute/emergent.
        esi_level: ESI triage level 1-5 (used as comorbidity proxy).
        ed_visits_6mo: ED visits in the prior 6 months (>= 0, capped at 4).

    Raises:
        ValueError: If esi_level is not 1-5 or ed_visits_6mo is negative.
    """
    if not 1 <= esi_level <= 5:
        raise ValueError(f"esi_level must be 1-5, got {esi_level}")
    if ed_visits_6mo < 0:
        raise ValueError(f"ed_visits_6mo must be >= 0, got {ed_visits_6mo}")

    los = _lace_los(los_days)
    acuity = _lace_acuity(is_acute)
    comorbidity = _lace_comorbidity(esi_level)
    ed = _lace_ed_visits(ed_visits_6mo)
    total = los + acuity + comorbidity + ed
    risk = lace_risk_label(total)
    return (total, risk)


def lace_risk_label(score: int) -> str:
    """Return HIGH, MODERATE, or LOW based on LACE score."""
    if score > 10:
        return "HIGH"
    if score > 6:
        return "MODERATE"
    return "LOW"
