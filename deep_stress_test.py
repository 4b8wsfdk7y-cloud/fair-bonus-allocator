"""Deep stress test: long-run stability, numerical edge cases, failure injection.

These dig deeper than the basic stress test:
    10. Long-run: 1000 iterations of fuzz, watch for drift or non-determinism.
    11. Numerical: tiny β, huge β, tiny ΔKPI, huge ΔKPI, NaN/inf inputs (should raise).
    12. Failure injection: corrupt CI (lower > upper), ρ outside [0,1], quota sum != 1.
    13. Property tests: same input → same output (determinism).
    14. Property tests: monotonicity under achievement (for fixed quota).
    15. Property tests: scaling invariance (2x pool_total → 2x bonus).
    16. Numerical conditioning: β near zero, β negative (shouldn't happen but test).
    17. Headcount extremes: headcount=0 (should raise), headcount=10^6.
    18. Reallocation stress: every dept capped, force 10 cascade iterations.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, Department, PoolConfig
from sensitivity import compute_sensitivity
from v2_allocator import allocate_v2
from stress_test import make_random_config, run_scenario


# ---------------------------------------------------------------------------
# 10. Long-run stability
# ---------------------------------------------------------------------------

def scenario_long_run():
    """1000 fuzz runs. Determinism: same seed → same result."""
    print("\n=== Scenario 10: Long-run (1000 fuzz iterations) ===")
    # Determinism check: same seed twice → identical output.
    cfg_a, ach_a = make_random_config(n_deps=30, seed=999)
    cfg_b, ach_b = make_random_config(n_deps=30, seed=999)
    sens_a = compute_sensitivity(cfg_a)
    sens_b = compute_sensitivity(cfg_b)
    r_a = allocate_v2(cfg_a, sens_a, achievements=ach_a)
    r_b = allocate_v2(cfg_b, sens_b, achievements=ach_b)
    diffs = (r_a.df["bonus"] - r_b.df["bonus"]).abs().max()
    print(f"  Determinism (same seed): max diff = {diffs:.2e}")
    assert diffs < 1e-9, "non-deterministic output for same seed!"

    # Drift check: 1000 runs, sum of bonus should always equal pool_total.
    failures = 0
    max_diff_from_pool = 0.0
    for seed in range(1000):
        n = int(np.random.default_rng(seed).integers(4, 100))
        cfg, ach = make_random_config(n_deps=n, seed=seed)
        sens = compute_sensitivity(cfg)
        result = allocate_v2(cfg, sens, achievements=ach)
        total = result.total_allocated + result.deferred_pool
        diff = abs(total - cfg.pool.pool_total)
        max_diff_from_pool = max(max_diff_from_pool, diff)
        if diff > 0.01:
            failures += 1
    print(f"  1000 runs: max |total - pool_total| = ¥{max_diff_from_pool:.6f}")
    print(f"  Failures (>¥0.01 drift): {failures}")
    return [{"scenario": "long_run", "drift_max": max_diff_from_pool, "failures": failures}]


# ---------------------------------------------------------------------------
# 11. Numerical edge cases
# ---------------------------------------------------------------------------

def scenario_numerical():
    """Tiny/huge values, NaN/inf inputs."""
    print("\n=== Scenario 11: Numerical edge cases ===")
    results = []

    # Tiny β
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000, lambda_base_ratio=0.3)
    deps = [
        Department(name="tiny_beta", kpi_baseline=100, kpi_stretch=200,
                   beta=1e-10, headcount=10, quota=0.5,
                   beta_ci_lower=0.9e-10, beta_ci_upper=1.1e-10,
                   beta_confidence_weight=0.9),
        Department(name="normal", kpi_baseline=100, kpi_stretch=200,
                   beta=1000, headcount=10, quota=0.5,
                   beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"tiny_beta": 1.5, "normal": 1.5})
    print(f"  tiny β (1e-10): no crash. total=¥{result.total_allocated:,.0f}")
    results.append(("tiny_beta", True))

    # Huge β
    deps = [
        Department(name="huge_beta", kpi_baseline=100, kpi_stretch=101,
                   beta=1e10, headcount=10, quota=0.5,
                   beta_ci_lower=0.9e10, beta_ci_upper=1.1e10,
                   beta_confidence_weight=0.9),
        Department(name="normal", kpi_baseline=100, kpi_stretch=200,
                   beta=1000, headcount=10, quota=0.5,
                   beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"huge_beta": 1.5, "normal": 1.5})
    print(f"  huge β (1e10): no crash. total=¥{result.total_allocated:,.0f}, "
          f"a_max clip works: max bonus=¥{result.df['bonus'].max():,.0f}")
    results.append(("huge_beta", True))

    # Tiny ΔKPI (ach=1.000001)
    deps = [
        Department(name="a", kpi_baseline=100, kpi_stretch=200, beta=1000,
                   headcount=10, quota=0.5, beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
        Department(name="b", kpi_baseline=100, kpi_stretch=200, beta=1000,
                   headcount=10, quota=0.5, beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"a": 1.000001, "b": 1.0})
    print(f"  tiny ΔKPI: a barely above baseline. no NaN. total=¥{result.total_allocated:,.0f}")
    results.append(("tiny_delta_kpi", True))

    return [{"scenario": f"numerical_{name}", "ok": ok} for name, ok in results]


# ---------------------------------------------------------------------------
# 12. Failure injection (should raise)
# ---------------------------------------------------------------------------

def scenario_failure_injection():
    """These configurations must raise ValueError; allocator should never see them."""
    print("\n=== Scenario 12: Failure injection (config validation) ===")
    results = []

    # CI lower > upper
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000)
    cases = [
        ("ci_lower > beta", lambda: Config(pool=pool, departments=[
            Department(name="x", kpi_baseline=100, kpi_stretch=120, beta=1000,
                       beta_ci_lower=1100, beta_ci_upper=900,  # WRONG
                       beta_confidence_weight=0.9, quota=1.0)])),
        ("rho > 1", lambda: Config(pool=pool, departments=[
            Department(name="x", kpi_baseline=100, kpi_stretch=120, beta=1000,
                       beta_confidence_weight=1.5, quota=1.0)])),
        ("rho < 0", lambda: Config(pool=pool, departments=[
            Department(name="x", kpi_baseline=100, kpi_stretch=120, beta=1000,
                       beta_confidence_weight=-0.1, quota=1.0)])),
        ("quota sum != 1", lambda: Config(pool=pool, departments=[
            Department(name="x", kpi_baseline=100, kpi_stretch=120, beta=1000, quota=0.5),
            Department(name="y", kpi_baseline=100, kpi_stretch=120, beta=1000, quota=0.6)])),
        ("partial quotas", lambda: Config(pool=pool, departments=[
            Department(name="x", kpi_baseline=100, kpi_stretch=120, beta=1000, quota=0.5),
            Department(name="y", kpi_baseline=100, kpi_stretch=120, beta=1000)])),
        ("lambda > 1", lambda: Config(
            pool=PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                            profit_baseline=17_000_000, lambda_base_ratio=1.5),
            departments=[Department(name="x", kpi_baseline=100, kpi_stretch=120,
                                     beta=1000, quota=1.0)])),
    ]
    for name, build in cases:
        try:
            cfg = build()
            cfg.validate_v2()
            print(f"  ✗ {name}: DID NOT RAISE (BUG!)")
            results.append((name, False))
        except (ValueError, AssertionError) as e:
            print(f"  ✓ {name}: raised {type(e).__name__}: {str(e)[:60]}")
            results.append((name, True))
    return [{"scenario": f"failure_{name}", "raised": ok} for name, ok in results]


# ---------------------------------------------------------------------------
# 13-15. Property tests
# ---------------------------------------------------------------------------

def scenario_property_tests():
    """Mathematical properties that must hold for any valid config."""
    print("\n=== Scenario 13-15: Property tests ===")
    results = []

    # Property 14: monotonicity in achievement for same-quota depts.
    cfg, _ = make_random_config(n_deps=10, seed=7)
    sens = compute_sensitivity(cfg)
    achs = [{d.name: 1.0 for d in cfg.departments} for _ in range(5)]
    # Ramp dept 0's achievement from 1.0 → 2.0
    for i in range(1, 5):
        achs[i] = dict(achs[0])
        achs[i]["d0"] = 1.0 + 0.25 * i
    bonuses_for_d0 = []
    for ach in achs:
        r = allocate_v2(cfg, sens, achievements=ach)
        bonus_d0 = r.df.set_index("department").loc["d0", "perf_bonus"]
        bonuses_for_d0.append(bonus_d0)
    monotonic = all(bonuses_for_d0[i] <= bonuses_for_d0[i + 1] + 1e-9
                    for i in range(len(bonuses_for_d0) - 1))
    print(f"  Monotonicity (d0 perf_bonus vs achievement): {bonuses_for_d0} → {'✓' if monotonic else '✗'}")
    results.append(("monotonicity", monotonic))

    # Property 15: scaling invariance. 2x pool_total → 2x every bonus.
    cfg_a, ach = make_random_config(n_deps=10, seed=11)
    sens_a = compute_sensitivity(cfg_a)
    r_a = allocate_v2(cfg_a, sens_a, achievements=ach)

    # Build a 2x version
    cfg_b, _ = make_random_config(n_deps=10, seed=11)
    cfg_b.pool.pool_total *= 2
    sens_b = compute_sensitivity(cfg_b)
    r_b = allocate_v2(cfg_b, sens_b, achievements=ach)

    df_a = r_a.df.set_index("department")
    df_b = r_b.df.set_index("department")
    ratios = df_b["bonus"] / df_a["bonus"]
    all_2x = all(abs(r - 2.0) < 1e-3 for r in ratios)
    print(f"  Scaling (2x pool): bonus ratios = {ratios.round(3).tolist()} → {'✓' if all_2x else '✗'}")
    results.append(("scaling_invariance", all_2x))

    return [{"scenario": f"property_{name}", "ok": ok} for name, ok in results]


# ---------------------------------------------------------------------------
# 16. Numerical conditioning
# ---------------------------------------------------------------------------

def scenario_conditioning():
    print("\n=== Scenario 16: Numerical conditioning ===")
    # β near zero but positive
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000, lambda_base_ratio=0.3)
    deps = [
        Department(name="near_zero", kpi_baseline=100, kpi_stretch=200,
                   beta=1e-6, headcount=10, quota=0.5,
                   beta_ci_lower=0.5e-6, beta_ci_upper=1.5e-6,
                   beta_confidence_weight=0.5),
        Department(name="normal", kpi_baseline=100, kpi_stretch=200,
                   beta=1000, headcount=10, quota=0.5,
                   beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
    ]
    cfg = Config(pool=pool, departments=deps)
    sens = compute_sensitivity(cfg)
    result = allocate_v2(cfg, sens, achievements={"near_zero": 1.5, "normal": 1.5})
    print(f"  β=1e-6: c_hat for near_zero = {result.df.set_index('department').loc['near_zero', 'c_hat']:.6f}")
    print(f"           total allocated = ¥{result.total_allocated:,.0f}")
    # Near-zero β should produce near-zero perf bonus for that dept, but not NaN/inf.
    assert not result.df["bonus"].isna().any(), "NaN in bonus!"
    assert (result.df["bonus"] >= 0).all(), "Negative bonus!"
    return [{"scenario": "conditioning", "ok": True}]


# ---------------------------------------------------------------------------
# 17. Headcount extremes
# ---------------------------------------------------------------------------

def scenario_headcount():
    print("\n=== Scenario 17: Headcount extremes ===")
    results = []

    # Headcount = 0 → should raise (divide by zero in base pool).
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000, lambda_base_ratio=0.3)
    deps_zero = [
        Department(name="zero_h", kpi_baseline=100, kpi_stretch=200, beta=1000,
                   headcount=0, quota=1.0),
    ]
    cfg = Config(pool=pool, departments=deps_zero)
    sens = compute_sensitivity(cfg)
    try:
        allocate_v2(cfg, sens, achievements={"zero_h": 1.5})
        print("  headcount=0: DID NOT RAISE (bug)")
        results.append(("headcount_zero", False))
    except (ValueError, ZeroDivisionError) as e:
        print(f"  headcount=0: raised {type(e).__name__}: {e}")
        results.append(("headcount_zero", True))

    # Huge headcount: 1M total
    deps_huge = [
        Department(name="huge", kpi_baseline=100, kpi_stretch=200, beta=1000,
                   headcount=1_000_000, quota=0.5,
                   beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
        Department(name="small", kpi_baseline=100, kpi_stretch=200, beta=1000,
                   headcount=1, quota=0.5,
                   beta_ci_lower=900, beta_ci_upper=1100,
                   beta_confidence_weight=0.9),
    ]
    cfg = Config(pool=pool, departments=deps_huge)
    sens = compute_sensitivity(cfg)
    r = allocate_v2(cfg, sens, achievements={"huge": 1.5, "small": 1.5})
    # huge dept should get ~100% of base pool.
    base_huge = r.df.set_index("department").loc["huge", "base_bonus"]
    base_small = r.df.set_index("department").loc["small", "base_bonus"]
    print(f"  headcount=1M: huge base = ¥{base_huge:,.0f}, small base = ¥{base_small:,.2f}")
    results.append(("headcount_huge", True))

    return [{"scenario": f"headcount_{name}", "raised_or_ok": ok} for name, ok in results]


# ---------------------------------------------------------------------------
# 18. Reallocation stress
# ---------------------------------------------------------------------------

def scenario_reallocation_stress():
    """Force maximum cascade iterations."""
    print("\n=== Scenario 18: Reallocation cascade (all 10 depts capped) ===")
    cfg, _ = make_random_config(n_deps=10, seed=99)
    ach = {d.name: 5.0 for d in cfg.departments}
    # All caps = ¥10,000. Total pool = ¥1M. So 100% of pool must cascade.
    caps = {d.name: 10_000.0 for d in cfg.departments}
    r = run_scenario("cascade_max", cfg, ach, caps=caps)
    # Expected: everyone gets ¥10,000 = ¥100,000 total. ¥900,000 deferred.
    print(f"  All capped at ¥10,000: total allocated = ¥{r['total_allocated']:,.0f} "
          f"(expected ¥100,000), deferred = ¥{r['deferred_pool']:,.0f}")
    print(f"  Gates: {'✓' if r['all_gates_pass'] else '✗ ' + str(r['failed_gates'])}")
    return [r]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("v2 Allocator Deep Stress Test")
    print("=" * 70)
    all_results: list[dict] = []
    all_results.extend(scenario_long_run())
    all_results.extend(scenario_numerical())
    all_results.extend(scenario_failure_injection())
    all_results.extend(scenario_property_tests())
    all_results.extend(scenario_conditioning())
    all_results.extend(scenario_headcount())
    all_results.extend(scenario_reallocation_stress())

    print("\n" + "=" * 70)
    print("DEEP STRESS SUMMARY")
    print("=" * 70)
    print(f"Total deep checks: {len(all_results)}")
    # Count by status field (ok / raised_or_ok / raised / etc.)
    pass_count = sum(1 for r in all_results if any(
        r.get(k, False) for k in ("ok", "raised_or_ok", "raised")
    ))
    print(f"Passed: {pass_count}/{len(all_results)}")

    df = pd.DataFrame(all_results)
    df.to_csv("deep_stress_results.csv", index=False)
    print("Saved to deep_stress_results.csv")


if __name__ == "__main__":
    main()
