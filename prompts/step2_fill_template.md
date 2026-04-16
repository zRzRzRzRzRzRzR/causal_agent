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

## 预计算字段（自动处理）

以下字段已**预计算**，管道会自动填入，你不需要手动推导：

**这些值在此提示词之后注入 —— 查看下面的"预验证方程元数据"部分。**

自动处理的字段：
- `literature_estimate.theta_hat` — 已转换为正确的尺度（比率用对数）
- `literature_estimate.ci` — 已转换为正确的尺度
- `epsilon.alpha.id_strategy` — 已从evidence_type推导

## 你需要自行判断的字段

以下字段**由你根据论文内容决定**，下面的"预验证方程元数据"部分提供了参考建议，但你必须根据论文的实际统计方法和公式结构来确认：

- `equation_type` — 根据论文的统计方法从 E1-E6 中选择
- `literature_estimate.model` — 根据论文的实际统计模型填写
- `epsilon.mu.core`（family、type、scale）— 根据效应指标类型填写

**⚠️ E3 识别规则**：如果论文包含下列任一特征，`equation_type` 应为 **E3**（而非 E1）：
- 报告 baseline + follow-up 数据，使用 **time × group interaction** 检验干预效应
- 使用 LMM / GEE / 混合效应模型分析纵向/重复测量数据
- 报告 "change from baseline" / "Δ Y" 作为主要结果，并用 ANCOVA 调整基线

对应的 `model_type` / `literature_estimate.model` 应填 `"LMM"` / `"GEE"` / `"ANCOVA"`，**不要**填 `"linear"`。`equation_formula` 必须包含 `time × group` 交互项（例如 `β_XT·I(TRE_i)·t`）。

**⚠️ E1 vs E3 判断**：
- E1（单一时点回归/对比）：只在单一时点比较，或仅报告最终值
- E3（纵向/重复测量）：显式建模时间或使用 change score + 基线协变量

RCT 通常是 **E3**（而非 E1），除非只报告 endpoint 单时点比较。

---

## 你需要填充的字段（将精力集中在这里）

### 1. 论文元数据
- `paper_title`: 完整的论文标题，空格替换为下划线
- `paper_abstract`: 简要摘要（1-3句话：设计、样本量、主要发现），空格替换为下划线

### 2. equation_formula（E1-6 框架方程）

`equation_formula` 是一个**对象**，只包含 `formula` 一个子字段：

```json
{
  "formula": "λ(t|do(X=x),Z) = λ₀(t)·exp(β_X·T_X(x) + γ₁·Age + γ₂·Sex)"
}
```

- `formula`: 根据论文实际统计方法选择对应的 equation_type 框架编写数学公式。**必须使用数学符号**（β, γ, λ 等），严禁纯文字描述。
  如果当前效应值来自未调整比较/直接比较/列联表/Fisher/t-test/Model 1=no adjustments，则不要为了套公式强行加入 `+ gamma^T * Z`。

**⚠️ 暴露变量编码要求（重要！）**：
- 公式中 X 必须使用论文的**实际编码形式**，不要使用泛化的 `T_X(x)` 或直接写 `X`
- 如果 X 是**分类变量**（如 lifestyle score = 4 vs 0），使用 indicator 形式：`β_X·I(LifestyleScore=4)`
- 如果 X 是**二分组**（如 TRE 干预组 vs 对照组），使用 indicator 形式：`β_X·I(TRE_i)`
- 如果 X 是**连续变量**（如 sleep duration in hours），直接使用变量名：`β_X·SleepDuration`
- 如果是 E3 重复测量模型，必须包含**时间×组交互项**：`β_XT·I(TRE_i)·t`

**❌ 错误**: `λ(t|do(X=x),Z) = λ₀(t)·exp(β_X·T_X(x) + γᵀ·Z)` — `T_X(x)` 过于泛化
**✅ 正确**: `λ(t|do(X=x),Z) = λ₀(t)·exp(β_X·I(LifestyleScore=4) + γ₁·Age + γ₂·Sex + γ₃·TDI)` — 具体编码

### 3. equation_formula_reported（论文报告方程 — 独立于 E1-6 框架）

此字段用于与 equation_formula 做**双重校验**。包含以下子字段：

