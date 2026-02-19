# 步骤 2：为单一因果边填充 HPP 模板

你是一名医学信息学研究员。你获得：

1. 一个 **JSON 模板**（带有内联的 `//` 注释解释每个字段）
2. 一篇论文的全文
3. **一条**提取出的因果边（X → Y 关系）及其汇总统计
4. 用于变量映射的检索到的 HPP 数据集字段

你的任务：**用论文中的实际值填充模板中的每个占位符，输出一个完整的 JSON 对象**。

---

## ⚠️ 核心约束：一条边 = 一个 X → 一个 Y

> **每次调用本提示词只处理一条因果边。**
>
> - 一条边只有**一个暴露变量 X** 和**一个结局变量 Y**。
> - 如果论文报告了同一个 X 对多个 Y 的效应（例如生活方式评分 → 高血压、生活方式评分 → 糖尿病），这些是**不同的边**
    ，应分别调用本提示词生成各自的 JSON。
> - 如果论文报告了多个 X 对同一个 Y 的效应，同样是**不同的边**。
> - **不要**在一个 JSON 输出中混合多条边的信息。

---

## 待填充的边

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

1. **仅使用论文中的信息**。不确定的字段填写 `null`。
2. **不要编造**论文中未提及的任何数据。
3. **所有变量名称**：使用下划线代替空格（例如，使用 `sleep_duration` 而非 `sleep duration`）。
4. **仔细阅读模板中的每个 `//` 注释** — 它会告诉你确切应该在相邻字段中填入什么值。
5. 输出应该是**有效的 JSON**，不包含 `//` 注释。

### 一致性校验清单（输出前必须逐项检查）

在生成最终 JSON 之前，请逐项确认以下条件全部满足：

| 编号 | 校验项              | 必须满足的条件                                                                 |
|----|------------------|-------------------------------------------------------------------------|
| C1 | X 一致性            | `epsilon.iota.core.name` == `epsilon.rho.X` == `hpp_mapping.X` 对应的变量    |
| C2 | Y 一致性            | `epsilon.o.name` == `epsilon.rho.Y` == `hpp_mapping.Y` 对应的变量            |
| C3 | Z 一致性            | `epsilon.rho.Z` == `literature_estimate.adjustment_set`                 |
| C4 | 尺度一致性            | 若 `mu.core.family="ratio"`，则 `theta_hat` 和 `ci` 必须是**对数尺度**的值           |
| C5 | equation_type 匹配 | `literature_estimate.model` 与 `equation_type` 对应（见下表）                   |
| C6 | M 字段条件           | 仅当 `equation_type="E4"` 时，`hpp_mapping.M` 才非 null；其他情况 `M` 必须为 `null`   |
| C7 | X2 字段条件          | 仅当 `equation_type="E6"` 时，`hpp_mapping.X2` 才非 null；其他情况 `X2` 必须为 `null` |
| C8 | 单边约束             | 整个 JSON 中只描述一条 X→Y 边，不混入其他边的数据                                          |

---

### 字段特定指南

#### edge_id

格式: `EV_{year}_{AuthorStudy}#{edge_number}`

示例: `EV_2023_RassyUKBiobank#1`

规则：

- `{year}` = 论文发表年份
- `{AuthorStudy}` = 第一作者姓 + 研究简称（无空格）
- `#{edge_number}` = 当前边的编号

#### paper_title & paper_abstract

- `paper_title`：完整的论文标题，空格用下划线代替
- `paper_abstract`：简短摘要（1-3 句话，总结设计、样本量、主要发现），空格用下划线代替

#### equation_type (E1–E6)

根据统计方法选择**一个**：

| equation_type | 适用统计方法                                     | 对应 model 值                                                 |
|---------------|--------------------------------------------|------------------------------------------------------------|
| **E1**        | logistic、linear、Poisson、ANCOVA、t 检验、MR/IVW | `"logistic"`, `"linear"`, `"poisson"`, `"ANCOVA"`, `"IVW"` |
| **E2**        | Cox 比例风险、生存模型、KM 比较                        | `"Cox"`, `"parametric_survival"`, `"KM"`                   |
| **E3**        | LMM、GEE、重复测量 ANOVA                         | `"LMM"`, `"GEE"`, `"mixed"`                                |
| **E4**        | 中介分析、路径分析（**必须有中介变量 M**）                   | `"mediation"`, `"path_analysis"`                           |
| **E5**        | 个体处理效应（ITE、CATE）                           | `"counterfactual"`, `"S-learner"`, `"T-learner"`           |
| **E6**        | 带交互项的联合干预（**必须有第二处理变量 X2**）                | `"interaction_model"`, `"factorial"`                       |

