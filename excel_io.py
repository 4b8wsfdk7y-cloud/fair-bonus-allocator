"""Excel I/O for the bonus allocator.

Three operations:
    1. generate_template()  → BytesIO with a blank .xlsx clients fill in.
    2. parse_excel_config() → Config object from an uploaded .xlsx.
    3. write_results_excel() → BytesIO with allocation + audit + gates.

Excel format (two sheets):
    Sheet "Pool":          2-column (parameter, value) key-value layout.
    Sheet "Departments":   one row per department, columns match the YAML
                           schema (name, kpi_baseline, kpi_stretch, beta, ...).
"""
from __future__ import annotations

import io
from typing import Any

import pandas as pd

from config import Config, Department, PoolConfig


# Pool parameters in the order they appear in the template.
# (key, label, default, note)
POOL_FIELDS: list[tuple[str, str, Any, str]] = [
    ("pool_total",            "奖金池总额 (¥)",      1_000_000,  "必填"),
    ("profit_target",         "利润目标 (¥)",        20_000_000, "必填"),
    ("profit_baseline",       "利润基线 (¥)",        17_000_000, "必填：所有部门 KPI=baseline 时的利润"),
    ("theta_a",               "A 档利润份额",        0.15,       "默认 0.15"),
    ("theta_s",               "S 档利润份额",        0.30,       "默认 0.30"),
    ("lambda_base_ratio",     "基础池比例 λ",        0.3,        "0-1，按人头分的池份额"),
    ("a_max",                 "达成率夹断 a_max",    1.5,        "防单一部门独占"),
    ("deferred_pool_enabled", "允许延迟池",          True,       "True/False"),
    ("min_pool_share",        "地板份额 (v1)",       0.02,       "每部门最小池份额"),
]

# Department columns in the order they appear in the template.
DEPT_COLUMNS: list[tuple[str, str, Any]] = [
    ("name",                   "部门名称",          ""),
    ("kpi_baseline",           "KPI 基线",          0),
    ("kpi_stretch",            "KPI Stretch",       0),
    ("beta",                   "β (利润弹性)",      0),
    ("headcount",              "人头数",            1),
    ("quota",                  "配额 q_d",          None),
    ("beta_ci_lower",          "β CI 下界",         None),
    ("beta_ci_upper",          "β CI 上界",         None),
    ("beta_confidence_weight", "β 置信权重 ρ",      1.0),
    ("beta_source",            "β 来源",            "unspecified"),
    ("base_bonus",             "v1 固定基础奖",     0.0),
    ("note",                   "备注",              ""),
]


def generate_template() -> bytes:
    """Return a blank .xlsx template as bytes.

    Sheet "Pool" has all pool parameters with defaults and notes.
    Sheet "Departments" has column headers + 3 example rows (so the client
    sees the shape, then replaces with their own data).
    """
    pool_df = pd.DataFrame(
        [
            {"参数": label, "值": default, "说明": note}
            for _key, label, default, note in POOL_FIELDS
        ]
    )
    dept_df = pd.DataFrame(
        [
            {label: default for _key, label, default in DEPT_COLUMNS}
        ]
        * 0  # empty, headers only — clients fill in their own
    )
    # Reorder columns explicitly (DataFrame from dict-of-lists preserves order).
    dept_df = pd.DataFrame(columns=[label for _k, label, _d in DEPT_COLUMNS])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pool_df.to_excel(writer, sheet_name="Pool", index=False)
        dept_df.to_excel(writer, sheet_name="Departments", index=False)
    return buf.getvalue()


def _coerce_pool_value(key: str, value: Any) -> Any:
    """Coerce an Excel cell value to the right Python type for PoolConfig."""
    if key == "deferred_pool_enabled":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "y", "1", "是")
        return bool(value)
    if key in ("pool_total", "profit_target", "profit_baseline",
               "theta_a", "theta_s", "lambda_base_ratio", "a_max",
               "min_pool_share"):
        return float(value)
    return value


