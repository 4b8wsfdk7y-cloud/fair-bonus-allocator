# bonus-allocator

**A sensitivity-driven bonus pool allocator that translates heterogeneous department KPIs into a common currency — marginal profit contribution — and distributes a shared bonus pool under explicit fairness and governance constraints.**

[English](README.md) · [简体中文](README.zh-CN.md)

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
9. [Statistical foundations](#9-statistical-foundations)
10. [Release gates](#10-release-gates)
11. [Configuration reference](#11-configuration-reference)
12. [CLI reference](#12-cli-reference)
13. [Validation & stress testing](#13-validation--stress-testing)
14. [Road to production](#14-road-to-production)
15. [What's NOT here](#15-whats-not-here)
16. [License & caveats](#16-license--caveats)

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

**This project's answer**: translate every KPI to **profit contribution** using the β elasticity ($\beta \cdot \Delta\text{KPI} = \text{profit yuan}$), then allocate under explicit fairness rules. This makes "who earned what" computable, auditable, and debatable on the same scale.

---

## 2. Design principle in one page

### The core problem

Three kinds of unfairness sneak into bonus pools:

1. **Apples-vs-oranges**: Sales gets more because revenue numbers look bigger, even when Procurement saved more profit.
2. **Moving goalposts**: Department stretch KPIs are set arbitrarily, so "A tier" in one dept ≠ "A tier" in another.
3. **Fake precision**: $\hat\beta$ is estimated from limited data, but the allocator treats it as truth — noise becomes money.

### The three-step pipeline

```
Heterogeneous KPIs
    │
    ▼  Layer 1: β · ΔKPI = profit yuan
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
| **L1 — Equal contribution** | Same $\Delta\text{KPI} \cdot \beta$ → same profit yuan | Layer 1 (math identity) |
| **L2 — Equal tier** | Hitting "A tier" in any dept closes the same share of profit gap | Layer 2 (calibration) |
| **L3 — Equal bonus** | Same contribution → same bonus; same headcount → same base | Layer 3 (v2 allocator) |

### Confidence-aware

$\hat\beta$ is an **estimate**, not truth. v2 uses a one-sided 95% lower bound:

$$C_d^{\,*} \;=\; \rho_d \cdot \max\!\Big(0,\;\hat\beta_d \cdot \Delta\text{KPI}_d \;-\; z_{0.95}\cdot|\Delta\text{KPI}_d|\cdot\text{SE}(\hat\beta_d)\Big)$$

where $z_{0.95} = 1.645$ is the one-sided 95% z-score. Only profit contribution we're confident was actually earned becomes bonus. Noisy estimates get clipped to zero rather than paid out.

### Launch threshold

**6 release gates** must all pass before any payout. If any fails, the run is marked `DO NOT PAY`. See [§10](#10-release-gates).

---

## 3. Three-layer architecture

```
Layer 1 · Sensitivity modeling      [sensitivity.py]
    Profit = Profit_baseline + Σ_d  β_d · (KPI_d − baseline_d)
    Validates via Monte Carlo + OLS regression (recovers β within <1%).

Layer 2 · Tier calibration          [tiers.py]
    β_d · (KPI_d^A − baseline) = θ_A · profit_gap
    β_d · (KPI_d^S − baseline) = θ_S · profit_gap
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

$$\text{Profit} \;=\; \text{Profit}_{\text{baseline}} \;+\; \sum_d \beta_d \cdot \big(\text{KPI}_d - \text{baseline}_d\big)$$

A log-linear form is also supported:

$$\log(\text{Profit}) \;=\; \log(\text{Profit}_{\text{baseline}}) \;+\; \sum_d \beta_d \cdot \log\!\left(\frac{\text{KPI}_d}{\text{baseline}_d}\right)$$

The two forms agree to first order for small KPI perturbations; the linear form is the default because its coefficients are directly interpretable as "profit per unit KPI."

### Unit convention

Every KPI's unit must pair with its $\beta$ such that $\beta \cdot \Delta\text{KPI} = \text{profit delta (yuan)}$. Example:

| Department | KPI unit | $\beta$ | Meaning |
|---|---|---|---|
| Sales | yuan revenue | 0.08 | each ¥1 revenue → ¥0.08 profit |
| Procurement | yuan cost reduction | 1.0 | direct pass-through |
| Manufacturing | efficiency % | 250,000 | each 1% → ¥250k profit |
| Quality | complaint count | 100,000 | each avoided complaint → ¥100k |

### β validation — Monte Carlo + OLS

`monte_carlo_profit()` simulates $N$ scenarios by drawing random achievement multipliers $a_d \sim \text{Uniform}(0.8, 1.5)$ for each department, computing the resulting profit, then OLS-regressing simulated profit on each dept's KPI:

$$\text{Profit}^{(s)} \;=\; \alpha \;+\; \sum_d \hat\beta_d^{\,\text{OLS}} \cdot \text{KPI}_d^{(s)} \;+\; \varepsilon^{(s)}$$

The recovered $\hat\beta_d^{\,\text{OLS}}$ should match the input $\beta_d$ within <1%. This is your safety check that the linear model is internally consistent and that the $\beta$ values you loaded actually reproduce the profit model you claim to have.

### Profit gap

$$\text{profit\_gap} \;=\; \text{profit\_target} \;-\; \text{profit\_baseline}$$

This is the "gap" the bonus pool is meant to motivate departments to close. It drives tier calibration in Layer 2.

---

## 6. Layer 2 · Tier calibration

**File**: `tiers.py`

Each department gets two tier lines (A and S) above baseline B. The calibration rule sets the tier line so that hitting it closes a fixed **share** of the profit gap:

$$\beta_d \cdot \big(\text{KPI}_d^{A} - \text{baseline}_d\big) \;=\; \theta_A \cdot \text{profit\_gap}$$

$$\beta_d \cdot \big(\text{KPI}_d^{S} - \text{baseline}_d\big) \;=\; \theta_S \cdot \text{profit\_gap}$$

Solving for the KPI tier lines:

$$\text{KPI}_d^{A} \;=\; \text{baseline}_d \;+\; \frac{\theta_A \cdot \text{profit\_gap}}{\beta_d}$$

$$\text{KPI}_d^{S} \;=\; \text{baseline}_d \;+\; \frac{\theta_S \cdot \text{profit\_gap}}{\beta_d}$$

### Why this works

- **Low-$\beta$ departments get wide tier bands.** They must swing further in KPI to earn the same bonus weight.
- **High-$\beta$ departments get narrow tier bands.** Small KPI moves → large profit impact → small tier distance.
- Every tier transition represents the **same ¥ contribution** to closing the profit gap. This is the mathematical core of cross-department fairness.

### Defaults

| Param | Default | Meaning |
|---|---|---|
| $\theta_A$ | 0.15 | A tier closes 15% of profit gap |
| $\theta_S$ | 0.30 | S tier closes 30% of profit gap |
| Cap rule | $\min(\text{KPI}^S, \text{KPI}^{\text{stretch}})$ | Tier lines never exceed user-defined stretch |

### Reachability

A department whose $\beta_d \cdot (\text{KPI}_d^{\text{stretch}} - \text{baseline}_d) < \theta_A \cdot \text{profit\_gap}$ **cannot reach A tier**, no matter how hard they perform. The v2 audit flags this so you can renegotiate quotas honestly (see [§8](#8-v2-governance-in-depth)).

---

## 7. Layer 3 · Allocation (v1 & v2)

### v1 — Knapsack greedy

**File**: `allocator.py`

The pool is sliced into $N$ divisions (default 1000). Each round, the department with the highest marginal value/cost ratio wins one division.

$$V_d \;=\; w_d \cdot \ln\!\left(1 + \frac{s_d' + \text{division}}{\text{target}_d}\right)$$

$$\text{KS}_d \;=\; \frac{V_d}{\text{rounds}_d + 1} \qquad \text{(winner penalty)}$$

Constraints:
- **Floor**: every department gets at least $\text{min\_pool\_share} \cdot P_{\text{total}}$
- **Cap**: scales linearly with achievement, clipped at S-tier allocation
- **Ineligible**: departments below baseline ($a_d < 1.0$) get floor only

### v2 — Base + perf pool

**File**: `v2_allocator.py`

Splits the pool into two pots:

$$\underbrace{\lambda P}_{\text{base pool}} \;\text{split by headcount} \qquad \underbrace{(1-\lambda) P}_{\text{perf pool}} \;\text{split by } C_d^{\,*}$$

The base pool enforces "same employees get same base." The perf pool enforces "same contribution gets same perf bonus." Full formulas in [§8](#8-v2-governance-in-depth).

---

## 8. v2 governance in depth

v1 was math-correct but had three real-world flaws Codex's review forced us to fix:

| Flaw | v1 behavior | v2 fix |
|---|---|---|
| Same contribution claimed but not delivered | caps and floors distorted ratios | quota + headcount-base separates base from perf |
| Stretch-reachability ignored | silent misclassification | explicit audit + renegotiation flag |
| β treated as truth | noisy estimates → real money | one-sided 95% lower bound |

### 8.1 Responsibility shares (`quota`)

Each department declares $q_d$ such that $\sum_d q_d = 1$. The interpretation: "if every department hits A tier, exactly one profit gap is closed — not $N \cdot \theta$."

$$\sum_{d=1}^{N} q_d \;=\; 1$$

- If all departments set `quota`, config validates sum = 1 (else raises).
- If no department sets `quota`, it's derived from stretch_impact share:

$$q_d \;=\; \frac{\beta_d \cdot (\text{KPI}_d^{\text{stretch}} - \text{baseline}_d)}{\sum_j \beta_j \cdot (\text{KPI}_j^{\text{stretch}} - \text{baseline}_j)}$$

### 8.2 Confidence-adjusted impact ($C_d^{\,*}$)

This is the heart of v2's statistical governance. We have a point estimate $\hat\beta_d$ with standard error $\text{SE}(\hat\beta_d)$. Given an observed KPI delta $\Delta\text{KPI}_d$, the profit contribution estimate is:

$$\hat C_d \;=\; \hat\beta_d \cdot \Delta\text{KPI}_d$$

Because the profit model is **linear in $\beta$**, the standard error propagates linearly:

$$\text{SE}(\hat C_d) \;=\; \left|\frac{\partial \hat C_d}{\partial \hat\beta_d}\right| \cdot \text{SE}(\hat\beta_d) \;=\; |\Delta\text{KPI}_d| \cdot \text{SE}(\hat\beta_d)$$

We then construct a **one-sided 95% lower confidence bound** on the true contribution:

$$\hat C_d^{\,\text{lower}} \;=\; \hat C_d \;-\; z_{0.95} \cdot \text{SE}(\hat C_d) \;=\; \hat\beta_d \cdot \Delta\text{KPI}_d \;-\; 1.645 \cdot |\Delta\text{KPI}_d| \cdot \text{SE}(\hat\beta_d)$$

**Why one-sided?** We're willing to tolerate upside surprise (true contribution higher than estimated) but not downside surprise (we paid out money that wasn't actually earned). A two-sided CI would be too conservative; a one-sided lower bound at 95% confidence means: *if the model is correct, the true contribution exceeds $\hat C_d^{\,\text{lower}}$ with probability 0.95.*

The lower bound is then **floored at zero** (you can't have negative contribution) and **scaled by source-quality weight** $\rho_d$:

$$\boxed{\;C_d^{\,*} \;=\; \rho_d \cdot \max\!\Big(0,\;\hat\beta_d \cdot \Delta\text{KPI}_d \;-\; 1.645 \cdot |\Delta\text{KPI}_d| \cdot \text{SE}(\hat\beta_d)\Big)\;}$$

where $\rho_d \in [0,1]$ (`beta_confidence_weight`) is source-quality:

| Source | Suggested $\rho_d$ |
|---|---|
| Randomized controlled trial | 1.0 |
| Regression on historical data | 0.7 |
| Bridge model (accounting identity) | 0.9 |
| Industry benchmark | 0.5 |
| Expert estimate | 0.3 |

The `max(0, \ldots)` clips noisy estimates to zero — **uncertain money is not paid out**.

### 8.3 Standard error from CI

If you supply a 95% CI $[\beta^{\,\text{lower}}, \beta^{\,\text{upper}}]$ for $\hat\beta_d$:

$$\text{SE}(\hat\beta_d) \;=\; \frac{\beta^{\,\text{upper}} - \beta^{\,\text{lower}}}{2 \cdot 1.96}$$

This follows from the symmetric two-sided CI construction $\hat\beta \pm 1.96 \cdot \text{SE}$. If no CI is supplied, $\text{SE}(\hat\beta_d) = 0$ and $C_d^{\,*}$ collapses to $\rho_d \cdot \max(0, \hat\beta_d \cdot \Delta\text{KPI}_d)$ — a pure point estimate with no uncertainty discount.

### 8.4 Score and perf bonus

Each department's "target" is its quota share of the profit gap:

$$\text{target}_d \;=\; q_d \cdot \text{profit\_gap}$$

The achievement rate is the ratio of confidence-adjusted contribution to target:

$$a_d \;=\; \frac{C_d^{\,*}}{\text{target}_d}$$

The score is the achievement rate clipped to $[0, a_{\max}]$ and reweighted by quota (so a high-achievement but low-quota department can still only claim its share):

$$s_d \;=\; q_d \cdot \min\!\big(\max(a_d,\,0),\;a_{\max}\big)$$

The perf bonus is the perf pool allocated proportional to $s_d$:

$$\text{PerfBonus}_d \;=\; (1-\lambda) \cdot P \cdot \frac{s_d}{\sum_j s_j}$$

The $a_{\max}$ clip (default 1.5) prevents one department with extreme achievement from dominating the perf pool.

### 8.5 Base bonus

$$\text{BaseBonus}_d \;=\; \lambda \cdot P \cdot \frac{h_d}{H}$$

where $h_d$ is department headcount and $H = \sum_j h_j$ is total headcount. Pure headcount split — same number of bodies → same base bonus, regardless of dept.

### 8.6 Total bonus and caps

$$\text{Bonus}_d \;=\; \text{BaseBonus}_d + \text{PerfBonus}_d$$

Optional per-department caps trigger cascading redistribution:

1. If $\text{Bonus}_d > \text{cap}_d$: clip to cap, route excess to remaining departments proportional to $s_d$.
2. Iterate up to 10 times (bounded to prevent infinite loops).
3. If everything is capped or zero-scored, residual goes to deferred pool.

### 8.7 Deferred pool

When `deferred_pool_enabled=true` (default), unallocated residual is **deferred** (set aside for management disposition) rather than force-distributed:

$$\text{deferred} \;=\; \max\!\Big(P \;-\; \sum_d \text{Bonus}_d,\;0\Big)$$

This is safer — paying out the full pool at any cost can create perverse incentives.

### 8.8 Reachability audit

**`cli audit example_config.yaml`** prints:

```
department    stretch_impact  a_target_profit  can_reach_a  can_reach_s
Sales               2,400,000          450,000         True         True
Engineering           900,000          450,000         True        False  ⚠
```

If `can_reach_a=False`, that department cannot hit A tier even at full stretch. **Don't silently let them underperform — renegotiate quota or KPI baseline first.**

---

## 9. Statistical foundations

This section makes the math and statistics explicit. Read it before trusting any payout.

### 9.1 The linear profit model and its assumptions

We assume:

$$\text{Profit}(\mathbf{x}) \;=\; \text{Profit}_0 \;+\; \sum_{d=1}^{N} \beta_d \cdot (x_d - x_d^{\,0}) \;+\; \varepsilon$$

where $\mathbf{x} = (x_1, \ldots, x_N)$ is the vector of department KPIs, $\mathbf{x}^{\,0}$ is the baseline, and $\varepsilon$ is an unmodeled residual.

**Key assumptions:**

1. **Linearity**: marginal profit per unit KPI is constant within the operating range. Validated by the Monte Carlo + OLS recovery test (§5).
2. **Additivity**: departments don't interact. Sales revenue and Manufacturing efficiency contribute independently. This breaks down when departments compete for the same resources (e.g., Sales selling capacity-constrained output).
3. **$\beta$ is constant in time**: the elasticity you measured last quarter still holds this quarter. Backtest this before paying.
4. **$\hat\beta$ is unbiased**: OLS gives an unbiased estimate under the Gauss-Markov conditions (exogeneity, no perfect multicollinearity, homoscedasticity). For time-series or panel data, use HAC or cluster-robust SE.

### 9.2 Why one-sided 95% lower bound, not two-sided CI

A two-sided 95% CI on $\hat C_d$ would be $\hat C_d \pm 1.96 \cdot \text{SE}(\hat C_d)$. Using the lower bound as the bonus basis means we pay out only when we're 97.5% confident the contribution is at least that large (because the lower bound itself is at the 2.5th percentile of the sampling distribution).

A **one-sided** 95% lower bound at $\hat C_d - 1.645 \cdot \text{SE}(\hat C_d)$ means: *the true contribution exceeds this value with probability 0.95.* We accept a 5% chance that we've overestimated the contribution; we don't care about the symmetric upper-tail risk because over-performance is not a financial risk to the firm.

| | Two-sided 95% CI | One-sided 95% lower bound |
|---|---|---|
| z-score | 1.96 | 1.645 |
| Confidence that $C \geq \text{bound}$ | 97.5% | 95% |
| Strictness | More conservative | Less conservative |
| Use case | General inference | Asymmetric loss (we only fear overpaying) |

**Sensitivity**: at $|\Delta\text{KPI}| = 1000$ and $\text{SE}(\hat\beta) = 0.01$, the uncertainty discount is $1.645 \cdot 1000 \cdot 0.01 = 16.45$ profit yuan per unit of $\hat\beta$. If $\hat\beta \cdot \Delta\text{KPI} < 16.45$, the entire contribution is clipped to zero.

### 9.3 Why $z_{0.95} = 1.645$?

For a standard normal $Z \sim \mathcal{N}(0,1)$:

$$P(Z \leq 1.645) \;\approx\; 0.95$$

So $P\!\big(\hat C_d - 1.645 \cdot \text{SE}(\hat C_d) \;\leq\; C_d\big) \;\approx\; 0.95$ — the lower bound holds with 95% confidence (asymptotically, by the CLT, for $\hat\beta$ estimated from enough data).

If you want a stricter gate (e.g., 99% confidence), use $z_{0.99} = 2.326$ by editing `Z_95_ONE_SIDED` in `v2_allocator.py`. The trade-off: stricter gates → more clipping → larger deferred pool → fewer departments paid.

### 9.4 Why $\rho_d \in [0,1]$ source-quality weight?

$\rho_d$ is a **Bayesian-style prior** on how much to trust $\hat\beta_d$. It multiplies the entire $C_d^{\,*}$ after the lower bound is applied. Rationale:

- $\rho_d = 1.0$: "I trust this $\hat\beta_d$ fully. The lower bound alone is enough discounting."
- $\rho_d = 0.7$: "I trust this regression estimate, but there might be unmodeled confounders. Discount 30%."
- $\rho_d = 0.3$: "This is an expert guess. Discount 70% — most of the apparent contribution shouldn't be paid out."

This is a **decision-theoretic** knob, not a statistical one. It lets the business communicate "this $\beta$ is shaky" without throwing the estimate away entirely.

### 9.5 Why quota sums to 1?

The quota system encodes the **responsibility allocation**: "Department $d$ is responsible for $q_d$ share of the profit gap." Without this, the v1 model implicitly assumed every department was responsible for the *entire* gap (sum of $\theta$ across depts could exceed 1, making "everyone at A tier" over-close the gap).

With $\sum q_d = 1$:

- If every department hits A tier, exactly one profit gap is closed (assuming $\theta_A \cdot q_d$ replaces $\theta_A$ in the tier equation).
- "Everyone hits A tier" is internally consistent with the profit target.
- Quota is a **negotiated** quantity, not a measured one. The model surfaces the trade-off; humans resolve it.

### 9.6 Determinism and reproducibility

`allocate_v2()` is a pure function of `(config, sens, achievements, caps)`. Given the same inputs, it returns byte-identical outputs (verified by the long-run stress test, §13). This is a hard requirement for audit — if a payout can't be reproduced, it can't be defended.

### 9.7 What the release gates actually check

See [§10](#10-release-gates). Each gate is a single boolean derived from the output DataFrame. They are **necessary, not sufficient** — passing all 6 gates means the run is internally consistent; it does not mean the $\beta$ values are correct or the model is appropriate for your business.

---

## 10. Release gates

Six boolean checks. **All must be `True`** before any payout.

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

| Gate | What it catches |
|---|---|
| `pool_utilization_90_to_100` | allocator bug (under/over-allocation), misconfigured deferred pool |
| `no_nan_bonus` | numerical blowup (e.g., $\hat\beta = \infty$) |
| `no_negative_bonus` | floor/cap interaction bug |
| `achievers_have_nonneg_c_star` | CI wider than $\hat\beta$ for an achiever → clipping logic failure |
| `quotas_sum_to_one` | config validation bypassed or float drift |
| `monotonic_in_c_star_within_quota` | "same contribution → same bonus" promise violated |

If any gate is `False`, CLI exits with code 1 and prints `all gates pass: NO — DO NOT PAY`.

---

## 11. Configuration reference

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
| `beta` | float | required | Profit elasticity ($\beta \cdot \Delta\text{KPI} = \text{profit yuan}$) |
| `headcount` | int | 1 | People in dept (drives v2 base pool) |
| `base_bonus` | float | 0.0 | v1 fixed B-tier bonus |
| `quota` | float | None | v2 responsibility share (all-or-none, sum=1) |
| `beta_ci_lower`, `beta_ci_upper` | float | None | 95% CI bounds for $\hat\beta$; SE=0 if omitted |
| `beta_confidence_weight` | float | 1.0 | $\rho \in [0,1]$, confidence in β estimate |
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

## 12. CLI reference

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

## 13. Validation & stress testing

| Suite | File | Scope | Pass rate |
|---|---|---|---|
| v1 arithmetic | `tests/test_validation.py` | 13 tests | 13/13 ✓ |
| v2 governance | `tests/test_v2_validation.py` | 14 tests | 14/14 ✓ |
| Basic stress | `stress_test.py` | 119 scenarios | 119/119 ✓ |
| Deep stress | `deep_stress_test.py` | 16 checks | 16/16 ✓ |

### Stress coverage

- **Scale**: 8 → 100 → 1000 → 5000 departments (linear time)
- **Extreme achievement**: 0.0, 0.5, 1.0, 2.0, 10.0, 100.0
- **Zero-score freeze**: all $\rho = 0$ → entire perf pool deferred
- **Cap overflow**: every dept capped → cascading redistribution
- **Wide CI**: CI > $\hat\beta$ → $C_d^{\,*}$ clipped to zero
- **Fuzz**: 100 random configs, all release gates must pass
- **Adversarial**: 1 mega-dept + 99 tiny depts
- **Long-run**: 1000 fuzz iterations, determinism + no drift
- **Failure injection**: inverted CI, $\rho \notin [0,1]$, quota sum ≠ 1 — all raise
- **Property tests**: monotonicity, scaling invariance ($2P \Rightarrow 2\,\text{bonus}$)

### Benchmark (8 departments, MacBook M-series)

```
v2 allocate:        < 5 ms
v2 allocate (5000 depts): ~38 ms
fuzz 1000 runs:    ~1.5 s total
```

---

## 14. Road to production

This is a **model**. Models do not pay people. Real bonus payouts require:

1. **All release gates pass** on the production config.
2. **Historical backtest**: feed 4–8 quarters of real KPI data through v2, verify $\hat\beta$ stability and ranking consistency vs. known business outcomes.
3. **Shadow-run quarter**: run v1 (current process) and v2 (new model) in parallel for one full quarter without telling anyone v2 exists. Compare distributions.
4. **Reproducibility snapshot**: record config SHA + code commit + seed + approver for every payout run. File it.
5. **Sign-off**: CFO + HR + business lead sign in writing. No sign-off, no payout.

If you skip any of these and pay people based solely on this code's output, **you are doing it wrong**.

---

## 15. What's NOT here

- **Historical backtest**: requires your real financial data; cannot be pre-baked.
- **Shadow run harness**: requires your real v1 baseline.
- **Reproducibility snapshot pipeline**: the code produces the dict, but wiring to your approval workflow is on you.
- **Feishu / Slack / Teams push integration**: this public release removes the messaging client. Set up your own integration using the allocator's output DataFrame.
- **Dashboard**: `dashboard.py` (Streamlit) is included but not documented; treat it as a reference visualization, not production UI.

---

## 16. License & caveats

**MIT License** — see `LICENSE`.

### Caveats

1. $\hat\beta$ values are **estimates**, not measurements. Garbage in, garbage out.
2. The linear profit model is a **first-order approximation**. Large KPI swings (±50%+) violate it.
3. v2's $a_{\max} = 1.5$ clip is a **policy choice**, not a law. Adjust per your risk appetite.
4. Headcount-based base pool assumes **all heads count equally**. Adjust if seniority/role mix matters.
5. Quota negotiation is **political**, not mathematical. This tool surfaces the trade-offs; it doesn't resolve them.

### Contributing

This is a sanitized public release of an internal prototype. Bug reports and math critiques are welcome; feature requests will be evaluated against the "does this belong in a governance-critical allocator" bar.

---

**Designed for CFOs, HR leaders, and business owners who want bonus allocation to be auditable, debatable, and defensible — not magical.**
