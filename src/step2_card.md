# Step 2: 构建完整证据卡

你是医学信息学研究员 + 严谨的信息抽取器。请从 PDF 论文中提取**一张完整的**结构化证据卡。

## 核心原则

- **仅使用 PDF 内容**（正文 + 补充材料）；不确定即 null；**禁止编造**
- **一篇论文一张卡**：所有效应（总体 + 亚组分层）都放在同一张卡的 effects 数组中
- **effects 穷举**：论文中每个「结局 Y × 亚组水平」的统计效应都必须是一条 effect
- 变量 label 必须带完整限定词（如 "Glucose AUC 120min" 而非 "Glucose AUC"）

## 目标对比路径
{target_path}

---

## 完整 Schema（你的输出必须包含以下所有模块）

```json
{
  "schema_version": "2.1",
  "evidence_id": "EV-YYYY-XXXXXX",
  "paper": {
    "title": "",
    "doi": "",
    "pmid": null,
    "year": 2024,
    "journal": "",
    "authors": [],
    "registry": null,
    "abstract": ""
  },
  "provenance": {
    "figure_table": [
      "Table 2 p.6",
      "Figure 1 p.3"
    ],
    "pages": [
      1,
      2,
      3
    ],
    "supplement": false
  },
  "design": {
    "type": "crossover | parallel_rct | cluster_rct | cohort | ...",
    "analysis": "统计分析方法描述",
    "estimand": "ITT | PP | null",
    "n_total": 0,
    "n_arms": 2,
    "randomization": "block | simple | stratified",
    "allocation_ratio": "1:1",
    "blinding": "none | single | double | triple",
    "hypothesis": "superiority | non-inferiority | equivalence",
    "population_description": "纳入人群特征描述",
    "missing_data": {
      "method": "complete_case | MI | LOCF",
      "notes": ""
    },
    "censoring": {
      "type": "",
      "competing_risks": false
    }
  },
  "population": {
    "eligibility_signature": {
      "age": "range or mean±SD",
      "sex": "both | male | female (with %)",
      "disease": "population description",
      "key_inclusions": [],
      "key_exclusions": []
    }
  },
  "transport_signature": {
    "center": "",
    "era": "",
    "geo": "",
    "care_setting": "laboratory | clinic | community",
    "data_source": "trial | registry | HER"
  },
  "arms": [
    {
      "arm_id": "A",
      "label": "",
      "role": "comparator | intervention",
      "n_randomized": 0,
      "n_analyzed_itt": 0,
      "description": "",
      "dose_intensity": "",
      "frequency": "",
      "duration": "ISO8601",
      "components": [],
      "crossover": {
        "sequence": "",
        "washout": "P1W"
      }
    }
  ],
  "adherence": {
    "definition": "",
    "method": "",
    "per_arm": [
      {
        "arm_ref": "A",
        "n_evaluable": 0,
        "adherence_rate": 1.0,
        "notes": ""
      }
    ],
    "source": ""
  },
  "variables": {
    "nodes": [
      {
        "node_id": "local:X",
        "label": "暴露变量名(带限定)",
        "type": "exposure | outcome | covariate | mediator",
        "unit": "",
        "description": "",
        "outcome_family": "continuous | binary | null"
      }
    ],
    "roles": {
      "X": [
        "暴露变量label"
      ],
      "C": [
        "对照label"
      ],
      "Y": [
        "结局1 label",
        "结局2 label",
        "..."
      ],
      "Z": [
        "调整变量label"
      ],
      "M": [],
      "IV": []
    },
    "outcome_direction": {
      "结局label": "higher_is_worse | higher_is_better"
    }
  },
  "time_semantics": {
    "baseline_window": "",
    "exposure_window": "",
    "outcome_window": "",
    "follow_up_start": "",
    "assessment_timepoints": [],
    "follow_up_duration": "",
    "notes": ""
  },
  "identification": {
    "identification_status": "identified",
    "backdoor_set": [],
    "instrument": [],
    "positivity_notes": ""
  },
  "outcome_summaries": [
    {
      "name": "结局变量名",
      "timepoint": "",
      "analysis_set": "ITT",
      "by_arm": [
        {
          "arm": "A",
          "n": 0,
          "mean": null,
          "sd": null
        }
      ],
      "source": "Table X p.Y"
    }
  ],
  "effects": [
    {
      "edge_id": "EV-YYYY-XXXXXX#1",
      "from": "X变量label",
      "to": "Y变量label",
      "mediators": [],
      "outcome_family": "continuous | binary",
      "effect_scale": "BETA | OR | RR | HR | MD | RD",
      "estimate": 0.0,
      "se": null,
      "ci": [
        0.0,
        0.0
      ],
      "ci_level": 0.95,
      "p_value": "",
      "link_function": "identity | logit | log",
      "model": "统计模型名称",
      "adjustment_set_used": [],
      "n_effective": 0,
      "time_horizon": "ISO8601",
      "exposure_contrast": {
        "type": "assignment | per_unit | per_SD",
        "delta": "对比描述(含亚组信息)",
        "x0": null,
        "x1": null,
        "unit": ""
      }
    }
  ],
  "estimand_equation": {
    "contrast_convention": {
      "I": "Late",
      "C": "Early"
    },
    "equations": [
      {
        "outcome": "",
        "formula": "ΔY = τ + ε",
        "parameters": {
          "τ": 0.0,
          "σ": 0.0
        },
        "derivation": "",
        "source_edge": "EV-YYYY-XXXXXX#1",
        "analysis_set": "ITT",
        "scale": "MD | BETA | log(OR)"
      }
    ]
  },
  "measurement": {
    "devices": [
      {
        "node": "",
        "device": "",
        "algo": ""
      }
    ],
    "derivations": [
      {
        "node": "",
        "rule": ""
      }
    ]
  },
  "hpp_mapping": {
    "X": {
      "name": "",
      "dataset": "",
      "field": "",
      "status": "exact|close|derived|missing",
      "notes": ""
    },
    "Y": [
      {
        "name": "",
        "dataset": "",
        "field": "",
        "status": "",
        "notes": ""
      }
    ],
    "Z": [
      {
        "name": "",
        "dataset": "",
        "field": "",
        "status": "",
        "notes": ""
      }
    ],
    "M": []
  },
  "governance": {
    "tier": "A | B | C",
    "risk_of_bias": "low | some_concerns | high",
    "notes": ""
  },
  "inference": {
    "contrast": "核心对比的一句话描述",
    "claim": "主要结论（1-3句话）",
    "assumptions": [
      "因果/统计假设1",
      "假设2"
    ]
  }
}
```

