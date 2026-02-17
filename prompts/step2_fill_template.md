# Step 2: 为单条 Edge 填写 HPP 统一模板

你是医学信息学研究员。下面给出了：
1. 一个 JSON 模板（所有字段已预定义）
2. 一篇论文的全文
3. 从这篇论文中提取的一条 edge（X → Y 关系）的摘要信息

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
- 下划线代替空格（如 "time_restricted_eating" 不是 "time restricted eating"）

### edge_id 命名规则
格式：`EV_{year}_{FIRST_AUTHOR_UPPER}#{edge_index}`
示例：`EV_2022_MANOOGIAN#3`, `EV_2025_WRIGHT#1`

### epsilon.Pi（目标人群）
- 写出完整的纳入排除标准的关键信息
- 包括：样本来源、年龄范围、性别比例、样本量、关键排除条件
- 示例："UK_Biobank_participants_aged_40-69,_N=492,114,_excluding_prevalent_diabetes_and_sleep_apnoea"

### epsilon.iota（暴露/干预变量的形式化描述）
- `core.name`: 暴露变量的完整名称
- `ext.contrast_type`: 从以下选项中选择一个：
  - `arm_vs_control` — RCT 中干预组 vs 对照组
  - `binary` — 二分类暴露（如 BMI≥30 vs <30）
  - `category` — 多类别暴露（如睡眠时长 ≤5h/6h/7h/8h/9h/≥10h）
  - `per_unit` — 每单位变化（如每增加 1 SD）
  - `continuous` — 连续暴露
  - `dose` — 剂量-反应
- `ext.x0`: 参照/对照值
- `ext.x1`: 暴露/干预值
- `ext.unit`: 单位

### epsilon.o（结局变量）
- `name`: 结局的完整描述
- `type`: continuous / binary / survival

### epsilon.tau（时间语义）
- `core.index`: 时间零点（如随机化时间、基线评估时间）
- `core.horizon`: 随访时长
- `ext.baseline_window`: 基线窗口
- `ext.follow_up_window`: 随访窗口

### epsilon.mu（效应量度量）
- `core.family`: difference（差值类）或 ratio（比值类）
  - difference 对应：MD, beta, RD, SD
  - ratio 对应：logOR, logRR, logHR
- `core.type`: MD / beta / logOR / logRR / logHR / RD / SD
- `core.scale`: identity（原始尺度）/ log（对数尺度）

### epsilon.rho（变量角色映射）
- `X`: 暴露变量名
- `Y`: 结局变量名
- `Z`: 协变量/调整变量列表（论文中模型调整了哪些变量）
- `M`: 中介变量列表（仅中介分析时使用，否则为空列表）
- `IV`: 工具变量（仅 MR 研究时使用，否则为 null）
- `X1`, `X2`: 联合干预变量（仅交互作用分析时使用，否则为 null）

### epsilon.alpha（因果识别策略）
- `id_strategy`: 用1-2句话描述论文使用的因果/统计识别方法
- `assumptions`: 列出关键假设
- `status`: 
  - `identified` — RCT 或有效的因果识别设计
  - `partially_identified` — 观察性研究有调整但可能有残余混杂
  - `not_identified` — 纯描述性，无因果推断

### equation_inference_hints（方程类型推断标记）
根据论文的**分析方法**判断以下 5 个 bool 值：
- `has_survival_outcome`: 结局是否为 time-to-event（用了 Cox / KM / 生存分析）？
- `has_longitudinal_timepoints`: 是否有重复测量/纵向数据分析（LMM / GEE）？
- `has_mediator`: 是否进行了中介分析？
- `has_counterfactual_query`: 是否有个体化反事实推断（CATE / ITE）？
- `has_joint_intervention`: 是否分析了两个暴露的联合/交互效应？

### equation_type（自动推断，按优先级）
根据 equation_inference_hints 的 5 个 bool 值，按以下优先级判断：
1. `has_joint_intervention` = true → `E6`
2. `has_counterfactual_query` = true → `E5`
3. `has_mediator` = true → `E4`
4. `has_survival_outcome` = true → `E2`
5. `has_longitudinal_timepoints` = true → `E3`
6. 以上全部 false → `E1`

