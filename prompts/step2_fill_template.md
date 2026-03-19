# 步骤 2: 为单个边填充HPP模板

你是一名医学信息学研究员。你收到：

1. 一个**JSON模板**（带有内联`//`注释解释每个字段）
2. 完整的论文文本
3. **一个**提取的边（X → Y关系）及汇总统计量
4. **预确定的方程元数据**（equation_type、model、mu、theta_hat、ci — 已验证）
5. 检索到的用于变量映射的HPP数据集字段

你的任务：**使用论文的实际值填充模板中的每个占位符，输出一个完整的JSON对象。**

---

## ⚠️ 核心约束：一个边 = 一个X → 一个Y

> 每次调用此提示词处理恰好**一个**边。
> **不要**混合来自其他边的数据。

---

## 要填充的边

```
Edge #{edge_index}: {X} → {Y}
Control/reference: {C}
Subgroup: {subgroup}
Outcome type: {outcome_type}
Effect scale: {effect_scale}
Estimate: {estimate}
CI: {ci}
P-value: {p_value}
Source: {source}
```

## 论文信息

```
First author: {first_author}
Year: {year}
DOI: {doi}
Evidence type: {evidence_type}
```

---

## 预确定字段（请勿更改）

以下字段已经**预验证和预计算**。完全按照给定值使用它们。
管道将用这些值覆盖你的输出，所以不要浪费时间重新推导它们。

**这些值在此提示词之后注入 —— 查看下面的"预验证方程元数据"部分。**

预确定的字段：
- `equation_type` — 已从effect_scale和outcome_type推导
- `literature_estimate.model` — 已推导
- `epsilon.mu.core`（family、type、scale）— 已推导
- `literature_estimate.theta_hat` — 已转换为正确的尺度（比率用对数）
- `literature_estimate.ci` — 已转换为正确的尺度
- `reported_HR` / `reported_OR` / `reported_RR` — 原始尺度值（用于比率测量）
- `reported_CI_HR` / `reported_CI_OR` / `reported_CI_RR` — 原始CI
- `epsilon.alpha.id_strategy` — 已从evidence_type推导

---

## 你需要填充的字段（将精力集中在这里）

### 1. 论文元数据
- `paper_title`: 完整的论文标题，空格替换为下划线
- `paper_abstract`: 简要摘要（1-3句话：设计、样本量、主要发现），空格替换为下划线

### 2. equation_formula 和 equation_formula_reported
- `equation_formula`: 使用预确定的equation_type框架编写特定的模型公式。如果提供了公式骨架，作为指导使用。
  如果当前效应值来自未调整比较/直接比较/列联表/Fisher/t-test/Model 1=no adjustments，则不要为了套公式强行加入 `+ gamma^T * Z`。
- `equation_formula_reported`: 填充子字段，但**不得为了填满模板而臆造统计细节**。如果论文未明确报告某项，优先使用 `null` 或 `[]`，而不是猜测。
- **Z 硬规则**：如果当前 edge 对应的是未调整结果，或论文只做了 direct comparison / contingency tables / difference in proportions / exact test，那么以下四处必须同时为 `[]`：
  `equation_formula_reported.Z`、`epsilon.rho.Z`、`literature_estimate.adjustment_set`、`hpp_mapping.Z`
- 如果论文同时报告多个模型（如 Model 1 / Model 2 / Model 3），**只能**填写与你当前 edge 的 estimate / CI / p_value 对应的那个模型的调整变量；不能混用别的模型的协变量。