- `equation`: 数学公式字符串（必须使用数学符号，严禁纯文字描述）。**与 equation_formula 相同的编码要求**：X 必须使用论文实际编码形式（indicator function / 连续变量名），协变量必须逐个列出而非用 `γᵀ·Z` 泛化。
- `source`: `"extracted"`（论文有明确方程）或 `"reconstructed"`（论文无方程，根据方法描述重构）
- `model_type`: `"Cox"` / `"logistic"` / `"linear"` / `"Poisson"` 等
- `link_function`: `"log"` / `"logit"` / `"identity"` 等
- `effect_measure`: `"HR"` / `"OR"` / `"RR"` / `"MD"` / `"BETA"` 等
- `reported_effect_value`: 论文原始尺度效应值（ratio 类填原值如 0.84，difference 类填原值如 -3.2）
- `reported_ci`: 论文原始尺度 CI `[下界, 上界]`
- `reported_p`: 论文报告的 p 值字符串
- `X`, `Y`, `Z`: 通路节点，必须与 `epsilon.rho` 一致

- **Z 硬规则**：如果当前 edge 对应的是未调整结果，或论文只做了 direct comparison / contingency tables / difference in proportions / exact test，那么以下四处必须同时为 `[]`：
  `equation_formula_reported.Z`、`epsilon.rho.Z`、`literature_estimate.adjustment_set`、`hpp_mapping.Z`
- 如果论文同时报告多个模型（如 Model 1 / Model 2 / Model 3），**只能**填写与你当前 edge 的 estimate / CI / p_value 对应的那个模型的调整变量；不能混用别的模型的协变量。

**⚠️ Z 决策流程（必须按此顺序判断）**：
1. 找到当前 edge 的 estimate 在论文中的**精确出处**（哪个 Table 的哪一行，或哪个模型）
2. 找到该 Table/模型的脚注或 Methods 中描述的**该模型的调整变量**
3. 如果脚注写 "adjusted for age, sex, BMI" → Z = ["age", "sex", "BMI"]
4. 如果脚注写 "unadjusted" 或无脚注说明调整 → Z = []
5. 如果模型是 t-test / ANOVA / Fisher's exact / chi-square → Z = **必须**为 []
6. **禁止**从 baseline table（Table 1）的列头反推 Z
7. **禁止**因为 HPP 字典中有 age/sex/BMI 字段就填入 Z

**❌ 错误示例（LLM 常犯）**：
```
公式: "Y_it = α + β₀·t + β_X·Treatment + ε_it"  ← 无协变量项
Z: ["Age", "Sex", "TDI"]  ← 错！公式中没有 γ^T·Z 项，Z 必须为 []
```
正确做法：如果你的 equation 中没有 `+ γ^T·Z` 或 `+ γ₁·Age + γ₂·Sex` 等协变量项，那么 Z 就必须是 `[]`。**公式和 Z 必须一致。**

### 4. epsilon字段（来自论文）
- `epsilon.Pi`: 人群标签 — `"adult_general"`（成人一般人群）、`"cvd"`（心血管疾病）、`"diabetes"`（糖尿病）、`"oncology"`（肿瘤）、`"pediatric"`（儿科）
- `epsilon.iota.core.name`: 暴露变量的**简洁名称**（例如 `"Healthy Lifestyle Score"` 而非 `"Healthy Lifestyle Score 4 (Never smoking, Physically active, ...)"）`。必须匹配rho.X。
  - **注意**：简洁名称指"变量本体"；对照/层次信息不应出现在 iota.core.name（那是 rho 和 equation_formula_reported.X 的职责）。例如 X 原始是 `"Healthy Lifestyle Score 4 vs 0"`，iota.core.name 填 `"Healthy Lifestyle Score"`。
- `epsilon.o.name`: 结局变量名称（必须匹配rho.Y）
- `epsilon.o.type`: `"binary"`（二分类）、`"continuous"`（连续）或`"survival"`（生存）
- `epsilon.tau`: 时间坐标（index、horizon，ISO 8601格式如"P5Y"、"P10Y"）
- `epsilon.mu.core.type`: 对于比率度量（HR/OR/RR），**必须使用log前缀**：`"logHR"`, `"logOR"`, `"logRR"`（因为theta_hat在对数尺度上）。对于差异度量：`"MD"`, `"BETA"`, `"SMD"`
- `epsilon.alpha.assumptions`: 选择适用的：`"exchangeability"`（可交换性）、`"positivity"`（正性）、`"consistency"`（一致性）。**不要**包含 `"proportional_hazards"`
- `epsilon.alpha.status`: `"identified"`（已识别，RCT）/ `"partially_identified"`（部分识别，观察性）/ `"not_identified"`（未识别）
- `epsilon.rho`: 变量角色 — X（使用简洁名称，与iota.core.name一致）、Y、Z（调整变量列表）。**默认填 `[]`**；只有论文对当前效应值明确报告了调整变量时才填写。不要把 baseline table 中的 Age/Sex/BMI/TDI 当成调整变量。IV（仅MR，否则为null）

