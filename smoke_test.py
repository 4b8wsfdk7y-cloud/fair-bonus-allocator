"""Headless smoke test — run all three layers and print results.

Run: uv run python smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from sensitivity import compute_sensitivity, monte_carlo_profit
from tiers import calibrate_tiers
from allocator import allocate, scenario_grid


def main() -> None:
    config = Config.from_yaml(Path(__file__).resolve().parent / "example_config.yaml")

    print("=" * 72)
    print("Layer 1 · 敏感性建模")
    print("=" * 72)
    sensitivity = compute_sensitivity(config)
    print(sensitivity.df.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))
    print(f"\n利润缺口 B→Target: ¥{sensitivity.profit_gap:,.0f}")
    print(f"各部门 stretch 总贡献: ¥{sensitivity.df['stretch_impact'].sum():,.0f}")

    print("\n" + "=" * 72)
    print("Layer 2 · B/A/S 档位校准")
    print("=" * 72)
    tiers = calibrate_tiers(config, sensitivity)
    print(tiers.df.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    print("\n" + "=" * 72)
    print("Layer 3 · Knapsack 分配（情景：全员达成 A 档）")
    print("=" * 72)
    # Approximate "all A" by setting achievement = 1.15 for everyone.
    achievements_a = {d.name: 1.15 for d in config.departments}
    alloc_a = allocate(config, sensitivity, tiers, achievements=achievements_a)
    print(alloc_a.df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\n已分配: ¥{alloc_a.total_allocated:,.0f}")
    print(f"剩余:   ¥{alloc_a.pool_remaining:,.0f}")
    print(f"轮次:   {alloc_a.n_rounds}")

    print("\n" + "=" * 72)
    print("Layer 3 · Knapsack 分配（情景：销售独大）")
    print("=" * 72)
    achievements_sales = {d.name: (1.40 if d.name == "Sales" else 1.0) for d in config.departments}
    alloc_sales = allocate(config, sensitivity, tiers, achievements=achievements_sales)
    print(alloc_sales.df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\n已分配: ¥{alloc_sales.total_allocated:,.0f}")
    print(f"剩余:   ¥{alloc_sales.pool_remaining:,.0f}")

    print("\n" + "=" * 72)
    print("Monte Carlo 利润分布验证（500 情景）")
    print("=" * 72)
    mc = monte_carlo_profit(config, n_scenarios=500)
    profit = mc["profit"]
    print(f"利润均值:     ¥{profit.mean():,.0f}")
    print(f"利润中位数:   ¥{profit.median():,.0f}")
    print(f"利润 5% 分位: ¥{profit.quantile(0.05):,.0f}")
    print(f"利润 95% 分位:¥{profit.quantile(0.95):,.0f}")
    print(f"达成目标 (¥{config.pool.profit_target:,.0f}) 的概率: {(profit >= config.pool.profit_target).mean():.1%}")

    # Quick regression sanity check: regress profit on kpi deltas to recover β.
    import numpy as np
    import pandas as pd
    X_list = []
    for d in config.departments:
        if d.kpi_baseline > 0:
            delta = mc[f"kpi_{d.name}"] * d.kpi_baseline - d.kpi_baseline
        else:
            delta = (mc[f"kpi_{d.name}"] - 1.0) * d.kpi_stretch
        X_list.append(delta.values)
    X = np.column_stack(X_list)
    y = profit.values
    # OLS: y = const + Σ β_d × ΔKPI_d
    X_with_const = np.column_stack([np.ones(len(X)), X])
    coefs, *_ = np.linalg.lstsq(X_with_const, y, rcond=None)
    print("\nβ 回归验证（应接近 config 里的 β）:")
    for i, d in enumerate(config.departments):
        print(f"  {d.name:20s}: 配置 β={d.beta:>12.3f}, 回归 β={coefs[i + 1]:>12.3f}")


if __name__ == "__main__":
    main()