### literature_estimate（论文报告的效应量）
- `theta_hat`: 点估计值（数字）
- `ci`: [下界, 上界]
- `p_value`: p 值
- `n`: 该分析的有效样本量
- `design`: RCT / cohort / cross-sectional / MR / registry / other
- `grade`: A（RCT）/ B（有因果识别的观察性）/ C（纯观察性/描述性）
- `model`: 用 1-2 句话描述统计模型（如 "Cox_PH_adjusted_for_age,_sex,_BMI"）
- `ref`: 完整引用（作者, 年份, 期刊, DOI）
- `adjustment_set`: 模型调整了哪些协变量

### modeling_directives（建模指令）
- 根据 equation_type，将对应的 e{N}.enabled 设为 true，其余设为 false
- 填写 enabled=true 的那个 e{N} 的具体参数：
  - e1: `model_preference`（如 ["OLS", "Logistic"]）, `target_parameter`（如 "beta_X"）
  - e2: `model_preference`（如 ["CoxPH"]）, `event_definition`, `censor_definition`
  - e3: `model_preference`（如 ["LMM", "GEE"]）, `time_variable`, `subject_id_variable`
  - e4: `target_effects`（如 ["TE", "NDE", "NIE"]）, `primary_target`, `mediator_model`, `outcome_model`
  - e5: `estimand`, `model.type`, `model.base_model`
  - e6: `interaction.enabled`, `interaction.term`, `primary_target`

### hpp_mapping（HPP 平台字段映射）

HPP 已知数据集及字段：

| 数据集 | 已知字段 | 备注 |
|--------|----------|------|
| 000-population | age, sex, ethnicity | 人口统计 |
| 002-anthropometrics | height, weight, bmi, waist_circumference | 人体测量 |
| 003-blood_pressure | systolic_bp, diastolic_bp | 血压 |
| 004-body_composition | body_fat_pct, lean_mass | 体成分 |
| 005-diet_logging | local_timestamp, calories, meal_type | 饮食 |
| 009-sleep | sleep_duration, bedtime, wake_time, total_sleep_time | 睡眠 |
| 014-human_genetics | gencove_vcf, variants_qc_parquet | 基因组 |
| 016-blood_tests | glucose, hba1c, hdl, ldl, triglycerides, crp | 血液 |
| 017-cgm | cgm_mean, cgm_auc, cgm_cv, cgm_mage | CGM |
| 020-health_and_medical_history | diagnosis, medication | 病史 |
| 021-medical_conditions | icd11_code, condition_name | 诊断 |
| 023-lifestyle_and_environment | physical_activity, smoking | 生活方式 |

**status 规则**：
- `exact`: HPP 字段与论文变量的定义、单位、测量方式完全一致
- `close`: 概念一致但测量方式不同（如论文用 OGTT 血糖, HPP 用 CGM）
- `derived`: 需要从 HPP 字段计算才能得到（notes 中写明计算公式）
- `tentative`: 仅概念相近，实际可能不可替代
- `missing`: HPP 中完全没有此类数据

**注意**：
- **严禁编造 HPP 中不存在的字段名**
- HPP 有 CGM 但**没有** OGTT；有空腹血糖但**没有**胰岛素
- 如果不确定某字段是否存在，标 `missing`

### analysis_plan
- `subgroup`: 论文中提到的亚组分析列表
- `sensitivity.run`: 论文是否做了敏感性分析
- `multiomics`: 如涉及组学数据，列出类型

### pi（论文级元信息）
- `ref`: 完整引用
- `design`: 研究设计描述
- `grade`: A/B/C
- `n_literature`: 论文样本量
- `source`: 固定为 "pdf_extraction"

### provenance（这条 edge 的来源）
- `pdf_name`: PDF 文件名
- `page`: 页码（数字或列表）
- `table_or_figure`: 来自哪个 Table/Figure
- `extractor`: 固定为 "llm"

---

## JSON 模板

请将下面的模板中所有 `"..."` 和占位符替换为论文中的实际值。不确定的填 null。

```json
{template_json}
```

---

## 输出

请输出**一个完整的 JSON 对象**（不是数组），严格遵循上述模板的结构。所有 key 必须保留，不要增减 key。
