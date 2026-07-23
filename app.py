"""Client-facing Streamlit app: upload Excel → get bonus allocation.

Run:
    uv run streamlit run app.py

Clients download a template (.xlsx), fill in their pool + departments,
upload it, and get back the v1 + v2 allocation results as a downloadable
.xlsx with 6 sheets (Summary, v2_Allocation, v1_Allocation, Reachability,
Release_Gates, Input_Pool).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from sensitivity import compute_sensitivity
from tiers import calibrate_tiers
from allocator import allocate
from v2_allocator import allocate_v2, reachability_audit
from excel_io import generate_template, parse_excel_config, write_results_excel

st.set_page_config(page_title="奖金池分配器", page_icon="💰", layout="wide")
st.title("💰 奖金池分配器")
st.caption("上传 Excel 配置 → v1 + v2 分配 → 下载结果。6 道发布闸门全过才能发薪。")

# ---------------------------------------------------------------------------
# Step 1: Download template / Upload config
# ---------------------------------------------------------------------------
st.subheader("第 1 步 · 上传配置")
col_dl, col_ul = st.columns([1, 2])

with col_dl:
    st.markdown("**下载模板**")
    if st.button("📥 下载 Excel 模板", type="primary"):
        tmpl = generate_template()
        st.download_button(
            label="点击保存模板.xlsx",
            data=tmpl,
            file_name="bonus_allocator_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

with col_ul:
    st.markdown("**上传填好的配置**")
    uploaded = st.file_uploader(
        "选择 .xlsx 文件",
        type=["xlsx", "xls"],
        help="模板含两个 sheet：Pool（参数）+ Departments（部门表）。填好上传。",
    )

if uploaded is None:
    st.info("👆 下载模板，填好后上传。")
    st.stop()

# ---------------------------------------------------------------------------
# Step 2: Parse + validate
# ---------------------------------------------------------------------------
try:
    cfg = parse_excel_config(uploaded.getvalue())
except ValueError as e:
    st.error(f"❌ 配置解析失败：{e}")
    st.caption("检查 Excel 格式：必填字段是否填了、quota 是否 sum=1、β CI 是否合法等。")
    st.stop()
except Exception as e:
    st.error(f"❌ 解析出错：{e}")
    st.stop()

st.success(
    f"✓ 配置解析成功：{len(cfg.departments)} 个部门，奖金池 ¥{cfg.pool.pool_total:,.0f}，"
    f"利润目标 ¥{cfg.pool.profit_target:,.0f}"
)

with st.expander("查看配置详情", expanded=False):
    pool_df = pd.DataFrame(
        [
            {"参数": "奖金池总额", "值": f"¥{cfg.pool.pool_total:,.0f}"},
            {"参数": "利润目标", "值": f"¥{cfg.pool.profit_target:,.0f}"},
            {"参数": "利润基线", "值": f"¥{cfg.pool.profit_baseline:,.0f}"},
            {"参数": "利润缺口", "值": f"¥{cfg.pool.profit_target - cfg.pool.profit_baseline:,.0f}"},
            {"参数": "λ (基础池比例)", "值": cfg.pool.lambda_base_ratio},
            {"参数": "a_max", "值": cfg.pool.a_max},
            {"参数": "θ_A / θ_S", "值": f"{cfg.pool.theta_a} / {cfg.pool.theta_s}"},
        ]
    )
    st.table(pool_df)
    st.markdown("**部门**")
    dept_summary = pd.DataFrame([
        {
            "部门": d.name, "KPI 基线": d.kpi_baseline, "KPI Stretch": d.kpi_stretch,
            "β": d.beta, "人头": d.headcount, "配额": d.quota,
            "ρ": d.beta_confidence_weight, "β 来源": d.beta_source,
        }
        for d in cfg.departments
    ])
    st.dataframe(dept_summary, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Step 3: Optional achievement overrides
# ---------------------------------------------------------------------------
st.subheader("第 2 步 · KPI 达成情况（可选）")
st.caption(
    "默认所有部门在 baseline（ach=1.0）。可以覆盖部分部门，例如 Sales=1.4 表示销售达成 1.4× baseline KPI。"
)
default_ach = "Sales=1.0, Manufacturing=1.0"
ach_input = st.text_input(
    "达成率覆盖（格式：部门名=数值，逗号分隔）",
    value="",
    placeholder=default_ach,
)

achievements: dict[str, float] | None = None
if ach_input.strip():
    achievements = {}
    for part in ach_input.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, val = part.rsplit("=", 1)
        name = name.strip()
        try:
            achievements[name] = float(val.strip())
        except ValueError:
            st.error(f"无法解析 '{part}'，应为 部门名=数值")
            st.stop()
    # Validate names
    valid_names = {d.name for d in cfg.departments}
    unknown = set(achievements) - valid_names
    if unknown:
        st.error(f"未知部门名: {unknown}。配置中的部门: {valid_names}")
        st.stop()

# ---------------------------------------------------------------------------
# Step 4: Run allocation
# ---------------------------------------------------------------------------
if st.button("🚀 开始分配", type="primary"):
    with st.spinner("计算中..."):
        sens = compute_sensitivity(cfg)
        tiers = calibrate_tiers(cfg, sens)
        r_v1 = allocate(cfg, sens, tiers, achievements=achievements)
        r_v2 = allocate_v2(cfg, sens, achievements=achievements)
        audit = reachability_audit(cfg, sens)

    all_gates_ok = all(bool(v) for v in r_v2.release_gates.values())

    # Header KPIs
    st.subheader("第 3 步 · 分配结果")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("奖金池", f"¥{cfg.pool.pool_total:,.0f}")
    c2.metric("v1 已分配", f"¥{r_v1.total_allocated:,.0f}")
    c3.metric("v2 已分配", f"¥{r_v2.total_allocated:,.0f}")
    c4.metric("v2 延迟池", f"¥{r_v2.deferred_pool:,.0f}")
    gate_label = "✓ 全部通过" if all_gates_ok else "✗ 未通过"
    c5.metric("发布闸门", gate_label)

    if not all_gates_ok:
        st.error("🛑 发布闸门未通过 — DO NOT PAY。检查下面哪些闸门挂了。")
    else:
        st.success("✓ 6 道发布闸门全部通过。")

    # Tabs for different views
    tab_v2, tab_v1, tab_audit, tab_gates, tab_chart = st.tabs([
        "v2 分配", "v1 分配", "可达性审计", "发布闸门", "对比图",
    ])

    with tab_v2:
        st.caption("基础池按人头分 + 绩效池按 C* 分。点击列头排序。")
        st.dataframe(
            r_v2.df.style.format({
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
            }),
            use_container_width=True, hide_index=True,
        )

    with tab_v1:
        st.caption("v1 背包贪心分配（基线对照）。")
        st.dataframe(
            r_v1.df.style.format({
                "allocated": "¥{:,.0f}",
                "rounds_won": "{:d}",
                "achievement": "{:.2f}",
                "weight": "{:.3f}",
                "cap": "¥{:,.0f}",
                "headcount": "{:d}",
                "per_capita": "¥{:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )

    with tab_audit:
        st.caption("每个部门的 stretch KPI 能否产生足够利润贡献闭合 A/S 档目标。")
        audit_display = audit.copy()
        audit_display["can_reach_a"] = audit_display["can_reach_a"].map({True: "✓", False: "⚠ 无法到达"})
        audit_display["can_reach_s"] = audit_display["can_reach_s"].map({True: "✓", False: "—"})
        st.dataframe(
            audit_display.style.format({
                "beta": "{:.4g}",
                "stretch_impact": "¥{:,.0f}",
                "quota": "{:.2f}",
                "a_target_profit": "¥{:,.0f}",
                "s_target_profit": "¥{:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )
        unreachable = audit[~audit["can_reach_a"]]
        if len(unreachable) > 0:
            st.warning(
                f"⚠ {len(unreachable)} 个部门 stretch 再拼也到不了 A 档。"
                "先重谈 quota 或 KPI baseline，别静默让他们背锅。"
            )

    with tab_gates:
        st.caption("6 道闸门必须全部通过才能发薪。")
        gate_df = pd.DataFrame(
            [(k, "✓" if bool(v) else "✗", "" if bool(v) else _gate_explanation(k))
             for k, v in r_v2.release_gates.items()],
            columns=["gate", "status", "说明"],
        )
        st.table(gate_df)

    with tab_chart:
        # Stacked bar: base + perf per dept
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=r_v2.df["department"], y=r_v2.df["base_bonus"],
            name="基础池 (按人头)", marker_color="#4C78A8",
        ))
        fig.add_trace(go.Bar(
            x=r_v2.df["department"], y=r_v2.df["perf_bonus"],
            name="绩效池 (按 C*)", marker_color="#F58518",
        ))
        fig.update_layout(
            barmode="stack", title="v2 分配：基础 + 绩效",
            xaxis_title="部门", yaxis_title="金额 (¥)", height=450,
        )
        st.plotly_chart(fig, use_container_width=True)

        # v1 vs v2 grouped
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(
            x=r_v1.df["department"], y=r_v1.df["allocated"],
            name="v1 (knapsack)", marker_color="#54A24B",
        ))
        fig2.add_trace(go.Bar(
            x=r_v2.df["department"], y=r_v2.df["bonus"],
            name="v2 (governance)", marker_color="#F58518",
        ))
        fig2.update_layout(
            barmode="group", title="v1 vs v2 对比",
            xaxis_title="部门", yaxis_title="金额 (¥)", height=450,
        )
        st.plotly_chart(fig2, use_container_width=True)

    # ---------------------------------------------------------------------------
    # Step 5: Download results
    # ---------------------------------------------------------------------------
    st.subheader("第 4 步 · 下载结果")
    result_xlsx = write_results_excel(
        cfg, r_v2.df, audit, r_v2.release_gates, r_v1.df,
    )
    st.download_button(
        label="📥 下载分配结果 Excel",
        data=result_xlsx,
        file_name="bonus_allocation_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.caption(
        "结果 Excel 含 6 个 sheet：Summary、v2_Allocation、v1_Allocation、"
        "Reachability、Release_Gates、Input_Pool（用于审计追溯）。"
    )


def _gate_explanation(gate: str) -> str:
    """Human-readable explanation for a failed gate."""
    return {
        "pool_utilization_90_to_100": "实际分配 < 90% 或 > 100% 奖金池。可能 perf pool 全延迟或配置错误。",
        "no_nan_bonus": "有部门奖金为 NaN，通常是数值爆炸（β=∞ 等）。",
        "no_negative_bonus": "有部门奖金为负，cap/floor 交互 bug。",
        "achievers_have_nonneg_c_star": "达成者（ach≥1.0）的 C* 被夹到负，CI 比 β 还宽。",
        "quotas_sum_to_one": "配额 sum ≠ 1，配置错误或浮点漂移。",
        "monotonic_in_c_star_within_quota": "同配额组内 C* ↑ 但 perf_bonus ↓，公平承诺被破坏。",
    }.get(gate, "")
