# 医学文献证据卡提取 LLM 提示词系统

## 概述

本系统用于从医学PDF文献中提取结构化证据卡，支持4种类型：
- **interventional** (介入性/RCT)
- **causal** (因果推断)
- **mechanistic** (机制/中介分析)
- **associational** (关联性研究)

---

## 第0步：文献分类判别

### 输入
- PDF文献

### 提示词

```
你是医学信息学研究员。请阅读提供的PDF论文，判断其研究类型并给出分类依据。

**分类规则（按优先级）：**

1) **interventional**（干预/RCT/临床试验）
   触发信号：
   - PubMed Publication Type含 "Randomized Controlled Trial"/"Clinical Trial"
   - 方法/摘要含：randomized, double-blind, placebo, allocation, trial, NCT注册号
   - 有明确的干预组vs对照组设计

2) **causal**（观察性因果推断）
   触发信号：
   - mendelian randomization/MR, instrumental variable/2SLS
   - target-trial emulation, front-door/back-door
   - difference-in-differences, regression discontinuity
   - propensity score/IPTW/g-formula/TMLE, negative control

3) **mechanistic**（机制/中介/通路）
   触发信号：
   - mediation analysis, indirect effect, ACME/ADE
   - 阐释生物机制（炎症/自主神经/内分泌/血管功能等）
   - 图中标明 X→M→Y 的路径分析

4) **associational**（相关/描述/一般观察）
   触发信号：
   - 队列/横断面仅报告调整后 OR/HR/β
   - 无干预或因果识别方法
   - 仅相关性描述

**冲突消解规则：**
- 同时具备"干预"和"因果（观察性）"信号 → **causal**
- 同时具备"因果"和"机制（中介）"信号 → **mechanistic**（secondary_tags加"mediation"）
- 仅有机制/中介而无因果识别 → **mechanistic**
- 其余 → **associational**

**输出格式（仅JSON）：**
```json
{
  "primary_category": "mechanistic|interventional|causal|associational",
  "secondary_tags": [],
  "category_signals": [
    "触发信号1：具体证据",
    "触发信号2：具体证据"
  ],
  "confidence": "high|medium|low",
  "rationale": "简要说明分类理由"
}
```
```

---

## 类型一：Mechanistic（机制/通路）证据卡

### Step 1: 通路路径提取

```
你是一名医学信息学研究员和严谨的信息抽取器。现在请你从提供的本地PDF论文中读取并提取该论文中所有被研究或提出的机制通路。

**任务：**
识别论文中涉及的所有因果机制链路（X→M→…→Y），其中：
- X 是论文关注的锚点暴露
- Y 是主要结局（疾病或生理指标）
- M 是中介变量（可能有多个，表示X影响Y的中间机制）

**提取内容：**
- 只提取论文正文和附录中明确提到的所有 X→M→…→Y 通路序列
- 保证顺序和符号与论文描述一致
- 包括直接的 X→Y 关系，或多级链条如 X→M1→M2→Y
- 如果论文讨论了多个独立通路，请逐一提取每条通路
- 不要遗漏附录或图表中提及的机制路径

**信息来源：**
只能参考提供的PDF内容，禁止使用任何外部知识或编造内容。

**输出格式（仅JSON数组）：**
```json
[
  "BMI → KDM-BY Acceleration → Cardiovascular Disease",
  "Waist Circumference → KDM-BY Acceleration → Cardiovascular Disease",
  "TyG Index → KDM-BY Acceleration → Stroke"
]
```
```

### Step 2: 以通路为单位构建证据卡