### 3. epsilon字段（来自论文）
- `epsilon.Pi`: 人群标签 — `"adult_general"`（成人一般人群）、`"cvd"`（心血管疾病）、`"diabetes"`（糖尿病）、`"oncology"`（肿瘤）、`"pediatric"`（儿科）
- `epsilon.iota.core.name`: 暴露变量的**简洁名称**（例如 `"Healthy Lifestyle Score"` 而非 `"Healthy Lifestyle Score 4 (Never smoking, Physically active, ...)"）`。必须匹配rho.X
- `epsilon.o.name`: 结局变量名称（必须匹配rho.Y）
- `epsilon.o.type`: `"binary"`（二分类）、`"continuous"`（连续）或`"survival"`（生存）
- `epsilon.tau`: 时间坐标（index、horizon，ISO 8601格式如"P5Y"、"P10Y"）
- `epsilon.mu.core.type`: 对于比率度量（HR/OR/RR），**必须使用log前缀**：`"logHR"`, `"logOR"`, `"logRR"`（因为theta_hat在对数尺度上）。对于差异度量：`"MD"`, `"BETA"`, `"SMD"`
- `epsilon.alpha.assumptions`: 选择适用的：`"exchangeability"`（可交换性）、`"positivity"`（正性）、`"consistency"`（一致性）。**不要**包含 `"proportional_hazards"`
- `epsilon.alpha.status`: `"identified"`（已识别，RCT）/ `"partially_identified"`（部分识别，观察性）/ `"not_identified"`（未识别）
- `epsilon.rho`: 变量角色 — X（使用简洁名称，与iota.core.name一致）、Y、Z（调整变量列表）。**默认填 `[]`**；只有论文对当前效应值明确报告了调整变量时才填写。不要把 baseline table 中的 Age/Sex/BMI/TDI 当成调整变量。IV（仅MR，否则为null）

### 4. literature_estimate（剩余字段）
- `n`: 总样本量
- `design`: `"RCT"` / `"cohort"`（队列）/ `"cross-sectional"`（横断面）/ `"case-control"`（病例对照）/ `"meta-analysis"`（荟萃分析）/ `"MR"`
- `grade`: `"A"`（高质量RCT）、`"B"`（中等）、`"C"`（低质量/未调整）
- `adjustment_set`: 调整变量列表（必须匹配rho.Z）。**默认填 `[]`**；只有论文对当前效应值明确报告了调整变量时才填写。
- `p_value`: 仅在论文明确报告时填写；**不要**根据 CI、显著性、Bonferroni 校正或常识反推。
- `ci_level`: 通常为0.95

⚠️ **literature_estimate 只包含以下字段**：`theta_hat`, `ci`, `ci_level`, `p_value`, `n`, `design`, `grade`, `model`, `adjustment_set`。**禁止**添加 `subgroup`, `control_reference`, `reported_HR`, `reported_CI_HR`, `group_means`, `notes` 等额外字段。

### 5. hpp_mapping（关键 — 这决定了管道能否运行）

⚠️ **hpp_mapping 结构必须严格遵循以下模式，不允许添加任何额外字段。**

每个变量映射**只能有4个字段**：
```json
{"name": "变量名", "dataset": "009-sleep", "field": "字段名", "status": "exact|close|tentative|missing"}
```

**hpp_mapping 顶层结构**（必须包含所有5个键）：
```json
{
  "X": {"name": "...", "dataset": "...", "field": "...", "status": "..."},
  "Y": {"name": "...", "dataset": "...", "field": "...", "status": "..."},
  "Z": [
    {"name": "协变量1", "dataset": "...", "field": "...", "status": "..."},
    ...
  ],
  "M": null,
  "X2": null
}
```

**关键约束**：
- 每个映射对象**恰好4个字段**：`name`, `dataset`, `field`, `status`。**禁止**添加 `mapping_notes`、`composite_components`、`subgroup_mapping` 或任何其他字段。
- `M`: 仅当 equation_type = E4 时填写，否则**必须为** `null`
- `X2`: 仅当 equation_type = E6 时填写，否则**必须为** `null`
- **M 和 X2 必须始终出现**在 hpp_mapping 中（作为 `null` 或映射对象）
- `Z`: 协变量必须在论文中作为**当前效应值的调整变量**明确出现，才能写上。若当前结果未调整，则 `hpp_mapping.Z` 必须为 `[]`。不要因为 HPP 里存在 age/sex/bmi 字段，就反向把它们写成论文协变量。
- **数据集ID使用连字符格式**：`"009-sleep"`、`"002-anthropometrics"`、`"021-medical_conditions"`（编号和名称之间用连字符`-`连接）
- 对于复合变量（如 Healthy Lifestyle Score），X.name 使用简洁名称（如 `"Healthy Lifestyle Score"`），X.field 中用 `+` 连接多个字段（如 `"smoking_status + activity_minutes + alcohol_frequency + diet_*"`），status 标为 `"tentative"`
- 对于需要从现有字段计算/推导的变量，status 必须标为 `"tentative"` 而非 `"exact"`

