"""CLI entry point: run the allocator.

Usage:
    uv run python -m cli allocate example_config.yaml --ach Sales=1.4 Manufacturing=1.2
    uv run python -m cli audit example_config.yaml
    uv run python -m cli stress

The v1 feishu-push subcommand has been removed in this public release.
If you need to push results to Feishu, set up your own integration using
FEISHU_APP_ID / FEISHU_APP_SECRET env vars; see the Feishu open-platform docs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parse_achievements(items: list[str]) -> dict[str, float]:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"Bad --ach format: {it} (expected 'Department=1.4')")
        name, val = it.rsplit("=", 1)
        out[name.strip()] = float(val)
    return out


def _cmd_allocate(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config
    from sensitivity import compute_sensitivity
    from tiers import calibrate_tiers
    from allocator import allocate
    from v2_allocator import allocate_v2, reachability_audit

    config = Config.from_yaml(args.config)
    sens = compute_sensitivity(config)
    tiers = calibrate_tiers(config, sens)

    print("\n=== Reachability audit (v2) ===")
    audit = reachability_audit(config, sens)
    print(audit.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    achievements = _parse_achievements(args.ach) if args.ach else None

    print("\n=== v1 allocation ===")
    result_v1 = allocate(config, sens, tiers, achievements=achievements)
    print(result_v1.df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\nv1 total: ¥{result_v1.total_allocated:,.0f} / ¥{config.pool.pool_total:,.0f}")
    print(f"v1 remaining: ¥{result_v1.pool_remaining:,.0f}")

    print("\n=== v2 allocation (governance) ===")
    result_v2 = allocate_v2(config, sens, achievements=achievements)
    print(result_v2.df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\nv2 total: ¥{result_v2.total_allocated:,.0f} / ¥{config.pool.pool_total:,.0f}")
    print(f"v2 deferred: ¥{result_v2.deferred_pool:,.0f}")
    print(f"release gates: {result_v2.release_gates}")
    all_gates = all(bool(v) for v in result_v2.release_gates.values())
    print(f"all gates pass: {'YES' if all_gates else 'NO — DO NOT PAY'}")
    return 0 if all_gates else 1


def _cmd_audit(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config
    from sensitivity import compute_sensitivity
    from v2_allocator import reachability_audit

    config = Config.from_yaml(args.config)
    sens = compute_sensitivity(config)
    audit = reachability_audit(config, sens)
    print(audit.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    unreachable = audit[~audit["can_reach_a"]]
    if len(unreachable) > 0:
        print(f"\n⚠ {len(unreachable)} department(s) cannot reach A tier at current stretch:")
        for _, r in unreachable.iterrows():
            print(f"  {r['department']}: stretch_impact=¥{r['stretch_impact']:,.0f} "
                  f"< A target=¥{r['a_target_profit']:,.0f}")
    return 0


def _cmd_stress(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import stress_test
    import deep_stress_test
    print("Running stress_test.main()...")
    stress_test.main()
    print("\nRunning deep_stress_test.main()...")
    deep_stress_test.main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cli", description="Bonus allocator CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_alloc = sub.add_parser("allocate", help="run both v1 and v2 allocators")
    p_alloc.add_argument("config", help="path to YAML config")
    p_alloc.add_argument("--ach", nargs="*", help="Department=achievement, e.g. Sales=1.4 Manufacturing=1.2")
    p_alloc.set_defaults(func=_cmd_allocate)

    p_audit = sub.add_parser("audit", help="run reachability audit")
    p_audit.add_argument("config", help="path to YAML config")
    p_audit.set_defaults(func=_cmd_audit)

    p_stress = sub.add_parser("stress", help="run stress + deep stress tests")
    p_stress.set_defaults(func=_cmd_stress)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