def _coerce_dept_value(key: str, value: Any) -> Any:
    """Coerce an Excel cell value to the right Python type for Department."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if key in ("kpi_baseline", "kpi_stretch", "beta", "base_bonus",
               "quota", "beta_ci_lower", "beta_ci_upper",
               "beta_confidence_weight"):
        return float(value)
    if key == "headcount":
        return int(value)
    return str(value)


def parse_excel_config(data: bytes) -> Config:
    """Parse an uploaded .xlsx into a validated Config object.

    Raises ValueError with a human-readable message on any misconfiguration
    (missing sheet, bad types, validation failure).
    """
    try:
        pool_df = pd.read_excel(io.BytesIO(data), sheet_name="Pool")
    except ValueError as e:
        raise ValueError(f"Excel 缺少 'Pool' sheet: {e}") from e
    try:
        dept_df = pd.read_excel(io.BytesIO(data), sheet_name="Departments")
    except ValueError as e:
        raise ValueError(f"Excel 缺少 'Departments' sheet: {e}") from e

    # Pool: label → key lookup
    label_to_key = {label: key for key, label, _d, _n in POOL_FIELDS}
    pool_kwargs: dict[str, Any] = {}
    for _, row in pool_df.iterrows():
        label = str(row.iloc[0]).strip()
        if label not in label_to_key:
            continue  # skip unknown rows silently
        key = label_to_key[label]
        raw_val = row.iloc[1] if len(row) > 1 else None
        try:
            pool_kwargs[key] = _coerce_pool_value(key, raw_val)
        except (TypeError, ValueError) as e:
            raise ValueError(f"参数 '{label}' 的值 '{raw_val}' 无法解析: {e}") from e

    # Required fields
    for required in ("pool_total", "profit_target", "profit_baseline"):
        if required not in pool_kwargs or pool_kwargs[required] is None:
            raise ValueError(f"Pool sheet 缺少必填参数: {required}")

    pool = PoolConfig(**pool_kwargs)

    # Departments
    label_to_dept_key = {label: key for key, label, _d in DEPT_COLUMNS}
    if len(dept_df) == 0:
        raise ValueError("Departments sheet 没有数据行")

    departments: list[Department] = []
    for idx, row in dept_df.iterrows():
        dept_kwargs: dict[str, Any] = {}
        for col_label, cell in row.items():
            col_label = str(col_label).strip()
            if col_label not in label_to_dept_key:
                continue
            key = label_to_dept_key[col_label]
            try:
                dept_kwargs[key] = _coerce_dept_value(key, cell)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"Departments 第 {idx + 2} 行 '{col_label}' 的值 '{cell}' 无法解析: {e}"
                ) from e

        if "name" not in dept_kwargs or not dept_kwargs["name"]:
            raise ValueError(f"Departments 第 {idx + 2} 行缺少部门名称")

        # Drop None-valued optional fields so dataclass defaults kick in.
        for k in list(dept_kwargs.keys()):
            if dept_kwargs[k] is None:
                del dept_kwargs[k]

        # Required fields check.
        for required in ("kpi_baseline", "kpi_stretch", "beta"):
            if required not in dept_kwargs:
                raise ValueError(
                    f"部门 '{dept_kwargs['name']}' 缺少必填字段: {required}"
                )

        try:
            departments.append(Department(**dept_kwargs))
        except TypeError as e:
            raise ValueError(f"部门 '{dept_kwargs['name']}' 字段错误: {e}") from e

    cfg = Config(pool=pool, departments=departments)
    # validate_v2 raises ValueError on bad quota/CI/ρ/λ — pass through.
    cfg.validate_v2()
    return cfg


def write_results_excel(
    config: Config,
    v2_result_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    release_gates: dict[str, bool],
    v1_result_df: pd.DataFrame | None = None,
) -> bytes:
    """Write allocation results to a multi-sheet .xlsx as bytes.

    Sheets:
        Summary      — pool utilization, deferred, gate status (one-shot overview)
        v2_Allocation — per-department breakdown (base, perf, total, c_star, ...)
        v1_Allocation — v1 knapsack result (for comparison)
        Reachability  — per-department stretch vs A/S target
        Release_Gates — 6 gates with pass/fail
        Input_Pool    — the pool config that was used (for audit trail)
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Summary sheet
        total_alloc = float(v2_result_df["bonus"].sum())
        P = config.pool.pool_total
        summary_rows = [
            ("奖金池总额", P),
            ("v2 已分配", total_alloc),
            ("v2 分配率", total_alloc / P if P > 0 else 0),
            ("v2 延迟池", P - total_alloc),
            ("发布闸门全部通过", all(release_gates.values())),
        ]
        if v1_result_df is not None:
            v1_total = float(v1_result_df["allocated"].sum())
            summary_rows.append(("v1 已分配", v1_total))
        pd.DataFrame(summary_rows, columns=["指标", "值"]).to_excel(
            writer, sheet_name="Summary", index=False
        )

        v2_result_df.to_excel(writer, sheet_name="v2_Allocation", index=False)
        if v1_result_df is not None:
            v1_result_df.to_excel(writer, sheet_name="v1_Allocation", index=False)
        audit_df.to_excel(writer, sheet_name="Reachability", index=False)
        pd.DataFrame(
            [(k, "✓" if v else "✗") for k, v in release_gates.items()],
            columns=["gate", "status"],
        ).to_excel(writer, sheet_name="Release_Gates", index=False)

        # Input pool echo
        pd.DataFrame(
            [
                {"参数": label, "值": getattr(config.pool, key)}
                for key, label, _d, _n in POOL_FIELDS
                if hasattr(config.pool, key)
            ]
        ).to_excel(writer, sheet_name="Input_Pool", index=False)

    return buf.getvalue()
