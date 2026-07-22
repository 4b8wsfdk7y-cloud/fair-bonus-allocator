# bonus-allocator

**A sensitivity-driven bonus pool allocator that translates heterogeneous department KPIs into a common currency — marginal profit contribution — and distributes a shared bonus pool under explicit fairness and governance constraints.**

> **Status**: model prototype, math-validated (27 unit tests + 119 stress scenarios + 16 deep checks all pass). **Not production-payroll ready** without historical backtest and a one-quarter shadow run.

---

## Table of contents

1. [Why this project exists](#1-why-this-project-exists)
2. [Design principle in one page](#2-design-principle-in-one-page)
3. [Three-layer architecture](#3-three-layer-architecture)
4. [Quick start](#4-quick-start)
5. [Layer 1 · Sensitivity modeling](#5-layer-1--sensitivity-modeling)
6. [Layer 2 · Tier calibration](#6-layer-2--tier-calibration)
7. [Layer 3 · Allocation (v1 & v2)](#7-layer-3--allocation-v1--v2)
8. [v2 governance in depth](#8-v2-governance-in-depth)
9. [Release gates](#9-release-gates)
10. [Configuration reference](#10-configuration-reference)
11. [CLI reference](#11-cli-reference)
12. [Validation & stress testing](#12-validation--stress-testing)
13. [Road to production](#13-road-to-production)
14. [What's NOT here](#14-whats-not-here)
15. [License & caveats](#15-license--caveats)

---

## 1. Why this project exists

The classic CFO/HR problem: 8 departments have KPIs in **different units**.

| Department | KPI | Unit |
|---|---|---|
| Sales | Revenue | ¥ |
| Procurement | Cost reduction | ¥ |
| Manufacturing | Production efficiency | % |
| Logistics | On-time delivery | % |
| Quality | Complaint reduction | count |
| Engineering | Cost-reduction projects | count |
| PMC | Inventory turn days | days |
| SupplyChain-Sourcing | New suppliers | count |

How do you let "Sales +¥30M revenue" and "Procurement ¥2M cost reduction" compete under the same rule for a ¥1M bonus pool? You can't compare raw KPIs — you have to translate them first.

**This project's answer**: translate every KPI to **profit contribution** using the β elasticity (`β × ΔKPI = profit yuan`), then allocate under explicit fairness rules. This makes "who earned what" computable, auditable, and debatable on the same scale.

---

## 2. Design principle in one page

### The core problem

Three kinds of unfairness sneak into bonus pools:

1. **Apples-vs-oranges**: Sales gets more because revenue numbers look bigger, even when Procurement saved more profit.
2. **Moving goalposts**: Department stretch KPIs are set arbitrarily, so "A tier" in one dept ≠ "A tier" in another.
3. **Fake precision**: β is estimated from limited data, but the allocator treats it as truth — noise becomes money.

### The three-step pipeline

```
Heterogeneous KPIs
    │
    ▼  Layer 1: β × ΔKPI = profit yuan
Common currency (profit contribution)
    │
    ▼  Layer 2: calibrate tier lines so equal tier = equal contribution
Comparable tier lines
    │
    ▼  Layer 3: allocate pool under fairness + governance
Bonus per department
```

### Three fairness levels

| Level | Rule | Where enforced |
|---|---|---|
| **L1 — Equal contribution** | Same ΔKPI × β → same profit yuan | Layer 1 (math identity) |
| **L2 — Equal tier** | Hitting "A tier" in any dept closes the same share of profit gap | Layer 2 (calibration) |
| **L3 — Equal bonus** | Same contribution → same bonus; same headcount → same base | Layer 3 (v2 allocator) |

### Confidence-aware

β̂ is an **estimate**, not truth. v2 uses a one-sided 95% lower bound:

```
C_d* = ρ_d × max(0, β̂·ΔKPI − 1.645 × |ΔKPI| × SE(β̂))
```

Only profit contribution we're confident was actually earned becomes bonus. Noisy estimates get clipped to zero rather than paid out.

### Launch threshold

**6 release gates** must all pass before any payout. If any fails, the run is marked `DO NOT PAY`. See [§9](#9-release-gates).

---

## 3. Three-layer architecture

```
Layer 1 · Sensitivity modeling      [sensitivity.py]
    Profit = Profit_baseline + Σ β_d × (KPI_d − baseline_d)
    Validates via Monte Carlo + OLS regression (recovers β within <1%).

Layer 2 · Tier calibration          [tiers.py]
    β_d × (KPI_d^A − baseline) = θ_A × profit_gap
    β_d × (KPI_d^S − baseline) = θ_S × profit_gap
    → every tier transition represents the same ¥ contribution

Layer 3 · Allocation
    v1 [allocator.py]    Knapsack-style greedy with rounds penalty.
    v2 [v2_allocator.py] Base pool (headcount) + perf pool (confidence-adjusted impact).
```

**v1 vs v2 — when to use each:**

| | v1 | v2 |
|---|---|---|
| Math correct? | ✓ | ✓ |
| Same contribution → same bonus? | claimed, not delivered | ✓ enforced |
| β treated as estimate? | no (point estimate) | ✓ (95% lower bound) |
| Stretch-reachability audit? | no | ✓ |
| Quota governance? | implicit | ✓ explicit (sum=1) |
| Use case | shadow run, baseline | **production candidate** |

---

## 4. Quick start

```bash
# Requires Python 3.11+
uv sync

# Run both v1 and v2 allocators on the example config,
# with Sales achieving 1.4× baseline KPI:
uv run python -m cli allocate example_config.yaml --ach Sales=1.4

# Reachability audit only — flags depts whose stretch can't reach A/S tier:
uv run python -m cli audit example_config.yaml

# Run stress + deep stress tests (135 scenarios total):
uv run python -m cli stress

# Run unit tests:
uv run pytest tests/ -v
```

**Expected output** of `allocate`:

```
=== Reachability audit (v2) ===
 department      beta  stretch_impact  a_target_profit  ... can_reach_a
       Sales     0.08        2400000           450000  ...        True
 Procurement      1.0        2000000           450000  ...        True
        ...

=== v1 allocation ===
 department  allocated  rounds_won  achievement  weight       cap  ...
       Sales   198,000         198          1.4    0.24   221,000  ...

=== v2 allocation (governance) ===
 department  headcount  achievement  quota    c_hat  c_lower_95  c_star  ...
       Sales         20          1.4   0.20  640000      460000  322000  ...

v2 total: ¥945,000 / ¥1,000,000
v2 deferred: ¥55,000
release gates: {'pool_utilization_90_to_100': True, 'no_nan_bonus': True, ...}
all gates pass: YES
```

---

## 5. Layer 1 · Sensitivity modeling

**File**: `sensitivity.py`

The simplest profit model is linear in KPI deltas:

```
Profit = Profit_baseline + Σ_d β_d × (KPI_d − baseline_d)
```

A log-linear form is also supported:

```
log(Profit) = log(Profit_baseline) + Σ_d β_d × log(KPI_d / baseline_d)
```

### Unit convention

Every KPI's unit must pair with its β such that `β × ΔKPI = profit delta (yuan)`. Example:

| Department | KPI unit | β | Meaning |
|---|---|---|---|
| Sales | yuan revenue | 0.08 | each ¥1 revenue → ¥0.08 profit |
| Procurement | yuan cost reduction | 1.0 | direct pass-through |
| Manufacturing | efficiency % | 250,000 | each 1% → ¥250k profit |
| Quality | complaint count | 100,000 | each avoided complaint → ¥100k |

### β validation

`monte_carlo_profit()` simulates N scenarios with random achievements, then OLS-regresses simulated profit on each dept's KPI. The recovered β̂ should match input β within <1%. This is your safety check that the linear model is internally consistent.

### Profit gap

```
profit_gap = profit_target − profit_baseline
```

This is the "gap" the bonus pool is meant to motivate departments to close. It drives tier calibration in Layer 2.

---

## 6. Layer 2 · Tier calibration

**File**: `tiers.py`

Each department gets two tier lines (A and S) above baseline B:

```
KPI_d^A = KPI_d^B + θ_A × profit_gap / β_d
KPI_d^S = KPI_d^B + θ_S × profit_gap / β_d
```

### Why this works

- **Low-β departments get wide tier bands.** They must swing further in KPI to earn the same bonus weight.
- **High-β departments get narrow tier bands.** Small KPI moves → large profit impact → small tier distance.
- Every tier transition represents the **same ¥ contribution** to closing the profit gap. This is the mathematical core of cross-department fairness.

### Defaults

| Param | Default | Meaning |
|---|---|---|
| `theta_a` | 0.15 | A tier closes 15% of profit gap |
| `theta_s` | 0.30 | S tier closes 30% of profit gap |
| Cap rule | `min(kpi_s, kpi_stretch)` | Tier lines never exceed user-defined stretch |

### Reachability

A department whose `β × (kpi_stretch − kpi_baseline) < θ_A × profit_gap` **cannot reach A tier**, no matter how hard they perform. The v2 audit flags this so you can renegotiate quotas honestly (see [§8](#8-v2-governance-in-depth)).

---

## 7. Layer 3 · Allocation (v1 & v2)

### v1 — Knapsack greedy

**File**: `allocator.py`

The pool is sliced into N divisions (default 1000). Each round, the department with the highest marginal value/cost ratio wins one division.

```
Value:       V_d = w_d × ln(1 + (s'_d + division) / target_d)
Knapsack:    KS_d = V_d / (rounds_d + 1)        ← winner penalty
```

Constraints:
- Floor: every department gets at least `min_pool_share × pool_total`
- Cap: scales linearly with achievement, clipped at S-tier allocation
- Ineligible: departments below baseline (ach < 1.0) get floor only

### v2 — Base + perf pool

**File**: `v2_allocator.py`

Splits the pool into two pots:

```
Base pool (λ P)  : split by headcount       → "same employees get same base"
Perf pool ((1−λ)P): split by c_star         → "same contribution gets same perf bonus"
```

Full formulas in [§8](#8-v2-governance-in-depth).

---

## 8. v2 governance in depth

v1 was math-correct but had three real-world flaws Codex's review forced us to fix:

| Flaw | v1 behavior | v2 fix |
|---|---|---|
| Same contribution claimed but not delivered | caps and floors distorted ratios | quota + headcount-base separates base from perf |
| Stretch-reachability ignored | silent misclassification | explicit audit + renegotiation flag |
| β treated as truth | noisy estimates → real money | one-sided 95% lower bound |

### 8.1 Responsibility shares (`quota`)

Each department declares `q_d` such that `Σ q_d = 1`. The interpretation: "if every department hits A tier, exactly one profit gap is closed — not N × θ."

- If all departments set `quota`, config validates sum = 1 (else raises).
- If no department sets `quota`, it's derived from stretch_impact share.

### 8.2 Confidence-adjusted impact (`c_star`)

```
ΔKPI_d    = actual KPI change from baseline
ĉ_hat     = β̂_d × ΔKPI_d                    ← point estimate
SE(ĉ)     = |ΔKPI_d| × SE(β̂_d)              ← linear model variance
ĉ_lower   = ĉ_hat − 1.645 × SE(ĉ)           ← one-sided 95% lower bound
C_d*      = ρ_d × max(0, ĉ_lower)           ← quality-adjusted, floored at 0
```

- **1.645** is the z-score for one-sided 95% confidence.
- **ρ_d ∈ [0,1]** (`beta_confidence_weight`) is source-quality: RCT=1.0, regression=0.7, expert=0.3.
- The `max(0, …)` clips noisy estimates to zero — uncertain money is **not paid out**.

### 8.3 Score and perf bonus

```
target_d  = q_d × profit_gap                 ← this dept's share of the gap
a_d       = C_d* / target_d                  ← achievement rate
s_d       = q_d × min(max(a_d, 0), a_max)    ← clipped score (a_max default 1.5)
PerfBonus_d = (1−λ) P × s_d / Σ s_j
```

The `a_max` clip prevents one department with extreme achievement from dominating the perf pool.

### 8.4 Base bonus

```
BaseBonus_d = λ P × h_d / H                  ← h_d = headcount, H = total
```

Pure headcount split. Same number of bodies → same base bonus, regardless of dept.

### 8.5 Cap and overflow

If a cap is set (`caps={dept: yuan}`) and a department's bonus exceeds it:
1. Clip to cap, route excess to remaining departments proportional to `s_d`.
2. Iterate up to 10 times (cascading redistribution).
3. If everything is capped or zero-scored, residual goes to deferred pool.

### 8.6 Deferred pool

When `deferred_pool_enabled=true` (default), unallocated residual is **deferred** (set aside for management disposition) rather than force-distributed. This is safer — paying out the full pool at any cost can create perverse incentives.

### 8.7 Reachability audit

**`cli audit example_config.yaml`** prints a table:

```
department    stretch_impact  a_target_profit  can_reach_a  can_reach_s
Sales               2,400,000          450,000         True         True
Engineering           900,000          450,000         True        False  ⚠
```

If `can_reach_a=False`, that department cannot hit A tier even at full stretch. **Don't silently let them underperform — renegotiate quota or KPI baseline first.**

---

## 9. Release gates

`six` boolean checks. **All must be `True`** before any payout.

```python
{
  "pool_utilization_90_to_100":         True,   # 90% ≤ allocated + deferred ≤ 100%
  "no_nan_bonus":                       True,   # no NaN in any bonus
  "no_negative_bonus":                  True,   # every bonus ≥ 0
  "achievers_have_nonneg_c_star":       True,   # ach ≥ 1.0 ⇒ c_star ≥ 0
  "quotas_sum_to_one":                  True,   # Σ quota = 1 (within 1e-6)
  "monotonic_in_c_star_within_quota":   True,   # same quota ⇒ c_star ↑ ⇒ perf_bonus ↑
}
```

If any gate is `False`, CLI exits with code 1 and prints `all gates pass: NO — DO NOT PAY`.

---

## 10. Configuration reference

See `example_config.yaml` for a fully documented example. Key fields:

### `pool:` section

| Field | Type | Default | Meaning |
|---|---|---|---|
| `pool_total` | float | required | Total bonus pool in yuan |
| `profit_target` | float | required | Target profit; gap = target − baseline drives calibration |
| `profit_baseline` | float | required | Profit when all depts are at KPI baseline |
| `theta_a`, `theta_s` | float | 0.15, 0.30 | A/S tier profit-share thresholds |
| `divisions` | int | 1000 | v1 knapsack granularity |
| `min_pool_share` | float | 0.0 | Floor: minimum pool share per dept |
| `lambda_base_ratio` | float | 0.3 | v2: share of pool split by headcount |
| `a_max` | float | 1.5 | v2: achievement-rate clip ceiling |
| `deferred_pool_enabled` | bool | true | v2: allow residual to defer |

### `departments:` section (per dept)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | required | Department identifier |
| `kpi_baseline` | float | required | 100% target KPI |
| `kpi_stretch` | float | required | S-tier stretch KPI (cap) |
| `beta` | float | required | Profit elasticity (`β × ΔKPI = profit yuan`) |
| `headcount` | int | 1 | People in dept (drives v2 base pool) |
| `base_bonus` | float | 0.0 | v1 fixed B-tier bonus |
| `quota` | float | None | v2 responsibility share (all-or-none, sum=1) |
| `beta_ci_lower`, `beta_ci_upper` | float | None | 95% CI bounds for β̂; SE=0 if omitted |
| `beta_confidence_weight` | float | 1.0 | ρ ∈ [0,1], confidence in β estimate |
| `beta_source` | str | "unspecified" | Provenance: regression / bridge_model / expert_estimate / industry_benchmark |
| `note` | str | "" | Free-form stakeholder note |

### Validation

`Config.validate_v2()` raises on misconfiguration:
- Partial `quota` (some set, some not) → raises
- `quota` sum ≠ 1.0 → raises
- `beta_confidence_weight` ∉ [0,1] → raises
- `beta_ci_lower > beta` or `beta > beta_ci_upper` → raises
- `lambda_base_ratio` ∉ [0,1] → raises

---

## 11. CLI reference

```bash
# Run v1 + v2 with optional achievement overrides:
uv run python -m cli allocate <config.yaml> [--ach Dept1=1.4 Dept2=1.2 ...]
# Exit code: 0 if all gates pass, 1 otherwise.

# Reachability audit only:
uv run python -m cli audit <config.yaml>

# Stress + deep stress tests:
uv run python -m cli stress
```

### Achievement format

`--ach Sales=1.4 Manufacturing=1.2` means Sales achieved 1.4× its baseline KPI, Manufacturing 1.2×. Departments not listed default to `1.0` (at baseline).

---

## 12. Validation & stress testing

| Suite | File | Scope | Pass rate |
|---|---|---|---|
| v1 arithmetic | `tests/test_validation.py` | 13 tests | 13/13 ✓ |
| v2 governance | `tests/test_v2_validation.py` | 14 tests | 14/14 ✓ |
| Basic stress | `stress_test.py` | 119 scenarios | 119/119 ✓ |
| Deep stress | `deep_stress_test.py` | 16 checks | 16/16 ✓ |

### Stress coverage

- **Scale**: 8 → 100 → 1000 → 5000 departments (linear time)
- **Extreme achievement**: 0.0, 0.5, 1.0, 2.0, 10.0, 100.0
- **Zero-score freeze**: all ρ=0 → entire perf pool deferred
- **Cap overflow**: every dept capped → cascading redistribution
- **Wide CI**: CI > β → c_star clipped to zero
- **Fuzz**: 100 random configs, all release gates must pass
- **Adversarial**: 1 mega-dept + 99 tiny depts
- **Long-run**: 1000 fuzz iterations, determinism + no drift
- **Failure injection**: inverted CI, ρ ∉ [0,1], quota sum ≠ 1 — all raise
- **Property tests**: monotonicity, scaling invariance (2× pool → 2× bonus)

### Benchmark (8 departments, MacBook M-series)

```
v2 allocate:        < 5 ms
v2 allocate (5000 depts): ~38 ms
fuzz 1000 runs:    ~1.5 s total
```

---

## 13. Road to production

This is a **model**. Models do not pay people. Real bonus payouts require:

1. **All release gates pass** on the production config.
2. **Historical backtest**: feed 4–8 quarters of real KPI data through v2, verify β stability and ranking consistency vs. known business outcomes.
3. **Shadow-run quarter**: run v1 (current process) and v2 (new model) in parallel for one full quarter without telling anyone v2 exists. Compare distributions.
4. **Reproducibility snapshot**: record config SHA + code commit + seed + approver for every payout run. File it.
5. **Sign-off**: CFO + HR + business lead sign in writing. No sign-off, no payout.

If you skip any of these and pay people based solely on this code's output, **you are doing it wrong**.

---

## 14. What's NOT here

- **Historical backtest**: requires your real financial data; cannot be pre-baked.
- **Shadow run harness**: requires your real v1 baseline.
- **Reproducibility snapshot pipeline**: the code produces the dict, but wiring to your approval workflow is on you.
- **Feishu / Slack / Teams push integration**: this public release removes the messaging client. Set up your own integration using the allocator's output DataFrame.
- **Dashboard**: `dashboard.py` (Streamlit) is included but not documented; treat it as a reference visualization, not production UI.

---

## 15. License & caveats

**MIT License** — see `LICENSE`.

### Caveats

1. β values are **estimates**, not measurements. Garbage in, garbage out.
2. The linear profit model is a **first-order approximation**. Large KPI swings (±50%+) violate it.
3. v2's `a_max=1.5` clip is a **policy choice**, not a law. Adjust per your risk appetite.
4. Headcount-based base pool assumes **all heads count equally**. Adjust if seniority/role mix matters.
5. Quota negotiation is **political**, not mathematical. This tool surfaces the trade-offs; it doesn't resolve them.

### Contributing

This is a sanitized public release of an internal prototype. Bug reports and math critiques are welcome; feature requests will be evaluated against the "does this belong in a governance-critical allocator" bar.

---

**Designed for CFOs, HR leaders, and business owners who want bonus allocation to be auditable, debatable, and defensible — not magical.**
