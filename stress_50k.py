"""50,000-iteration stability test for the v2 allocator.

Long-run drift + determinism check beyond the 1,000-iteration default in
deep_stress_test.py. Intended as an ad-hoc validation before shipping —
run it, read the summary, don't bake it into CI (too slow).

Checks:
    1. Determinism: same seed → identical bonus vector (max abs diff = 0).
    2. Drift:       for each of 50,000 random configs, sum(bonus) + deferred
                    must equal pool_total to within ¥0.01.
    3. Release gates: all 6 gates must pass on every run.

Usage:
    uv run python stress_50k.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, Department, PoolConfig
from sensitivity import compute_sensitivity
from v2_allocator import allocate_v2
from stress_test import make_random_config


N_ITER = 50_000
DRIFT_TOLERANCE = 0.01  # ¥


def run() -> int:
    print(f"=== 50,000-iteration stability test ===")
    print(f"Checks: determinism, drift (tol ¥{DRIFT_TOLERANCE}), release gates\n")

    # ---- 1. Determinism: same seed twice → identical bonus vector ----
    cfg_a, ach_a = make_random_config(n_deps=30, seed=7777)
    cfg_b, ach_b = make_random_config(n_deps=30, seed=7777)
    r_a = allocate_v2(cfg_a, compute_sensitivity(cfg_a), achievements=ach_a)
    r_b = allocate_v2(cfg_b, compute_sensitivity(cfg_b), achievements=ach_b)
    det_diff = (r_a.df["bonus"] - r_b.df["bonus"]).abs().max()
    print(f"[1/3] Determinism (same seed): max diff = {det_diff:.2e}")
    assert det_diff < 1e-9, f"non-deterministic: diff={det_diff}"
    print("      PASS\n")

    # ---- 2 & 3. Drift + release gates over 50,000 random configs ----
    drift_failures = 0
    gate_failures = 0
    max_drift = 0.0
    max_drift_seed = -1
    elapsed_total = 0.0
    elapsed_max = 0.0
    t0 = time.perf_counter()

    for seed in range(N_ITER):
        n = int(np.random.default_rng(seed).integers(4, 100))
        cfg, ach = make_random_config(n_deps=n, seed=seed)
        sens = compute_sensitivity(cfg)

        ts = time.perf_counter()
        result = allocate_v2(cfg, sens, achievements=ach)
        te = time.perf_counter()
        dt = te - ts
        elapsed_total += dt
        if dt > elapsed_max:
            elapsed_max = dt

        total = result.total_allocated + result.deferred_pool
        drift = abs(total - cfg.pool.pool_total)
        if drift > max_drift:
            max_drift = drift
            max_drift_seed = seed
        if drift > DRIFT_TOLERANCE:
            drift_failures += 1

        if not all(bool(v) for v in result.release_gates.values()):
            gate_failures += 1

        # Progress every 10,000 iterations.
        if (seed + 1) % 10_000 == 0:
            print(f"  [{seed + 1:>5d}/{N_ITER}]  drift_max=¥{max_drift:.4f}  "
                  f"drift_fail={drift_failures}  gate_fail={gate_failures}  "
                  f"avg={elapsed_total * 1000 / (seed + 1):.2f}ms  "
                  f"max={elapsed_max * 1000:.2f}ms")

    total_elapsed = time.perf_counter() - t0

    print(f"\n[2/3] Drift check (tol ¥{DRIFT_TOLERANCE}):")
    print(f"      max |total - pool_total| = ¥{max_drift:.6f}  (seed={max_drift_seed})")
    print(f"      failures: {drift_failures}/{N_ITER}")
    print(f"      {'PASS' if drift_failures == 0 else 'FAIL'}\n")

    print(f"[3/3] Release gates:")
    print(f"      failures: {gate_failures}/{N_ITER}")
    print(f"      {'PASS' if gate_failures == 0 else 'FAIL'}\n")

    print(f"=== Summary ===")
    print(f"  iterations:     {N_ITER}")
    print(f"  total elapsed:  {total_elapsed:.1f}s")
    print(f"  avg per run:    {elapsed_total * 1000 / N_ITER:.2f}ms")
    print(f"  max per run:    {elapsed_max * 1000:.2f}ms")
    print(f"  drift max:      ¥{max_drift:.6f}")
    print(f"  drift fails:    {drift_failures}")
    print(f"  gate fails:     {gate_failures}")

    ok = drift_failures == 0 and gate_failures == 0 and det_diff < 1e-9
    print(f"\n{'✓ ALL PASS' if ok else '✗ FAILURES DETECTED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