**判断流程**：

1. 论文用了什么统计模型？→ 查上表找到对应的 equation_type
2. 如果是 Cox 比例风险模型 → 选 E2
3. 如果有中介变量 → 选 E4
4. 如果有两个处理变量的交互 → 选 E6
5. 其他静态回归 → 选 E1

#### equation_formula

编写具体的模型公式。示例：

- E1: `"logit(P(Y=1)) = α + β*X + γ*Age + δ*Sex"`
- E2: `"λ(t|do(X=x),Z) = λ₀(t) · exp(β_X · X + γ_age · Age + γ_sex · Sex)"`
- E1 (t检验): `"E[Y | BMI_group] = α + β · BMI_group"`

#### epsilon.Pi (人群标签)

根据论文的研究人群选择一个：

| 值                 | 含义      |
|-------------------|---------|
| `"adult_general"` | 一般成人    |
| `"cvd"`           | 心血管疾病人群 |
| `"diabetes"`      | 糖尿病人群   |
| `"oncology"`      | 肿瘤人群    |
| `"pediatric"`     | 儿科人群    |

#### epsilon.mu.core（效应度量）

**这是最容易出错的部分，请仔细阅读。**

分两种情况：

**情况 A：比值类指标（HR / OR / RR）**

```json
{
  "family": "ratio",
  "type": "HR",
  // 或 "OR"、"RR"
  "scale": "log"
}
```

- `theta_hat` 必须填**对数值**：`theta_hat = ln(HR)`
    - 例如：论文报告 HR = 0.84 → `theta_hat = ln(0.84) ≈ -0.1744`
- `ci` 也必须填对数值：`ci = [ln(CI_lower), ln(CI_upper)]`
    - 例如：95% CI [0.78, 0.90] → `ci = [ln(0.78), ln(0.90)] ≈ [-0.2485, -0.1054]`
- **同时**在 `literature_estimate` 中添加原始值字段：
    - `"reported_HR": 0.84`（或 `reported_OR`、`reported_RR`）
    - `"reported_CI_HR": [0.78, 0.90]`（或 `reported_CI_OR`、`reported_CI_RR`）

**情况 B：差值类指标（MD / BETA / SMD）**

```json
{
  "family": "difference",
  "type": "MD",
  // 或 "BETA"、"SMD"
  "scale": "identity"
}
```

- `theta_hat` 填原始差值（无需转换）
- `ci` 填原始置信区间

#### epsilon.alpha（因果识别）

- `id_strategy`：从以下选择一个
    - `"rct"` — 随机对照试验
    - `"observational"` — 观察性研究
    - `"MR"` — 孟德尔随机化
    - `"IV"` — 工具变量
    - `"pooled_estimates"` — 汇总估计
- `assumptions`：从以下列表中选择适用的（可多选）
    - `"exchangeability"` — 可交换性
    - `"positivity"` — 正值性
    - `"consistency"` — 一致性
    - `"proportional_hazards"` — 比例风险假设（Cox 模型专用）
    - `"no_publication_bias"` — 无发表偏倚
    - `"sequential_ignorability"` — 序列可忽略性（E4 中介专用）
- `status`：
    - `"identified"` — 可识别（RCT 或假设完全满足）
    - `"partially_identified"` — 部分可识别（观察性研究，有调整但可能有残余混杂）
    - `"not_identified"` — 不可识别

#### epsilon.rho（变量角色）

```json
{
  "X": "暴露变量名",
  // 必须 == epsilon.iota.core.name
  "Y": "结局变量名",
  // 必须 == epsilon.o.name
  "Z": [
    "协变量1",
    "协变量2"
  ],
  // 论文中的调整变量列表
  "IV": null
  // 仅 MR 研究填写，否则为 null
}
```

注意：

- **不要**在 rho 中添加 M 或 X2 字段。M 和 X2 仅出现在 `hpp_mapping` 中。
- rho.Z 应与 literature_estimate.adjustment_set 保持一致。