```
你是医学信息学研究员 + 严谨的信息抽取器，专注于从PDF文献中提取机制/中介分析研究的结构化证据。

**核心原则：**
- 仅使用PDF内容：正文 + 补充材料，**禁止**联网或使用任何外部信息
- 不确定即 null：对未明确的信息**禁止编造或推测**
- 完整性优先：提取**所有**符合条件的变量和效应值
- 可复现性：所有数值必须可追溯到原文**具体位置**

**输入：**
- PDF文献
- 目标机制通路（由Step 1选定）："[具体通路，如 Obesity → Biological Age Acceleration → CVD]"

**必提取模块：**

1) **paper（文献信息）**
   - title: 论文完整标题
   - journal: 期刊名称
   - year: 发表年份（整数）
   - pmid: PubMed ID（PDF中明确给出，否则 null）
   - doi: DOI号（PDF中明确给出，否则 null）
   - registry: 临床试验注册号（如NCTxxxxxxx，否则 null）
   - abstract: 基于论文摘要的英文总结（≤150词）

2) **provenance（证据溯源）**
   - figure_table: 如 ["Table 2 p.6", "Fig 3 p.5"]
   - pages: 整数数组
   - supplement: 是否使用补充材料（boolean）

3) **design（研究设计）**
   - type: "prospective cohort" | "retrospective cohort" | "cross-sectional" | "case-control" | ...
   - analysis: "causal mediation analysis" | "SEM" | "path analysis" | ...
   - n_total: 总样本量（整数）

4) **population（人群特征）**
   - eligibility_signature:
     - age: 年龄范围/中位数
     - sex: "both" | "male" | "female"
     - disease: 疾病状态
     - key_inclusions: 纳入标准数组
     - key_exclusions: 排除标准数组

5) **variables（变量定义）**
   - nodes: 数组，每个变量包含：
     - node_id: "local:变量简称"
     - label: 完整变量名
     - type: "state" | "event" | "intervention"
     - unit: 单位（如 "kg/m²", "years"）
     - system_tags: 系统标签数组

6) **roles（角色分配）**
   - X: 暴露变量node_id数组
   - M: 中介变量node_id数组
   - Y: 结局变量node_id数组
   - Z: 协变量node_id数组

7) **mediation_equations（中介方程）**
   针对每条X→M→Y通路，提取：
   - path: "X_label → M_label → Y_label"
   - total_effect: { "estimate": 数值, "ci_lower": 数值, "ci_upper": 数值, "p": 数值, "scale": "OR|HR|RR|β" }
   - direct_effect: 同上（NDE）
   - indirect_effect: 同上（NIE/ACME）
   - proportion_mediated: { "estimate": 百分比, "ci_lower": 数值, "ci_upper": 数值 }

8) **identification（识别假设）**
   - assumptions: 中介分析假设数组（如sequential ignorability）

**输出格式（仅JSON）：**
见下方完整模板...
```

### Step 2 完整JSON模板

```json
{
  "schema_version": "1",
  "evidence_id": "EV-[年份]-[暴露简称]-[中介简称]-[结局简称]",
  "paper": {
    "title": "",
    "journal": "",
    "year": 0,
    "pmid": null,
    "doi": "",
    "registry": null,
    "abstract": ""
  },
  "provenance": {
    "figure_table": [],
    "pages": [],
    "supplement": false
  },
  "design": {
    "type": "",
    "analysis": "",
    "estimand": null,
    "n_total": 0,
    "n_arms": null,
    "randomization": null,
    "blinding": null,
    "itt": null
  },
  "population": {
    "eligibility_signature": {
      "age": "",
      "sex": "",
      "disease": "",
      "key_inclusions": [],
      "key_exclusions": []
    }
  },
  "transport_signature": {
    "center": "",
    "era": "",
    "geo": "",
    "care_setting": "",
    "data_source": ""
  },
  "time_semantics": {
    "exposure_window": "",
    "baseline_window": "",
    "assessment_timepoints": [],
    "follow_up_duration": "",
    "effect_lag": "",
    "effect_duration": "",
    "temporal_level": "coarse|fine"
  },
  "variables": {
    "nodes": [
      {
        "node_id": "local:VAR_NAME",
        "label": "Full Variable Name",
        "type": "state|event|intervention",
        "unit": "unit_string",
        "unit_ucum": "ucum_code",
        "system_tags": ["TAG1", "TAG2"],
        "ontology": {
          "UMLS": "CUI_or_null"
        }
      }
    ]
  },
  "roles": {
    "X": ["local:EXPOSURE"],
    "M": ["local:MEDIATOR"],
    "Y": ["local:OUTCOME"],
    "Z": ["local:COVARIATE1", "local:COVARIATE2"]
  },
  "mediation_equations": [
    {
      "path": "Exposure → Mediator → Outcome",
      "contrast": "per 1-SD increase | Q4 vs Q1 | ...",
      "total_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "direct_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "indirect_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "proportion_mediated": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0
      },
      "provenance": "Table X p.Y"
    }
  ],
  "identification": {
    "assumptions": [
      "Sequential ignorability assumption",
      "Temporal ordering assumption",
      "No exposure-induced mediator-outcome confounding"
    ]
  }
}
```

