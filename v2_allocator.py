"""v2 governance — reachability audit, confidence-adjusted impact, and allocator.

Implements the v2 spec written into the Feishu doc by Codex:

    BaseBonus_d = λ P · h_d / H
    Ĉ_d = β̂_d · ΔKPI_d
    SE(Ĉ_d) = |dΔKPI_d/dβ̂| · SE(β̂_d) = |ΔKPI_d| · SE(β̂_d)   (linear model)
    C_d* = ρ_d · max(0, Ĉ_d - 1.645 · SE(Ĉ_d))
    T_d = q_d · profit_gap
    a_d = C_d* / T_d
    s_d = q_d · min(max(a_d, 0), a_max)
    PerfBonus_d = (1 - λ) P · s_d / Σ s_j
    Bonus_d = BaseBonus_d + PerfBonus_d

Overflow handling: when a department hits its cap, excess (cap - Bonus_d) is
re-allocated to the remaining departments proportionally to their s_d. If all
scores are zero or all departments are frozen by quality gates, the residual
goes to a deferred pool for management disposition.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import Config, Department
from sensitivity import SensitivityResult


# One-sided 95% lower-bound z-score.
Z_95_ONE_SIDED = 1.645


@dataclass
class V2AllocationResult:
    df: pd.DataFrame
    pool_total: float
    base_pool: float
    perf_pool: float
    deferred_pool: float
    release_gates: dict[str, bool]

    @property
    def total_allocated(self) -> float:
        return float(self.df["bonus"].sum())

    @property
    def pool_remaining(self) -> float:
        return self.pool_total - self.total_allocated - self.deferred_pool


# ---------------------------------------------------------------------------
# Reachability audit
# ---------------------------------------------------------------------------

def reachability_audit(config: Config, sens: SensitivityResult) -> pd.DataFrame:
    """For each department, can its stretch KPI actually produce enough profit
    contribution to hit A and S tier targets?

    With v2 quotas, the per-department A/S target is q_d × θ × gap (so that
    "every department at A" closes exactly one profit gap). Without quotas,
    falls back to the v1 θ × gap target.
    """
    gap = sens.profit_gap
    quotas = resolve_quotas(config, sens)
    rows = []
    for d in config.departments:
        stretch_impact = d.beta * (d.kpi_stretch - d.kpi_baseline)
        q_d = quotas[d.name]
        a_target = q_d * config.pool.theta_a * gap
        s_target = q_d * config.pool.theta_s * gap
        rows.append({
            "department": d.name,
            "beta": d.beta,
            "stretch_impact": stretch_impact,
            "quota": q_d,
            "a_target_profit": a_target,
            "s_target_profit": s_target,
            "can_reach_a": stretch_impact >= a_target - 1e-6,
            "can_reach_s": stretch_impact >= s_target - 1e-6,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Confidence-adjusted impact
# ---------------------------------------------------------------------------

def beta_standard_error(d: Department) -> float:
    """SE(β̂) from a 95% CI. If no CI provided, returns 0 (point estimate only)."""
    if d.beta_ci_lower is None or d.beta_ci_upper is None:
        return 0.0
    return (d.beta_ci_upper - d.beta_ci_lower) / (2 * 1.96)


def actual_kpi_delta(d: Department, achievement: float) -> float:
    """Translate achievement multiplier into ΔKPI in the unit β expects."""
    if d.kpi_baseline > 0:
        return (achievement - 1.0) * d.kpi_baseline
    # baseline = 0: achievement=1.0 → at baseline (0); achievement=2.0 → at stretch
    return (achievement - 1.0) * d.kpi_stretch


def confidence_adjusted_impact(
    d: Department, achievement: float
) -> tuple[float, float, float]:
    """Returns (ĉ_hat, ĉ_lower, c_star) where:
       ĉ_hat   = point estimate of profit contribution
       ĉ_lower = one-sided 95% lower bound
       c_star  = ρ_d × max(0, ĉ_lower) — the value used for bonus attribution.
    """
    delta = actual_kpi_delta(d, achievement)
    c_hat = d.beta * delta
    se = beta_standard_error(d) * abs(delta)  # SE on impact = |ΔKPI| × SE(β)
    c_lower = c_hat - Z_95_ONE_SIDED * se
    c_star = d.beta_confidence_weight * max(0.0, c_lower)
    return c_hat, c_lower, c_star


# ---------------------------------------------------------------------------
# Quota normalization
# ---------------------------------------------------------------------------

def resolve_quotas(config: Config, sens: SensitivityResult) -> dict[str, float]:
    """Return q_d for each department. If user provided quotas, validate and use
    them. Otherwise derive from stretch_impact share."""
    if all(d.quota is not None for d in config.departments):
        return {d.name: float(d.quota) for d in config.departments}  # type: ignore[arg-type]
    total_stretch = sum(
        d.beta * (d.kpi_stretch - d.kpi_baseline) for d in config.departments
    )
    if total_stretch <= 0:
        # Degenerate: equal split.
        n = len(config.departments)
        return {d.name: 1.0 / n for d in config.departments}
    return {
        d.name: d.beta * (d.kpi_stretch - d.kpi_baseline) / total_stretch
        for d in config.departments
    }


# ---------------------------------------------------------------------------
# v2 allocator
# ---------------------------------------------------------------------------

def allocate_v2(
    config: Config,
    sens: SensitivityResult,
    achievements: dict[str, float] | None = None,
    caps: dict[str, float] | None = None,
) -> V2AllocationResult:
    """Run the v2 bonus allocation.

    Parameters
    ----------
    achievements : dict, optional
        Per-department KPI achievement multiplier (1.0 = baseline).
    caps : dict, optional
        Per-department cap on total bonus. If None, no cap (only pool_total bounds).
    """
    pool = config.pool
    P = pool.pool_total
    λ = pool.lambda_base_ratio
    gap = sens.profit_gap

    base_pool = λ * P
    perf_pool = (1.0 - λ) * P

    # Headcount normalization for base pool.
    H = sum(d.headcount for d in config.departments)
    if H == 0:
        raise ValueError("total headcount must be > 0")

    quotas = resolve_quotas(config, sens)

    # Compute per-department raw scores.
    rows = []
    for d in config.departments:
        ach = achievements.get(d.name, 1.0) if achievements else 1.0
        c_hat, c_lower, c_star = confidence_adjusted_impact(d, ach)
        target = quotas[d.name] * gap
        a = c_star / target if target > 0 else 0.0
        a_clipped = min(max(a, 0.0), pool.a_max)
        s = quotas[d.name] * a_clipped

        base_bonus = base_pool * d.headcount / H
        rows.append({
            "department": d.name,
            "headcount": d.headcount,
            "achievement": ach,
            "quota": quotas[d.name],
            "c_hat": c_hat,
            "c_lower_95": c_lower,
            "c_star": c_star,
            "target": target,
            "achievement_rate": a,
            "achievement_rate_clipped": a_clipped,
            "score": s,
            "base_bonus": base_bonus,
            "perf_bonus": 0.0,  # filled after normalization
            "bonus": 0.0,      # filled after cap handling
        })

    df = pd.DataFrame(rows)

    # Initial performance bonus = perf_pool × s_d / Σ s_j.
    total_score = df["score"].sum()
    if total_score > 0:
        df["perf_bonus"] = df["score"] / total_score * perf_pool
    else:
        # No performance signal — entire perf pool goes to deferred.
        pass

    df["bonus"] = df["base_bonus"] + df["perf_bonus"]

    # Cap handling. Caps apply to TOTAL bonus (base + perf), but only the
    # perf portion is reducible — base_bonus is a rigid headcount-based
    # entitlement (L3 invariant: same headcount → same base bonus). So:
    #
    #   - If base_bonus alone > cap: config error, but we don't silently
    #     clip+redistribute base (that would break L3). Instead, leave
    #     base untouched, set perf=0 for that dept, carry the residual as
    #     deferred. Release gate `pool_utilization_90_to_100` will catch it.
    #   - If base_bonus + perf_bonus > cap: clip perf down to (cap - base),
    #     redistribute the freed perf pool to other depts proportionally
    #     to their score. Base untouched.
    if caps:
        for _ in range(10):  # bounded iterations to prevent infinite loops
            excess = 0.0
            for idx, row in df.iterrows():
                cap = caps.get(row["department"], float("inf"))
                if row["bonus"] > cap + 1e-6:
                    # Reduce perf_bonus, not bonus. Never touch base_bonus.
                    reduction = row["bonus"] - cap
                    # perf_bonus can't go below 0; anything beyond is
                    # base-overflow → carried to deferred, not redistributed.
                    perf_reduction = min(reduction, row["perf_bonus"])
                    if perf_reduction <= 0:
                        # base_bonus itself exceeds cap; can't reduce perf.
                        # Mark score=0 so this dept won't absorb more cascade
                        # overflow, and leave bonus as-is. Residual handled
                        # by release gate.
                        df.at[idx, "score"] = 0.0
                        continue
                    df.at[idx, "perf_bonus"] -= perf_reduction
                    df.at[idx, "bonus"] = df.at[idx, "base_bonus"] + df.at[idx, "perf_bonus"]
                    df.at[idx, "score"] = 0.0  # remove from future redistribution
                    excess += perf_reduction
            if excess <= 1e-6:
                break
            remaining = df[df["score"] > 0]
            if remaining.empty or remaining["score"].sum() == 0:
                break
            shares = remaining["score"] / remaining["score"].sum()
            for idx in remaining.index:
                df.at[idx, "perf_bonus"] += excess * shares[idx]
                df.at[idx, "bonus"] = df.at[idx, "base_bonus"] + df.at[idx, "perf_bonus"]

    # Deferred pool = whatever couldn't be distributed.
    allocated = df["bonus"].sum()
    if pool.deferred_pool_enabled:
        deferred = max(P - allocated, 0.0)
    else:
        # Force allocation to exactly P. Base pool is a rigid headcount-based
        # entitlement — scaling it would break "same headcount → same base
        # bonus" (the L3 fairness invariant). Only the perf pool is scalable.
        # Strategy: if perf pool over-allocated, scale perf down; if under-
        # allocated, scale perf up to absorb the slack. Base pool untouched.
        perf_sum = df["perf_bonus"].sum()
        target_perf = P - base_pool  # whatever's left after rigid base pool
        if perf_sum > 1e-9:
            perf_scale = max(target_perf, 0.0) / perf_sum
            df["perf_bonus"] *= perf_scale
        else:
            # No perf signal to scale; leave base as-is. If base_pool < P,
            # there's a residual we can't distribute without violating the
            # base-pool rigidity. Carry it as deferred (even though
            # deferred_pool_enabled=False) to avoid overpaying.
            pass
        df["bonus"] = df["base_bonus"] + df["perf_bonus"]
        # In the edge case where base_pool alone exceeds P (misconfiguration),
        # we have to scale base down to fit — there's no other way. Flag it.
        if df["base_bonus"].sum() > P:
            base_scale = P / df["base_bonus"].sum()
            df["base_bonus"] *= base_scale
            df["bonus"] = df["base_bonus"] + df["perf_bonus"]
        deferred = max(P - df["bonus"].sum(), 0.0)

    # Release gates (audit flags).
    release_gates = _evaluate_release_gates(df, config, sens, deferred)

    # Reorder columns for readability.
    df = df[[
        "department", "headcount", "achievement", "quota",
        "c_hat", "c_lower_95", "c_star", "target",
        "achievement_rate", "achievement_rate_clipped",
        "score", "base_bonus", "perf_bonus", "bonus",
    ]].sort_values("bonus", ascending=False).reset_index(drop=True)

    return V2AllocationResult(
        df=df,
        pool_total=P,
        base_pool=base_pool,
        perf_pool=perf_pool,
        deferred_pool=deferred,
        release_gates=release_gates,
    )


def _evaluate_release_gates(
    df: pd.DataFrame, config: Config, sens: SensitivityResult, deferred: float
) -> dict[str, bool]:
    """Release gates: each must be true before v2 result can be paid out."""
    return {
        # Gate 1: actually-allocated bonus within [90%, 100%] of pool.
        # Deferred pool doesn't count — it's the residual we FAILED to
        # distribute. Previously this gate checked (allocated + deferred),
        # which always equals P by construction when deferred_pool_enabled,
        # making the 90% floor meaningless. Now checks `allocated` only.
        "pool_utilization_90_to_100": (
            0.9 * config.pool.pool_total
            <= df["bonus"].sum()
            <= 1.0 * config.pool.pool_total + 1e-6
        ),
        # Gate 2: every department with β confidence weight > 0 has a non-NaN bonus.
        "no_nan_bonus": bool(df["bonus"].notna().all()),
        # Gate 3: no department gets negative bonus.
        "no_negative_bonus": bool((df["bonus"] >= -1e-9).all()),
        # Gate 4: every department with achievement ≥ 1.0 has c_star ≥ 0.
        "achievers_have_nonneg_c_star": bool(
            (df.loc[df["achievement"] >= 1.0, "c_star"] >= -1e-9).all()
        ),
        # Gate 5: quotas are normalized (sum = 1).
        "quotas_sum_to_one": abs(df["quota"].sum() - 1.0) < 1e-6,
        # Gate 6: same confidence-adjusted contribution → same perf bonus
        # (verified by tests; here we check monotonicity in c_star among equal-quota depts).
        "monotonic_in_c_star_within_quota": _check_monotonicity(df),
    }


def _check_monotonicity(df: pd.DataFrame) -> bool:
    """Within equal-quota groups, perf_bonus should be monotonic in c_star."""
    for q, group in df.groupby("quota"):
        if len(group) <= 1:
            continue
        sorted_by_c = group.sort_values("c_star")
        bonuses = sorted_by_c["perf_bonus"].values
        if not all(bonuses[i] <= bonuses[i + 1] + 1e-9 for i in range(len(bonuses) - 1)):
            return False
    return True
