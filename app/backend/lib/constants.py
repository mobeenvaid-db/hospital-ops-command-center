"""
Capacity Command — Research-Calibrated Constants

All parameters validated against:
- Gemini deep research report (70 citations)
- De Santis et al. 2022 (NHPP for ED arrivals)
- SJTREM (weekly ED patterns)
- HCUP/AHRQ (LOS by service line)
- CMS ED benchmarks 2024
- Frontiers in Digital Health 2024 (OR durations)
- Healthcare Facilities Today (bed cleaning)
- ACEP boarding crisis data
- van Walraven et al. (LACE index)
"""

# ── ED Arrival Rate: Nonhomogeneous Poisson Process ─────────────────
# Hourly lambda (arrivals/hr) for a 300-bed community hospital (~115/day)
# Shape: nadir 03-06, morning ramp 08-10, peak 10-11, afternoon plateau,
# evening decline. Source: Gemini Table 1 + De Santis et al. 2022
HOURLY_LAMBDA = [
    2.0, 1.5, 1.2, 1.0, 1.0, 1.5,   # 00-05: night nadir
    2.5, 4.0, 5.5, 7.0, 8.0, 8.5,   # 06-11: morning ramp + peak
    7.5, 7.0, 6.5, 6.5, 6.0, 6.0,   # 12-17: afternoon plateau
    5.5, 5.0, 4.5, 4.0, 3.5, 3.0,   # 18-23: evening decline
]

# Day-of-week multiplier (Mon=0, Sun=6). Source: SJTREM
DOW_MULTIPLIER = [1.15, 1.05, 1.00, 1.00, 0.95, 0.85, 0.90]

# ── ESI Acuity Distribution ─────────────────────────────────────────
# Source: Gemini Table 2, ESI Handbook, Beckman Coulter 2022
# Daytime (08:00-20:00): standard distribution
ESI_WEIGHTS_DAY = [0.01, 0.22, 0.50, 0.18, 0.03]
# Nighttime (20:00-08:00): higher acuity density (Gemini Sec 2.2.2)
ESI_WEIGHTS_NIGHT = [0.02, 0.28, 0.45, 0.15, 0.02]
# Surge mode: shift toward critical
ESI_WEIGHTS_SURGE = [0.05, 0.30, 0.40, 0.15, 0.02]

# ── ESI-Dependent Admission Probability ─────────────────────────────
# Source: Gemini Table 2 — admission probability by ESI level
ADMISSION_PROB_BY_ESI = {1: 0.95, 2: 0.50, 3: 0.20, 4: 0.03, 5: 0.01}

# ── Length of Stay: Log-Normal Parameters ────────────────────────────
# (mu, sigma) for random.lognormvariate — in hours
# Compressed for demo pacing; maintains correct inter-department ratios
# Source: HCUP/AHRQ + Gemini Sec 3.1 (real LOS is 3.5-5 days; we compress)
DEPT_LOS_LOGNORMAL = {
    "ICU":     (4.28, 0.47),   # median ~72h (3 days)
    "TELE":    (3.87, 0.47),   # median ~48h (2 days)
    "MEDSURG": (3.58, 0.47),   # median ~36h (1.5 days)
    "PEDS":    (3.18, 0.47),   # median ~24h (1 day)
}

# Acuity multiplier on LOS (higher acuity = longer stay)
ACUITY_LOS_MULTIPLIER = {1: 2.0, 2: 1.5, 3: 1.0, 4: 0.7, 5: 0.5}

# ── Discharge Time-of-Day Curve ──────────────────────────────────────
# Relative probability of discharge by hour (0=midnight)
# Peak 14:00-16:00, near-zero at night
# Source: Gemini Sec 3.2 — Triangular(10:00, 15:00, 20:00)
DISCHARGE_HOUR_MULTIPLIER = [
    0.02, 0.02, 0.01, 0.01, 0.01, 0.02,  # 00-05
    0.05, 0.10, 0.20, 0.40, 0.60, 0.80,  # 06-11
    0.90, 0.95, 1.00, 1.00, 0.90, 0.80,  # 12-17
    0.60, 0.40, 0.20, 0.10, 0.05, 0.03,  # 18-23
]

# Weekend discharge drop (Sat -25%, Sun -30%)
# Source: Gemini Sec 3.2 — no case managers, PT, pharmacy on weekends
DOW_DISCHARGE_MULTIPLIER = [1.0, 1.0, 1.0, 1.0, 1.0, 0.75, 0.70]