---

## 类型二：Interventional（介入性）证据卡

### Step 1: 介入对比路径提取

```
你是一名医学信息学研究员和严谨的信息抽取器。请从提供的本地PDF论文中读取并提取所有介入对比路径（Intervention/Comparator → Outcome）。

**任务：**
提取论文中全部明确开展并报告的介入对比及主要结局：X（Intervention） vs C（Comparator） → Y

- X：研究的干预/暴露（介入组）
- Y：主要结局（疾病或生理指标）
- C：对照/比较（如 "usual care", "placebo", "baseline" 或另一干预剂量）

**提取范围：**
正文与附录（包含图表、图例、脚注）。对多时间点/多结局/多剂量、多组并行试验逐一提取。

**输出格式（仅JSON数组）：**
```json
[
  {
    "contrast": "Resmetirom 80mg vs Placebo → NASH resolution",
    "timepoint": "Week 52",
    "claim": "Resmetirom 80mg superior to placebo for NASH resolution (25.9% vs 9.7%, P<0.001)"
  },
  {
    "contrast": "Resmetirom 100mg vs Placebo → Fibrosis improvement",
    "timepoint": "Week 52",
    "claim": "Resmetirom 100mg superior to placebo for fibrosis improvement by ≥1 stage (25.9% vs 14.2%, P<0.001)"
  }
]
```
```

### Step 2: 以对比为单位构建证据卡

```
你是医学信息学研究员 + 严谨的信息抽取器，专注于从PDF临床介入研究中提取结构化证据。

**核心原则：**
- 仅使用PDF内容（正文 + 补充）；不确定即 null；禁止编造
- 完整性优先：提取所有主要变量与效应值（含CI/模型信息）
- 可复现性：给出页码/表格/图出处（provenance）
- 本类别不包含中介M：围绕介入对比（X vs C → Y），不进行机制/中介提取

**输入：**
- PDF文献
- 目标对比（由Step 1选定）："Resmetirom 80mg vs Placebo → NASH resolution @ Week 52"

**必提取模块：**

1) **paper** - 同mechanistic

2) **provenance** - 同mechanistic

3) **design**
   - type: "phase 3 RCT" | "phase 2 RCT" | "open-label trial" | ...
   - randomization: "1:1:1" | "1:1" | ...
   - blinding: "double-blind" | "single-blind" | "open-label"
   - n_total: 总随机化人数
   - n_arms: 治疗组数量
   - itt: true | false（是否意向性治疗分析）
   - estimand: "superiority" | "non-inferiority" | "equivalence"

4) **population** - 同mechanistic

5) **variables** - 定义 X（干预）、C（对照）、Y（结局）、Z（协变量）

6) **roles**
   - X: 干预变量
   - C: 对照变量
   - Y: 结局变量
   - Z: 协变量/分层因素

7) **intervention_effects**（干预效应）
   ```json
   [
     {
       "contrast": "Resmetirom 80mg vs Placebo",
       "outcome": "NASH resolution with no worsening of fibrosis",
       "timepoint": "Week 52",
       "intervention_rate": { "n": 82, "N": 316, "pct": 25.9 },
       "control_rate": { "n": 31, "N": 318, "pct": 9.7 },
       "effect": {
         "estimate": 16.4,
         "ci_lower": 11.0,
         "ci_upper": 21.8,
         "p": "<0.001",
         "scale": "percentage_points"
       },
       "relative_effect": {
         "estimate": null,
         "ci_lower": null,
         "ci_upper": null,
         "scale": "RR|OR|HR"
       },
       "nnt": null,
       "provenance": "Table 2 p.503"
     }
   ]
   ```

8) **safety**（安全性数据）
   ```json
   {
     "any_ae": { "intervention": 91.9, "control": 92.8 },
     "serious_ae": { "intervention": 10.9, "control": 11.5 },
     "discontinuation_due_to_ae": { "intervention": 1.9, "control": 2.2 },
     "common_aes": [
       { "event": "Diarrhea", "intervention": 27.0, "control": 15.6 },
       { "event": "Nausea", "intervention": 22.0, "control": 12.5 }
     ]
   }
   ```

9) **governance**
   - evidence_level: "1a" | "1b" | "2a" | ... (按Oxford CEBM)
   - risk_of_bias: "low" | "some concerns" | "high"
   - funding: 资助来源
   - coi: 利益冲突声明
```

