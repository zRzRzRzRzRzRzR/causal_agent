# 步骤 2：为单一边缘填充 HPP 模板

你是一名医学信息学研究员。你获得：

1. 一个 **JSON 模板**（带有内联的 `//` 注释解释每个字段）
2. 一篇论文的全文
3. 一个提取出的边缘（X → Y 关系）及其汇总统计
4. 用于变量映射的检索到的 HPP 数据集字段

你的任务：**用论文中的实际值填充模板中的每个占位符**。

---

## 待填充的边缘

```
边缘 #{edge_index}: {X} → {Y}
对照/参考: {C}
亚组: {subgroup}
结局类型: {outcome_type}
效应尺度: {effect_scale}
估计值: {estimate}
置信区间: {ci}
P值: {p_value}
来源: {source}
```

## 论文信息

```
第一作者: {first_author}
年份: {year}
DOI: {doi}
证据类型: {evidence_type}
```

---

## 如何填充模板

### 核心规则

- **仅使用论文中的信息**。不确定的字段填写 `null`。
- **不要编造**论文中未提及的任何数据。
- **所有变量名称**：使用下划线代替空格（例如，使用 `sleep_duration` 而非 `sleep duration`）。
- **仔细阅读模板中的每个 `//` 注释** — 它会告诉你确切应该在相邻字段中填入什么值。
- 输出应该是**有效的 JSON**，不包含 `//` 注释。

### 字段特定指南

#### edge_id

格式: `EV_{year}_{AuthorStudy}#{edge_number}`，例如 `EV_2023_RassyUKBiobank#1`

#### paper_title & paper_abstract

- `paper_title`：完整的论文标题，空格用下划线代替
- `paper_abstract`：简短摘要（1-3 句话，总结设计、样本量、主要发现），空格用下划线代替

#### equation_type (E1–E6)

根据统计方法选择：

- **E1**：logistic、linear、Poisson、ANCOVA、t 检验、MR/IVW — 静态模型
- **E2**：Cox 比例风险、生存模型、KM 比较
- **E3**：LMM、GEE、重复测量 — 纵向模型
- **E4**：中介分析、路径分析（需要中介变量 M）
- **E5**：个体处理效应（ITE、CATE）
- **E6**：带交互项的联合干预（需要第二个处理 X2）

#### equation_formula

编写具体的模型公式，例如：

- `"λ(t|do(X=x),Z) = λ₀(t) · exp(β_X · X + γ_age · Age + γ_sex · Sex)"`
- `"logit(P(Y=1)) = α + β*X + γ*Age + δ*Sex"`
- `"E[Y | BMI_group] = α + β · BMI_group"`（用于 t 检验）

#### epsilon.Pi (人群标签)

常见值：`"adult_general"`（一般成人）、`"cvd"`（心血管疾病）、`"diabetes"`（糖尿病）、`"oncology"`（肿瘤）、`"pediatric"`（儿科）

#### epsilon.mu.core

- 对于 HR/OR/RR：`family="ratio"`，`type="HR"/"OR"/"RR"`，`scale="log"`
    - `theta_hat` 必须在**对数尺度**上：theta_hat = ln(HR)
    - `ci` 也必须在对数尺度上：ci = [ln(CI_lower), ln(CI_upper)]
    - 同时在 `reported_HR` 和 `reported_CI_HR` 中报告原始值
- 对于 MD/BETA/SMD：`family="difference"`，`type="MD"/"BETA"/"SMD"`，`scale="identity"`
    - `theta_hat` 是原始差值

#### epsilon.alpha

- `id_strategy`：`"rct"`（随机对照试验） / `"observational"`（观察性研究） / `"MR"`（孟德尔随机化） / `"IV"`（工具变量） /
  `"pooled_estimates"`（汇总估计）
- `assumptions`：从以下列表选择
  `["exchangeability"`（可交换性）, `"positivity"`（正值性）, `"consistency"`（一致性）, `"proportional_hazards"`（比例风险假设）,
  `"no_publication_bias"`（无发表偏倚）, `"sequential_ignorability"`（序列可忽略性）]`
- `status`：`"identified"`（可识别） / `"partially_identified"`（部分可识别） / `"not_identified"`（不可识别）

#### epsilon.rho

- `X`：暴露/处理变量名称（必须关联 `iota.core.name`）
- `Y`：结局变量名称（必须等于 `o.name`）
- `Z`：论文中的协变量/调整变量列表
- `IV`：工具变量（仅用于 MR 研究，否则为 `null`）

#### literature_estimate

- `theta_hat`：效应估计值，为**数字**（非字符串），尺度与 `mu.core.scale` 匹配
- `ci`：[下限, 上限]，尺度相同，如未报告则为 `null`
- `p_value`：数字或字符串如 `"<0.001"`，或 `null`
- `n`：样本量（整数）
- `design`：`"RCT"`（随机对照试验） / `"cohort"`（队列研究） / `"cross-sectional"`（横断面研究） / `"case-control"`
  （病例对照研究） / `"meta-analysis"`（荟萃分析） / `"MR"`（孟德尔随机化）
- `grade`：`"A"`（高质量 RCT）、`"B"`（中等质量）、`"C"`（低质量/无调整）
- `model`：与 equation_type 匹配的统计模型名称
- `adjustment_set`：调整变量列表（应与 `rho.Z` 匹配）
- **允许额外字段**：`reported_HR`、`reported_CI_HR`、`group_means`、`notes` 等

#### hpp_mapping

将每个变量映射到 HPP 数据集+字段：

- 使用下方**检索到的 HPP 数据集**找到最佳匹配
- 数据集 ID 使用下划线格式：`"009_sleep"`、`"002_anthropometrics"`
- 仅在 E4 中包含 `M`，仅在 E6 中包含 `X2`
- **允许额外字段**：`mapping_notes`、`composite_components`、`BMI_covariate` 等

### HPP 字段映射

以下是此边缘的**检索到的 HPP 数据集和字段**。使用它们来填充 `hpp_mapping`：

```
{hpp_context}
```

---

## 模板（// 注释作为提示）

仔细阅读每个 `//` 注释 — 它们解释了每个字段的含义以及哪些值是有效的。
你的输出必须是**干净的 JSON**，所有字段都已填充，**没有 // 注释**。

```
{template_json}
```

---

## 输出要求

输出**一个完整的 JSON 对象**（不是数组）。它必须：

1. 匹配模板结构 — 所有顶级键都存在
2. 未知字段使用 `null`（而非 `"..."` 或空字符串）
3. `theta_hat` 必须是**数字**或 `null`
4. 对于比值比（HR/OR/RR）：theta_hat 和 ci 在**对数尺度**上；同时包含原始值的 `reported_HR`/`reported_CI_HR`
5. 变量命名：`rho.Y` 必须等于 `o.name`
6. hpp_mapping 中的数据集 ID 使用**下划线**格式（例如，`009_sleep`）
7. 你可以添加额外的描述性字段（mapping_notes、composite_components、notes、group_means 等）
8. 不要在输出中包含任何 `//` 注释或 `_comment` 键
