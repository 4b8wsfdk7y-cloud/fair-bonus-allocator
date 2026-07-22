"""Ground-truth validation tests for the bonus allocator.

Methodology (borrowed from Jott2121/compensation-equity-analysis):
    1. Generate a synthetic config with KNOWN parameters.
    2. Run the pipeline against the synthetic config.
    3. Assert that recovered values match injected ground truth within
       tight confidence intervals.
    4. Run null-effect sanity checks: when no gap is injected, the
       pipeline should return a non-significant result.

Each test below corresponds to one validation assertion.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, Department, PoolConfig
from sensitivity import compute_sensitivity, monte_carlo_profit
from tiers import calibrate_tiers
from allocator import allocate


# ---------------------------------------------------------------------------
# Fixtures: synthetic configs with known ground truth
# ---------------------------------------------------------------------------

def _make_synthetic_config(
    n_departments: int = 4,
    pool_total: float = 1_000_000,
    profit_target: float = 20_000_000,
    profit_baseline: float = 17_000_000,
    seed: int = 42,
) -> Config:
    """Generate a Config with KNOWN β values for ground-truth testing.

    We build departments so that β × ΔKPI_stretch is on the same order
    as profit_gap / n_departments, while keeping stretch/baseline ratio
    reasonable (1.1–1.5×). This ensures the achievement multiplier
    framework (1.0 = baseline) maps meaningfully into tier progress.

    Construction: pick baseline and stretch_ratio, then solve β from
    the target impact requirement.
    """
    rng = np.random.default_rng(seed)
    profit_gap = profit_target - profit_baseline
    departments = []
    for i in range(n_departments):
        # Target impact: each dept contributes a fraction of the profit gap.
        target_impact = profit_gap * rng.uniform(0.3, 1.0) / n_departments * 4
        # Pick baseline and a reasonable stretch ratio (10-50% above baseline).
        baseline = float(rng.uniform(100, 1000))
        stretch_ratio = float(rng.uniform(1.1, 1.5))
        stretch = baseline * stretch_ratio
        delta_kpi = stretch - baseline
        # Solve β = target_impact / delta_kpi so the impact is correct.
        beta = target_impact / delta_kpi
        departments.append(
            Department(
                name=f"dept_{i}",
                kpi_baseline=baseline,
                kpi_stretch=stretch,
                beta=beta,
                headcount=10,
            )
        )
    pool = PoolConfig(
        pool_total=pool_total,
        profit_target=profit_target,
        profit_baseline=profit_baseline,
        theta_a=0.15,
        theta_s=0.30,
        divisions=1000,
        min_pool_share=0.02,
    )
    return Config(pool=pool, departments=departments)


# ---------------------------------------------------------------------------
# Layer 1 — β recovery via OLS regression on simulated scenarios
# ---------------------------------------------------------------------------

def test_layer1_beta_recovery():
    """β coefficients must be recoverable from Monte Carlo profit scenarios.

    Mirrors comp-equity's "injected 5% gender gap, recovered 5.12%" test.
    We inject known βs, run Monte Carlo, then OLS-regress profit on KPI
    deltas. The recovered coefficients must match the injected βs.
    """
    config = _make_synthetic_config(n_departments=5, seed=42)
    mc = monte_carlo_profit(config, n_scenarios=2000, seed=42)

    # Build design matrix: profit = const + Σ β_d × ΔKPI_d
    X_list = []
    for d in config.departments:
        if d.kpi_baseline > 0:
            delta = mc[f"kpi_{d.name}"] * d.kpi_baseline - d.kpi_baseline
        else:
            delta = (mc[f"kpi_{d.name}"] - 1.0) * d.kpi_stretch
        X_list.append(delta.values)
    X = np.column_stack(X_list)
    X_with_const = np.column_stack([np.ones(len(X)), X])
    coefs, *_ = np.linalg.lstsq(X_with_const, mc["profit"].values, rcond=None)

    print("\nβ recovery (Layer 1):")
    for i, d in enumerate(config.departments):
        injected = d.beta
        recovered = coefs[i + 1]
        rel_error = abs(recovered - injected) / max(abs(injected), 1e-9)
        print(f"  {d.name}: injected={injected:.4f}, recovered={recovered:.4f}, err={rel_error:.2%}")
        assert rel_error < 0.01, f"β recovery failed for {d.name}: {recovered} vs {injected}"


def test_layer1_stretch_impact_sums_to_known_value():
    """stretch_impact for each department must equal β × ΔKPI_stretch."""
    config = _make_synthetic_config(n_departments=4, seed=7)
    sensitivity = compute_sensitivity(config)

    for _, row in sensitivity.df.iterrows():
        dept = next(d for d in config.departments if d.name == row["department"])
        expected = dept.beta * (dept.kpi_stretch - dept.kpi_baseline)
        actual = row["stretch_impact"]
        rel_error = abs(actual - expected) / max(abs(expected), 1e-9)
        assert rel_error < 1e-6, f"stretch_impact wrong for {dept.name}: {actual} vs {expected}"


def test_layer1_profit_gap_arithmetic():
    """profit_gap must equal profit_target - profit_baseline."""
    config = _make_synthetic_config()
    sensitivity = compute_sensitivity(config)
    expected = config.pool.profit_target - config.pool.profit_baseline
    assert abs(sensitivity.profit_gap - expected) < 1e-6


# ---------------------------------------------------------------------------
# Layer 2 — Tier calibration formula verification
# ---------------------------------------------------------------------------

def test_layer2_tier_formula():
    """A/S tier KPIs must satisfy the calibration formula exactly.

        β_d × (KPI_d^A − baseline) = θ_A × profit_gap
        β_d × (KPI_d^S − baseline) = θ_S × profit_gap

    (except when capped by kpi_stretch)
    """
    config = _make_synthetic_config(n_departments=5, seed=11)
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)

    for _, row in tiers.df.iterrows():
        dept = next(d for d in config.departments if d.name == row["department"])
        # Expected (uncapped) values
        expected_a = dept.kpi_baseline + config.pool.theta_a * sensitivity.profit_gap / dept.beta
        expected_s = dept.kpi_baseline + config.pool.theta_s * sensitivity.profit_gap / dept.beta

        if row["capped"]:
            # S tier should be capped at kpi_stretch
            assert abs(row["kpi_s"] - dept.kpi_stretch) < 1e-6, f"{dept.name} S cap not applied"
            # A tier should be min(expected_a, kpi_s)
            assert row["kpi_a"] <= row["kpi_s"] + 1e-9
        else:
            assert abs(row["kpi_a"] - expected_a) < 1e-6, f"{dept.name} A formula wrong"
            assert abs(row["kpi_s"] - expected_s) < 1e-6, f"{dept.name} S formula wrong"


def test_layer2_tier_shares_sum_correctly():
    """Each uncapped department's tier_a_share must equal θ_A exactly.

    The calibration formula is:
        β_d × (KPI_d^A − baseline) = θ_A × profit_gap
    Dividing both sides by profit_gap:
        tier_a_share = θ_A
    This holds for every uncapped department, regardless of β.
    """
    # Make a config where some departments have different β but none capped.
    # To avoid capping, stretch KPI must be large enough to accommodate θ_S × gap / β.
    # With profit_gap = 1M, θ_S = 0.30, β = [1, 2, 3]:
    #   delta_s = 0.3M / β = [300K, 150K, 100K]
    # Make stretch at least baseline + delta_s for each.
    config = Config(
        pool=PoolConfig(
            pool_total=1_000_000,
            profit_target=20_000_000,
            profit_baseline=19_000_000,  # gap = 1M
            theta_a=0.15,
            theta_s=0.30,
        ),
        departments=[
            Department(name="d0", kpi_baseline=100, kpi_stretch=500_000, beta=1.0),
            Department(name="d1", kpi_baseline=100, kpi_stretch=500_000, beta=2.0),
            Department(name="d2", kpi_baseline=100, kpi_stretch=500_000, beta=3.0),
        ],
    )
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)

    for _, row in tiers.df.iterrows():
        assert not row["capped"], f"{row['department']} should not be capped"
        assert abs(row["tier_a_share"] - config.pool.theta_a) < 1e-6, (
            f"{row['department']} tier_a_share = {row['tier_a_share']}, expected {config.pool.theta_a}"
        )
        assert abs(row["tier_s_share"] - config.pool.theta_s) < 1e-6, (
            f"{row['department']} tier_s_share = {row['tier_s_share']}, expected {config.pool.theta_s}"
        )


def test_layer2_capped_when_stretch_too_low():
    """If stretch KPI < formula-implied S tier, capped=True."""
    config = Config(
        pool=PoolConfig(
            pool_total=1_000_000,
            profit_target=20_000_000,
            profit_baseline=10_000_000,  # huge gap → S tier would be unreachable
            theta_a=0.15,
            theta_s=0.30,
        ),
        departments=[
            Department(name="d0", kpi_baseline=100, kpi_stretch=110, beta=1.0),
        ],
    )
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    assert tiers.df.iloc[0]["capped"] is True or tiers.df.iloc[0]["capped"] == True


# ---------------------------------------------------------------------------
# Layer 3 — Knapsack fairness properties
# ---------------------------------------------------------------------------

def test_layer3_total_allocation_respects_pool():
    """Total allocated must not exceed pool_total."""
    config = _make_synthetic_config(n_departments=4, seed=3)
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {d.name: 1.2 for d in config.departments}
    result = allocate(config, sensitivity, tiers, achievements=achievements)
    assert result.total_allocated <= config.pool.pool_total + 1e-6
    # Must have allocated at least 90% of the pool (most should go out).
    assert result.total_allocated >= config.pool.pool_total * 0.9


def test_layer3_equal_contribution_equal_allocation():
    """Two departments with identical β, baseline, stretch, achievement
    must receive identical allocations.

    This is the core cross-department fairness guarantee.
    """
    config = Config(
        pool=PoolConfig(
            pool_total=1_000_000,
            profit_target=20_000_000,
            profit_baseline=17_000_000,
            theta_a=0.15,
            theta_s=0.30,
            min_pool_share=0.0,
        ),
        departments=[
            Department(name="d_clone_a", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
            Department(name="d_clone_b", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
        ],
    )
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {"d_clone_a": 1.2, "d_clone_b": 1.2}
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    a = result.df[result.df["department"] == "d_clone_a"]["allocated"].iloc[0]
    b = result.df[result.df["department"] == "d_clone_b"]["allocated"].iloc[0]
    assert abs(a - b) < 1e-6, f"Identical departments got different allocations: {a} vs {b}"


def test_layer3_higher_achievement_gets_more():
    """A department with higher achievement must not get less than one
    with lower achievement (all else equal)."""
    config = Config(
        pool=PoolConfig(
            pool_total=1_000_000,
            profit_target=20_000_000,
            profit_baseline=17_000_000,
            theta_a=0.15,
            theta_s=0.30,
            min_pool_share=0.0,
        ),
        departments=[
            Department(name="d_low", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
            Department(name="d_high", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
        ],
    )
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {"d_low": 1.0, "d_high": 1.4}
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    high = result.df[result.df["department"] == "d_high"]["allocated"].iloc[0]
    low = result.df[result.df["department"] == "d_low"]["allocated"].iloc[0]
    assert high > low, f"Higher achiever should get more: high={high}, low={low}"


def test_layer3_c_tier_gets_only_floor():
    """A department below baseline (C tier) should only get the floor,
    not compete for additional pool."""
    config = Config(
        pool=PoolConfig(
            pool_total=1_000_000,
            profit_target=20_000_000,
            profit_baseline=17_000_000,
            theta_a=0.15,
            theta_s=0.30,
            min_pool_share=0.05,
        ),
        departments=[
            Department(name="d_c", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
            Department(name="d_a", kpi_baseline=100, kpi_stretch=150, beta=2.0, headcount=10),
        ],
    )
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {"d_c": 0.8, "d_a": 1.3}  # d_c underperforms, d_a overperforms
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    c_alloc = result.df[result.df["department"] == "d_c"]["allocated"].iloc[0]
    floor = config.pool.min_pool_share * config.pool.pool_total
    assert abs(c_alloc - floor) < 1e-6, f"C-tier should get only floor: got {c_alloc}, expected {floor}"


def test_layer3_allocation_within_cap():
    """No department should receive more than its cap."""
    config = _make_synthetic_config(n_departments=6, seed=99)
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {d.name: 1.5 for d in config.departments}  # all S tier
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    for _, row in result.df.iterrows():
        assert row["allocated"] <= row["cap"] + 1e-6, (
            f"{row['department']} exceeded cap: {row['allocated']} > {row['cap']}"
        )


# ---------------------------------------------------------------------------
# Null-effect sanity check (mirrors comp-equity's zero-gap test)
# ---------------------------------------------------------------------------

def test_null_effect_zero_achievement_variance():
    """When all departments have identical achievement (1.0), the allocation
    should be proportional to S-share only — no department should dominate
    just by virtue of being first in the list.

    This is our analogue of comp-equity's "inject zero gap, recover ~0%".
    """
    config = _make_synthetic_config(n_departments=4, seed=5)
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {d.name: 1.0 for d in config.departments}
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    # Compare to pure proportional-by-S-share allocation.
    s_shares = tiers.df.set_index("department")["tier_s_share"]
    s_shares = s_shares / s_shares.sum()
    for _, row in result.df.iterrows():
        expected = s_shares[row["department"]] * config.pool.pool_total
        actual = row["allocated"]
        # Allow 10% tolerance due to knapsack discretization + floors.
        rel_err = abs(actual - expected) / expected if expected > 0 else 0
        assert rel_err < 0.20, (
            f"{row['department']}: expected ~{expected:.0f}, got {actual:.0f} (err {rel_err:.1%})"
        )


# ---------------------------------------------------------------------------
# End-to-end: pipeline runs without error on realistic config
# ---------------------------------------------------------------------------

def test_end_to_end_realistic_config():
    """The example wind cable config must run end-to-end without error."""
    example_path = Path(__file__).resolve().parent.parent / "example_wind_cable.yaml"
    if not example_path.exists():
        pytest.skip("example_wind_cable.yaml not found")
    config = Config.from_yaml(example_path)
    sensitivity = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sensitivity)
    achievements = {d.name: 1.1 for d in config.departments}
    result = allocate(config, sensitivity, tiers, achievements=achievements)

    assert result.total_allocated > 0
    assert result.total_allocated <= config.pool.pool_total + 1e-6
    assert len(result.df) == len(config.departments)
