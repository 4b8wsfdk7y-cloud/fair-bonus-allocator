"""Layer 1 — Department profit sensitivity modeling.

Given KPI elasticities (beta) and the company profit model, this module
produces the sensitivity matrix used by Layers 2 and 3.

The simplest model is linear in KPI deltas:

    Profit = Profit_baseline + Σ_d β_d × (KPI_d − baseline_d)

A more realistic log-linear form is also supported:

    log(Profit) = log(Profit_baseline) + Σ_d β_d × log(KPI_d / baseline_d)

Both forms recover the same β_d in the small-delta limit.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import Config, Department


@dataclass
class SensitivityResult:
    """Per-department sensitivity to company profit."""

    df: pd.DataFrame
    # Profit gap that needs to be closed by stretch KPI achievements.
    profit_gap: float

    @property
    def betas(self) -> dict[str, float]:
        return dict(zip(self.df["department"], self.df["beta"]))

    @property
    def stretch_impact(self) -> dict[str, float]:
        """Profit contribution if department moves from baseline to stretch."""
        return dict(zip(self.df["department"], self.df["stretch_impact"]))


def compute_sensitivity(config: Config, form: str = "linear") -> SensitivityResult:
    """Compute per-department profit sensitivity.

    Parameters
    ----------
    config : Config
        Loaded YAML configuration.
    form : str
        "linear" or "log" — which profit model form to use.

    Returns
    -------
    SensitivityResult
        Per-department β, stretch impact, and profit gap.
    """
    pool = config.pool
    profit_gap = pool.profit_target - pool.profit_baseline

    rows = []
    for d in config.departments:
        # Profit impact when department hits stretch (B → S).
        if form == "log":
            ratio = d.kpi_stretch / d.kpi_baseline
            stretch_impact = pool.profit_baseline * d.beta * np.log(ratio)
        else:
            delta = d.kpi_stretch - d.kpi_baseline
            stretch_impact = d.beta * delta

        rows.append(
            {
                "department": d.name,
                "beta": d.beta,
                "kpi_baseline": d.kpi_baseline,
                "kpi_stretch": d.kpi_stretch,
                "stretch_impact": stretch_impact,
                # Share of total stretch contribution (Layer 3 weight hint).
                "stretch_share": 0.0,  # filled below
            }
        )

    df = pd.DataFrame(rows)
    total_stretch = df["stretch_impact"].sum()
    # Normalized stretch share — how much of the achievable upside this dept owns.
    df["stretch_share"] = df["stretch_impact"] / total_stretch if total_stretch > 0 else 0.0

    return SensitivityResult(df=df, profit_gap=profit_gap)


def monte_carlo_profit(
    config: Config,
    n_scenarios: int = 1000,
    seed: int = 42,
    achievement_range: tuple[float, float] = (0.8, 1.5),
) -> pd.DataFrame:
    """Run Monte Carlo across random department KPI achievements.

    Used for sensitivity regression: regress simulated profit on each
    department's KPI to validate β values.

    Returns DataFrame with one row per scenario:
        scenario_id, kpi_<dept>..., profit
    """
    rng = np.random.default_rng(seed)
    lo, hi = achievement_range
    n_deps = len(config.departments)

    # Each department's KPI achievement as a multiplier of baseline.
    achievements = rng.uniform(lo, hi, size=(n_scenarios, n_deps))

    profit = np.full(n_scenarios, config.pool.profit_baseline)
    for j, d in enumerate(config.departments):
        # For baseline=0 departments, achievements can't multiply baseline;
        # treat the achievement multiplier as a fraction of stretch.
        if d.kpi_baseline > 0:
            actual_kpi = achievements[:, j] * d.kpi_baseline
            delta = actual_kpi - d.kpi_baseline
        else:
            # Stretch-anchored: achievement=1.0 → at baseline (0), achievement=2.0 → at stretch.
            actual_kpi = (achievements[:, j] - 1.0) * d.kpi_stretch
            delta = actual_kpi
        profit = profit + d.beta * delta

    cols = {f"kpi_{d.name}": achievements[:, j] for j, d in enumerate(config.departments)}
    cols["profit"] = profit
    cols["scenario_id"] = np.arange(n_scenarios)
    return pd.DataFrame(cols)
