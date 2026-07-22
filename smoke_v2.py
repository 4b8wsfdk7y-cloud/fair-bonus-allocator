"""Smoke test: run v2 allocator on the real example_wind_cable.yaml."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from sensitivity import compute_sensitivity
from v2_allocator import allocate_v2, reachability_audit


def main() -> None:
    cfg = Config.from_yaml("example_wind_cable.yaml")
    sens = compute_sensitivity(cfg)

    print("=== Reachability audit ===")
    audit = reachability_audit(cfg, sens)
    print(audit.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print("\n=== v2 allocation: scenario A — all at A-tier (ach=1.15) ===")
    ach_a = {d.name: 1.15 for d in cfg.departments}
    result_a = allocate_v2(cfg, sens, achievements=ach_a)
    print(result_a.df.to_string(
        index=False, float_format=lambda x: f"{x:,.2f}"
    ))
    print(f"\nTotal allocated: ¥{result_a.total_allocated:,.0f}")
    print(f"Deferred pool:   ¥{result_a.deferred_pool:,.0f}")
    print(f"Pool remaining:  ¥{result_a.pool_remaining:,.0f}")
    print(f"Release gates:   {result_a.release_gates}")

    print("\n=== v2 allocation: scenario B — sales alone excels ===")
    ach_b = {d.name: 1.0 for d in cfg.departments}
    ach_b["销售"] = 1.4
    result_b = allocate_v2(cfg, sens, achievements=ach_b)
    print(result_b.df.to_string(
        index=False, float_format=lambda x: f"{x:,.2f}"
    ))
    print(f"\nTotal allocated: ¥{result_b.total_allocated:,.0f}")
    print(f"Release gates:   {result_b.release_gates}")


if __name__ == "__main__":
    main()
