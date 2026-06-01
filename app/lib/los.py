# Capacity Command — Length of Stay Logic (Log-Normal + Acuity)
# TDD: Tests written first in tests/test_los.py

import random

from lib.constants import DEPT_LOS_LOGNORMAL, ACUITY_LOS_MULTIPLIER


def generate_los_hours(
    dept_id: str, esi_level: int, rng: random.Random | None = None
) -> int:
    """Returns LOS in hours using Log-Normal distribution."""
    if dept_id not in DEPT_LOS_LOGNORMAL:
        raise ValueError(f"Unknown dept_id: {dept_id}")
    if esi_level not in ACUITY_LOS_MULTIPLIER:
        raise ValueError(f"esi_level must be 1-5, got {esi_level}")

    mu, sigma = DEPT_LOS_LOGNORMAL[dept_id]
    multiplier = ACUITY_LOS_MULTIPLIER[esi_level]

    if rng is not None:
        raw = rng.lognormvariate(mu, sigma)
    else:
        raw = random.lognormvariate(mu, sigma)

    los = raw * multiplier
    return max(4, int(round(los)))


def get_acuity_multiplier(esi_level: int) -> float:
    """Returns the multiplier from ACUITY_LOS_MULTIPLIER."""
    if esi_level not in ACUITY_LOS_MULTIPLIER:
        raise ValueError(f"esi_level must be 1-5, got {esi_level}")
    return ACUITY_LOS_MULTIPLIER[esi_level]
