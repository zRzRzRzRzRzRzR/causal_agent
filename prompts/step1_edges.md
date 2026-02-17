# Step 1: 枚举论文中的所有统计效应边（Edge）

你是医学信息学研究员和严谨的信息抽取器。你的任务是从论文中**穷举**所有报告了统计效应的 X → Y 关系。

## 核心概念

一个 **edge** = 一条 X → Y 的统计关系，其中：
- **X** = 暴露/干预/自变量
- **Y** = 结局/因变量
- 论文中报告了该关系的**统计量**（如 HR, OR, β, mean difference, p-value, CI 等）

## 枚举规则（非常重要，请严格遵守）

### 规则 1: 逐行扫描所有 Table
- 论文 Results 部分的**每一个 Table** 的**每一行**
- 如果某行报告了一个因变量的统计效应（estimate / p-value / CI），它就是一条 edge
- 不要只提取 "主要结局"——次要结局、安全性指标、生化指标全部都要

### 规则 2: 扫描所有 Figure
- Figure 中如果报告了具体统计值（如森林图中的 OR/HR、散点图中的 β/p-value），每个统计值是一条 edge
- 仅展示趋势但无具体统计量的 Figure 也可以记录，但需标注 `has_numeric_estimate: false`

### 规则 3: 亚组 × Y 的展开
- 如果论文按亚组（如基因型、性别、年龄组、基线风险水平）报告了分层效应：
  - 总体效应是一条 edge
  - **每个亚组水平**的效应也是独立的 edge
- 示例：论文报告了 "全体人群 X→Y" + "男性 X→Y" + "女性 X→Y" = 3 条 edge

### 规则 4: 多个 X 或多对比
- 如果论文有多个暴露组（如 4h-TRF vs Control、6h-TRF vs Control、4h vs 6h），每对比是独立的 edge
- 如果论文有多个暴露变量（如 sleep duration 和 insomnia），它们与同一 Y 的关系是不同的 edge

### 规则 5: 不显著的效应也要记录
- 结果不显著（p > 0.05）的 edge 同样必须记录
- 在 `significant` 字段标注 true/false

## 论文分类信息
本论文已被分类为：**{evidence_type}**

## 输出格式（严格 JSON，不要输出任何其他内容）

```json
{
  "paper_info": {
    "first_author": "第一作者姓氏",
    "year": 2024,
    "doi": "doi号或null",
    "short_title": "论文简短标题（英文，5个词以内）"
  },
  "edges": [
    {
      "edge_index": 1,
      "X": "暴露/干预变量的完整描述（含剂量/时间/单位等限定词）",
      "C": "对照/参照组描述（如 placebo, 7h sleep, control arm）",
      "Y": "结局变量的完整描述（含测量方式/时间窗/单位）",
      "subgroup": "总体人群 或 亚组描述（如 '男性', 'baseline HbA1c≥5.7%'）",
      "outcome_type": "continuous 或 binary 或 survival",
      "effect_scale": "HR 或 OR 或 RR 或 MD 或 beta 或 SMD 或 other",
      "estimate": "效应量数值（数字或null）",
      "ci": [下界或null, 上界或null],
      "p_value": "p值（数字/字符串如'<0.001'/null）",
      "significant": true,
      "source": "Table 2 p.6 或 Figure 1 p.3 或 Results text p.5",
      "has_numeric_estimate": true,
      "notes": "补充说明（可选）"
    }
  ]
}
```

## 自查清单（输出前请逐项检查）

1. ✅ 我是否扫描了论文中的**每一个 Table**？
2. ✅ 我是否扫描了论文中的**每一个 Figure**？
3. ✅ 我是否提取了**不显著**的结果？
4. ✅ 如果有亚组分析，我是否为每个亚组 × 每个 Y 都建立了独立的 edge？
5. ✅ 我是否包含了次要结局和安全性指标？
6. ✅ 每个 edge 的 Y 描述是否包含了完整的限定词（单位、时间窗等）？