### 5. literature_estimate（剩余字段）
- `n`: 与当前效应值对应的样本量（若 edge 是亚组效应则为**亚组样本量**，不是全队列；若是 completer 分析则为 completer 数）。定位方法：找到 estimate 数值**旁边**的 n/events 计数，不是 Methods 或流程图的总 n。
- `design`: `"RCT"` / `"cohort"`（队列）/ `"cross-sectional"`（横断面）/ `"case-control"`（病例对照）/ `"meta-analysis"`（荟萃分析）/ `"MR"`
- `grade`: `"A"`（高质量RCT）、`"B"`（中等）、`"C"`（低质量/未调整）
- `adjustment_set`: 调整变量列表（必须匹配rho.Z）。**默认填 `[]`**；只有论文对当前效应值明确报告了调整变量时才填写。
- `p_value`: 仅在论文明确报告时填写；**不要**根据 CI、显著性、Bonferroni 校正或常识反推。
- `ci_level`: 通常为0.95
- `equation_type`: 必须与顶层 `equation_type` 相同（用于双重校验）
- `equation_formula`: 必须与顶层 `equation_formula.formula` 相同或高度相似（用于双重校验）

⚠️ **literature_estimate 只包含以下字段**：`theta_hat`, `ci`, `ci_level`, `p_value`, `n`, `design`, `grade`, `model`, `adjustment_set`, `equation_type`, `equation_formula`。**禁止**添加 `subgroup`, `control_reference`, `reported_HR`, `reported_CI_HR`, `group_means`, `notes`, `reason` 等额外字段。

### 6. hpp_mapping（关键 — 这决定了管道能否运行）

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
| C7 | CI→效应值 | 如果 `reported_ci` ≠ [null,null]，则 `reported_effect_value` ≠ null |
| C8 | p值格式  | `reported_p` 和 `p_value` 是 float 数字（不是字符串） |
| C9 | 公式-Z一致 | 如果 equation 中无 γ/gamma/covariate 项，则 Z 必须为 [] |

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

## ⛔ 硬匹配规则（Hard-Match Rules）

> 这些规则优先于所有其他填充指令。违反任何一条都会导致该值被管道自动清零。

### R1: 所有数值必须可追溯到论文原文
- `reported_effect_value`、`reported_ci`、`theta_hat`（差异尺度）、`literature_estimate.ci`（差异尺度）中的每个数值，**必须**能在论文的 Table、Figure 或正文中找到原始出处。
- 如果论文**只报告了 group means**（如 403.2 vs 45.9 nmol/L）而**没有报告 mean difference / beta / regression coefficient**，则：
  - `reported_effect_value` = **null**
  - `theta_hat` = **null**
  - `ci` = [null, null]
  - **不要**自己计算差值。管道会在后处理中计算。

### R2: 百分比变化 ≠ 绝对差值
- 如果论文报告 "decreased by 38.3% (95% CI 27.0-49.6)"，这是一个**百分比差异**，不是 mean difference。
- 对于百分比差异，`effect_measure` 应为 `"percent_change"` 或标注 `source: "percent_difference"`
- **不要**将百分比差异值直接填入 `theta_hat` 当作 mean difference。

### R3: 禁止跨边复制数值
- 每个 edge 的 `theta_hat`、`reported_effect_value`、`ci` 必须独立从论文中对应的 Table/正文行提取。
- 如果你发现自己给多个不同 Y 变量填入了相同的 theta_hat，这几乎一定是错误。

### R4: fold-change 不是 beta coefficient
- 如果论文报告 "13.5-fold increase" 或 "9.5-fold vs baseline"，这是 fold-change，不是 linear regression beta。
- 对于 fold-change 结果：`reported_effect_value` = fold-change 值，`effect_measure` = `"fold_change"`
- **不要**将 fold-change 转换为 log 值填入 theta_hat，除非你能验证转换后的值。

### R5: is_reported 只能用于论文直报值
- `study_cohort.age.is_reported = true` 意味着论文**原文中有这个确切数值**
- 如果论文分别报告了 "维生素组 45.8 (7.0)，安慰剂组 46.2 (8.1)"，**不要**计算一个合并平均值 "46.0 (7.6)" 然后标记为 `is_reported = true`
- 正确做法：直接引用论文的原始表述，如 "45.8 (SD 7.0) in vitamin group, 46.2 (SD 8.1) in placebo group"

