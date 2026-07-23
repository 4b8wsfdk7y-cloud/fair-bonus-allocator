# bonus-allocator

**一个以敏感度驱动的奖金池分配器：将各部门异质 KPI 转换为统一货币——边际利润贡献——在显式公平与治理约束下分配共享奖金池。**

[English](README.md) · [简体中文](README.zh-CN.md)

> **状态**：模型原型，数学已验证（27 单元测试 + 119 压测场景 + 16 深度检查全部通过）。**未做历史回测和一季度影子运行前，不可直接用于发薪。**

---

## 给赶时间的人——大白话版

你有 8 个部门，KPI 单位五花八门（营收元、效率 %、客诉件数等），要分一个 ¥1M 的奖金池。这个工具回答的是：*谁拿多少，为什么？*

**用大白话讲：**

1. **把每个 KPI 都翻译成利润元。** 销售 +¥30M 营收，按 8% 利润率 = ¥2.4M 利润。采购降本 ¥2M = ¥2M 利润。现在能放一起比了。
2. **校准档位线，让"A 档"在每个部门含义相同。** 销售到 A 档和采购到 A 档，闭合的利润缺口份额一模一样。
3. **奖金池切两半。** 一半按人头分（同人数 → 同基础奖），一半按利润贡献分（同贡献 → 同绩效奖）。
4. **打折扣处理不靠谱的估计。** 如果 β 是专家估的、置信区间很宽，只按"下界"来发奖金——我们确信挣到的那部分才发。噪声大的估计直接夹到零——**不确定的钱不发**。
5. **6 道发布闸门必须全过。** 任何一道挂掉，运行标记为 `DO NOT PAY`，退出码 1。

**整个流程用 R 写就 12 行：**

```r
# 输入：beta_hat（β估计）, se_beta, delta_kpi, rho（来源权重）, quota, lambda, P, gap, h, H
c_hat     <- beta_hat * delta_kpi              # 利润贡献点估计
se_c      <- abs(delta_kpi) * se_beta          # SE 线性传播
c_lower   <- c_hat - 1.645 * se_c              # 单侧 95% 下界
c_star    <- rho * max(0, c_lower)             # 夹到 ≥0，再乘来源质量
target_d  <- quota * gap                       # 该部门应闭合的缺口份额
a_d       <- c_star / target_d                 # 达成率
s_d       <- quota * min(max(a_d, 0), 1.5)     # 夹断评分
base_d    <- lambda * P * h / H                # 按人头的基础奖金
perf_d    <- (1 - lambda) * P * s_d / sum(s_d) # 按贡献的绩效奖金
bonus_d   <- base_d + perf_d                   # 总奖金
deferred  <- max(P - sum(bonus_d), 0)          # 残差延迟
gates_ok  <- all(bonus_d >= 0) && abs(sum(quota) - 1) < 1e-6  # 闸门（简化）
```

