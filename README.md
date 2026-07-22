# bonus-allocator

A sensitivity-driven bonus pool allocator that translates heterogeneous department KPIs (revenue yuan, efficiency %, cost-reduction yuan, project count, etc.) into a common currency — **marginal profit contribution** — and distributes a shared bonus pool under explicit fairness and governance constraints.

> **Status**: model prototype, math-validated. Not production-payroll ready without historical backtest + 1-quarter shadow run.

---

## Why

The CFO/HR problem: 8 departments have KPIs in different units. How do you let "Sales +¥30M revenue" and "Procurement ¥2M cost reduction" compete under the same rule for a ¥1M bonus pool?

This project's answer: translate every KPI to profit contribution using the β elasticity (`β × ΔKPI = profit yuan`), then allocate under explicit fairness rules.

## Three-layer architecture

```
Layer 1 · Sensitivity modeling (sensitivity.py)
    Profit = Profit_baseline + Σ β_d × (KPI_d − baseline_d)
    Validates via Monte Carlo + OLS regression (recovers β within <1%).

Layer 2 · Tier calibration (tiers.py)
    β_d × (KPI_d^A − baseline) = τ_A × q_d × profit_gap
    β_d × (KPI_d^S − baseline) = τ_S × q_d × profit_gap
    where q_d are user-provided responsibility shares summing to 1.

Layer 3 · Allocation
    v1 (allocator.py): knapsack-style greedy with rounds penalty.
    v2 (v2_allocator.py): base pool by headcount + perf pool by confidence-adjusted impact.
```

## v2 governance (the part Codex's review forced)

v1 was math-correct but had three real-world flaws:
1. "Same contribution → same bonus" was claimed but not delivered.
2. Departments whose stretch KPI couldn't reach A tier were silently misclassified.
3. β was treated as truth, not as an estimate with confidence intervals.

v2 fixes:
- **Responsibility shares** (`quota`) sum to 1 → "everyone at A tier" closes exactly one profit gap, not N×θ.
- **Base pool by headcount + perf pool by contribution** — separates "same employees get same base" from "same contribution gets same perf bonus."
- **One-sided 95% lower bound** on impact: `C_d* = ρ_d × max(0, β̂ ΔKPI − 1.645 × |ΔKPI| × SE(β̂))`. Only "money we're confident was earned" becomes bonus.
- **Reachability audit** flags departments whose stretch can't reach A/S so quotas can be renegotiated honestly.
- **Release gates**: 6 invariants that must all be True before paying out (pool utilization, no NaN, no negative, achievers non-negative c_star, quotas sum to 1, monotonicity).

## Quick start

```bash
# Install dependencies (Python 3.11+)
uv sync

# Run both v1 and v2 allocators on the example config
uv run python -m cli allocate example_config.yaml --ach Sales=1.4

# Reachability audit only
uv run python -m cli audit example_config.yaml

# Run stress + deep stress tests
uv run python -m cli stress

# Run unit tests
uv run pytest tests/ -v
```

## Config schema

See `example_config.yaml` for a fully documented example. Key fields:

| Field | Type | Meaning |
|-------|------|---------|
| `pool_total` | float | Total bonus pool in yuan |
| `profit_target`, `profit_baseline` | float | Profit gap = target − baseline; this drives tier calibration |
| `lambda_base_ratio` | float | Share of pool split by headcount (default 0.3) |
| `a_max` | float | Achievement-rate clip ceiling (default 1.5) |
| `theta_a`, `theta_s` | float | A/S tier profit-share thresholds (0.15, 0.30) |
| `quota` | float | Responsibility share q_d; all-or-none, must sum to 1 |
| `beta` | float | Profit elasticity |
| `beta_ci_lower`, `beta_ci_upper` | float | 95% CI bounds (optional; SE=0 if omitted) |
| `beta_confidence_weight` | float | ρ_d ∈ [0,1], confidence in β estimate (default 1.0) |
| `beta_source` | str | Provenance tag for audit trail |

## Validation

```
tests/test_validation.py       — 13 v1 arithmetic tests
tests/test_v2_validation.py    — 14 v2 governance tests
stress_test.py                 — 119 scenarios (scale, extreme achievement, fuzz)
deep_stress_test.py            — 16 deep checks (long-run, numerical, failure injection)
```

All 27 unit tests pass. All 119 stress scenarios pass release gates. All 16 deep checks pass.

## What's NOT here

- **Historical backtest**: requires your real financial data; cannot be pre-baked.
- **Shadow run harness**: requires your real v1 baseline.
- **Reproducibility snapshot pipeline**: the code produces the dict, but wiring to your approval workflow is on you.
- **Feishu push integration**: this public release removes the Feishu client. If you want it back, see the Feishu open-platform docs and set `FEISHU_APP_ID` / `FEISHU_APP_SECRET` env vars in your own integration.

## License

MIT. See `LICENSE`.

## Caveats

This is a model. Models do not pay people. Real bonus payouts require:
1. All release gates passing.
2. Historical backtest on your real data showing β stability.
3. A shadow-run quarter comparing v1 vs v2 distributions.
4. A reproducibility snapshot (config SHA + code commit + seed + approver).
5. CFO + HR + business sign-off recorded in writing.

If you skip any of these and pay people based solely on this code's output, you are doing it wrong.
