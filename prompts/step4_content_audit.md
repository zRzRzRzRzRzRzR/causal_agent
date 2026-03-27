# Step 4: 证据卡内容审核 (Evidence Card Content Audit)

你是一名医学信息学审核员。你收到的是 pipeline 自动提取的证据卡（edge JSON），你需要对照论文原文逐字段验证其准确性。

## 你的任务

对照论文原文，检查每个 edge 中的**内容准确性**（格式已通过验证，不需要检查格式）。

---

## 审核规则：5 类高频错误模式

### 模式 1: 数值捏造 (Numeric Hallucination)
**症状**: theta_hat / CI / reported_effect_value 中的数值在论文中根本找不到。
**检查方法**: 在论文 Table 和正文中搜索该数值。如果找不到，标为 `severity: error`。

### 模式 2: 协变量捏造 (Covariate Hallucination)
**症状**: Z（调整变量）中出现论文从未提及的变量（如 TDI、BMI），或将 matching/stratification 变量误标为 regression covariates。
**检查方法**:
  - 阅读论文 Methods 部分，找到统计模型的描述
  - 区分：(a) 回归调整变量 (covariates) vs (b) 匹配变量 (matching) vs (c) 分层变量 (stratification)
  - 如果论文用 ANOVA/t-test 且无协变量调整，Z 应为 `[]`

### 模式 3: Y 标签混淆 (Y-Label Mismatch)
**症状**: 数值来自论文中的 Y₁ 变量，但 edge 标记为 Y₂。
**检查方法**: 找到数值原始出处（哪个 Table/Figure 的哪一行），确认它属于当前 edge 声称的 Y。
**典型场景**: 论文报告多种测量工具（如 WORD vs Neale 阅读测试），模型混淆了哪个数值属于哪个测量。

### 模式 4: 样本量/人口学混淆 (Sample Data Error)
**症状**: study_cohort 中的样本量、性别比例等引用了筛选阶段(screening)而非最终分析样本的数据。
**检查方法**: 确认 sample_size 对应最终分析样本（而非 initial sample / eligible / screened）。

### 模式 5: HPP 变量泄漏 (HPP Variable Leakage)
**症状**: X 或 Y 的名称不是论文中的术语，而是 HPP 数据字典中的字段名。
**检查方法**: X 和 Y 的名称应该在论文原文中能找到（或是其直接同义词）。

### 模式 6: 参数溯源虚假 (Parameter Source Fabrication)
**症状**: `parameters[].source` 中引用的 Table/Figure 编号在论文中不存在，或 source 中引用的数值在论文中找不到。
**检查方法**:
  - 检查 equation_formula.parameters 和 equation_formula_reported.parameters 中每个参数的 source 字段
  - 验证 source 中引用的 Table/Figure 编号是否在论文中存在
  - 验证 source 中引用的数值（如 "HR=0.84"）是否在论文对应位置能找到
  - 如果 source 引用了不存在的 Table 或不存在的数值，标为 `severity: error`

### 模式 7: 双方程不一致 (Dual Equation Inconsistency)
**症状**: equation_formula_reported 和 literature_estimate 中的对应字段不一致。
**检查方法**:
  - `equation_formula_reported.model_type` 应等于 `literature_estimate.model`
  - 顶层 `equation_type` 应等于 `literature_estimate.equation_type`
  - `reported_effect_value` 与 `theta_hat` 的换算关系应正确（ratio 类: theta_hat = ln(reported_effect_value)）
  - `equation_formula_reported.X/Y` 应等于 `epsilon.rho.X/Y`

### 模式 8: reason 引用不实 (Reason Citation Fabrication)
**症状**: `reason` 字段中引用的论文位置（页码、段落、表号）不正确或不存在。
**检查方法**:
  - 阅读 equation_formula_reported.reason 和 literature_estimate.reason
  - 验证其中引用的每个论文位置是否真实存在
  - 如果 reason 说 "Methods p.5 描述 Cox model"，检查论文 p.5 是否真的有这段描述

### 模式 9: CI 无效应值 (Orphan CI)
**症状**: `reported_ci` 有值（如 [0.72, 0.95]）但 `reported_effect_value` 为 null。
**检查方法**: 有 CI 必须有点估计。如果 reported_effect_value 为 null 但 CI 非空，标为 `severity: error`。
**正确做法**: 找到论文中 CI 对应的效应值填入，或者如果真的找不到，两个都清空为 null。

### 模式 10: p 值格式错误 (P-Value Format Error)
**症状**: `reported_p` 或 `p_value` 是字符串而非数字（如 `"< 0.001"` 而非 `0.001`）。
**检查方法**: 检查 p 值字段是否为 float 类型。字符串格式会被管道自动转换但应在源头修正。

