"""Layer 3 — Knapsack-based bonus pool allocation.

Adapts Enekk/merit_distributor's knapsack idea from individual employees
to departments. The pool is sliced into N divisions; each round, the
department with the highest marginal value/cost ratio wins one division.

Value function (per department, per division):

    V_d = w_d × ln(1 + (s'_d + division) / target_d)

where:
    w_d      = S-tier share × achievement multiplier
               (s_share already encodes β via tier calibration; multiplying
                by ach lets high-achieving departments bid more aggressively)
    s'_d     = current allocated amount to department d
    target_d = A-tier allocation (the "MRP" equivalent)
    division = pool_total / N

Knapsack score divides value by the winner penalty:

    KS_d = V_d / (rounds_d + 1)

The (rounds + 1) denominator is the "winner penalty" — departments that
have already won many divisions see their ratio decay, giving other
departments a chance to catch up.

Constraints:
    - Each department gets at least min_pool_share × pool_total (floor).
    - Each department gets at most floor + (full_s_cap − floor) × tier_progress
      (cap scales linearly from floor at zero achievement to full S-tier
      allocation at S-tier achievement; base_bonus cannot push past cap).
    - Departments with achievement < 1.0 (below baseline) get floor only.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log

import numpy as np
import pandas as pd

from config import Config
from sensitivity import SensitivityResult
from tiers import TierResult


@dataclass
class AllocationResult:
    df: pd.DataFrame
    pool_remaining: float
    n_rounds: int

    @property
    def total_allocated(self) -> float:
        return float(self.df["allocated"].sum())


def _value(
    current: float,
    division: float,
    target: float,
    weight: float,
) -> float:
    """Knapsack value V_d(d) for one department at one round.

    Returns negative infinity if the move is infeasible.

    The value is a weighted, log-damped function of how much closer this
    division brings the department to its target allocation:

        ratio = (current + division) / target
        V = weight × ln(1 + ratio)

    The log dampens marginal returns as the department approaches its cap.
    """
    new_total = current + division
    ratio = new_total / target
    if ratio <= 0:
        return float("-inf")

    return weight * log(1 + ratio)


def allocate(
    config: Config,
    sensitivity: SensitivityResult,
    tiers: TierResult,
    # Actual KPI achieved per department, as a fraction of baseline (1.0 = baseline).
    achievements: dict[str, float] | None = None,
) -> AllocationResult:
    """Run the knapsack allocator.

    Parameters
    ----------
    achievements : dict, optional
        Per-department KPI achievement multiplier. If None, all departments
        are assumed to hit their stretch KPI (used for cap analysis).
    """
    pool = config.pool
    division = pool.pool_total / pool.divisions

    # Per-department state.
    state = {}
    for d in config.departments:
        tier_row = tiers.df[tiers.df["department"] == d.name].iloc[0]
        # Maximum allocation at full S-tier achievement.
        s_share = tier_row["tier_s_share"] / tiers.df["tier_s_share"].sum()
        full_s_cap = max(s_share * pool.pool_total, pool.pool_total * 0.01)

        # Achievement ratio at S tier (relative to baseline).
        # For baseline>0: ach_at_s = kpi_s / kpi_baseline (e.g., 1.2 means S is 20% above baseline).
        # For baseline=0: ach_at_s = kpi_s / kpi_stretch (ach=1.0 means hit stretch = S tier).
        if d.kpi_baseline > 0:
            ach_at_s = tier_row["kpi_s"] / d.kpi_baseline
        else:
            ach_at_s = tier_row["kpi_s"] / max(d.kpi_stretch, 1.0)

        # tier_progress ∈ [0, 1]: 0 at zero achievement, 1 at S-tier achievement.
        ach = achievements.get(d.name, 1.0) if achievements else 1.0
        tier_progress = min(max(ach / max(ach_at_s, 1e-9), 0.0), 1.0)

        # Cap scales linearly: floor at zero achievement, full S cap at S-tier achievement.
        floor = pool.min_pool_share * pool.pool_total
        cap = floor + (full_s_cap - floor) * tier_progress

        # Target = MRP equivalent (A-tier allocation).
        a_share = tier_row["tier_a_share"] / tiers.df["tier_a_share"].sum()
        target = max(a_share * pool.pool_total, floor + division)

        # base_bonus is a B-tier fixed award paid out of the pool before knapsack.
        # Starting position = base_bonus + floor, but clamped to cap so a large
        # base_bonus can't push allocation above the department's ceiling.
        current = min(d.base_bonus + floor, cap)
        eligible = ach >= 1.0 if d.kpi_baseline > 0 else ach > 0

        weight = s_share * (ach if ach > 0 else 0)

        state[d.name] = {
            "current": current,
            "target": target,
            "cap": cap,
            "weight": weight,
            "rounds": 0,
            "eligible": eligible,
            "achievement": ach,
            "headcount": d.headcount,
        }

    pool_remaining = pool.pool_total - sum(s["current"] for s in state.values())

    n_rounds = 0
    while pool_remaining >= division:
        best_dept = None
        best_ks = float("-inf")
        for name, s in state.items():
            if not s["eligible"]:
                continue
            if s["current"] + division > s["cap"] + 1e-9:
                continue
            v = _value(
                current=s["current"],
                division=division,
                target=s["target"],
                weight=s["weight"],
            )
            ks = v / (s["rounds"] + 1)
            if ks > best_ks:
                best_ks = ks
                best_dept = name

        if best_dept is None:
            break

        state[best_dept]["current"] += division
        state[best_dept]["rounds"] += 1
        pool_remaining -= division
        n_rounds += 1

    rows = []
    for name, s in state.items():
        rows.append(
            {
                "department": name,
                "allocated": s["current"],
                "rounds_won": s["rounds"],
                "achievement": s["achievement"],
                "weight": s["weight"],
                "cap": s["cap"],
                "headcount": s["headcount"],
                "per_capita": s["current"] / max(s["headcount"], 1),
            }
        )

    df = pd.DataFrame(rows).sort_values("allocated", ascending=False).reset_index(drop=True)
    return AllocationResult(df=df, pool_remaining=pool_remaining, n_rounds=n_rounds)


def scenario_grid(
    config: Config,
    sensitivity: SensitivityResult,
    tiers: TierResult,
    scenarios: list[dict[str, float]],
) -> pd.DataFrame:
    """Run allocation across a list of achievement scenarios.

    Each scenario is a dict of {department_name: achievement_multiplier}.
    Returns a tidy DataFrame for Dashboard visualization.
    """
    rows = []
    for i, sc in enumerate(scenarios):
        result = allocate(config, sensitivity, tiers, achievements=sc)
        for _, r in result.df.iterrows():
            rows.append(
                {
                    "scenario_id": i,
                    "department": r["department"],
                    "allocated": r["allocated"],
                    "achievement": r["achievement"],
                    "per_capita": r["per_capita"],
                }
            )
        # Record pool-level stats.
        rows.append(
            {
                "scenario_id": i,
                "department": "__pool__",
                "allocated": result.total_allocated,
                "achievement": 1.0,
                "per_capita": 0.0,
            }
        )
    return pd.DataFrame(rows)