---

## 类型三：Causal（因果推断）证据卡

### Step 1: 因果对比路径提取

```
你是一名医学信息学研究员和严谨的信息抽取器。请从提供的本地PDF论文中读取并提取所有因果对比通路。

**通路表示：**
- 若论文未做因果中介："X → Y"（X=暴露/处理，Y=结局）
- 若论文做了因果中介："X → M1 → M2 → … → Y"

**提取内容：**
- 对比签名：简短字符串描述暴露对比（如 "short sleep <6h vs 7–8h"）
- 估计目标：ATE/ATT/ATC/LATE；如有中介则 TE/NDE/NIE/CDE
- 方法家族/子类型：PS-IPTW, AIPW, TMLE, IV-2SLS, MR-IVW, DiD, RD等
- 时间点：主要评估时间点或随访窗口
- 效应尺度：OR/RR/HR/MD/SMD/BETA/RD

**输出格式（仅JSON数组）：**
```json
[
  {
    "path": "Sleep Duration → Cardiovascular Disease",
    "contrast": "short sleep <6h vs 7-8h",
    "estimand": "ATE",
    "method_family": "PS-IPTW",
    "method_subtype": "stabilized weights with doubly robust",
    "timepoint": "P10Y",
    "effect_scale": "HR",
    "conclusion": "Short sleep duration causally associated with increased CVD risk (HR=1.23, 95%CI 1.10-1.38)",
    "source": "Table 3 p.8"
  }
]
```
```

### Step 2: 以因果对比为单位构建证据卡

核心特点：
- 必须明确 **identification strategy**（PS/IV/DiD/RD/MR等）
- 需要记录 **识别假设** 和 **诊断检验**
- 如有工具变量，需定义 IV 节点

---

## 类型四：Associational（关联性）证据卡

### Step 1: 关联对提取

```
你是一名医学信息学研究员与严谨的信息抽取器。请从提供的PDF中提取所有暴露-结局关联对（X→Y）。

**关联对定义：**
- X：暴露变量
- Y：结局变量
- contrast：对比方式（如 "per +1 h", "Q4 vs Q1"）
- temporality：时序（cross-sectional | prospective | retrospective | lagged）

**输出格式（仅JSON数组）：**
```json
[
  {
    "X": "Sleep Duration",
    "Y": "Hypertension",
    "contrast": "per -1 hour",
    "temporality": "cross-sectional",
    "claim": "Each 1-hour decrease in sleep duration associated with 15% higher odds of hypertension (OR=1.15, 95%CI 1.08-1.23)",
    "source": "Table 2 p.6"
  }
]
```
```

