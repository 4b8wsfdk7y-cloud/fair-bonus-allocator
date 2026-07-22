"""Configuration schema for the bonus allocator.

All business inputs are loaded from a single YAML file. The schema is
intentionally minimal — every field maps to something a CFO/HR can read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Department:
    """A department participating in the bonus pool."""

    name: str
    # KPI baseline (100% target), in the unit the business uses.
    kpi_baseline: float
    # KPI cap — the "S tier" stretch goal. Knapsack upper bound.
    kpi_stretch: float
    # Profit elasticity: absolute profit change per unit KPI change.
    # Estimated via Layer 1 sensitivity regression or finance bridge model.
    beta: float
    # Fixed base bonus allocated when KPI baseline is met (B tier).
    # Set to 0 if you want Knapsack to handle everything.
    base_bonus: float = 0.0
    # Headcount — used for per-capita visualization and v2 base-pool split.
    headcount: int = 1
    # Free-form notes for stakeholders.
    note: str = ""

    # ---- v2 governance fields (optional; default values degrade to v1) ----
    # Responsibility share q_d. If None, derived from stretch_impact share.
    # If provided across all departments, must sum to 1.0 (validated at load).
    quota: float | None = None
    # 95% confidence interval for β̂. If both provided, SE_β = (upper - lower) / (2 × 1.96).
    # Used by v2 to compute one-sided 95% lower bound on impact.
    beta_ci_lower: float | None = None
    beta_ci_upper: float | None = None
    # Model confidence weight ρ_d ∈ [0, 1]. How trustworthy the β estimate is.
    # Scaled by source quality (e.g., RCT=1.0, regression=0.7, expert guess=0.3).
    beta_confidence_weight: float = 1.0
    # Provenance: where β came from. One of: "regression", "bridge_model",
    # "expert_estimate", "industry_benchmark", "unspecified".
    beta_source: str = "unspecified"


@dataclass
class PoolConfig:
    """Top-level bonus pool and governance parameters."""

    pool_total: float
    # Profit target (net profit) the company must hit to trigger the pool.
    profit_target: float
    # Profit baseline (what KPI=100% across all departments yields).
    profit_baseline: float
    # Threshold ratio for A tier — department contributes this share of
    # the incremental profit gap (baseline → target).
    theta_a: float = 0.15
    # Threshold ratio for S tier.
    theta_s: float = 0.30
    # Number of knapsack divisions. Higher = finer allocation granularity.
    divisions: int = 1000
    # Minimum share of pool every participating department receives
    # (even if KPI is below A tier), as a fraction of pool_total.
    min_pool_share: float = 0.0

    # ---- v2 governance parameters ----
    # Base pool ratio λ — fraction of pool_total split by headcount.
    # Remaining (1 - λ) is the performance pool split by confidence-adjusted impact.
    lambda_base_ratio: float = 0.3
    # Achievement cap a_max — score s_d is clipped at q_d × a_max to prevent
    # a single department with extreme achievement from dominating the pool.
    a_max: float = 1.5
    # Allow v2 to defer residual pool to a separate pot when all departments
    # are frozen by quality gates or zero scores.
    deferred_pool_enabled: bool = True


@dataclass
class Config:
    pool: PoolConfig
    departments: list[Department] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f)
        pool_raw = raw["pool"]
        pool = PoolConfig(
            pool_total=float(pool_raw["pool_total"]),
            profit_target=float(pool_raw["profit_target"]),
            profit_baseline=float(pool_raw["profit_baseline"]),
            theta_a=float(pool_raw.get("theta_a", 0.15)),
            theta_s=float(pool_raw.get("theta_s", 0.30)),
            divisions=int(pool_raw.get("divisions", 1000)),
            min_pool_share=float(pool_raw.get("min_pool_share", 0.0)),
            lambda_base_ratio=float(pool_raw.get("lambda_base_ratio", 0.3)),
            a_max=float(pool_raw.get("a_max", 1.5)),
            deferred_pool_enabled=bool(pool_raw.get("deferred_pool_enabled", True)),
        )
        deps = [
            Department(
                name=d["name"],
                kpi_baseline=float(d["kpi_baseline"]),
                kpi_stretch=float(d["kpi_stretch"]),
                beta=float(d["beta"]),
                base_bonus=float(d.get("base_bonus", 0.0)),
                headcount=int(d.get("headcount", 1)),
                note=d.get("note", ""),
                quota=d.get("quota"),
                beta_ci_lower=d.get("beta_ci_lower"),
                beta_ci_upper=d.get("beta_ci_upper"),
                beta_confidence_weight=float(d.get("beta_confidence_weight", 1.0)),
                beta_source=d.get("beta_source", "unspecified"),
            )
            for d in raw["departments"]
        ]
        cfg = cls(pool=pool, departments=deps)
        cfg.validate_v2()
        return cfg

    def validate_v2(self) -> None:
        """Sanity-check v2 governance fields. Raises on misconfiguration."""
        # Quota: if any department sets quota, all must set it, and they sum to ~1.
        quotas = [d.quota for d in self.departments if d.quota is not None]
        if quotas:
            if len(quotas) != len(self.departments):
                raise ValueError(
                    "quota must be set on ALL departments or NONE; "
                    f"got {len(quotas)}/{len(self.departments)}"
                )
            total = sum(quotas)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"quota must sum to 1.0, got {total:.6f}")
            if any(q < 0 for q in quotas):
                raise ValueError("quota values must be non-negative")

        # β confidence weight in [0, 1].
        for d in self.departments:
            if not (0.0 <= d.beta_confidence_weight <= 1.0):
                raise ValueError(
                    f"dept '{d.name}': beta_confidence_weight must be in [0,1], "
                    f"got {d.beta_confidence_weight}"
                )

        # CI lower ≤ β ≤ CI upper, if provided.
        for d in self.departments:
            if d.beta_ci_lower is not None and d.beta_ci_upper is not None:
                if not (d.beta_ci_lower <= d.beta <= d.beta_ci_upper):
                    raise ValueError(
                        f"dept '{d.name}': beta_ci_lower={d.beta_ci_lower} "
                        f"> beta={d.beta} or beta > beta_ci_upper={d.beta_ci_upper}"
                    )

        # λ in [0, 1].
        if not (0.0 <= self.pool.lambda_base_ratio <= 1.0):
            raise ValueError(
                f"lambda_base_ratio must be in [0,1], got {self.pool.lambda_base_ratio}"
            )