### R6: disease_indication 是参与者的实际状态
- 如果参与者是**健康人**（即使他们有疾病家族史），`disease_indication` 应反映"healthy"
- "healthy siblings of patients with premature atherothrombotic disease" 的 disease_indication 是 **"healthy (family history of premature atherothrombotic disease)"**，不是 "subclinical atherosclerosis"

### R7: reported_ci 存在 → reported_effect_value 必须存在
- 如果你填写了 `reported_ci`（不是 [null, null]），则 `reported_effect_value` **必须也有值**。
- 逻辑：你不可能有置信区间但没有点估计。
- 如果论文只报告了 CI 但你找不到对应的效应值，两个都填 null：
  ```json
  "reported_effect_value": null,
  "reported_ci": [null, null]
  ```
- **❌ 以下输出会被管道自动清零**：
  ```json
  "reported_effect_value": null,
  "reported_ci": [0.72, 0.95]   ← 有CI但无效应值，管道会把CI也清空
  ```

### R8: reported_p / p_value 必须是数字，不是字符串
- `reported_p` 和 `literature_estimate.p_value` 必须输出为 **float 数字**，不是字符串。
- 如果论文写 "p < 0.001"，填 `0.001`（float），不是 `"< 0.001"`（string）。
- 如果论文写 "p = 0.032"，填 `0.032`（float），不是 `"0.032"`（string）。
- 如果论文写 "p < 0.05" 但未给出精确值，填 `0.05`。
- 如果论文写 "NS" 或 "not significant" 但未给出数值，填 `null`。
- **❌ 错误**：`"reported_p": "< 0.001"` ← 字符串，会被管道转换
- **✅ 正确**：`"reported_p": 0.001` ← 数字

---

## 输出要求

输出**一个完整的JSON对象**。它必须：

1. 匹配模板结构 — 所有顶级键都存在，**不多不少**
2. 对未知字段使用`null`（不是`"..."`或空字符串）
3. `theta_hat`必须是**数字**或`null`（不是字符串）
4. 通过上述一致性检查清单（C1–C6）
5. 数据集ID使用**连字符**格式（例如，`009-sleep`、`055-lifestyle_and_environment`）
6. **hpp_mapping中每个变量映射只允许4个字段**：`name`, `dataset`, `field`, `status`。**禁止**添加 `mapping_notes`、`composite_components`、`subgroup_mapping` 等。
7. **hpp_mapping必须包含** `M` 和 `X2` 键（值为 `null` 除非 E4/E6）
8. **literature_estimate只允许**：`theta_hat`, `ci`, `ci_level`, `p_value`, `n`, `design`, `grade`, `model`, `adjustment_set`, `equation_type`, `equation_formula`。**禁止** `reported_HR`, `reported_CI_HR`, `subgroup`, `control_reference`, `notes`, `group_means`, `reason` 等。
9. **不要**在输出中包含`//`注释、`_comment`键、`_validation`键
10. `epsilon.mu.core.type` 对于比率度量必须使用log前缀（`logHR`/`logOR`/`logRR`）
11. `epsilon.iota.core.name` 和 `epsilon.rho.X` 使用简洁变量名
12. 恰好描述**一个**边
13. 如果当前结果未调整，则 `equation_formula_reported.Z`、`epsilon.rho.Z`、`literature_estimate.adjustment_set`、`hpp_mapping.Z` 必须全部为 `[]`
14. 不允许根据 baseline characteristics、常见协变量模板、或 HPP 候选字段反推 Z
15. 不允许反推 `reported_p` 或 `literature_estimate.p_value`
16. 如果论文有多个模型，只能使用与当前 edge 的 estimate / CI / p_value 对应的那个模型
17. `equation_formula` 必须是对象 `{formula}`，不是字符串
18. **equation_formula_reported 只允许**：`equation`, `source`, `model_type`, `link_function`, `effect_measure`, `reported_effect_value`, `reported_ci`, `reported_p`, `X`, `Y`, `Z`。**禁止**添加 `parameters`, `reason` 等额外字段。
19. `study_cohort` 的每个子字段必须包含 `value`（字符串）和 `is_reported`（布尔值）
20. **严格按模板输出，不要添加模板中不存在的字段**
21. **如果 `reported_ci` 非空，`reported_effect_value` 必须也非空**（R7）
22. **`reported_p` 和 `p_value` 必须是 float 数字**，不是字符串。`"< 0.001"` → `0.001`，`"0.032"` → `0.032`（R8）
23. **公式和 Z 必须一致**：如果 `equation_formula_reported.equation` 中没有协变量调整项（γ/gamma/Z），则所有 Z 字段必须为 `[]`