如果你看完觉得"嗯，说得通"，那这份 README 剩下的部分只是在解释*为什么每一行要这么写*。先看 [§2 设计原理](#2-一页纸设计原理) 了解大思路，再看 [§8 v2 治理](#8-v2-治理深入) 看统计细节。

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

**本项目的答案**：用 $\beta$ 弹性系数把每个 KPI 转成**利润贡献**（$\beta \cdot \Delta\mathrm{KPI} = \mathrm{利润}$），再在显式公平规则下分配。这让"谁挣了什么"可计算、可审计、可辩论——而且大家都在同一把尺子上。

---

## 2. 一页纸设计原理

### 核心问题

奖金池里潜入三种不公平：

1. **苹果比橘子**：销售数字看起来大就多拿，哪怕采购实际省下更多利润。
2. **目标可以随便挪**：部门 stretch KPI 设得随心所欲，A 档在不同部门含义不一样。
3. **虚假精度**：$\hat{\beta}$ 是从有限数据估出来的，但分配器把它当真理——噪声变成了钱。

### 三步管线

```
异质 KPI
    │
    ▼  Layer 1: β · ΔKPI = 利润元
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
| **L1 — 等贡献** | 同 $\Delta\mathrm{KPI} \cdot \beta$ → 同利润元 | Layer 1（数学恒等式） |
| **L2 — 等档位** | 任一部门达到"A 档"都闭合相同份额的利润缺口 | Layer 2（校准） |
| **L3 — 等奖金** | 同贡献 → 同奖金；同人数 → 同基础奖 | Layer 3（v2 分配器） |

### 置信度意识

$\hat{\beta}$ 是**估计**，不是真理。v2 使用单侧 95% 下界：

$$C^{*}_d = \rho_d \cdot \max\left(0, \hat{\beta}_d \cdot \Delta\mathrm{KPI}_d - 1.645 \cdot |\Delta\mathrm{KPI}_d| \cdot \mathrm{SE}(\hat{\beta}_d)\right)$$

其中 $1.645$ 是单侧 95% 置信的 z 值。只有我们**确信挣到**的利润贡献才转为奖金。噪声大的估计被夹到零，而不是发出去。完整推导以及"为什么用单侧而不是双侧"见 [§8.2](#82-置信调整贡献-c_d--v2-的统计核心)。

### 发布门槛

**6 道发布闸门**在每次发薪前必须全部通过。任何一道挂掉，运行标记为 `DO NOT PAY`。见 [§9](#9-发布闸门)。

---

## 3. 三层架构

```
Layer 1 · 敏感度建模                  [sensitivity.py]
    Profit = Profit_baseline + Σ_d  β_d · (KPI_d − baseline_d)
    通过 Monte Carlo + OLS 回归验证（β 恢复误差 <1%）。

Layer 2 · 档位校准                    [tiers.py]
    β_d · (KPI_d^A − baseline) = θ_A · profit_gap
    β_d · (KPI_d^S − baseline) = θ_S · profit_gap
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

### 5.1 线性利润模型

最简的利润模型对 KPI 增量是线性的：

$$\mathrm{Profit} = \mathrm{Profit}_{\mathrm{base}} + \sum_d \beta_d \cdot (\mathrm{KPI}_d - \mathrm{base}_d)$$

也支持对数线性形式：

$$\log(\mathrm{Profit}) = \log(\mathrm{Profit}_{\mathrm{base}}) + \sum_d \beta_d \cdot \log\left(\frac{\mathrm{KPI}_d}{\mathrm{base}_d}\right)$$

两种形式在小扰动下一阶等价；默认用线性形式，因为其系数可直接解读为"每单位 KPI 多少利润"。

### 5.2 模型假设——读后才敢信

§5.1 的线性模型看着干净，但站在四条假设上。违反任何一条都会静默产出错误奖金。

| # | 假设 | 什么情况会违反 |
|---|---|---|
| 1 | **线性性**：单位 KPI 的边际利润在工作区间内恒定 | 产能瓶颈、边际递减、KPI 大幅波动（±50%+） |
| 2 | **可加性**：部门之间互不交互 | 销售卖产能受限的产出；共享基础设施 |
| 3 | **β 时不变**：上季度测的弹性本季度仍成立 | 市场变动、产品组合变化、新竞争对手 |
| 4 | **$\hat{\beta}$ 无偏**：在 Gauss-Markov 条件下（外生性、无完全共线性、同方差）OLS 给出无偏估计 | 混杂因子、内生回归元、时序自相关 |

时序或面板数据请用 HAC 或聚类稳健 SE。**发薪前必须用 4–8 个季度真实数据回测 β 稳定性。**

### 5.3 单位约定

每个 KPI 的单位必须和它的 $\beta$ 配对，使得 $\beta \cdot \Delta\mathrm{KPI} = \mathrm{利润}$（元）。示例：

| 部门 | KPI 单位 | $\beta$ | 含义 |
|---|---|---|---|
| 销售 | 元营收 | 0.08 | 每 ¥1 营收 → ¥0.08 利润 |
| 采购 | 元降本 | 1.0 | 直接 1:1 传递 |
| 制造 | 效率 % | 250,000 | 每 1% → ¥25 万利润 |
| 质量 | 客诉件数 | 100,000 | 每少一单客诉 → ¥10 万利润 |

### 5.4 β 验证 — Monte Carlo + OLS

`monte_carlo_profit()` 用随机业绩模拟 $N$ 个场景，对每个部门抽取随机达成率 $a_d \sim \mathrm{Uniform}(0.8, 1.5)$，计算相应利润，再用 OLS 把模拟利润对各部门 KPI 回归：

$$\mathrm{Profit}^{(s)} = \alpha + \sum_d \hat{\beta}^{\mathrm{OLS}}_d \cdot \mathrm{KPI}^{(s)}_d + \varepsilon^{(s)}$$

恢复出的 $\hat{\beta}^{\mathrm{OLS}}_d$ 应与输入 $\beta_d$ 误差 <1%。**这是确认线性模型内部自洽**、以及你载入的 $\beta$ 确实复现了你声称的利润模型的安全检查。如果恢复误差 >1%，说明配置中某处违反了线性假设。

R 实现极简（Python 实现约 38 行，R 只要 5 行）：

```r
# 模拟 N 个场景，每个部门的 KPI 是 baseline 的随机倍数
ach <- matrix(runif(N * n_dep, 0.8, 1.5), N)              # N × n_dep 达成率矩阵
profit <- profit_base + (ach - 1) %*% (beta * baseline)   # 线性模型算利润
df <- data.frame(profit = profit, ach)                     # 整理为 lm 可用格式
fit <- lm(profit ~ ., data = df)                           # 一行 OLS
recovered <- coef(fit)[-1] / baseline                      # → 应与 `beta` 误差 <1%
```

### 5.5 利润缺口

$$\mathrm{gap} = \mathrm{target} - \mathrm{base}$$

这是奖金池要激励部门去闭合的"缺口"。它驱动 Layer 2 的档位校准。

---

## 6. Layer 2 · 档位校准

**文件**：`tiers.py`

每个部门在 baseline B 之上有两条档位线（A 和 S）。校准规则让档位线恰好闭合**固定份额**的利润缺口：

$$\beta_d \cdot (\mathrm{KPI}^{A}_d - \mathrm{base}_d) = \theta_A \cdot \mathrm{gap}$$

$$\beta_d \cdot (\mathrm{KPI}^{S}_d - \mathrm{base}_d) = \theta_S \cdot \mathrm{gap}$$

解出 KPI 档位线：

$$\mathrm{KPI}^{A}_d = \mathrm{base}_d + \frac{\theta_A \cdot \mathrm{gap}}{\beta_d}$$

$$\mathrm{KPI}^{S}_d = \mathrm{base}_d + \frac{\theta_S \cdot \mathrm{gap}}{\beta_d}$$

### 为什么这样有效

- **低 $\beta$ 部门档位带宽更大**：KPI 要变动更多才能挣到相同的奖金权重。
- **高 $\beta$ 部门档位带宽更窄**：KPI 小幅变动 → 利润影响大 → 档位距离小。
- 每次档位跃迁代表**相同的¥贡献**闭合利润缺口。这是跨部门公平的数学核心。

### 默认值

| 参数 | 默认 | 含义 |
|---|---|---|
| $\theta_A$ | 0.15 | A 档闭合 15% 利润缺口 |
| $\theta_S$ | 0.30 | S 档闭合 30% 利润缺口 |
| 封顶规则 | $\min(\mathrm{KPI}^{S}, \mathrm{KPI}^{\mathrm{stretch}})$ | 档位线永不超出用户定义的 stretch |

### 可达性

某部门若 $\beta_d \cdot (\mathrm{KPI}^{\mathrm{stretch}}_d - \mathrm{base}_d) < \theta_A \cdot \mathrm{gap}$，则**永远到不了 A 档**，无论多努力。v2 审计会标记这一点，让你能诚实地重新协商配额（见 [§8.7](#87-可达性审计)）。

---

## 7. Layer 3 · 分配算法（v1 与 v2）

### v1 — 背包贪心

**文件**：`allocator.py`

奖金池切成 $N$ 份（默认 1000）。每轮让边际价值/成本比最高的部门赢得一份。

$$V_d = w_d \cdot \ln\left(1 + \frac{s'_d + \mathrm{division}}{\mathrm{target}_d}\right)$$

$$\mathrm{KS}_d = \frac{V_d}{\mathrm{rounds}_d + 1} \quad (\text{胜者惩罚})$$

约束：
- **地板**：每部门至少 $\mathrm{min\_share} \cdot P$
- **封顶**：随业绩线性增长，封顶在 S 档分配额
- **不合格**：业绩低于 baseline（$a_d < 1.0$）的部门只拿地板

### v2 — 基础池 + 绩效池

**文件**：`v2_allocator.py`

奖金池切两半：

$$\underbrace{\lambda P}_{\mathrm{基础}} \text{按人头分} \quad \underbrace{(1-\lambda) P}_{\mathrm{绩效}} \text{按 } C^{*}_d \text{ 分}$$

基础池执行"同人数拿同基础"。绩效池执行"同贡献拿同绩效"。完整公式见 [§8](#8-v2-治理深入)。

---

## 8. v2 治理深入

v1 数学正确但被 Codex review 揪出三个现实问题：

| 问题 | v1 行为 | v2 修复 |
|---|---|---|
| 声称"同贡献同奖金"但没做到 | cap 和 floor 扭曲了比例 | quota + 人头基础池分离基础与绩效 |
| 忽略 stretch 可达性 | 静默误分类 | 显式审计 + 重新协商提示 |
| β 当真理 | 噪声估计 → 真钱 | 单侧 95% 下界 |

### 8.1 责任份额（`quota`）

每部门声明 $q_d$，满足 $\sum_d q_d = 1$：

$$\sum_{d=1}^{N} q_d = 1$$

含义："如果每个部门都到 A 档，正好闭合一个利润缺口——不是 $N \cdot \theta$。"

**为什么 sum=1 是承重墙**：没有这个约束，v1 隐式假设每个部门都对**整个**缺口负责（跨部门 $\theta$ 加总会超过 1，使"全员 A 档"过度闭合缺口）。有 $\sum q_d = 1$：

- 若每个部门都到 A 档，正好闭合一个利润缺口（假设 $\theta_A \cdot q_d$ 替代档位方程中的 $\theta_A$）。
- "全员 A 档"与利润目标在内部一致。
- Quota 是**谈判**量，不是测量量。模型呈现权衡，人来解决。

- 所有部门都设 `quota`：配置校验 sum=1（否则报错）。
- 都不设 `quota`：按 stretch_impact 份额推导：

$$q_d = \frac{\beta_d \cdot (\mathrm{KPI}^{\mathrm{stretch}}_d - \mathrm{base}_d)}{\sum_j \beta_j \cdot (\mathrm{KPI}^{\mathrm{stretch}}_j - \mathrm{base}_j)}$$

### 8.2 置信调整贡献（$C^{*}_d$）— v2 的统计核心

**大白话版**：你估出"1 元营收 → 0.08 元利润"（这就是 β）。但估计有噪声——可能真是 0.06，也可能是 0.10。你又看到销售多卖了 ¥30M。按 β 算，β × 30M = ¥2.4M 利润。但你不是 95% 确定 β 真是 0.08，所以不能按整 ¥2.4M 发奖金。换个做法：算出"95% 确信真挣到的那部分"的**下界**，若为负就夹到零，再按你对 β 来源的信任程度打折（RCT vs 专家估计）。这个打折过后、夹到非负的下界就是 $C^{*}_d$——我们确信被挣到的利润。只有这部分才转为奖金。

**形式化版**：我们有点估计 $\hat{\beta}_d$ 和标准误 $\mathrm{SE}(\hat{\beta}_d)$。给定观测到的 KPI 增量 $\Delta\mathrm{KPI}_d$，利润贡献点估计为：

$$\hat{C}_d = \hat{\beta}_d \cdot \Delta\mathrm{KPI}_d$$

因为利润模型对 $\beta$ 是**线性**的，标准误线性传播（链式法则，单一项）：

$$\mathrm{SE}(\hat{C}_d) = \left|\frac{\partial \hat{C}_d}{\partial \hat{\beta}_d}\right| \cdot \mathrm{SE}(\hat{\beta}_d) = |\Delta\mathrm{KPI}_d| \cdot \mathrm{SE}(\hat{\beta}_d)$$

然后构造真实贡献的**单侧 95% 下置信界**：

$$\hat{C}^{\mathrm{lower}}_d = \hat{C}_d - 1.645 \cdot \mathrm{SE}(\hat{C}_d) = \hat{\beta}_d \cdot \Delta\mathrm{KPI}_d - 1.645 \cdot |\Delta\mathrm{KPI}_d| \cdot \mathrm{SE}(\hat{\beta}_d)$$

下界先**夹到 0**（贡献不能为负），再**乘以来源质量权重** $\rho_d$。最终用于奖金归因的值：

$$C^{*}_d = \rho_d \cdot \max\left(0, \hat{\beta}_d \cdot \Delta\mathrm{KPI}_d - 1.645 \cdot |\Delta\mathrm{KPI}_d| \cdot \mathrm{SE}(\hat{\beta}_d)\right)$$

**同样的事用 R 写**（如果你更熟代码不熟公式）：

```r
# 每部门的置信调整贡献
c_hat   <- beta_hat * delta_kpi                         # 点估计
se_c    <- abs(delta_kpi) * se_beta                     # SE 线性传播
c_lower <- c_hat - qnorm(0.95) * se_c                   # 单侧 95% 下界（qnorm = 1.645）
c_star  <- rho * pmax(0, c_lower)                       # 夹到 ≥0，乘来源质量
```

### 8.3 为什么用单侧 95%，不用双侧 CI？

$\hat{C}_d$ 的双侧 95% CI 是 $\hat{C}_d \pm 1.96 \cdot \mathrm{SE}(\hat{C}_d)$。用下界作为奖金基准意味着我们对"贡献至少这么大"有 97.5% 信心。

**单侧** 95% 下界 $\hat{C}_d - 1.645 \cdot \mathrm{SE}(\hat{C}_d)$ 意味着：*真实贡献超过此值的概率为 0.95。* 我们接受 5% 的高估风险；我们不在乎对称的上尾风险，因为超额完成对公司不构成财务风险。

| | 双侧 95% CI | 单侧 95% 下界 |
|---|---|---|
| z 值 | 1.96 | 1.645 |
| 对 $C \geq \mathrm{bound}$ 的置信度 | 97.5% | 95% |
| 严格程度 | 更保守 | 较不保守 |
| 适用场景 | 通用推断 | 非对称损失（我们只怕多发） |

**为什么是 1.645？** 对 $Z \sim \mathcal{N}(0,1)$：$P(Z \leq 1.645) \approx 0.95$。R 里：`qnorm(0.95)` 返回 `1.6448536`。所以 $P(\hat{C}_d - 1.645 \cdot \mathrm{SE}(\hat{C}_d) \leq C_d) \approx 0.95$——下界以 95% 置信度成立（渐近意义下，由 CLT 保证）。

要改成 99% 置信，把 `v2_allocator.py` 中 `Z_95_ONE_SIDED` 改成 $2.326$（即 `qnorm(0.99)`）。权衡：更严 → 更多夹零 → 更大延迟池 → 更少部门拿到钱。

**敏感度示例**：当 $|\Delta\mathrm{KPI}| = 1000$、$\mathrm{SE}(\hat{\beta}) = 0.01$ 时，不确定性折价为 $1.645 \cdot 1000 \cdot 0.01 = 16.45$ 元/$\hat{\beta}$ 单位。若 $\hat{\beta} \cdot \Delta\mathrm{KPI} < 16.45$，整笔贡献被夹到零。

### 8.4 为什么 ρ 是来源质量权重？

$\rho_d \in [0,1]$（`beta_confidence_weight`）是对 $\hat{\beta}_d$ 信任度的**决策论先验**。它在下界应用后乘到整个 $C^{*}_d$ 上。

| 来源 | 建议 $\rho_d$ | 推理 |
|---|---|---|
| 随机对照试验 | 1.0 | 黄金标准；下界本身已足够 |
| Bridge 模型（会计恒等式） | 0.9 | 在会计假设内机械地真 |
| 历史数据回归 | 0.7 | 可能有未建模混杂 |
| 行业基准 | 0.5 | 市场情境不同 |
| 专家估计 | 0.3 | 主观性高；重度打折 |

这是一个**政策旋钮**，不是统计旋钮。它让业务方沟通"这个 $\beta$ 不靠谱"而不必把估计整个扔掉。

### 8.5 从 CI 反推标准误

若你提供 $\hat{\beta}_d$ 的 95% CI $[\beta^{\mathrm{lower}}, \beta^{\mathrm{upper}}]$：

$$\mathrm{SE}(\hat{\beta}_d) = \frac{\beta^{\mathrm{upper}} - \beta^{\mathrm{lower}}}{2 \cdot 1.96}$$

由对称双侧 CI 构造 $\hat{\beta} \pm 1.96 \cdot \mathrm{SE}$ 反推。若不提供 CI，$\mathrm{SE}(\hat{\beta}_d) = 0$，$C^{*}_d$ 退化为 $\rho_d \cdot \max(0, \hat{\beta}_d \cdot \Delta\mathrm{KPI}_d)$——纯点估计，无不确定性折价。

只有当 β 是从外部来源（行业基准、专家估计、第三方报告）载入时，你才需要上面这个反推公式。如果你在本仓库里跑回归，R 和 Stata 会直接把 SE 和 CI 吐出来：

```r
# R: lm() 直接返回 SE；confint() 给出 CI
fit <- lm(profit ~ kpi_sales + kpi_procurement + ..., data = df)
se_beta  <- summary(fit)$coefficients["kpi_sales", "Std. Error"]
ci_beta  <- confint(fit, "kpi_sales", level = 0.95)   # 2.5% / 97.5% 上下界
```

```stata
* Stata: SE 和 CI 随回归自动输出
reg profit kpi_*
* matrix list e(V)       → 方差-协方差矩阵，sqrt(diag) = SE
* matrix list e(b)       → 系数向量
* ereturn display        → 直接显示 [95% Conf. Interval]
```

### 8.6 评分、奖金与封顶

每部门的"目标"是它的配额份额乘以利润缺口：

$$\mathrm{target}_d = q_d \cdot \mathrm{gap}$$

达成率是置信调整贡献与目标之比：

$$a_d = \frac{C^{*}_d}{\mathrm{target}_d}$$

评分是达成率夹到 $[0, a_{\max}]$ 后再乘以配额：

$$s_d = q_d \cdot \min(\max(a_d, 0), a_{\max})$$

绩效奖金按 $s_d$ 比例分配绩效池：

$$\mathrm{PerfBonus}_d = (1-\lambda) \cdot P \cdot \frac{s_d}{\sum_j s_j}$$

基础奖金是纯人头分配：

$$\mathrm{BaseBonus}_d = \lambda \cdot P \cdot \frac{h_d}{H}$$

$h_d$ 是部门人头，$H = \sum_j h_j$ 是总人头。总奖金：

$$\mathrm{Bonus}_d = \mathrm{BaseBonus}_d + \mathrm{PerfBonus}_d$$

$a_{\max}$ 夹断（默认 1.5）防止单一部门极端业绩独占绩效池。

可选的部门封顶触发级联再分配：
1. 若 $\mathrm{Bonus}_d > \mathrm{cap}_d$：夹到 cap，超额按 $s_d$ 比例分给剩余部门。
2. 最多迭代 10 次（有界，防死循环）。
3. 全部封顶或零分时，残差进入 deferred 池。

### 8.7 可达性审计

**`cli audit example_config.yaml`** 打印：

```
department    stretch_impact  a_target_profit  can_reach_a  can_reach_s
Sales               2,400,000          450,000         True         True
Engineering           900,000          450,000         True        False  ⚠
```

若 `can_reach_a=False`，该部门 stretch 再拼也到不了 A 档。**别静默让他们背锅——先重谈 quota 或 KPI baseline。**

### 8.8 确定性与可复现

`allocate_v2()` 是 `(config, sens, achievements, caps)` 的纯函数。给定相同输入，返回字节一致输出（由长时压测验证，§12）。这是审计的硬要求——一发薪不可复现，就不可辩护。

---

## 9. 发布闸门

6 个布尔检查。**必须全部为 `True`** 才能发薪。它们**必要不充分**——6 道全过意味着运行内部自洽，并不意味着 $\beta$ 值正确或模型适合你的业务。

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

| 闸门 | 能抓什么问题 |
|---|---|
| `pool_utilization_90_to_100` | 分配器 bug（少/多发）、deferred 池配置错误 |
| `no_nan_bonus` | 数值爆炸（如 $\hat{\beta} = \infty$） |
| `no_negative_bonus` | floor/cap 交互 bug |
| `achievers_have_nonneg_c_star` | 达成者的 CI 比 $\hat{\beta}$ 还宽 → 夹零逻辑失败 |
| `quotas_sum_to_one` | 配置校验被绕过或浮点漂移 |
| `monotonic_in_c_star_within_quota` | "同贡献同奖金"承诺被违反 |

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
| `beta` | float | 必填 | 利润弹性（$\beta \cdot \Delta\mathrm{KPI} = \mathrm{利润}$） |
| `headcount` | int | 1 | 部门人数（驱动 v2 基础池） |
| `base_bonus` | float | 0.0 | v1 固定 B 档奖金 |
| `quota` | float | None | v2 责任份额（全设或全不设，sum=1） |
| `beta_ci_lower`, `beta_ci_upper` | float | None | $\hat{\beta}$ 的 95% CI 上下界；省略则 SE=0 |
| `beta_confidence_weight` | float | 1.0 | $\rho \in [0,1]$，对 β 估计的置信度 |
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
- **零分冻结**：所有 $\rho = 0$ → 整个绩效池延迟
- **封顶溢出**：每个部门都封顶 → 级联再分配
- **超宽 CI**：CI > $\hat{\beta}$ → $C^{*}_d$ 夹到零
- **模糊测试**：100 个随机配置，所有发布闸门必须通过
- **对抗场景**：1 个 mega 部门 + 99 个 tiny 部门
- **长时运行**：1000 次模糊迭代，确定性 + 无漂移
- **故障注入**：CI 倒挂、$\rho \notin [0,1]$、quota sum ≠ 1——全部抛错
- **属性测试**：单调性、缩放不变性（$2P \Rightarrow 2\,\mathrm{奖金}$）

### 性能（8 部门，MacBook M 系列）

```
v2 分配:           < 5 ms
v2 分配 (5000 部门): ~38 ms
fuzz 1000 次:      ~1.5 秒总计
```

### 关于实现语言的一点说明

本仓库的生产代码用 Python，主要为了和 numpy/pandas 生态一致、便于接 Streamlit dashboard。但 README 里的示例大量使用 R（和少量 Stata），因为 CFO/HR/分析师群体更熟悉这两者，而且对于"算一算、看一眼"这类探索性任务，R 通常更简洁。下表是同一任务在两种语言下的对比：

| 任务 | 本仓库 Python 实现 | R 等价实现 |
|---|---|---|
| 随机配置生成（Dirichlet 采样） | ~73 行 `np.random.default_rng` + Dirichlet | ~25 行 `rdirichlet` + `tibble` |
| 1000 次确定性检查 | ~26 行 for-loop + 断言 | ~5 行 `replicate(1000, ...)` + `sapply` |
| 结果汇总 + CSV 输出 | ~13 行 `pd.DataFrame` + summary | ~5 行 `bind_rows` + `summary()` |

权衡：Python 版本与主代码库一致、便于 CI 集成；R 版本更适合一次性脚本、ad-hoc 分析、向非技术读者演示。如果你要做严肃的回归诊断（残差图、异方差检验、聚类稳健 SE），R 的 `lm()` + `sandwich` + `ggplot2` 工作流仍然是最顺手的。

---

## 13. 通往生产环境之路

这是一个**模型**。模型不发钱。真实发薪需要：

1. **所有发布闸门通过**生产配置。
2. **历史回测**：把 4–8 个季度的真实 KPI 数据喂给 v2，验证 $\hat{\beta}$ 稳定性和排名一致性（对照已知业务结果）。
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

1. $\hat{\beta}$ 是**估计值**，不是测量值。垃圾进，垃圾出。
2. 线性利润模型是**一阶近似**。KPI 大幅波动（±50%+）会违反它。
3. v2 的 $a_{\max} = 1.5$ 夹断是**政策选择**，不是定律。按你的风险偏好调整。
4. 人头基础池假设**每颗人头权重相等**。如果资历/角色构成有差异，自己调。
5. 配额协商是**政治**，不是数学。本工具呈现权衡，不解决权衡。

### 贡献

这是内部原型的脱敏公开版。欢迎 bug 报告和数学批评；功能请求按"这是不是该进治理关键分配器"的标准评估。

---

**为那些希望奖金分配可审计、可辩论、可辩护——而不是魔法——的 CFO、HR 负责人和业务负责人而设计。**