#### literature_estimate

| 字段               | 类型              | 说明                                                                                         |
|------------------|-----------------|--------------------------------------------------------------------------------------------|
| `theta_hat`      | 数字              | 效应估计值（比值类填 log 值，差值类填原值）                                                                   |
| `ci`             | [数字, 数字] 或 null | 95% CI，尺度同 theta_hat                                                                       |
| `ci_level`       | 数字              | 置信水平，通常 0.95                                                                               |
| `p_value`        | 数字、字符串或 null    | 如 `0.001` 或 `"<0.001"`                                                                     |
| `n`              | 整数              | 样本量                                                                                        |
| `design`         | 字符串             | `"RCT"` / `"cohort"` / `"cross-sectional"` / `"case-control"` / `"meta-analysis"` / `"MR"` |
| `grade`          | 字符串             | `"A"`（高质量 RCT）、`"B"`（中等质量）、`"C"`（低质量/无调整）                                                  |
| `model`          | 字符串             | 必须与 equation_type 对应（见上方表格）                                                                |
| `adjustment_set` | 数组              | 调整变量列表，必须与 rho.Z 一致                                                                        |

**允许添加的额外字段**（视情况使用）：

- `reported_HR` / `reported_OR` / `reported_RR`：论文报告的原始比值
- `reported_CI_HR` / `reported_CI_OR` / `reported_CI_RR`：原始比值的 CI
- `subgroup`：亚组描述
- `group_means`：各组均值
- `notes`：其他备注

#### hpp_mapping（HPP 数据集映射）

**关键规则**：

1. `hpp_mapping` 中**始终只有 4 个键**：`X`、`Y`、`M`、`X2`
2. `X` 和 `Y` 始终非 null（每条边必须有暴露和结局的映射）
3. `M` 仅当 `equation_type = "E4"` 时非 null，**其他情况必须为 `null`**
4. `X2` 仅当 `equation_type = "E6"` 时非 null，**其他情况必须为 `null`**

每个非 null 的映射项结构如下：

```json
{
  "dataset": "数据集编号",
  // 如 "055_lifestyle_and_environment"
  "field": "具体字段名",
  // 如 "smoking_current_status"
  "status": "exact"
  // 见下方 status 定义
}
```

**status 的四种取值**：

| status        | 含义                  | 示例                                              |
|---------------|---------------------|-------------------------------------------------|
| `"exact"`     | 字段含义与论文变量完全一致       | 论文 "Age" → 字段 `age_at_research_stage`           |
| `"close"`     | 存在近似字段，但定义略有差异      | 论文 "每日步数" → 字段 `activity_walking_minutes_daily` |
| `"tentative"` | 需通过现有字段计算得出，不确定是否正确 | BMI 需由身高体重计算                                    |
| `"missing"`   | 在 HPP 数据集中完全找不到     | 无对应字段                                           |

**数据集 ID 格式**：使用下划线（如 `"009_sleep"`、`"002_anthropometrics"`），不使用连字符。

**允许添加的额外字段**：

- `mapping_notes`：映射说明
- `composite_components`：复合变量的组成部分
- `BMI_covariate`：BMI 作为协变量时的映射

### HPP 字段映射参考

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

输出**一个完整的 JSON 对象**（不是数组，不是多个对象）。它必须：

1. 匹配模板结构 — 所有顶级键都存在
2. 未知字段使用 `null`（而非 `"..."` 或空字符串）
3. `theta_hat` 必须是**数字**或 `null`（不是字符串）
4. 对于比值类指标（HR/OR/RR）：`theta_hat` 和 `ci` 在**对数尺度**上；同时包含 `reported_HR`/`reported_CI_HR` 等原始值字段
5. 通过上方的**一致性校验清单**（C1–C8）全部检查
6. `hpp_mapping` 中的数据集 ID 使用**下划线**格式（例如，`009_sleep`）
7. 当 `equation_type` 不是 E4 时，`hpp_mapping.M` 必须为 `null`
8. 当 `equation_type` 不是 E6 时，`hpp_mapping.X2` 必须为 `null`
9. 可以添加额外的描述性字段（`mapping_notes`、`composite_components`、`notes`、`group_means`、`subgroup` 等）
10. **不要**在输出中包含任何 `//` 注释或 `_comment` 键
11. 整个输出只描述**一条边**的信息
