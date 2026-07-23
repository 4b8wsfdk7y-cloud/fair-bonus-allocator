"""Streamlit dashboard for the bonus allocator.

Run: streamlit run dashboard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from sensitivity import compute_sensitivity, monte_carlo_profit
from tiers import calibrate_tiers
from allocator import allocate, scenario_grid
from v2_allocator import allocate_v2, reachability_audit

st.set_page_config(page_title="科学分钱 · 奖金池分配器", page_icon="💰", layout="wide")
st.title("💰 科学分钱 — 多部门奖金池分配器")

# --- Sidebar: configuration file -------------------------------------------
DEFAULT_CONFIG = Path(__file__).resolve().parent / "example_config.yaml"
config_path = st.sidebar.text_input("配置文件路径", value=str(DEFAULT_CONFIG))

try:
    config = Config.from_yaml(config_path)
except FileNotFoundError:
    st.error(f"配置文件未找到: {config_path}")
    st.stop()
except Exception as e:
    st.error(f"配置加载失败: {e}")
    st.stop()

# --- Sidebar: governance knobs ----------------------------------------------
st.sidebar.header("治理参数")
pool_total = st.sidebar.number_input(
    "奖金池总额 (¥)", min_value=0.0, value=config.pool.pool_total, step=100_000.0
)
profit_target = st.sidebar.number_input(
    "净利润目标 (¥)", min_value=0.0, value=config.pool.profit_target, step=1_000_000.0
)
profit_baseline = st.sidebar.number_input(
    "KPI 全达成的利润基线 (¥)",
    min_value=0.0,
    value=config.pool.profit_baseline,
    step=1_000_000.0,
)
theta_a = st.sidebar.slider("A 档门槛 θ_A", 0.05, 0.40, config.pool.theta_a, 0.01)
theta_s = st.sidebar.slider("S 档门槛 θ_S", 0.10, 0.80, config.pool.theta_s, 0.01)

# Apply edits back to config.
config.pool.pool_total = pool_total
config.pool.profit_target = profit_target
config.pool.profit_baseline = profit_baseline
config.pool.theta_a = theta_a
config.pool.theta_s = theta_s

# --- Sidebar: per-department achievement sliders ----------------------------
st.sidebar.header("各部门 KPI 达成情况")
achievements = {}
for d in config.departments:
    label = f"{d.name}（基线 {d.kpi_baseline:.0f}）"
    default = 1.0
    achievements[d.name] = st.sidebar.slider(label, 0.5, 1.5, default, 0.05, key=f"ach_{d.name}")

# --- Compute all layers ----------------------------------------------------
sensitivity = compute_sensitivity(config)
tiers = calibrate_tiers(config, sensitivity)
allocation = allocate(config, sensitivity, tiers, achievements=achievements)
v2_result = allocate_v2(config, sensitivity, achievements=achievements)
v2_audit = reachability_audit(config, sensitivity)

profit_gap = sensitivity.profit_gap
st.session_state["sensitivity"] = sensitivity
st.session_state["tiers"] = tiers
st.session_state["allocation"] = allocation
st.session_state["v2_result"] = v2_result
st.session_state["v2_audit"] = v2_audit

# --- Header KPIs -----------------------------------------------------------
all_gates_ok = all(bool(v) for v in v2_result.release_gates.values())
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("奖金池", f"¥{pool_total:,.0f}")
c2.metric("利润缺口 (B→Target)", f"¥{profit_gap:,.0f}", help="净利润目标减去 KPI 全达成的基线")
c3.metric("v1 已分配", f"¥{allocation.total_allocated:,.0f}")
c4.metric("v2 已分配", f"¥{v2_result.total_allocated:,.0f}")
c5.metric(
    "v2 延迟池",
    f"¥{v2_result.deferred_pool:,.0f}",
    help="无法分配（如所有部门被置信门冻结）的残差，由管理层处置",
)
gate_color = "🟢" if all_gates_ok else "🔴"
st.warning(
    f"{gate_color} v2 发布闸门：{'全部通过' if all_gates_ok else '未通过 — DO NOT PAY'}"
) if not all_gates_ok else st.success(f"{gate_color} v2 发布闸门：全部通过")

st.divider()

# --- Layer 1: sensitivity table --------------------------------------------
st.subheader("Layer 1 · 部门-利润敏感度")
st.caption("β 是部门 KPI 每变动 1 单位对公司利润的边际贡献。stretch_impact 是该部门从基线冲到 stretch KPI 时贡献的利润增量。")
st.dataframe(
    sensitivity.df.style.format(
        {
            "beta": "{:.4f}",
            "kpi_baseline": "{:.2f}",
            "kpi_stretch": "{:.2f}",
            "stretch_impact": "{:,.0f}",
            "stretch_share": "{:.1%}",
        }
    ),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# --- Layer 2: tier calibration ---------------------------------------------
st.subheader("Layer 2 · B/A/S 档位校准")
st.caption(
    f"规则：β_d × (KPI_d^A − B) = θ_A × 利润缺口 = {theta_a:.0%} × ¥{profit_gap:,.0f}。"
    " 同样的利润贡献额对应同样的档位升级，跨部门可比。"
)
tier_df_display = tiers.df.copy()
tier_df_display["capped"] = tier_df_display["capped"].map({True: "⚠️ 是", False: ""})
st.dataframe(
    tier_df_display.style.format(
        {
            "kpi_baseline": "{:.2f}",
            "kpi_a": "{:.2f}",
            "kpi_s": "{:.2f}",
            "kpi_stretch": "{:.2f}",
            "tier_a_share": "{:.1%}",
            "tier_s_share": "{:.1%}",
        }
    ),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# --- Layer 3: allocation result --------------------------------------------
st.subheader("Layer 3 · Knapsack 分配结果")
st.caption("基于各部门当前 KPI 达成情况，将奖金池按"价值/成本"贪心分配。")

alloc_display = allocation.df.copy()


def _actual_kpi(dept, ach: float) -> float:
    """Achievement multiplier → actual KPI value (mirrors sensitivity.py)."""
    if dept.kpi_baseline > 0:
        return ach * dept.kpi_baseline
    return (ach - 1.0) * dept.kpi_stretch


dept_by_name = {d.name: d for d in config.departments}
alloc_display["档位"] = alloc_display["department"].map(
    lambda n: tiers.tier_for(n, _actual_kpi(dept_by_name[n], achievements[n]))
)
alloc_display["达成率"] = alloc_display["achievement"]
st.dataframe(
    alloc_display.style.format(
        {
            "allocated": "¥{:,.0f}",
            "rounds_won": "{:d}",
            "achievement": "{:.0%}",
            "weight": "{:.3f}",
            "cap": "¥{:,.0f}",
            "per_capita": "¥{:,.0f}",
        }
    ),
    use_container_width=True,
    hide_index=True,
)

# Bar chart
fig = go.Figure(
    data=[
        go.Bar(
            x=allocation.df["department"],
            y=allocation.df["allocated"],
            text=[f"¥{v:,.0f}" for v in allocation.df["allocated"]],
            textposition="outside",
            marker_color="#4C78A8",
        )
    ]
)
fig.update_layout(
    title="各部门奖金分配",
    xaxis_title="部门",
    yaxis_title="分配金额 (¥)",
    height=400,
)
st.plotly_chart(fig, use_container_width=True)

st.divider()

# --- v2 governance: reachability audit & allocation ------------------------
st.subheader("Layer 3 · v2 治理分配")
st.caption(
    "基础池按人头分（同人数同基础奖），绩效池按置信调整后贡献 $C^*_d$ 分（同贡献同绩效奖）。"
    "6 道发布闸门全部通过才能发薪。"
)

st.markdown("**可达性审计**")
audit_display = v2_audit.copy()
audit_display["can_reach_a"] = audit_display["can_reach_a"].map({True: "✓", False: "⚠ 无法到达"})
audit_display["can_reach_s"] = audit_display["can_reach_s"].map({True: "✓", False: "—"})
st.dataframe(
    audit_display.style.format(
        {
            "beta": "{:.4g}",
            "stretch_impact": "¥{:,.0f}",
            "a_target_profit": "¥{:,.0f}",
            "s_target_profit": "¥{:,.0f}",
        }
    ),
    use_container_width=True,
    hide_index=True,
)
unreachable = v2_audit[~v2_audit["can_reach_a"]]
if len(unreachable) > 0:
    st.warning(
        f"⚠ {len(unreachable)} 个部门的 stretch 再拼也到不了 A 档。"
        "别静默让他们背锅——先重谈 quota 或 KPI baseline。"
    )

st.markdown("**v2 分配结果**")
v2_display = v2_result.df.copy()
st.dataframe(
    v2_display.style.format(
        {
            "headcount": "{:d}",
            "achievement": "{:.2f}",
            "quota": "{:.2f}",
            "c_hat": "¥{:,.0f}",
            "c_lower_95": "¥{:,.0f}",
            "c_star": "¥{:,.0f}",
            "target": "¥{:,.0f}",
            "achievement_rate": "{:.2f}",
            "achievement_rate_clipped": "{:.2f}",
            "score": "{:.4f}",
            "base_bonus": "¥{:,.0f}",
            "perf_bonus": "¥{:,.0f}",
            "bonus": "¥{:,.0f}",
        }
    ),
    use_container_width=True,
    hide_index=True,
)

st.markdown("**发布闸门**")
gate_df = pd.DataFrame(
    [(k, "✓" if v else "✗") for k, v in v2_result.release_gates.items()],
    columns=["gate", "status"],
)
st.table(gate_df)

# v1 vs v2 对比图
fig_v2 = go.Figure()
fig_v2.add_trace(go.Bar(
    x=v2_result.df["department"], y=v2_result.df["base_bonus"],
    name="基础池 (按人头)", marker_color="#4C78A8"
))
fig_v2.add_trace(go.Bar(
    x=v2_result.df["department"], y=v2_result.df["perf_bonus"],
    name="绩效池 (按 C*)", marker_color="#F58518"
))
fig_v2.update_layout(
    barmode="stack", title="v2 分解：基础 + 绩效",
    xaxis_title="部门", yaxis_title="金额 (¥)", height=400,
)
st.plotly_chart(fig_v2, use_container_width=True)

st.divider()

# --- Scenario grid: explore patterns ---------------------------------------
st.subheader("情景试算：不同达成组合下的分配")
st.caption("勾选要跑的情景模式，对比"全 S"、"销售独大"、"采购独大"等典型情况。")

default_scenarios = {
    "全 B (基线)": {d.name: 1.0 for d in config.departments},
    "全 A": {d.name: 1.15 for d in config.departments},
    "全 S": {d.name: 1.30 for d in config.departments},
    "销售独大": {d.name: (1.40 if "销售" in d.name else 1.0) for d in config.departments},
    "采购独大": {d.name: (1.40 if "采购" in d.name else 1.0) for d in config.departments},
    "生产独大": {d.name: (1.40 if "生产" in d.name or "制造" in d.name else 1.0) for d in config.departments},
}
selected = st.multiselect("选择情景", list(default_scenarios.keys()), default=["全 B (基线)", "全 A", "全 S"])
scenarios = [default_scenarios[s] for s in selected]
if scenarios:
    grid = scenario_grid(config, sensitivity, tiers, scenarios)
    pivot = grid[grid["department"] != "__pool__"].pivot_table(
        index="department", columns="scenario_id", values="allocated", aggfunc="sum"
    )
    pivot.columns = [selected[i] for i in pivot.columns]
    st.dataframe(pivot.style.format("¥{:,.0f}"), use_container_width=True)

    fig2 = go.Figure()
    for col in pivot.columns:
        fig2.add_trace(go.Bar(x=pivot.index, y=pivot[col], name=col))
    fig2.update_layout(
        barmode="group", title="情景对比", xaxis_title="部门", yaxis_title="分配金额 (¥)", height=400
    )
    st.plotly_chart(fig2, use_container_width=True)

st.divider()

# --- Monte Carlo: profit distribution --------------------------------------
with st.expander("Layer 1 · Monte Carlo 利润分布验证", expanded=False):
    st.caption("跑 1000 个随机达成情景，验证各部门 KPI 波动对公司利润的传导。")
    n_scenarios = st.number_input("情景数", 100, 5000, 1000, 100, key="mc_n")
    mc_df = monte_carlo_profit(config, n_scenarios=int(n_scenarios))
    fig3 = go.Figure(data=[go.Histogram(x=mc_df["profit"], nbinsx=40)])
    fig3.add_vline(x=config.pool.profit_target, line_dash="dash", line_color="red", annotation_text="目标")
    fig3.add_vline(x=config.pool.profit_baseline, line_dash="dash", line_color="green", annotation_text="基线")
    fig3.update_layout(title="利润分布", xaxis_title="公司净利润 (¥)", height=350)
    st.plotly_chart(fig3, use_container_width=True)
