"""Unit tests for the in-process simulator's pure logic.

These cover the deterministic helpers (no DB needed). The event-application
path is exercised end to end by scripts/verify_e2e.py against a real Postgres.
"""

import random

from lib.constants import (
    ESI_WEIGHTS_DAY,
    ESI_WEIGHTS_NIGHT,
    ESI_WEIGHTS_SURGE,
    INPATIENT_DEPTS,
)
from simulator import (
    _admit_dept,
    _discharge_hour_weight,
    _esi_weights,
    _sample_poisson,
)


def test_sample_poisson_zero_mean():
    rng = random.Random(0)
    assert _sample_poisson(0, rng) == 0
    assert _sample_poisson(-1, rng) == 0


def test_sample_poisson_mean_is_approximately_lambda():
    rng = random.Random(42)
    target = 3.0
    n = 20000
    avg = sum(_sample_poisson(target, rng) for _ in range(n)) / n
    assert abs(avg - target) < 0.1  # within 3% of lambda over a large sample


def test_esi_weights_selection():
    # surge dominates regardless of hour
    assert _esi_weights(14, surge=2.0) == ESI_WEIGHTS_SURGE
    # daytime vs nighttime split at 08:00 / 20:00
    assert _esi_weights(9, surge=1.0) == ESI_WEIGHTS_DAY
    assert _esi_weights(19, surge=1.0) == ESI_WEIGHTS_DAY
    assert _esi_weights(3, surge=1.0) == ESI_WEIGHTS_NIGHT
    assert _esi_weights(20, surge=1.0) == ESI_WEIGHTS_NIGHT


def test_discharge_hour_weight_curve():
    assert _discharge_hour_weight(3) < _discharge_hour_weight(9)
    assert _discharge_hour_weight(14) == 1.0          # midday peak
    assert _discharge_hour_weight(23) < _discharge_hour_weight(14)
    # always non-negative
    assert all(_discharge_hour_weight(h) >= 0 for h in range(24))


def test_admit_dept_always_inpatient():
    rng = random.Random(7)
    for esi in (1, 2, 3, 4, 5):
        for _ in range(50):
            assert _admit_dept(esi, rng) in INPATIENT_DEPTS


def test_admit_dept_high_acuity_skews_critical():
    rng = random.Random(7)
    picks = [_admit_dept(1, rng) for _ in range(500)]
    # ESI-1 should land in ICU more than in MEDSURG
    assert picks.count("ICU") > picks.count("MEDSURG")