# ── OR Case Duration: Log-Normal Parameters ──────────────────────────
# (mu, sigma) for lognormvariate — in minutes
# Compressed ~50% from research values for demo pacing
# Source: Gemini Table 4 + Frontiers in Digital Health 2024
OR_DURATION_LOGNORMAL = {
    "General Surgery":  (4.10, 0.35),   # median ~60 min
    "Orthopedic":       (4.38, 0.35),   # median ~80 min
    "Cardiac":          (4.79, 0.40),   # median ~120 min
    "Neuro":            (4.61, 0.40),   # median ~100 min
    "GI / Endoscopy":   (3.69, 0.30),   # median ~40 min
    "Urology":          (3.99, 0.30),   # median ~55 min
    "Plastics":         (4.25, 0.35),   # median ~70 min
    "Thoracic Surgery": (4.70, 0.40),   # median ~110 min
}

# ── Transition Probabilities (per 10-second cycle) ───────────────────
P_DISCHARGE = 0.004        # occupied -> cleaning (~4.2 min avg, scaled by discharge curve)
P_CLEAN_DONE = 0.005       # cleaning -> available (~33 min avg)
P_BLOCK = 0.0005           # available -> blocked (rare)
P_UNBLOCK = 0.003          # blocked -> available (~56 min avg)
P_TRIAGE = 0.10            # waiting -> triaged (~100s)
P_BED_ASSIGN = 0.06        # triaged -> bed assigned (~167s)
P_DISPOSITION = 0.025       # bed assigned -> final disposition (~400s)
P_OR_TURNOVER_DONE = 0.005  # turnover -> complete (~33 min avg)

# ── Blocked Bed Reasons ──────────────────────────────────────────────
# Source: Gemini Sec 3.4 — "Awaiting Placement" is #1 real-world reason
BLOCKED_REASONS = [
    "Maintenance",
    "Staffing Shortage",
    "Infection Control",
    "Equipment Failure",
    "Awaiting Placement (SNF/Rehab)",
    "Discharge Planning Delay",
]

# ── Anomaly Thresholds ───────────────────────────────────────────────
OCCUPANCY_CRITICAL = 90     # percentage
OCCUPANCY_WARNING = 85
ED_WAIT_HIGH_MIN = 60       # minutes
BOARDING_CRITICAL_HOURS = 4  # Joint Commission target
BOARDING_SURGE_COUNT = 5
LWBS_WARNING_PCT = 3.0
LWBS_CRITICAL_PCT = 5.0
OR_OVERRUN_THRESHOLD_MIN = 30
BLOCKED_BEDS_HIGH_PCT = 10.0

# ── Department Definitions ───────────────────────────────────────────
DEPARTMENTS = [
    {"dept_id": "ED",      "name": "Emergency Department",  "floor": 1, "total_beds": 30},
    {"dept_id": "OR",      "name": "Operating Rooms",       "floor": 1, "total_beds": 8},
    {"dept_id": "ICU",     "name": "Intensive Care Unit",   "floor": 2, "total_beds": 20},
    {"dept_id": "TELE",    "name": "Telemetry",             "floor": 3, "total_beds": 24},
    {"dept_id": "MEDSURG", "name": "Medical / Surgical",    "floor": 3, "total_beds": 40},
    {"dept_id": "PEDS",    "name": "Pediatrics",            "floor": 4, "total_beds": 16},
]

INPATIENT_DEPTS = ["ICU", "TELE", "MEDSURG", "PEDS"]

# ── Occupancy Targets (for seed function) ────────────────────────────
# Calibrated for demo impact: hospital execs expect 85-95% occupancy.
# Using upper-normal range to show a "busy but not critical" baseline
# that the surge button can push into CONSTRAINED/CRITICAL.
DEPT_OCCUPANCY_TARGET = {
    "ICU": 0.82, "TELE": 0.78, "MEDSURG": 0.75, "PEDS": 0.60, "ED": 0.85,
}

# ── Procedure Types & Surgeon Specialties ────────────────────────────
PROCEDURE_TYPES = [
    "General Surgery", "Orthopedic", "Cardiac", "Neuro",
    "GI / Endoscopy", "Urology", "Plastics", "Thoracic Surgery",
]

SURGEON_SPECIALTIES = [
    "General", "Orthopedics", "Cardiothoracic", "Neurosurgery",
    "Gastroenterology", "Urology", "Plastic Surgery", "Thoracic Surgery",
]

# ── System Status Thresholds ─────────────────────────────────────────
# For the header System Status badge: Normal / Constrained / Critical
SYSTEM_STATUS_THRESHOLDS = {
    "normal_max_occupancy": 80,
    "normal_max_ed_wait": 30,
    "constrained_max_occupancy": 90,
    "constrained_max_ed_wait": 60,
    "constrained_max_boarders": 5,
    # Anything above constrained = critical
}
