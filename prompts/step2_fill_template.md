# Step 2: 为单条 Edge 填写 HPP 统一模板

你是医学信息学研究员。下面给出了：
1. 一个 JSON 模板（所有字段已预定义）
2. 一篇论文的全文
3. 从这篇论文中提取的一条 edge（X → Y 关系）的摘要信息
4. **与本条 edge 相关的 HPP 数据集和字段**（由检索系统提供）

你的任务是**根据论文内容填写模板中的每一个字段**。

---

## 待填写的 Edge 信息

```
Edge #{edge_index}: {X} → {Y}
对照/参照: {C}
亚组: {subgroup}
结局类型: {outcome_type}
效应尺度: {effect_scale}
效应量: {estimate}
CI: {ci}
P值: {p_value}
来源: {source}
```

## 论文基本信息

```
第一作者: {first_author}
年份: {year}
DOI: {doi}
论文类型: {evidence_type}
```

---

## 填写规则（请严格遵守）

### 总体原则
- **仅使用论文中的信息**，不确定的字段填 null
- **禁止编造**任何论文中未提到的数据
- **所有变量名用下划线代替空格**，例如：`dinner_timing` 而非 `dinner timing`

---

### 命名一致性规则（极其重要！）
在整个 JSON 中，同一个变量必须用**完全相同的名称**。规则如下：
- `epsilon.rho.X` = `epsilon.iota.core.name` = `hpp_mapping.X.field` 中对应的概念名，三者必须一致
- `epsilon.rho.Y` = `epsilon.o.name` 中的核心词，必须一致
- 名称格式：全小写 + 下划线，例如 `dinner_timing_condition`、`glucose_auc_0_120min`
- **禁止**在同一 JSON 里对同一变量使用不同名称

---

### edge_id 命名规则
格式：`EV_{year}_{FIRST_AUTHOR_UPPER}#{edge_index}`
示例：`EV_2022_GARAULET#7`

---

### epsilon.Pi（目标人群）
写出纳入排除标准的关键信息，包括：样本来源、年龄范围、性别比例、样本量、关键排除条件。
示例：`"Spanish_adults_N=588,_overweight/obese,_excluding_T2D_and_shift_workers"`

---

### epsilon.iota（暴露/干预变量）
- `core.name`: 暴露变量的规范名称（下划线格式，与 rho.X 完全一致）
- `ext.contrast_type`: 从以下选项中选一个：
  - `arm_vs_control` — RCT 中干预组 vs 对照组
  - `binary` — 二分类暴露
  - `category` — 多类别暴露
  - `per_unit` — 每单位变化
  - `continuous` — 连续暴露
  - `dose` — 剂量-反应
- `ext.x0`: 参照/对照值（字符串）
- `ext.x1`: 暴露/干预值（字符串）
- `ext.unit`: 单位（如 "hours_before_bedtime"，无单位填 null）

**E6 交互边的特殊处理**：
- `iota_1` 填第一个暴露变量（X1）
- `iota_2` 填第二个暴露变量（X2）
- `iota`（主 iota）填交互项本身，`core.name` 用格式 `"X1_x_X2_interaction"`

---

### epsilon.o（结局变量）
- `name`: 结局的规范名称（下划线格式，与 rho.Y 完全一致）
- `type`: `continuous` / `binary` / `survival`

---

### epsilon.tau（时间语义）
- `core.index`: 时间零点（如 `"randomization"`, `"OGTT_start"`, `"baseline_visit"`）
- `core.horizon`: 随访时长（如 `"2_hours"`, `"12_weeks"`, `"baseline_only"`）
- `ext.baseline_window`: 基线评估窗口（如 `"1_week_pre_randomization"` 或 null）
- `ext.follow_up_window`: 随访窗口（如 `"0_to_120_min"` 或 null）

---

### epsilon.mu（效应量度量）
- `core.family`:
  - `difference` — 对应：MD, beta, RD, SD
  - `ratio` — 对应：logOR, logRR, logHR
- `core.type`: `MD` / `beta` / `logOR` / `logRR` / `logHR` / `RD` / `SD`
- `core.scale`: `identity`（原始尺度）/ `log`（对数尺度）

---

### epsilon.rho（变量角色映射）
- `X`: 暴露变量规范名（与 iota.core.name 完全一致）
- `Y`: 结局变量规范名（与 o.name 完全一致）
- `Z`: 协变量/调整变量列表（论文模型中调整的变量，用下划线格式）
- `M`: 中介变量列表（仅 E4 时填，否则为 `[]`）
- `IV`: 工具变量（仅 MR 研究时填，否则为 `null`）
- `X1`, `X2`: 仅 E6 交互边时填，分别为两个交互变量的规范名；否则为 `null`

---

### epsilon.alpha（因果识别策略）
- `id_strategy`: 1-2句话描述统计识别方法
  - RCT 示例：`"Randomized_crossover_design_with_within-subject_comparison"`
  - 观察性示例：`"Multivariable_logistic_regression_adjusted_for_age_sex_BMI"`
- `assumptions`: 关键假设列表
- `status`:
  - `identified` — RCT 或有效因果识别
  - `partially_identified` — 观察性研究有调整但可能有残余混杂
  - `not_identified` — 纯描述性

---

