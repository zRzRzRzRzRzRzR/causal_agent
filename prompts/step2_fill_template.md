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
- `equation_formula_reported`: 填充所有子字段（equation、source、model_type、link_function、effect_measure、reported_effect_value、reported_ci、reported_p、X、Y、Z）

### 3. epsilon字段（来自论文）
- `epsilon.Pi`: 人群标签 — `"adult_general"`（成人一般人群）、`"cvd"`（心血管疾病）、`"diabetes"`（糖尿病）、`"oncology"`（肿瘤）、`"pediatric"`（儿科）
- `epsilon.iota.core.name`: 暴露变量的**简洁名称**（例如 `"Healthy Lifestyle Score"` 而非 `"Healthy Lifestyle Score 4 (Never smoking, Physically active, ...)"）`。必须匹配rho.X
- `epsilon.o.name`: 结局变量名称（必须匹配rho.Y）
- `epsilon.o.type`: `"binary"`（二分类）、`"continuous"`（连续）或`"survival"`（生存）
- `epsilon.tau`: 时间坐标（index、horizon，ISO 8601格式如"P5Y"、"P10Y"）
- `epsilon.mu.core.type`: 对于比率度量（HR/OR/RR），**必须使用log前缀**：`"logHR"`, `"logOR"`, `"logRR"`（因为theta_hat在对数尺度上）。对于差异度量：`"MD"`, `"BETA"`, `"SMD"`
- `epsilon.alpha.assumptions`: 选择适用的：`"exchangeability"`（可交换性）、`"positivity"`（正性）、`"consistency"`（一致性）。**不要**包含 `"proportional_hazards"`
- `epsilon.alpha.status`: `"identified"`（已识别，RCT）/ `"partially_identified"`（部分识别，观察性）/ `"not_identified"`（未识别）
- `epsilon.rho`: 变量角色 — X（使用简洁名称，与iota.core.name一致）、Y、Z（调整变量列表）、IV（仅MR，否则为null）

### 4. literature_estimate（剩余字段）
- `n`: 总样本量
- `design`: `"RCT"` / `"cohort"`（队列）/ `"cross-sectional"`（横断面）/ `"case-control"`（病例对照）/ `"meta-analysis"`（荟萃分析）/ `"MR"`
- `grade`: `"A"`（高质量RCT）、`"B"`（中等）、`"C"`（低质量/未调整）
- `adjustment_set`: 调整变量列表（必须匹配rho.Z）
- `p_value`: 如论文中所报告
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
| C3 | Z一致性  | `epsilon.rho.Z` == `literature_estimate.adjustment_set` |
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