**status值**：
| status | 含义 |
|--------|------|
| `exact` | HPP中有直接对应字段，语义完全匹配 |
| `close` | HPP中有近似字段，定义略有不同 |
| `tentative` | 需要从HPP现有字段计算或组合得到 |
| `missing` | HPP字典中无对应字段 |

---

## 一致性检查清单（输出前验证）

| # | 检查    | 条件 |
|---|-------|------|
| C1 | X一致性  | `epsilon.iota.core.name` == `epsilon.rho.X` == `hpp_mapping.X.name` |
| C2 | Y一致性  | `epsilon.o.name` == `epsilon.rho.Y` == `hpp_mapping.Y.name` |
| C3 | Z一致性  | `equation_formula_reported.Z` == `epsilon.rho.Z` == `literature_estimate.adjustment_set` == `hpp_mapping.Z[*].name`（若未调整则四处都为空） |
| C4 | M条件   | `hpp_mapping.M`非null仅当equation_type = "E4" |
| C5 | X2条件  | `hpp_mapping.X2`非null仅当equation_type = "E6" |
| C6 | 单个边   | JSON恰好描述一个X→Y边 |

---

## HPP字段映射参考

为该边检索的HPP数据集和字段：

```
{hpp_context}
```

---

## 模板（//注释是提示，代表每个字段的内容。阅读它们，但不要包含在输出中）

```
{template_json}
```

---

## 输出要求

输出**一个完整的JSON对象**。它必须：

1. 匹配模板结构 — 所有顶级键都存在
2. 对未知字段使用`null`（不是`"..."`或空字符串）
3. `theta_hat`必须是**数字**或`null`（不是字符串）
4. 通过上述一致性检查清单（C1–C6）
5. 数据集ID使用**连字符**格式（例如，`009-sleep`、`055-lifestyle_and_environment`）
6. **hpp_mapping中每个变量映射只允许4个字段**：`name`, `dataset`, `field`, `status`。**禁止**添加 `mapping_notes`、`composite_components`、`subgroup_mapping` 等。
7. **hpp_mapping必须包含** `M` 和 `X2` 键（值为 `null` 除非 E4/E6）
8. **literature_estimate只允许**：`theta_hat`, `ci`, `ci_level`, `p_value`, `n`, `design`, `grade`, `model`, `adjustment_set`。**禁止** `reported_HR`, `reported_CI_HR`, `subgroup`, `control_reference`, `notes`, `group_means` 等。
9. **不要**在输出中包含`//`注释、`_comment`键、`_validation`键
10. `epsilon.mu.core.type` 对于比率度量必须使用log前缀（`logHR`/`logOR`/`logRR`）
11. `epsilon.iota.core.name` 和 `epsilon.rho.X` 使用简洁变量名
12. 恰好描述**一个**边
13. 如果当前结果未调整，则 `equation_formula_reported.Z`、`epsilon.rho.Z`、`literature_estimate.adjustment_set`、`hpp_mapping.Z` 必须全部为 `[]`
14. 不允许根据 baseline characteristics、常见协变量模板、或 HPP 候选字段反推 Z
15. 不允许反推 `reported_p` 或 `literature_estimate.p_value`
16. 如果论文有多个模型，只能使用与当前 edge 的 estimate / CI / p_value 对应的那个模型
