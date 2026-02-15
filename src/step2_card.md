# Step 2: 构建证据卡（对齐 HPP EL-GSE 验证模板）

你是医学信息学研究员 + 严谨的信息抽取器。请从 PDF 论文中提取结构化证据卡，**严格对齐下方的 HPP 模板 schema**。

## 核心原则
- **仅使用 PDF 内容**（正文 + 补充材料）；不确定即 null；**禁止编造**
- **字段名必须与模板完全一致**，不可自造字段名
- 提取**所有**报告的效应（含亚组分层），每个效应对应一条 effects 记录
- 给出溯源（页码/表格/图号）

## 目标对比路径
{target_path}

---

## ★ 权威模板 Schema（你的输出必须严格对齐此结构）

```json
{
  "evidence_id": "EV-YYYY-STUDY_NAME",
  "paper": {
    "title": "论文完整标题",
    "doi": "10.xxxx/xxxxx",
    "pmid": "PubMed ID 或 null",
    "year": 2024,
    "journal": "期刊名称",
    "authors": ["第一作者", "第二作者"]
  },
  "design": {
    "type": "cohort | RCT | crossover | MR | meta-analysis",
    "analysis": "使用的统计分析方法描述",
    "n_total": 10000,
    "population_description": "人群特征描述"
  },
  "variables": {
    "nodes": [
      {
        "node_id": "local:X",
        "label": "暴露/干预变量名",
        "type": "exposure",
        "unit": "单位",
        "description": "变量描述",
        "outcome_family": null
      },
      {
        "node_id": "local:Y",
        "label": "结局变量名",
        "type": "outcome",
        "unit": "单位或binary",
        "outcome_family": "continuous | binary",
        "description": "变量描述"
      },
      {
        "node_id": "local:M",
        "label": "中介变量名（可选）",
        "type": "mediator",
        "unit": "单位",
        "description": "..."
      },
      {
        "node_id": "local:Z1",
        "label": "协变量名",
        "type": "covariate",
        "unit": "单位"
      }
    ],
    "roles": {
      "X": ["暴露变量label列表"],
      "Y": ["结局变量label列表"],
      "Z": ["调整变量label列表"],
      "M": [],
      "IV": []
    },
    "outcome_direction": {
      "结局变量label": "higher_is_worse | higher_is_better"
    }
  },
  "effects": [
    {
      "edge_id": "EV-YYYY-STUDY#1",
      "from": "X变量label",
      "to": "Y变量label",
      "mediators": [],
      "outcome_family": "continuous | binary",
      "effect_scale": "BETA | OR | RR | HR | MD | RD",
      "estimate": 0.72,
      "se": 0.048,
      "ci": [0.65, 0.80],
      "ci_level": 0.95,
      "p_value": "<0.001",
      "link_function": "identity | logit | log",
      "model": "统计模型名称",
      "adjustment_set_used": ["Age", "Sex"],
      "n_effective": 84200,
      "time_horizon": "ISO8601格式如P10Y或PT2H",
      "exposure_contrast": {
        "type": "per_unit | per_SD | q5_vs_q1 | treat_vs_control | assignment",
        "delta": "对比的文字描述",
        "x0": null,
        "x1": null,
        "unit": "SD | 原始单位"
      }
    }
  ],
  "estimand_equation": {
    "equations": [
      {
        "outcome": "结局变量名",
        "formula": "数学公式",
        "parameters": {"β": -0.33, "σ": 1.0},
        "window": "时间窗",
        "derivation": "推导说明",
        "source_edge": "EV-YYYY-STUDY#1",
        "analysis_set": "ITT | PP | full_cohort",
        "scale": "BETA | log(OR) | MD"
      }
    ]
  },
  "hpp_mapping": {
    "X": {
      "name": "变量名",
      "dataset": "HPP数据集如016-blood_tests",
      "field": "字段名如hdl_cholesterol",
      "status": "exact | close | derived | missing",
      "notes": "映射说明"
    },
    "Y": [
      {
        "name": "结局变量名",
        "dataset": "...",
        "field": "...",
        "status": "exact | close | derived | missing",
        "notes": "..."
      }
    ],
    "Z": [
      {
        "name": "协变量名",
        "dataset": "...",
        "field": "...",
        "status": "exact | close | derived | missing",
        "notes": "..."
      }
    ],
    "M": []
  },
  "time_semantics": {
    "baseline_window": "baseline±90d",
    "exposure_window": "暴露测量时间",
    "outcome_window": "ISO8601如P10Y",
    "follow_up_start": "baseline",
    "notes": ""
  },
  "governance": {
    "tier": "A | B | C",
    "risk_of_bias": "low | moderate | high | critical",
    "notes": "质量评估说明"
  }
}
```

---

## 关键字段说明

### effects 字段规则
| 字段 | 必填 | 说明 |
|------|------|------|
| outcome_family | ✅ | continuous→用BETA+identity; binary→用OR+logit |
| se | ⚠️ | 标准误，尽量从论文提取；无法获取填 null |
| estimate | ✅ | OR/RR填原始比值；BETA填回归系数；MD填均数差 |
| exposure_contrast.type | ✅ | per_unit/per_SD/assignment/treat_vs_control/q5_vs_q1 |
| link_function | ✅ | 连续结局=identity, 二分类=logit |

### 结局类型速查
- **连续结局** (如血糖AUC, BMI): outcome_family="continuous", effect_scale="BETA"或"MD", link_function="identity"
- **二分类结局** (如患病是/否): outcome_family="binary", effect_scale="OR", link_function="logit"

### HPP 数据集参考
常用数据集：000-population, 001-events, 002-anthropometrics, 003-blood_pressure, 005-diet_logging, 009-sleep, 014-human_genetics, 016-blood_tests, 017-cgm, 020-health_and_medical_history, 021-medical_conditions, 023-lifestyle_and_environment

### hpp_mapping.status 规则
- exact: 定义/单位/测量完全一致
- close: 概念一致但单位/设备/时间窗有差异
- derived: 需从多个HPP字段计算
- missing: HPP中无此变量

---

## 亚组效应提取规则
如果论文按亚组（如基因型 GG/CG/CC、性别、年龄分层）报告了分层效应：
- 每个**亚组 × 结局**单独提取一条 effect
- exposure_contrast.delta 中注明亚组（如 "Late vs Early in MTNR1B GG carriers"）
- adjustment_set_used 中包含分层变量

## 输出格式
输出为 **JSON 数组**（即使只有一张卡也用 `[{...}]` 包裹），严格对齐上述模板。不确定的字段填 null。