### Step 2: 构建关联性证据卡

核心特点：
- **禁止使用因果术语**（ATE/NDE/NIE等）
- 使用 `association_equation` 而非 `causal_equation`
- 比值类参数在方程层面用对数（log）表示

---

## Step 3: HPP平台字段映射（通用）

```
你是医学信息学研究员与严谨的字段映射执行者。

**任务：**
将证据卡中的 X、M（如适用）、Y 变量映射到 Human Phenotype Project (HPP) 数据集字段。

**工具：**
- Pheno AI Knowledgebase: https://knowledgebase.pheno.ai/datasets.html
- HPP前端Demo: https://pheno-demo-app.vercel.app

**映射规则：**

1. **字段名唯一来源：** 必须使用HPP数据字典中的 `tabular_field_name`（原样拼写）

2. **状态枚举：**
   - `"exact"`：定义、单位、测量方式与时间锚点均一致
   - `"close"`：概念一致但单位/量表/设备或时间窗略有差异
   - `"derived"`：需由多个HPP字段计算/聚合得到
   - `"missing"`：HPP中无对应字段

3. **输出格式：**
```json
{
  "hpp_mapping": {
    "X": [
      {
        "name": "Body Mass Index",
        "dataset": "002-anthropometrics",
        "field": "bmi",
        "status": "exact",
        "notes": "Direct match for BMI measurement"
      }
    ],
    "M": [
      {
        "name": "KDM-BE Acceleration",
        "dataset": "016-blood_tests",
        "field": "albumin|creatinine|glucose|...",
        "status": "derived",
        "notes": "Requires multiple biomarkers to calculate KDM biological age"
      }
    ],
    "Y": [
      {
        "name": "Cardiovascular Disease",
        "dataset": "058-health_and_medical_history",
        "field": "health_pain_chest|...",
        "status": "close",
        "notes": "No direct CVD diagnosis field; using proxy indicators"
      }
    ],
    "Z": []
  }
}
```
```

---

## 验证提示词

用于检查生成的证据卡是否可用于复现：

```
检查这个证据卡的内容，查看其是否可以正常构建出其中所指的机制方程等等。

我的核心目的是需要在我本地的数据集上能够复现出和论文相同的内容。

假设我仅仅只是想要在我的本地数据集上，看看能不能跑通这个机制通路（回归模型），由于数据集的源头不同，其中的一些参数不同也可以理解。

**检查清单：**
1. 变量定义是否完整？能否明确识别 X/M/Y？
2. 方程形式是否清晰？（如 logit(Y) = α + β1*X + β2*M + β3*Z）
3. 效应值是否有足够信息？（estimate + CI 或 SE + p）
4. 时间窗口是否明确？
5. 协变量列表是否完整？
6. HPP字段映射是否可操作？

**输出：**
- 可行性评估：可行/部分可行/不可行
- 缺失信息列表
- 建议补充内容
```

---

## 使用流程总结

```
1. 上传PDF到LLM
2. 运行 Step 0（分类判别）→ 确定 primary_category
3. 根据类型选择对应的 Step 1 提示词 → 提取路径/对比列表
4. 对每条路径/对比运行 Step 2 提示词 → 生成完整证据卡JSON
5. 运行 Step 3 HPP映射 → 填充 hpp_mapping 字段
6. 使用验证提示词检查完整性
```

---

## 注意事项

1. **严禁编造**：所有数值必须来自PDF原文，不确定即填 null
2. **来源追溯**：每个关键数值都需要在 provenance 中标注出处
3. **单位规范**：使用标准单位（kg/m², mmHg, events/h等）
4. **时间格式**：使用ISO8601（如 P52W, P1Y, P9Y）
5. **对比方向统一**：比值类方程在 estimand_equation 用对数尺度（log(OR|RR|HR)
