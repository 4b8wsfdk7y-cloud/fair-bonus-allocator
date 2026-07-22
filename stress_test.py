"""Stress test for v2 allocator.

Scenarios:
    1. Scale: 8 / 100 / 1000 / 5000 departments.
    2. Extreme achievement: 0.0, 0.5, 1.0, 2.0, 10.0, 100.0.
    3. Zero-score freeze: all β_confidence_weight = 0 → entire perf pool deferred.
    4. Cap overflow: every department has cap = 1 → massive redistribution.
    5. Mixed CI: some depts have huge SE (CI wider than β) → c_star goes negative → clipped.
    6. Randomized fuzzing: 100 random configs, assert release gates all True.
    7. Adversarial: one dept with astronomical β/quota vs many small ones.
    8. Reallocation stability: cap that forces cascading overflow.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, Department, PoolConfig
from sensitivity import compute_sensitivity
from v2_allocator import allocate_v2, reachability_audit, resolve_quotas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_random_config(
    n_deps: int,
    seed: int,
    pool_total: float = 1_000_000,
    with_cis: bool = True,
    with_quotas: bool = True,
    extreme_achievement: float | None = None,
) -> tuple[Config, dict[str, float]]:
    """Generate a random config + a matching achievements dict."""
    rng = np.random.default_rng(seed)
    profit_gap = 3_000_000  # like the real config
    profit_target = 17_000_000 + profit_gap
    pool = PoolConfig(
        pool_total=pool_total,
        profit_target=profit_target,
        profit_baseline=17_000_000,
        min_pool_share=0.02,
        lambda_base_ratio=0.3,
        a_max=1.5,
        deferred_pool_enabled=True,
    )
    # Generate departments so that sum(stretch_impact) ≈ 2x profit_gap.
    # This keeps most departments' stretch_impact in a reasonable range relative
    # to theta_a × profit_gap (450k).
    target_total_stretch = 2 * profit_gap
    stretch_shares = rng.dirichlet(np.ones(n_deps))
    stretch_impacts = stretch_shares * target_total_stretch

    # Stretch ratio 1.1-1.5; baseline = stretch_impact / (beta * (stretch_ratio - 1))
    # Pick baseline and stretch_ratio first, solve beta = stretch_impact / (baseline * (sr - 1))
    deps = []
    quotas = []
    for i in range(n_deps):
        baseline = float(rng.uniform(100, 1000))
        stretch_ratio = float(rng.uniform(1.1, 1.5))
        stretch = baseline * stretch_ratio
        beta = float(stretch_impacts[i] / (stretch - baseline))
        # Generate a CI around beta: ±10-30%
        ci_width_pct = float(rng.uniform(0.1, 0.3))
        ci_lower = beta * (1 - ci_width_pct) if with_cis else None
        ci_upper = beta * (1 + ci_width_pct) if with_cis else None
        # ρ in [0.3, 1.0]
        rho = float(rng.uniform(0.3, 1.0))
        deps.append(Department(
            name=f"d{i}",
            kpi_baseline=baseline,
            kpi_stretch=stretch,
            beta=beta,
            headcount=int(rng.integers(2, 50)),
            beta_ci_lower=ci_lower,
            beta_ci_upper=ci_upper,
            beta_confidence_weight=rho,
            beta_source="regression",
            quota=None,  # filled below
        ))
        # For quotas: use the same stretch_shares (must sum to 1).
        quotas.append(float(stretch_shares[i]))

    if with_quotas:
        for d, q in zip(deps, quotas):
            d.quota = q
        # Snap to sum exactly 1.0.
        total = sum(d.quota for d in deps)
        deps[0].quota = deps[0].quota + (1.0 - total)

    cfg = Config(pool=pool, departments=deps)
    cfg.validate_v2()
    # Achievements
    if extreme_achievement is not None:
        ach = {d.name: extreme_achievement for d in deps}
    else:
        ach = {d.name: float(rng.uniform(0.5, 2.0)) for d in deps}
    return cfg, ach


def run_scenario(name: str, cfg: Config, ach: dict[str, float], caps: dict[str, float] | None = None) -> dict:
    sens = compute_sensitivity(cfg)
    t0 = time.perf_counter()
    result = allocate_v2(cfg, sens, achievements=ach, caps=caps)
    elapsed = time.perf_counter() - t0
    gates = result.release_gates
    all_gates_pass = all(bool(v) for v in gates.values())
    return {
        "scenario": name,
        "n_deps": len(cfg.departments),
        "elapsed_s": elapsed,
        "total_allocated": result.total_allocated,
        "deferred_pool": result.deferred_pool,
        "pool_remaining": result.pool_remaining,
        "all_gates_pass": all_gates_pass,
        "failed_gates": [k for k, v in gates.items() if not bool(v)],
        "max_bonus": float(result.df["bonus"].max()) if len(result.df) > 0 else 0,
        "min_bonus": float(result.df["bonus"].min()) if len(result.df) > 0 else 0,
        "n_zero_bonus": int((result.df["bonus"] == 0).sum()),
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_scale():
    """Vary department count. Expect roughly linear time."""
    print("\n=== Scenario 1: Scale (8 → 5000 departments) ===")
    results = []
    for n in [8, 100, 1000, 5000]:
        cfg, ach = make_random_config(n_deps=n, seed=42)
        r = run_scenario(f"scale_{n}", cfg, ach)
        results.append(r)
        print(f"  n={n:5d}: {r['elapsed_s']*1000:7.1f}ms  "
              f"allocated=¥{r['total_allocated']:,.0f}  "
              f"deferred=¥{r['deferred_pool']:,.0f}  "
              f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return results


def scenario_extreme_achievement():
    """Extreme achievement values."""
    print("\n=== Scenario 2: Extreme achievement (0.0 → 100.0) ===")
    results = []
    for ach_val in [0.0, 0.5, 1.0, 2.0, 10.0, 100.0]:
        cfg, _ = make_random_config(n_deps=20, seed=42)
        ach = {d.name: ach_val for d in cfg.departments}
        r = run_scenario(f"ach_{ach_val}", cfg, ach)
        results.append(r)
        print(f"  ach={ach_val:6.1f}: {r['elapsed_s']*1000:7.1f}ms  "
              f"max=¥{r['max_bonus']:,.0f}  min=¥{r['min_bonus']:,.0f}  "
              f"zero_count={r['n_zero_bonus']}  "
              f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return results


def scenario_zero_score_freeze():
    """All confidence weights = 0 → all perf pool deferred."""
    print("\n=== Scenario 3: Zero-score freeze (all ρ=0) ===")
    cfg, _ = make_random_config(n_deps=50, seed=42, with_cis=False)
    for d in cfg.departments:
        d.beta_confidence_weight = 0.0
    cfg.validate_v2()
    ach = {d.name: 2.0 for d in cfg.departments}
    r = run_scenario("zero_score", cfg, ach)
    print(f"  {r['elapsed_s']*1000:.1f}ms  "
          f"allocated=¥{r['total_allocated']:,.0f} (= base pool only)  "
          f"deferred=¥{r['deferred_pool']:,.0f}  "
          f"zero_count={r['n_zero_bonus']}  "
          f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return [r]


def scenario_cap_overflow():
    """Every dept has cap = 1 → massive redistribution loop."""
    print("\n=== Scenario 4: Cap overflow (every cap = ¥100) ===")
    cfg, _ = make_random_config(n_deps=50, seed=42)
    ach = {d.name: 2.0 for d in cfg.departments}
    caps = {d.name: 100.0 for d in cfg.departments}
    r = run_scenario("cap_overflow", cfg, ach, caps=caps)
    print(f"  {r['elapsed_s']*1000:.1f}ms  "
          f"allocated=¥{r['total_allocated']:,.0f}  "
          f"deferred=¥{r['deferred_pool']:,.0f}  "
          f"max=¥{r['max_bonus']:,.0f}  "
          f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return [r]


def scenario_wide_ci():
    """Some CIs so wide that c_star goes negative for many departments."""
    print("\n=== Scenario 5: Wide CI (CI > β, c_star clipping stress) ===")
    cfg, _ = make_random_config(n_deps=50, seed=42)
    # Make first 25 departments have CI wider than β itself.
    for i, d in enumerate(cfg.departments[:25]):
        d.beta_ci_lower = -d.beta * 2
        d.beta_ci_upper = d.beta * 3
    cfg.validate_v2()
    ach = {d.name: 1.05 for d in cfg.departments}  # tiny achievement
    r = run_scenario("wide_ci", cfg, ach)
    print(f"  {r['elapsed_s']*1000:.1f}ms  "
          f"allocated=¥{r['total_allocated']:,.0f}  "
          f"deferred=¥{r['deferred_pool']:,.0f}  "
          f"zero_count={r['n_zero_bonus']}  "
          f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return [r]


def scenario_fuzz():
    """Fuzz: 100 random configs, assert all release gates pass."""
    print("\n=== Scenario 6: Fuzz (100 random configs) ===")
    results = []
    fails = 0
    for seed in range(100):
        n_deps = int(np.random.default_rng(seed).integers(4, 50))
        cfg, ach = make_random_config(n_deps=n_deps, seed=seed)
        r = run_scenario(f"fuzz_{seed}", cfg, ach)
        results.append(r)
        if not r["all_gates_pass"]:
            fails += 1
            if fails <= 3:
                print(f"  seed={seed} FAIL: {r['failed_gates']}")
    print(f"  100 runs: {fails} failures, avg {np.mean([r['elapsed_s'] for r in results])*1000:.1f}ms, "
          f"max {max(r['elapsed_s'] for r in results)*1000:.1f}ms")
    return results


def scenario_adversarial():
    """One dept with huge β vs many small."""
    print("\n=== Scenario 7: Adversarial (1 huge dept + 99 small) ===")
    pool = PoolConfig(pool_total=1_000_000, profit_target=20_000_000,
                      profit_baseline=17_000_000, lambda_base_ratio=0.3, a_max=1.5)
    deps = [
        Department(name="mega", kpi_baseline=100, kpi_stretch=200, beta=100_000,
                   headcount=100, beta_confidence_weight=0.9,
                   beta_ci_lower=90_000, beta_ci_upper=110_000,
                   beta_source="bridge_model", quota=0.5),
    ]
    for i in range(99):
        deps.append(Department(
            name=f"tiny{i}", kpi_baseline=100, kpi_stretch=120, beta=1000,
            headcount=5, beta_confidence_weight=0.7,
            beta_ci_lower=800, beta_ci_upper=1200,
            beta_source="regression", quota=0.5 / 99,
        ))
    cfg = Config(pool=pool, departments=deps)
    cfg.validate_v2()
    ach = {d.name: 1.5 for d in cfg.departments}
    r = run_scenario("adversarial", cfg, ach)
    print(f"  {r['elapsed_s']*1000:.1f}ms  "
          f"allocated=¥{r['total_allocated']:,.0f}  "
          f"deferred=¥{r['deferred_pool']:,.0f}  "
          f"max=¥{r['max_bonus']:,.0f}  "
          f"min=¥{r['min_bonus']:,.0f}  "
          f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return [r]


def scenario_cascading_caps():
    """Caps set so every dept hits cap → cascading redistribution."""
    print("\n=== Scenario 8: Cascading caps (90% of depts capped) ===")
    cfg, _ = make_random_config(n_deps=30, seed=42)
    ach = {d.name: 2.0 for d in cfg.departments}
    # Set caps so first 27 hit cap immediately, last 3 absorb the rest.
    caps = {d.name: 1000.0 for d in cfg.departments[:27]}
    # No cap for last 3 → they absorb everything that flows through.
    r = run_scenario("cascading", cfg, ach, caps=caps)
    print(f"  {r['elapsed_s']*1000:.1f}ms  "
          f"allocated=¥{r['total_allocated']:,.0f}  "
          f"deferred=¥{r['deferred_pool']:,.0f}  "
          f"max=¥{r['max_bonus']:,.0f}  "
          f"gates={'✓' if r['all_gates_pass'] else '✗ '+str(r['failed_gates'])}")
    return [r]


def scenario_realistic():
    """Real-world config (example_config.yaml)."""
    print("\n=== Scenario 9: Real-world (example_config.yaml) ===")
    cfg = Config.from_yaml("example_config.yaml")
    sens = compute_sensitivity(cfg)
    # Run multiple historical-style scenarios.
    scenarios = [
        ("baseline", {d.name: 1.0 for d in cfg.departments}),
        ("all_A", {d.name: 1.15 for d in cfg.departments}),
        ("sales_boom", {d.name: 1.0 for d in cfg.departments} | {"Sales": 1.4}),
        ("mixed", {"Sales": 1.3, "Manufacturing": 1.2, "Procurement": 1.1, "Engineering": 1.0,
                    "Logistics": 1.0, "SupplyChain-Sourcing": 0.9, "PMC": 1.0, "Quality": 1.2}),
    ]
    results = []
    for name, ach in scenarios:
        t0 = time.perf_counter()
        result = allocate_v2(cfg, sens, achievements=ach)
        elapsed = time.perf_counter() - t0
        gates_ok = all(bool(v) for v in result.release_gates.values())
        r = {
            "scenario": f"real_{name}",
            "n_deps": 8,
            "elapsed_s": elapsed,
            "total_allocated": result.total_allocated,
            "deferred_pool": result.deferred_pool,
            "all_gates_pass": gates_ok,
        }
        results.append(r)
        print(f"  {name:12s}: {r['elapsed_s']*1000:6.1f}ms  "
              f"allocated=¥{r['total_allocated']:,.0f}  "
              f"deferred=¥{r['deferred_pool']:,.0f}  "
              f"gates={'✓' if gates_ok else '✗'}")
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("v2 Allocator Stress Test")
    print("=" * 70)
    all_results: list[dict] = []
    all_results.extend(scenario_scale())
    all_results.extend(scenario_extreme_achievement())
    all_results.extend(scenario_zero_score_freeze())
    all_results.extend(scenario_cap_overflow())
    all_results.extend(scenario_wide_ci())
    all_results.extend(scenario_fuzz())
    all_results.extend(scenario_adversarial())
    all_results.extend(scenario_cascading_caps())
    all_results.extend(scenario_realistic())

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    df = pd.DataFrame(all_results)
    print(f"Total scenarios: {len(df)}")
    print(f"All release gates pass: {df['all_gates_pass'].sum()}/{len(df)}")
    print(f"Max elapsed: {df['elapsed_s'].max()*1000:.1f}ms (scenario: {df.loc[df['elapsed_s'].idxmax(), 'scenario']})")
    print(f"Mean elapsed: {df['elapsed_s'].mean()*1000:.1f}ms")
    failed = df[~df['all_gates_pass']]
    if len(failed) > 0:
        print(f"\nFailed scenarios ({len(failed)}):")
        for _, r in failed.iterrows():
            print(f"  {r['scenario']}: {r['failed_gates']}")

    # Save to CSV for reference.
    df.to_csv("stress_test_results.csv", index=False)
    print(f"\nResults saved to stress_test_results.csv")


if __name__ == "__main__":
    main()
