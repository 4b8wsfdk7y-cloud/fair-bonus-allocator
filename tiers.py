"""Layer 2 — Tier calibration.

Maps the company-level governance thresholds (θ_A, θ_S) to per-department
KPI tier lines, so that achieving the A tier in any department represents
the same marginal profit contribution.

The calibration rule:

    β_d × (KPI_d^A − KPI_d^B) = θ_A × Profit_gap
    β_d × (KPI_d^S − KPI_d^B) = θ_S × Profit_gap

Solving for KPI_d^A and KPI_d^S:

    KPI_d^A = KPI_d^B + θ_A × Profit_gap / β_d
    KPI_d^S = KPI_d^B + θ_S × Profit_gap / β_d

Departments with low β (low sensitivity) get wide tier bands — they have
to swing further to earn the same bonus weight. This is the mathematical
core of cross-department fairness: every tier transition represents the
same dollar contribution to closing the profit gap.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import Config
from sensitivity import SensitivityResult


@dataclass
class TierResult:
    df: pd.DataFrame

    def tier_for(self, dept_name: str, kpi_value: float) -> str:
        """Classify a KPI value into C / B / A / S tier."""
        row = self.df[self.df["department"] == dept_name].iloc[0]
        if kpi_value < row["kpi_baseline"]:
            return "C"
        if kpi_value < row["kpi_a"]:
            return "B"
        if kpi_value < row["kpi_s"]:
            return "A"
        return "S"


def calibrate_tiers(config: Config, sensitivity: SensitivityResult) -> TierResult:
    """Compute per-department B/A/S tier KPI lines.

    Returns DataFrame with one row per department:
        department, kpi_baseline, kpi_a, kpi_s, kpi_stretch,
        tier_a_share, tier_s_share, capped (bool)
    """
    pool = config.pool
    rows = []
    for d in config.departments:
        # Ideal A and S lines from the calibration formula.
        delta_a = pool.theta_a * sensitivity.profit_gap / d.beta
        delta_s = pool.theta_s * sensitivity.profit_gap / d.beta

        kpi_a = d.kpi_baseline + delta_a
        kpi_s = d.kpi_baseline + delta_s

        # Cap tier lines at the user-defined stretch KPI to keep them realistic.
        capped = kpi_s > d.kpi_stretch
        kpi_s = min(kpi_s, d.kpi_stretch)
        kpi_a = min(kpi_a, kpi_s)

        # Share of profit gap closed if dept hits A or S.
        share_a = d.beta * (kpi_a - d.kpi_baseline) / sensitivity.profit_gap
        share_s = d.beta * (kpi_s - d.kpi_baseline) / sensitivity.profit_gap

        rows.append(
            {
                "department": d.name,
                "kpi_baseline": d.kpi_baseline,
                "kpi_a": kpi_a,
                "kpi_s": kpi_s,
                "kpi_stretch": d.kpi_stretch,
                "tier_a_share": share_a,
                "tier_s_share": share_s,
                "capped": capped,
            }
        )

    return TierResult(df=pd.DataFrame(rows))