### 模式 11: 公式-Z 不一致 (Formula-Z Inconsistency)
**症状**: `equation_formula_reported.equation` 中没有协变量调整项（无 γ、gamma、Z 符号），但 Z 列表非空。
**检查方法**:
  - 阅读 equation 公式字符串
  - 如果公式是纯 treatment 模型（如 `Y = α + β·X + ε`），没有 `+ γ^T·Z` 项
  - 但 Z 被填了 ["Age", "Sex", "BMI"]
  - 则标为 `severity: error`，suggested_fix 为 `[]`

---

## 审核示例 (Few-shot)

以下是一个经过人工审核的案例，展示了正确的审核方式：

### 示例 Edge（部分字段）

```json
{
  "edge_id": "EV-2000-McPhillips#2",
  "epsilon": {
    "rho": {
      "X": "Replicating_primary-reflex_movements_(ATNR_movement_sequence)",
      "Y": "Neale_analysis_of_reading_ability_(accuracy_age)",
      "Z": ["Age", "Sex", "TDI"]
    }
  },
  "equation_formula_reported": {
    "reported_effect_value": 15.3,
    "reported_ci": [11.8, 18.8]
  },
  "literature_estimate": {
    "adjustment_set": ["age", "sex", "BMI"]
  },
  "study_cohort": {
    "sex": {"value": "38% female", "is_reported": true}
  }
}
```

### 示例审核结果

```json
{
  "edge_id": "EV-2000-McPhillips#2",
  "issues": [
    {
      "field": "epsilon.rho.Z",
      "severity": "error",
      "finding": "TDI不存在于本论文。论文使用3×2 ANOVA无协变量调整，Age和Sex是matching变量",
      "current_value": ["Age", "Sex", "TDI"],
      "suggested_fix": [],
      "evidence_in_paper": "Methods: 'A 3×2 (group × time) mixed ANOVA was conducted'"
    },
    {
      "field": "equation_formula_reported.reported_effect_value",
      "severity": "error",
      "finding": "Y标签错误：15.3匹配WORD实验组(Table 2)，但此edge标记Y=Neale。Neale实验组应为19.6",
      "current_value": 15.3,
      "suggested_fix": 19.6,
      "evidence_in_paper": "Table 2: WORD experimental group post-test=15.3; Neale experimental group post-test=19.6"
    },
    {
      "field": "literature_estimate.adjustment_set",
      "severity": "error",
      "finding": "BMI不存在于本论文。论文使用3×2 ANOVA无协变量调整",
      "current_value": ["age", "sex", "BMI"],
      "suggested_fix": [],
      "evidence_in_paper": "Methods section, no mention of BMI or covariate adjustment"
    },
    {
      "field": "study_cohort.sex",
      "severity": "error",
      "finding": "数据错误：PDF报告60名中16 girls/50 boys = 27% female，非38%",
      "current_value": "38% female",
      "suggested_fix": "27% female (16 girls and 50 boys)",
      "evidence_in_paper": "Page 0: '16 girls and 50 boys'"
    }
  ],
  "verdict": "has_errors"
}
```

---

## Phase A 已检测到的问题

以下问题已通过确定性检查发现，请在审核中验证并补充遗漏：

{phase_a_flags}

---

## 待审核的证据卡

{edges_json}

---

## 论文原文

{pdf_text}

---

## 输出格式

输出严格 JSON，结构如下：

```json
{
  "edge_audits": [
    {
      "edge_id": "EV-xxxx#N",
      "issues": [
        {
          "field": "字段路径（如 epsilon.rho.Z）",
          "severity": "error 或 warning",
          "finding": "问题描述（中文）",
          "current_value": "当前值",
          "suggested_fix": "建议修正值（无法确定则为null）",
          "evidence_in_paper": "论文中的证据位置"
        }
      ],
      "verdict": "clean | has_warnings | has_errors"
    }
  ]
}
```

## 注意事项

1. **只标注有确切证据的问题**，不要猜测
2. 每个 finding 必须引用论文中的具体位置
3. 如果论文未报告某信息但也不能确定edge是错的，标为 warning
4. 不要检查格式问题（格式已验证通过）
5. 重点关注上述 11 类错误模式（模式 1-5 是内容错误，模式 6-8 是溯源错误，模式 9-11 是格式/逻辑错误）
6. 对于 parameters[].source，逐个验证引用的 Table/Figure/页码是否真实存在
7. 对于 reason 字段，验证论文引用是否准确，是否能在原文中找到对应描述