---

## ★ Effects 穷举规则（最重要）

### 规则1：逐行扫描所有 Table
- 论文 Results 部分的每个 Table 的**每一行**（每个因变量/结局指标），只要报告了 estimate / p-value / CI，就必须提取为一条 effect
- 不要只提取 2 个"主要结局"就停——beta-cell function 指标（CIR, DI）、胰岛素敏感性（ISI）、比值指标都要提取

### 规则2：亚组穷举
- 如果论文按亚组（如基因型 GG/CG/CC）报告了分层效应，**每个亚组 × 每个 Y** 都是一条独立的 effect
- 在 `exposure_contrast.delta` 中注明亚组（如 "Late vs Early dinner in MTNR1B GG carriers"）
- 在 `adjustment_set_used` 中标注分层变量

### 规则3：总体 + 亚组都要
- 先提取全体人群的效应（总体效应），再逐个亚组提取
- edge_id 编号连续：#1, #2, ... #N

### 效应量快查
| 结局类型 | outcome_family | effect_scale | link_function | formula 示例 |
|----------|---------------|-------------|---------------|-------------|
| AUC (连续) | continuous | MD | identity | ΔY = Y_late - Y_early |
| log 变换后 | continuous | BETA | identity | Δlog(Y) = β |
| 二分类 | binary | OR | logit | logit(P) = α + β·X |

### HPP 数据集参考（用于 hpp_mapping）
000-population, 001-events, 002-anthropometrics, 003-blood_pressure, 005-diet_logging, 009-sleep, 014-human_genetics, 016-blood_tests, 017-cgm, 020-health_and_medical_history, 021-medical_conditions, 023-lifestyle_and_environment

### hpp_mapping.status 规则
- exact: HPP 字段与论文变量完全一致
- close: 概念一致但测量方式不同（如 CGM glucose vs OGTT glucose）
- derived: 需从 HPP 字段计算（notes 中说明公式）
- missing: HPP 中确实无此变量
- **严禁编造不存在的 HPP 字段名**，如不确定某字段是否存在，标 missing

### estimand_equation
每条 effect 对应一个 equation，通过 `source_edge` 关联 `edge_id`。equations 数量应与 effects 数量一致。

---

## 输出
输出为 JSON **数组**（`[{...}]`），即使只有一张卡。严格对齐上述 schema。不确定的字段填 null。
