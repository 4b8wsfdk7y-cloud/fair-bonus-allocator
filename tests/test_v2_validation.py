"""v2 governance validation tests.

Each test maps to one release gate / formula invariant from the v2 spec
written into the Feishu doc by Codex.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, Department, PoolConfig
from sensitivity import compute_sensitivity
from v2_allocator import (
    allocate_v2,
    beta_standard_error,
    confidence_adjusted_impact,
    reachability_audit,
    resolve_quotas,
    Z_95_ONE_SIDED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _v2_config(
    achievements="uniform",
    with_cis: bool = True,
    with_quotas: bool = True,
) -> Config:
    """Build a v2 test config with 4 departments."""
    pool = PoolConfig(
        pool_total=1_000_000,
        profit_target=20_000_000,
        profit_baseline=17_000_000,
        min_pool_share=0.02,
        lambda_base_ratio=0.3,
        a_max=1.5,
        deferred_pool_enabled=True,
    )
    deps = [
        Department(
            name="d_alpha", kpi_baseline=100, kpi_stretch=120, beta=1000,
            headcount=10,
            beta_ci_lower=900 if with_cis else None,
            beta_ci_upper=1100 if with_cis else None,
            beta_confidence_weight=0.9 if with_cis else 1.0,
            beta_source="regression" if with_cis else "unspecified",
            quota=0.25 if with_quotas else None,
        ),
        Department(
            name="d_beta", kpi_baseline=100, kpi_stretch=120, beta=1000,
            headcount=20,
            beta_ci_lower=900 if with_cis else None,
            beta_ci_upper=1100 if with_cis else None,
            beta_confidence_weight=0.9 if with_cis else 1.0,
            beta_source="regression" if with_cis else "unspecified",
            quota=0.25 if with_quotas else None,
        ),
        Department(
            name="d_gamma", kpi_baseline=100, kpi_stretch=120, beta=1000,
            headcount=10,
            beta_ci_lower=900 if with_cis else None,
            beta_ci_upper=1100 if with_cis else None,
            beta_confidence_weight=0.9 if with_cis else 1.0,
            beta_source="regression" if with_cis else "unspecified",
            quota=0.25 if with_quotas else None,
        ),
        Department(
            name="d_delta", kpi_baseline=100, kpi_stretch=120, beta=1000,
            headcount=10,
            beta_ci_lower=900 if with_cis else None,
            beta_ci_upper=1100 if with_cis else None,
            beta_confidence_weight=0.9 if with_cis else 1.0,
            beta_source="regression" if with_cis else "unspecified",
            quota=0.25 if with_quotas else None,
        ),
    ]
    return Config(pool=pool, departments=deps)


# ---------------------------------------------------------------------------
# Quota normalization
# ---------------------------------------------------------------------------

def test_v2_quotas_sum_to_one_when_explicit():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    q = resolve_quotas(cfg, sens)
    assert sum(q.values()) == pytest.approx(1.0, abs=1e-9)
    assert q["d_alpha"] == 0.25


def test_v2_quotas_derived_from_stretch_when_not_set():
    cfg = _v2_config(with_quotas=False)
    sens = compute_sensitivity(cfg)
    q = resolve_quotas(cfg, sens)
    assert sum(q.values()) == pytest.approx(1.0, abs=1e-9)
    # All 4 depts have identical stretch_impact → equal shares.
    for v in q.values():
        assert v == pytest.approx(0.25, abs=1e-9)


def test_v2_config_rejects_partial_quotas():
    """If quota is set on some but not all departments, Config must reject."""
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000)
    deps = [
        Department(name="a", kpi_baseline=100, kpi_stretch=120, beta=1.0, quota=0.5),
        Department(name="b", kpi_baseline=100, kpi_stretch=120, beta=1.0),  # no quota
    ]
    with pytest.raises(ValueError, match="must be set on ALL"):
        Config(pool=pool, departments=deps).validate_v2()


def test_v2_config_rejects_non_normal_quotas():
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000)
    deps = [
        Department(name="a", kpi_baseline=100, kpi_stretch=120, beta=1.0, quota=0.5),
        Department(name="b", kpi_baseline=100, kpi_stretch=120, beta=1.0, quota=0.7),
    ]
    with pytest.raises(ValueError, match="sum to 1.0"):
        Config(pool=pool, departments=deps).validate_v2()


# ---------------------------------------------------------------------------
# Confidence-adjusted impact
# ---------------------------------------------------------------------------

def test_v2_confidence_lower_bound_uses_one_sided_z():
    """C_lower = β̂·ΔKPI - 1.645 · |ΔKPI| · SE(β̂)."""
    cfg = _v2_config()
    dept = cfg.departments[0]
    # Achievement = 1.5 → ΔKPI = 50 (out of baseline=100).
    ach = 1.5
    delta = (ach - 1.0) * dept.kpi_baseline
    se_beta = beta_standard_error(dept)
    assert se_beta == pytest.approx((1100 - 900) / (2 * 1.96))
    c_hat, c_lower, c_star = confidence_adjusted_impact(dept, ach)
    expected_hat = dept.beta * delta
    expected_lower = expected_hat - Z_95_ONE_SIDED * abs(delta) * se_beta
    assert c_hat == pytest.approx(expected_hat)
    assert c_lower == pytest.approx(expected_lower)
    assert c_star == pytest.approx(dept.beta_confidence_weight * max(0, expected_lower))


def test_v2_c_star_zero_when_ci_lower_is_negative():
    """A negative CI bound (impact could be zero) is clipped to 0."""
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000)
    dept = Department(
        name="shaky", kpi_baseline=100, kpi_stretch=120, beta=1.0,
        beta_ci_lower=-0.5, beta_ci_upper=2.5,  # SE ≈ 0.765, huge
        beta_confidence_weight=0.5,
    )
    _, c_lower, c_star = confidence_adjusted_impact(dept, achievement=1.05)
    assert c_lower < 0
    assert c_star == 0.0


# ---------------------------------------------------------------------------
# Base + Performance pool split
# ---------------------------------------------------------------------------

def test_v2_base_pool_split_by_headcount():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"d_alpha": 1.5})
    # d_beta has 2× headcount of d_alpha → should get 2× base bonus.
    df = result.df.set_index("department")
    assert df.loc["d_beta", "base_bonus"] == pytest.approx(
        2 * df.loc["d_alpha", "base_bonus"], rel=1e-9
    )
    # Total base pool = 0.3 × 1,000,000 = 300,000.
    assert df["base_bonus"].sum() == pytest.approx(300_000, rel=1e-9)


def test_v2_same_contribution_same_perf_bonus():
    """Two departments with identical (quota, c_star) must get identical perf bonus.
    This is the core fairness invariant Codex flagged as broken in v1."""
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    # All departments identical → identical c_star at uniform achievement.
    result = allocate_v2(cfg, sens, achievements={"d_alpha": 1.2})
    df = result.df.set_index("department")
    perf_alpha = df.loc["d_alpha", "perf_bonus"]
    perf_beta = df.loc["d_beta", "perf_bonus"]
    perf_gamma = df.loc["d_gamma", "perf_bonus"]
    perf_delta = df.loc["d_delta", "perf_bonus"]
    # All have same quota=0.25 and same achievement through c_star formula.
    # alpha gets 1.2 achievement, others default to 1.0 (baseline).
    # But c_star differs because achievement differs.
    # For equal-contribution test: reset all to same achievement.
    result2 = allocate_v2(cfg, sens, achievements={
        "d_alpha": 1.2, "d_beta": 1.2, "d_gamma": 1.2, "d_delta": 1.2
    })
    df2 = result2.df.set_index("department")
    perfs = [df2.loc[n, "perf_bonus"] for n in ["d_alpha", "d_beta", "d_gamma", "d_delta"]]
    assert max(perfs) - min(perfs) < 1e-6  # all equal


def test_v2_higher_achievement_more_perf_bonus():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={
        "d_alpha": 1.5, "d_beta": 1.2, "d_gamma": 1.0, "d_delta": 1.0
    })
    df = result.df.set_index("department")
    assert df.loc["d_alpha", "perf_bonus"] > df.loc["d_beta", "perf_bonus"]
    assert df.loc["d_beta", "perf_bonus"] > df.loc["d_gamma", "perf_bonus"]
    assert df.loc["d_gamma", "perf_bonus"] == pytest.approx(
        df.loc["d_delta", "perf_bonus"]
    )


# ---------------------------------------------------------------------------
# Pool utilization & deferred pool
# ---------------------------------------------------------------------------

def test_v2_pool_not_exceeded():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"d_alpha": 2.0})
    assert result.total_allocated + result.deferred_pool <= result.pool_total + 1e-6


def test_v2_zero_scores_defer_perf_pool():
    """When all confidence weights are 0 or all achievements are at baseline,
    perf pool must be deferred (or distributed as base pool only)."""
    pool = PoolConfig(
        pool_total=1_000_000, profit_target=20_000_000, profit_baseline=17_000_000,
        lambda_base_ratio=0.3, deferred_pool_enabled=True,
    )
    deps = [
        Department(name=f"d{i}", kpi_baseline=100, kpi_stretch=120, beta=1000,
                   headcount=10, beta_confidence_weight=0.0, quota=0.25)
        for i in range(4)
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={f"d{i}": 1.5 for i in range(4)})
    # ρ_d = 0 → c_star = 0 → no perf bonus. Entire 70% perf pool deferred.
    assert result.df["perf_bonus"].sum() == pytest.approx(0, abs=1e-9)
    assert result.deferred_pool == pytest.approx(700_000, rel=1e-9)


# ---------------------------------------------------------------------------
# Release gates
# ---------------------------------------------------------------------------

def test_v2_release_gates_pass_on_normal_run():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"d_alpha": 1.3, "d_beta": 1.2})
    for gate, ok in result.release_gates.items():
        assert ok, f"release gate '{gate}' failed"


# ---------------------------------------------------------------------------
# Overflow redistribution
# ---------------------------------------------------------------------------

def test_v2_overflow_redistribution_respects_cap():
    cfg = _v2_config()
    sens = compute_sensitivity(cfg)
    # Set a cap so d_alpha can't take its full bonus.
    caps = {"d_alpha": 50_000}
    result = allocate_v2(cfg, sens, achievements={"d_alpha": 2.0}, caps=caps)
    df = result.df.set_index("department")
    assert df.loc["d_alpha", "bonus"] <= 50_000 + 1e-6
    # Excess must have been redistributed to other departments.
    assert df.loc["d_beta", "bonus"] > 0
    # Total ≤ pool.
    assert df["bonus"].sum() <= result.pool_total + 1e-6


# ---------------------------------------------------------------------------
# Reachability audit
# ---------------------------------------------------------------------------

def test_v2_reachability_audit_flags_unreachable_tier():
    """A department whose stretch_impact < A-tier profit target must be flagged."""
    pool = PoolConfig(
        pool_total=1_000_000, profit_target=20_000_000, profit_baseline=17_000_000,
        theta_a=0.15, theta_s=0.30,
    )
    # profit_gap = 3M; A target = 0.45M; S target = 0.9M
    deps = [
        # Strong dept: stretch_impact = 1000 × 100 = 100k > A target? No, < 450k. NOT reach A.
        Department(name="weak", kpi_baseline=0, kpi_stretch=100, beta=1000),
        # Strong dept: stretch_impact = 1000 × 1000 = 1M > 900k → reaches S.
        Department(name="strong", kpi_baseline=0, kpi_stretch=1000, beta=1000),
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    audit = reachability_audit(cfg, sens).set_index("department")
    assert not audit.loc["weak", "can_reach_a"]
    assert not audit.loc["weak", "can_reach_s"]
    assert audit.loc["strong", "can_reach_a"]
    assert audit.loc["strong", "can_reach_s"]