### equation_inference_hints（方程类型推断）
根据论文的**分析方法**判断以下 5 个 bool 值：
- `has_survival_outcome`: 结局是否为 time-to-event（Cox / KM / 生存分析）？
- `has_longitudinal_timepoints`: 是否有重复测量/纵向分析（LMM / GEE）？
- `has_mediator`: 是否进行了中介分析？
- `has_counterfactual_query`: 是否有个体化反事实推断（CATE / ITE）？
- `has_joint_intervention`: 是否分析了**两个**暴露的联合/交互效应？

---

### equation_type（按优先级自动推断）
根据 hints 按以下优先级填写：
1. `has_joint_intervention = true` → `E6`
2. `has_counterfactual_query = true` → `E5`
3. `has_mediator = true` → `E4`
4. `has_survival_outcome = true` → `E2`
5. `has_longitudinal_timepoints = true` → `E3`
6. 全 false → `E1`

---

### literature_estimate（论文报告的效应量）
- `theta_hat`: 点估计值（**数字**，不是字符串）
- `ci`: [下界, 上界]（数字或 null）
- `p_value`: p 值（数字 或 字符串 如 `"<0.0001"` 或 null）
- `n`: 该分析的有效样本量（数字）
- `design`: `RCT` / `cohort` / `cross-sectional` / `MR` / `registry` / `other`
- `grade`: `A`（RCT）/ `B`（有因果识别的观察性）/ `C`（纯观察性/描述性）
- `model`: 统计模型描述，下划线格式，如 `"linear_mixed_model_adjusted_for_sequence_period"`
- `ref`: `"作者姓, 年份, 期刊, DOI:xxx"`
- `adjustment_set`: 模型调整的协变量列表（与 rho.Z 一致）

---

### hpp_mapping（HPP 平台字段映射）

下面是**检索系统为本条 edge 提供的相关 HPP 数据集和字段**。请基于这些信息完成映射。

{hpp_context}

**status 取值规则**：
- `exact`: HPP 字段与论文变量定义、单位、测量方式完全一致
- `close`: 概念一致但测量方式不同（如论文用问卷自报 BMI，HPP 用实测 BMI）
- `derived`: 需从 HPP 字段计算才能得到（notes 写明计算公式）
- `tentative`: 仅概念相近，实际可能无法替代
- `missing`: HPP 中完全没有此类数据

**dataset 和 field 字段规则（严格！）**：
- 当 status = `missing` 时：`dataset` 填 `"N/A"`，`field` 填 `"N/A"`，notes 写明原因
- 当 status = `derived` 时：`dataset` 填来源数据集，`field` 填需计算的基础字段，notes 写计算方法
- **禁止**把 `"missing"` 或 `"..."` 填入 `dataset` 或 `field` 字段
- `dataset` 和 `field` 必须使用上面检索结果中列出的**实际**数据集 ID 和字段名
- 如果检索结果中没有匹配的字段，设 status=`missing`

---

### modeling_directives（建模指令）
- 将 `equation_type` 对应的 `e{N}.enabled` 设为 `true`，其余全部设为 `false`
- 只填写 `enabled=true` 那个 e{N} 的参数，其余 e{N} 的子字段保持模板默认值
  - **E1**: `model_preference`（如 `["OLS", "Logistic"]`）, `target_parameter`（如 `"beta_X"`）
  - **E2**: `model_preference`, `event_definition`, `censor_definition`
  - **E3**: `model_preference`, `time_variable`, `subject_id_variable`
  - **E4**: `target_effects`, `primary_target`, `mediator_model`, `outcome_model`
  - **E5**: `estimand`, `model.type`, `model.base_model`
  - **E6**: `interaction.enabled=true`, `interaction.term`（如 `"dinner_timing_x_mtnr1b_genotype"`）, `primary_target`（`"beta_12"`）, `joint_contrast.x0/x1`

---

### analysis_plan
- `subgroup`: 论文中报告的亚组分析列表（下划线格式）
- `sensitivity.run`: 论文是否做了敏感性分析（true/false）
- `multiomics`: 涉及组学数据时列出类型，否则为 `[]`

---

### pi（论文级元信息）
- `ref`: `"作者姓 Year 期刊名 DOI:xxx"`
- `design`: 研究设计描述（如 `"randomized_crossover_trial"`）
- `grade`: 同 literature_estimate.grade
- `n_literature`: 论文总样本量（数字）
- `source`: 固定为 `"pdf_extraction"`

---

### provenance（来源信息）
- `pdf_name`: PDF 文件名（不含路径和扩展名）
- `page`: 数字或列表
- `table_or_figure`: 如 `"Table_2"`, `"Figure_1"`, `"Results_text"`
- `extractor`: 固定为 `"llm"`

---

## JSON 模板

请将下面的模板中所有 `"..."` 和占位符替换为论文中的实际值。不确定的填 null。

```json
{template_json}
```

---

## 输出要求

输出**一个完整的 JSON 对象**（不是数组）。必须满足：
1. 所有 key 必须保留，不能增减
2. 所有变量名使用下划线格式，无空格
3. 同一变量在 `rho.X`、`iota.core.name`、`hpp_mapping.X.field` 中名称完全一致
4. `hpp_mapping` 中 missing 的字段：`dataset="N/A"`, `field="N/A"`，不填 `"missing"`
5. `theta_hat` 必须是数字（不是字符串），如果无法确定则填 null
