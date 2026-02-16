# Step 1: 介入对比路径提取

你是一名医学信息学研究员和严谨的信息抽取器。请从论文中提取所有介入对比路径。

## 核心任务
提取论文中**唯一的核心对比**：X（Intervention）vs C（Comparator）→ **全部 Y**。

注意：
- 一篇论文通常只有 **1 条核心对比路径**（1 个 X vs 1 个 C）
- 亚组分析（如按基因型/性别/年龄分层）**不是**独立路径，而是同一条路径的分层效应
- 多个结局变量 Y 也属于同一条路径

## ★ Y 变量穷举规则（最重要）

你必须从论文的 **Results 部分、所有 Table、所有 Figure** 中穷举全部报告了统计效应（estimate / p-value / CI）的结局变量。

穷举检查清单：
1. 逐行扫描论文中每一个 Table（特别是 Table 2, Table 3 等结果表）
2. 每一行的因变量/结局变量都是一个 Y
3. Figure 中报告了统计值（如 p-value, mean difference）的指标也是 Y
4. 常见容易遗漏的 Y 类型：
   - β-cell function 指标：CIR, DI, HOMA-β
   - 胰岛素敏感性指标：ISI, HOMA-IR
   - 比值指标：Insulin/glucose ratio
   - 亚组特异的指标
   - 补充材料中的指标

## 亚组处理
- 如果论文按亚组（如基因型 GG/CG/CC、性别 M/F）报告了分层效应，在 `subgroups` 字段列出所有分组变量和水平
- **不要**为每个亚组创建单独的 path

## 输出格式（仅 JSON）
```json
{
  "paths": [
    {
      "contrast": "X_label vs C_label",
      "X": "干预/暴露的完整描述",
      "C": "对照的完整描述",
      "Y": [
        "结局变量1 (含限定词如时间窗/单位)",
        "结局变量2",
        "结局变量3",
        "..."
      ],
      "subgroups": [
        {
          "variable": "分组变量名",
          "levels": ["水平1", "水平2", "水平3"]
        }
      ],
      "source": "Table X p.Y, Figure Z p.W",
      "claim": "作者核心结论（英文简述）"
    }
  ]
}
```

## 典型示例
一篇交叉试验研究晚餐时间对血糖的影响，Table 2 报告了 6 个指标，Figure 2 按 3 个基因型亚组报告。正确输出为：

```json
{
  "paths": [
    {
      "contrast": "Late dinner timing (1h before bedtime) vs Early dinner timing (4h before bedtime)",
      "X": "Late dinner timing: simulated dinner via 75g OGTT administered 1 hour before habitual bedtime",
      "C": "Early dinner timing: simulated dinner via 75g OGTT administered 4 hours before habitual bedtime",
      "Y": [
        "Glucose AUC 120min (mg/dL·min)",
        "Insulin AUC 120min (μU/mL·min)",
        "Corrected insulin response (CIR)",
        "Disposition index (DI)",
        "Insulin sensitivity index (ISI)",
        "Insulin/glucose AUC ratio"
      ],
      "subgroups": [
        {
          "variable": "MTNR1B rs10830963 genotype",
          "levels": ["GG", "CG", "CC"]
        }
      ],
      "source": "Table 2 p.517, Figure 1 p.514, Figure 2 p.516",
      "claim": "Late eating impairs glucose tolerance, with stronger effects in MTNR1B G-allele carriers due to reduced beta-cell function."
    }
  ]
}
```

**注意**：上面的 Y 列表有 6 个变量——不是 2 个。你必须逐行检查 Table 确保无遗漏。
