# bonus-allocator

**一个以敏感度驱动的奖金池分配器：将各部门异质 KPI 转换为统一货币——边际利润贡献——在显式公平与治理约束下分配共享奖金池。**

[English](README.md) · [简体中文](README.zh-CN.md)

> **状态**：模型原型，数学已验证（27 单元测试 + 119 压测场景 + 16 深度检查全部通过）。**未做历史回测和一季度影子运行前，不可直接用于发薪。**

---

## 目录

1. [项目为什么存在](#1-项目为什么存在)
2. [一页纸设计原理](#2-一页纸设计原理)
3. [三层架构](#3-三层架构)
4. [快速开始](#4-快速开始)
5. [Layer 1 · 敏感度建模](#5-layer-1--敏感度建模)
6. [Layer 2 · 档位校准](#6-layer-2--档位校准)
7. [Layer 3 · 分配算法（v1 与 v2）](#7-layer-3--分配算法v1-与-v2)
8. [v2 治理深入](#8-v2-治理深入)
9. [发布闸门](#9-发布闸门)
10. [配置参考](#10-配置参考)
11. [CLI 参考](#11-cli-参考)
12. [验证与压测](#12-验证与压测)
13. [通往生产环境之路](#13-通往生产环境之路)
14. [本项目不包含的内容](#14-本项目不包含的内容)
15. [License 与注意事项](#15-license-与注意事项)

---

## 1. 项目为什么存在

经典的 CFO/HR 难题：8 个部门的 KPI **单位完全不同**。

| 部门 | KPI | 单位 |
|---|---|---|
| 销售 | 营收 | 元 |
| 采购 | 降本 | 元 |
| 制造 | 生产效率 | % |
| 物流 | 准时交付率 | % |
| 质量 | 客诉减少 | 件数 |
| 工程 | 降本项目数 | 个数 |
| PMC | 库存周转天数 | 天 |
| 供应链-寻源 | 新增供应商数 | 个数 |

如何让"销售多卖 ¥30M 营收"和"采购降本 ¥2M"在 ¥1M 奖金池下用同一套规则竞争？不能直接比 KPI 数值——必须先翻译。

**本项目的答案**：用 β 弹性系数把每个 KPI 转成**利润贡献**（`β × ΔKPI = 利润元`），再在显式公平规则下分配。这让"谁挣了什么"可计算、可审计、可辩论——而且大家都在同一把尺子上。

---

## 2. 一页纸设计原理

### 核心问题

奖金池里潜入三种不公平：

1. **苹果比橘子**：销售数字看起来大就多拿，哪怕采购实际省下更多利润。
2. **目标可以随便挪**：部门 stretch KPI 设得随心所欲，A 档在不同部门含义不一样。
3. **虚假精度**：β 是从有限数据估出来的，但分配器把它当真理——噪声变成了钱。

### 三步管线

```
异质 KPI
    │
    ▼  Layer 1: β × ΔKPI = 利润元
统一货币（利润贡献）
    │
    ▼  Layer 2: 校准档位线，使同等档位 = 同等贡献
可比较的档位线
    │
    ▼  Layer 3: 在公平 + 治理约束下分配奖金池
每部门奖金
```

### 三级公平

| 级别 | 规则 | 在哪里实现 |
|---|---|---|
| **L1 — 等贡献** | 同 ΔKPI × β → 同利润元 | Layer 1（数学恒等式） |
| **L2 — 等档位** | 任一部门达到"A 档"都闭合相同份额的利润缺口 | Layer 2（校准） |
| **L3 — 等奖金** | 同贡献 → 同奖金；同人数 → 同基础奖 | Layer 3（v2 分配器） |

### 置信度意识

β̂ 是**估计**，不是真理。v2 使用单侧 95% 下界：

```
C_d* = ρ_d × max(0, β̂·ΔKPI − 1.645 × |ΔKPI| × SE(β̂))
```

只有我们**确信挣到**的利润贡献才转为奖金。噪声大的估计被夹到零，而不是发出去。

### 发布门槛

**6 道发布闸门**在每次发薪前必须全部通过。任何一道挂掉，运行标记为 `DO NOT PAY`。见 [§9](#9-发布闸门)。

---

## 3. 三层架构

```
Layer 1 · 敏感度建模                  [sensitivity.py]
    Profit = Profit_baseline + Σ β_d × (KPI_d − baseline_d)
    通过 Monte Carlo + OLS 回归验证（β 恢复误差 <1%）。

Layer 2 · 档位校准                    [tiers.py]
    β_d × (KPI_d^A − baseline) = θ_A × profit_gap
    β_d × (KPI_d^S − baseline) = θ_S × profit_gap
    → 每次档位跃迁代表相同的¥贡献

Layer 3 · 分配
    v1 [allocator.py]    背包式贪心 + 轮次惩罚。
    v2 [v2_allocator.py] 基础池（按人头）+ 绩效池（按置信调整后贡献）。
```

**v1 vs v2 — 该用哪个？**

| | v1 | v2 |
|---|---|---|
| 数学正确？ | ✓ | ✓ |
| 同贡献 → 同奖金？ | 声称了但没做到 | ✓ 强制执行 |
| β 当估计值处理？ | 否（点估计） | ✓（95% 下界） |
| 可达性审计？ | 无 | ✓ |
| 配额治理？ | 隐式 | ✓ 显式（sum=1） |
| 适用场景 | 影子运行、基线 | **生产候选** |

---

## 4. 快速开始

```bash
# 需要 Python 3.11+
uv sync

# 在 example config 上跑 v1 + v2，
# 假设销售业绩达成 1.4× baseline KPI：
uv run python -m cli allocate example_config.yaml --ach Sales=1.4

# 只跑可达性审计——标记 stretch 够不到 A/S 档的部门：
uv run python -m cli audit example_config.yaml

# 跑压测 + 深度压测（共 135 场景）：
uv run python -m cli stress

# 跑单元测试：
uv run pytest tests/ -v
```

**`allocate` 的预期输出**：

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

## 5. Layer 1 · 敏感度建模

**文件**：`sensitivity.py`

最简的利润模型对 KPI 增量是线性的：

```
Profit = Profit_baseline + Σ_d β_d × (KPI_d − baseline_d)
```

也支持对数线性形式：

```
log(Profit) = log(Profit_baseline) + Σ_d β_d × log(KPI_d / baseline_d)
```

### 单位约定

每个 KPI 的单位必须和它的 β 配对，使得 `β × ΔKPI = 利润增量（元）`。示例：

| 部门 | KPI 单位 | β | 含义 |
|---|---|---|---|
| 销售 | 元营收 | 0.08 | 每 ¥1 营收 → ¥0.08 利润 |
| 采购 | 元降本 | 1.0 | 直接 1:1 传递 |
| 制造 | 效率 % | 250,000 | 每 1% → ¥25 万利润 |
| 质量 | 客诉件数 | 100,000 | 每少一单客诉 → ¥10 万利润 |

### β 验证

`monte_carlo_profit()` 用随机业绩模拟 N 个场景，再用 OLS 把模拟利润对各部门 KPI 回归。恢复出的 β̂ 应与输入 β 误差 <1%。这是确认线性模型内部自洽的安全检查。

### 利润缺口

```
profit_gap = profit_target − profit_baseline
```

这是奖金池要激励部门去闭合的"缺口"。它驱动 Layer 2 的档位校准。

---

## 6. Layer 2 · 档位校准

**文件**：`tiers.py`

每个部门在 baseline B 之上有两条档位线（A 和 S）：

```
KPI_d^A = KPI_d^B + θ_A × profit_gap / β_d
KPI_d^S = KPI_d^B + θ_S × profit_gap / β_d
```

### 为什么这样有效

- **低 β 部门档位带宽更大**：KPI 要变动更多才能挣到相同的奖金权重。
- **高 β 部门档位带宽更窄**：KPI 小幅变动 → 利润影响大 → 档位距离小。
- 每次档位跃迁代表**相同的¥贡献**闭合利润缺口。这是跨部门公平的数学核心。

### 默认值

| 参数 | 默认 | 含义 |
|---|---|---|
| `theta_a` | 0.15 | A 档闭合 15% 利润缺口 |
| `theta_s` | 0.30 | S 档闭合 30% 利润缺口 |
| 封顶规则 | `min(kpi_s, kpi_stretch)` | 档位线永不超出用户定义的 stretch |

### 可达性

某部门若 `β × (kpi_stretch − kpi_baseline) < θ_A × profit_gap`，则**永远到不了 A 档**，无论多努力。v2 审计会标记这一点，让你能诚实地重新协商配额（见 [§8](#8-v2-治理深入)）。

---

## 7. Layer 3 · 分配算法（v1 与 v2）

### v1 — 背包贪心

**文件**：`allocator.py`

奖金池切成 N 份（默认 1000）。每轮让边际价值/成本比最高的部门赢得一份。

```
价值:    V_d = w_d × ln(1 + (s'_d + division) / target_d)
背包:    KS_d = V_d / (rounds_d + 1)        ← 胜者惩罚
```

约束：
- 地板：每部门至少 `min_pool_share × pool_total`
- 封顶：随业绩线性增长，封顶在 S 档分配额
- 不合格：业绩低于 baseline（ach < 1.0）的部门只拿地板

### v2 — 基础池 + 绩效池

**文件**：`v2_allocator.py`

奖金池切两半：

```
基础池 (λ P)  : 按人头分       → "同人数拿同基础"
绩效池 ((1−λ)P): 按 c_star 分   → "同贡献拿同绩效"
```

完整公式见 [§8](#8-v2-治理深入)。

---

## 8. v2 治理深入

v1 数学正确但被 Codex review 揪出三个现实问题：

| 问题 | v1 行为 | v2 修复 |
|---|---|---|
| 声称"同贡献同奖金"但没做到 | cap 和 floor 扭曲了比例 | quota + 人头基础池分离基础与绩效 |
| 忽略 stretch 可达性 | 静默误分类 | 显式审计 + 重新协商提示 |
| β 当真理 | 噪声估计 → 真钱 | 单侧 95% 下界 |

### 8.1 责任份额（`quota`）

每部门声明 `q_d`，满足 `Σ q_d = 1`。含义："如果每个部门都到 A 档，正好闭合一个利润缺口——不是 N × θ。"

- 所有部门都设 `quota`：配置校验 sum=1（否则报错）。
- 都不设 `quota`：按 stretch_impact 份额推导。

### 8.2 置信调整贡献（`c_star`）

```
ΔKPI_d    = 实际 KPI 相对 baseline 的变化
ĉ_hat     = β̂_d × ΔKPI_d                    ← 点估计
SE(ĉ)     = |ΔKPI_d| × SE(β̂_d)              ← 线性模型方差
ĉ_lower   = ĉ_hat − 1.645 × SE(ĉ)           ← 单侧 95% 下界
C_d*      = ρ_d × max(0, ĉ_lower)           ← 质量加权，夹到 ≥ 0
```

- **1.645** 是单侧 95% 置信的 z 值。
- **ρ_d ∈ [0,1]**（`beta_confidence_weight`）是来源质量：RCT=1.0，回归=0.7，专家=0.3。
- `max(0, …)` 把噪声估计夹到零——**不确定的钱不发**。

### 8.3 评分与绩效奖金

```
target_d  = q_d × profit_gap                 ← 该部门应闭合的缺口份额
a_d       = C_d* / target_d                  ← 达成率
s_d       = q_d × min(max(a_d, 0), a_max)    ← 夹断评分（a_max 默认 1.5）
PerfBonus_d = (1−λ) P × s_d / Σ s_j
```

`a_max` 夹断防止单一部门极端业绩独占绩效池。

### 8.4 基础奖金

```
BaseBonus_d = λ P × h_d / H                  ← h_d = 部门人头，H = 总人头
```

纯人头分配。同人数 → 同基础奖金，不分部门。

### 8.5 封顶与溢出

若设了 `caps={dept: 元}` 且某部门奖金超出：
1. 夹到 cap，超额按 `s_d` 比例分给剩余部门。
2. 最多迭代 10 次（级联再分配）。
3. 全部封顶或零分时，残差进入 deferred 池。

### 8.6 延迟池

`deferred_pool_enabled=true`（默认）时，未分完的残差**延迟**（留给管理层处置），而不是强行发完。这更安全——不惜代价发完整池会产生反向激励。

### 8.7 可达性审计

**`cli audit example_config.yaml`** 打印：

```
department    stretch_impact  a_target_profit  can_reach_a  can_reach_s
Sales               2,400,000          450,000         True         True
Engineering           900,000          450,000         True        False  ⚠
```

若 `can_reach_a=False`，该部门 stretch 再拼也到不了 A 档。**别静默让他们背锅——先重谈 quota 或 KPI baseline。**

---

## 9. 发布闸门

6 个布尔检查。**必须全部为 `True`** 才能发薪。

```python
{
  "pool_utilization_90_to_100":         True,   # 90% ≤ allocated + deferred ≤ 100%
  "no_nan_bonus":                       True,   # 任何奖金无 NaN
  "no_negative_bonus":                  True,   # 每个奖金 ≥ 0
  "achievers_have_nonneg_c_star":       True,   # ach ≥ 1.0 ⇒ c_star ≥ 0
  "quotas_sum_to_one":                  True,   # Σ quota = 1（容差 1e-6）
  "monotonic_in_c_star_within_quota":   True,   # 同 quota 内 c_star ↑ ⇒ perf_bonus ↑
}
```

任一闸门挂掉，CLI 退出码为 1 并打印 `all gates pass: NO — DO NOT PAY`。

---

## 10. 配置参考

完整带注释示例见 `example_config.yaml`。关键字段：

### `pool:` 段

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `pool_total` | float | 必填 | 奖金池总额（元） |
| `profit_target` | float | 必填 | 利润目标；缺口 = target − baseline 驱动校准 |
| `profit_baseline` | float | 必填 | 所有部门 KPI=baseline 时的利润 |
| `theta_a`, `theta_s` | float | 0.15, 0.30 | A/S 档利润份额阈值 |
| `divisions` | int | 1000 | v1 背包粒度 |
| `min_pool_share` | float | 0.0 | 地板：每部门最小池份额 |
| `lambda_base_ratio` | float | 0.3 | v2：按人头分的池份额 |
| `a_max` | float | 1.5 | v2：达成率夹断天花板 |
| `deferred_pool_enabled` | bool | true | v2：允许残差延迟 |

### `departments:` 段（每部门）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `name` | str | 必填 | 部门标识 |
| `kpi_baseline` | float | 必填 | 100% 目标 KPI |
| `kpi_stretch` | float | 必填 | S 档 stretch KPI（封顶） |
| `beta` | float | 必填 | 利润弹性（`β × ΔKPI = 利润元`） |
| `headcount` | int | 1 | 部门人数（驱动 v2 基础池） |
| `base_bonus` | float | 0.0 | v1 固定 B 档奖金 |
| `quota` | float | None | v2 责任份额（全设或全不设，sum=1） |
| `beta_ci_lower`, `beta_ci_upper` | float | None | β̂ 的 95% CI 上下界；省略则 SE=0 |
| `beta_confidence_weight` | float | 1.0 | ρ ∈ [0,1]，对 β 估计的置信度 |
| `beta_source` | str | "unspecified" | 来源：regression / bridge_model / expert_estimate / industry_benchmark |
| `note` | str | "" | 自由文本备注 |

### 校验

`Config.validate_v2()` 在配置错误时抛错：
- 部分设 `quota`（一些设了一些没设）→ 抛错
- `quota` sum ≠ 1.0 → 抛错
- `beta_confidence_weight` ∉ [0,1] → 抛错
- `beta_ci_lower > beta` 或 `beta > beta_ci_upper` → 抛错
- `lambda_base_ratio` ∉ [0,1] → 抛错

---

## 11. CLI 参考

```bash
# 跑 v1 + v2，可选业绩覆盖：
uv run python -m cli allocate <config.yaml> [--ach Dept1=1.4 Dept2=1.2 ...]
# 退出码：所有闸门通过=0，否则=1。

# 只跑可达性审计：
uv run python -m cli audit <config.yaml>

# 压测 + 深度压测：
uv run python -m cli stress
```

### 业绩格式

`--ach Sales=1.4 Manufacturing=1.2` 表示销售达成 1.4× baseline KPI，制造 1.2×。未列出的部门默认 `1.0`（在 baseline）。

---

## 12. 验证与压测

| 套件 | 文件 | 范围 | 通过率 |
|---|---|---|---|
| v1 算术 | `tests/test_validation.py` | 13 测试 | 13/13 ✓ |
| v2 治理 | `tests/test_v2_validation.py` | 14 测试 | 14/14 ✓ |
| 基础压测 | `stress_test.py` | 119 场景 | 119/119 ✓ |
| 深度压测 | `deep_stress_test.py` | 16 检查 | 16/16 ✓ |

### 压测覆盖

- **规模**：8 → 100 → 1000 → 5000 部门（线性时间）
- **极端业绩**：0.0, 0.5, 1.0, 2.0, 10.0, 100.0
- **零分冻结**：所有 ρ=0 → 整个绩效池延迟
- **封顶溢出**：每个部门都封顶 → 级联再分配
- **超宽 CI**：CI > β → c_star 夹到零
- **模糊测试**：100 个随机配置，所有发布闸门必须通过
- **对抗场景**：1 个 mega 部门 + 99 个 tiny 部门
- **长时运行**：1000 次模糊迭代，确定性 + 无漂移
- **故障注入**：CI 倒挂、ρ ∉ [0,1]、quota sum ≠ 1——全部抛错
- **属性测试**：单调性、缩放不变性（2× 池 → 2× 奖金）

### 性能（8 部门，MacBook M 系列）

```
v2 分配:           < 5 ms
v2 分配 (5000 部门): ~38 ms
fuzz 1000 次:      ~1.5 秒总计
```

---

## 13. 通往生产环境之路

这是一个**模型**。模型不发钱。真实发薪需要：

1. **所有发布闸门通过**生产配置。
2. **历史回测**：把 4–8 个季度的真实 KPI 数据喂给 v2，验证 β 稳定性和排名一致性（对照已知业务结果）。
3. **影子运行季度**：v1（当前流程）和 v2（新模型）并行跑一整季度，不告诉任何人 v2 存在。对比分布。
4. **可复现快照**：每次发薪运行记录 config SHA + 代码 commit + 随机种子 + 审批人。归档。
5. **签字**：CFO + HR + 业务负责人书面签字。没签字，不发钱。

跳过任何一步、只靠这代码发钱——**就是错的**。

---

## 14. 本项目不包含的内容

- **历史回测**：需要你真实的财务数据；无法预置。
- **影子运行框架**：需要你真实的 v1 基线。
- **可复现快照管线**：代码产出 dict，但接到你的审批工作流要你自己接。
- **飞书 / Slack / Teams 推送**：公开版移除了消息客户端。用分配器输出的 DataFrame 自己接集成。
- **Dashboard**：`dashboard.py`（Streamlit）包含但未文档化；当参考可视化用，不是生产 UI。

---

## 15. License 与注意事项

**MIT License** — 见 `LICENSE`。

### 注意事项

1. β 是**估计值**，不是测量值。垃圾进，垃圾出。
2. 线性利润模型是**一阶近似**。KPI 大幅波动（±50%+）会违反它。
3. v2 的 `a_max=1.5` 夹断是**政策选择**，不是定律。按你的风险偏好调整。
4. 人头基础池假设**每颗人头权重相等**。如果资历/角色构成有差异，自己调。
5. 配额协商是**政治**，不是数学。本工具呈现权衡，不解决权衡。

### 贡献

这是内部原型的脱敏公开版。欢迎 bug 报告和数学批评；功能请求按"这是不是该进治理关键分配器"的标准评估。

---

**为那些希望奖金分配可审计、可辩论、可辩护——而不是魔法——的 CFO、HR 负责人和业务负责人而设计。**
