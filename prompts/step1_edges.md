# 步骤 1: 枚举所有统计效应边

你是一名医学信息学研究员和严谨的信息提取专家。
你的任务是**详尽枚举**所有报告统计效应的 X → Y 关系。

## 核心概念

一个**边** = 一个 X → Y 统计关系，其中：

- **X** = 暴露 / 干预 / 自变量
- **Y** = 结局 / 因变量
- 论文报告了该关系的**统计量**（HR、OR、β、均值差、p值、CI等）

**⚠️ X 命名硬规则：X 必须编码对照/参照组（除连续型 X 外）**

当 X 是分类变量或干预（几乎所有 RCT 和分层分析的情况），X 字符串**必须**显式包含对照组：

- ✅ `"4-h TRF vs Control"`
- ✅ `"Sleep duration ≥10 h/day (vs 7 h/day reference)"`
- ✅ `"Healthy Lifestyle Score 4 vs 0"`
- ✅ `"10-h TRE vs Standard of Care"`
- ❌ `"4-h Time-Restricted Feeding"` （缺对照组）
- ❌ `"Healthy Lifestyle Score"` （缺对比层次）
- ❌ `"10-h TRE"` （缺对照组）

连续型 X（如 `"Sleep duration (hours)"`）可不加对照，但如果论文报告的是**分层比较**（例：≥10h vs 7h），则必须写出层次。

同时，`C` 字段仍然必须填写对照组名（可以与 X 里的 "vs ..." 重复，保留两处是为下游双重校验）。

## 枚举规则（严格遵循）

### 规则 1: 逐行扫描每个表格

- 对于结果部分的**每个表格**，**逐行扫描**
- 如果某行报告了针对因变量的效应估计值/p值/CI，它就是一个边
- 提取主要结局、次要结局、安全性终点和生物标志物 —— 全部都要

### 规则 2: 扫描每个图表

- 报告具体统计量的图表（森林图 OR/HR、散点图 β/p）→ 每个统计量一个边
- 显示趋势但没有数值统计量的图表 → 记录为 `has_numeric_estimate: false`

### 规则 3: 亚组 × Y 扩展

- 如果论文按亚组（基因型、性别、年龄组）报告**分层效应**：
  - 总体效应是一个边
  - **每个亚组层次**是一个单独的边，并填写亚组字段
- 示例："总体 X→Y" + "男性 X→Y" + "女性 X→Y" = 3个边

### 规则 4: 多个 X 或多次比较

- 多个暴露组（例如，4h-TRF vs 对照组, 6h-TRF vs 对照组）→ 单独的边
- 多个暴露变量（例如，睡眠时长和失眠）→ 单独的边

### 规则 4a: 分类 X 的多层次 vs 单一参照 —— 每个非参照层次一个 edge（**关键！**）

这是观察性研究最常见的结构，也是 LLM 最容易漏掉的情形。当一个**分类暴露**（categorical exposure）有多个层次，每个层次都与同一个参照组比较时：

**规则：有 N 个非参照层次 → 产生 N 个独立 edges**，每个 edge 的 X 字段必须写明该层次 + 参照层次。

**示例 A（Wright 2025 风格，睡眠时长五分类）**：
- 分类：Sleep duration = {≤5, 5–6, 7 (ref), 8–9, ≥10} h/day
- 报告：论文给出 ≤5 / 5–6 / 8–9 / ≥10 各自相对于 7h 的 HR
- ✅ **必须产生 4 个 edges**：
  - `X = "Sleep duration ≤5 h/day (vs 7 h/day reference)"` → Y=Incident T2D, HR=1.20
  - `X = "Sleep duration 5–6 h/day (vs 7 h/day reference)"` → HR=?
  - `X = "Sleep duration 8–9 h/day (vs 7 h/day reference)"` → HR=?
  - `X = "Sleep duration ≥10 h/day (vs 7 h/day reference)"` → HR=1.28
- ❌ **错误做法**：只产生 1 条 `X = "Sleep duration"` → HR=... 这是常见错误，会丢失 3 条 GT edges。

**示例 B（Rassy 风格，lifestyle score 分类）**：
- 分类：Healthy Lifestyle Score = {0 (ref), 1, 2, 3, 4}
- 报告：每个 Score 级别 vs Score=0 的 HR
- ✅ 产生 4 个 edges（分数 1/2/3/4 vs 0），每个对每个疾病结局再复制

**示例 C（失眠频率三分类）**：
- 分类：Insomnia symptoms = {never/rarely (ref), sometimes, usually}
- 报告：sometimes / usually 各自 vs never/rarely
- ✅ 产生 **2 个 edges**（sometimes 和 usually 各自一条）
- 如果论文对同一分类在多个模型（如 model 1 / model 2）下报告，**还要乘以模型数**——见规则 4c 的"模型区分符"

**强制自检（输出前必问）**：
- 我提取的每个 X 是否对应论文里 Table/Figure 中的**一行**（一个具体层次），而不是整个变量名？
- 如果论文 Table 里 Sleep duration 有 4 个非参照行，我是不是产生了 4 个独立 edges？
- 如果答案"否"，**回去补齐**——这是最常见的漏召回来源。

### 规则 4b: RCT/干预研究——优先组间差异，但不要漏掉其他合法终点

对于干预研究（RCT、临床试验），**优先**提取**组间差异**（between-group difference）。但**不要为了遵守这条规则而漏掉合法终点**——先提取所有终点，再判断每个终点的最佳形式。

- 如果论文对**同一 Y** 同时报告了"干预组前后变化"和"干预组 vs 对照组差异"，**只提取组间差异**（避免冗余）。
- 但如果论文只对某个 Y 报告了**组内前后变化**或只报告了**各组的均值±SD（无差异估计）**，这仍然是该 Y 的合法 edge——**必须保留**，标注 `priority: secondary` 并在 `notes` 里说明 "only within-group changes reported" 或 "group means only"。
- 不要因为次要终点效应小、CI 跨 0 或 p>0.05 就丢弃——无统计显著性的终点也必须保留（规则 5）。
- 典型组间差异来源：调整后的组间均值差（adjusted mean difference）、组间差值 95% CI、ANCOVA 模型的组间效应
- `C`（对照组）字段必须填实际对照组名（"control group"、"placebo"），**不能**填 "baseline" 或留空

**❌ 错误**: X="4h TRF", C="baseline", estimate=-3.2（这是组内前后变化）→ 应改为组间差异
**✅ 正确**: X="4h TRF vs control group", C="control group", estimate=-3.3（组间差异）
**✅ 也正确**（fallback）: X="4h TRF vs baseline", C="baseline", priority="secondary", notes="only within-group changes reported" （当组间差异不可得时）

### 规则 4c: 同 (X, Y) 必须通过时间点或亚组区分（重要！）

如果论文对**同一 (X, Y) 对**报告了多个值（例如不同随访时间点、不同亚组、不同模型），每个值都是一个单独的 edge，**Y 字段必须包含区分符**：

- ✅ `Y = "HbA1c (%) at week 8"` 和 `Y = "HbA1c (%) at week 12"`
- ✅ `Y = "SBP (mmHg), males"` 和 `Y = "SBP (mmHg), females"`（亚组分层）
- ✅ `Y = "BMI change (kg/m²), adjusted"` 和 `Y = "BMI change (kg/m²), unadjusted"`（不同模型）
- ❌ 三条 edge 都写 `Y = "HbA1c (%)"` 且 X 完全相同，这会导致下游去重误删。

**原则**：如果去掉所有 subgroup/时间点/模型信息后，你发现同一论文有两个 edge 的 (X, Y, subgroup) 完全一样，就必须在 Y 或 subgroup 里加区分符。

### 规则 4d: Y 字段必须包含测量形式 / 时间窗 / 单位（重要！）

Y 不是一个变量名，而是"论文报告的那个具体统计量对应的那个结局"。Y 字段必须完整到能唯一定位论文里的**那一行数据**。

**强制包含的信息（能写就写全）**：
- **测量形式**：是 "change"、"absolute value"、"rate"、"incidence"、"odds"？
  - ✅ `Y = "HbA1c change (V3-V1, %)"`（RCT 报告变化量）
  - ✅ `Y = "Incident type 2 diabetes"`（队列研究报告发病）
  - ❌ `Y = "HbA1c"`（不知道是变化还是绝对值）
- **时间点/随访窗**：week X / visit X / at baseline / follow-up 等
  - ✅ `Y = "Weight (kg) at week 12"`
  - ❌ `Y = "Weight (kg)"`
- **单位**：`(mg/dL)`、`(mmHg)`、`(%)`、`(kg/m²)` 等
- **亚组/条件**（如果适用）

**RCT 专项提醒**：RCT 报告"组间差异 (V3−V1 或 change from baseline)"是最常见的 Y 形式。如果论文 Table 的列标题是"Change from baseline"、"V3 − V1"、"Δ"、"week 12 − baseline"，你的 Y **必须**把这个写进去（如 `"LDL change (V3-V1, mg/dL)"`），否则后续匹配会失败。

**自检**：从你填的 Y 字段，能不能判断出它来自论文的哪一行/哪一列？如果不能，补单位/时间/测量形式。

### 规则 5: 必须包含无统计学显著性的结果

- 记录 p > 0.05 的边 —— 标记为 `significant: false`

### 规则 6: 排除基线平衡检查行（重要！）

**完全跳过**这些行：

- 表1/补充表格中检验各组间基线特征是否平衡的行
- 典型模式：X是组/基因型变量，Y是年龄/性别/BMI/种族，目的是显示无差异
- **例外**：如果论文明确研究该关联作为研究问题，则包含

### 规则 7: 交互效应标准化

当论文对同一交互作用使用多种统计形式时：

- **保留回归交互项**（beta或P_interaction）作为一个边；X = "交互: [X1] × [X2]"
- **保留亚组分层的效应**作为单独的边（每个亚组一个）
- 无亚组效应量的ANOVA全局p值：一个边，标记 `has_numeric_estimate: false`
- **不要**保留同一交互作用的冗余表示

## 论文分类

本文论文分类为：**{evidence_type}**

## 输出格式（严格JSON格式，不包含其他内容）

```json
{
  "paper_info": {
    "first_author": "第一作者的姓氏",
    "year": 2024,
    "doi": "DOI或null",
    "short_title": "英文简短标题（最多5个词）"
  },
  "edges": [
    {
      "edge_index": 1,
      "X": "暴露/干预的完整描述（包括剂量/时间/单位限定符）",
      "C": "对照/参考组（例如，安慰剂、7小时睡眠、对照组）",
      "Y": "结局的完整描述（包括测量/时间窗口/单位）",
      "subgroup": "总体人群或亚组描述（例如，'男性'、'MTNR1B GG基因型'）",
      "outcome_type": "continuous或binary或survival",
      "effect_scale": "HR或OR或RR或MD或beta或SMD或其他",
      "estimate": "数值效应值或null",
      "ci": [下限或null, 上限或null],
      "p_value": "p值（数字/字符串如'<0.001'/null）",
      "significant": true,
      "source": "表2 第6页或图1 第3页或结果文本第5页",
      "has_numeric_estimate": true,
      "statistical_method": "Cox或logistic或linear或Poisson或LMM或GEE或t-test或ANCOVA或MR_IVW或KM或mediation或其他",
      "adjustment_variables": ["年龄", "性别", "BMI"],
      "priority": "primary或secondary或exploratory",
      "notes": "可选补充说明"
    }
  ]
}
```

### 字段说明

- **statistical_method**: 识别论文对该边使用的实际统计模型。仔细阅读方法部分。常见值：
  - `"Cox"` — Cox比例风险/生存分析
  - `"logistic"` — Logistic回归
  - `"linear"` — 线性回归/OLS
  - `"Poisson"` — 泊松回归/负二项回归
  - `"LMM"` — 线性混合模型/随机效应
  - `"GEE"` — 广义估计方程
  - `"t-test"` — 独立/配对t检验
  - `"ANCOVA"` — 协方差分析
  - `"MR_IVW"` — 孟德尔随机化逆方差加权
  - `"KM"` — Kaplan-Meier/对数秩检验
  - `"mediation"` — 因果中介分析/路径分析
  - `"other"` — 如果以上都不适用，在notes中说明

- **adjustment_variables**: 模型调整的协变量/混杂因素列表。从论文的方法或表注中提取。如果论文说"调整了年龄、性别、BMI、吸烟和教育"，写作 `["age", "sex", "BMI", "smoking", "education"]`。如果未调整或未说明，写作 `[]`。

### 规则 8: Edge 优先级标注

为每个 edge 标注 `priority` 字段，用于后续筛选：

- `"primary"` — 论文的主要结局（出现在摘要、研究目标、或 primary outcome 中）
- `"secondary"` — 论文的次要结局（secondary outcome、safety endpoint、生物标志物、次要终点）
- `"exploratory"` — **仅用于**下列情形：post-hoc 亚组分析、敏感性分析、补充材料的额外分析、或明显的 "exploratory" 标注

**默认规则**：如果不确定是 secondary 还是 exploratory，**一律填 "secondary"**。下游管道会根据 priority 过滤，错误地填 "exploratory" 会导致合法终点被丢弃。次要终点（LDL、甘油三酯、CRP、BMI、日卡路里摄入等）**不是** exploratory，应填 "secondary"。

### 规则 9: E3（重复测量 / 前后变化）识别

`statistical_method` 如果是下列情形，对应的 equation_type 应为 **E3**（而非 E1）：
- 论文报告 baseline + follow-up 数据并用 **time × group interaction** 检验干预效应
- 使用 LMM / GEE / ANCOVA change score 分析纵向数据
- 报告 "change from baseline" 或 "Δ (Y)" 作为主要结果

这种情况下 `statistical_method` 应是 `"LMM"` / `"GEE"` / `"ANCOVA"` 而非 `"linear"`。

### 规则 10: n（样本量）必须是**当前 edge 对应的样本量**

如果论文按亚组或 completer 分析报告效应值，`n` **不是**全样本量，而是该效应值实际计算时使用的样本量：

- ❌ Rassy 论文 obesity 子组 HR 时填 n=438,583（全样本）
- ✅ 该 HR 使用的 n=107,041（obesity 子组）
- 定位方法：看 estimate 数值**旁边**的 n 或 events 计数，不要看 Methods 或 Figure 1 流程图里的总 n

## 输出之前，请你思考和自检

1. 是否扫描了论文中的**每个表格**？
2. 是否扫描了论文中的**每个图表**？
3. 是否提取了**无统计学显著性**的结果？
4. 对于亚组分析，是否为每个亚组 × 每个Y创建了单独的边？
5. 是否包含了次要结局和安全性终点？
6. 每个边的Y是否包含完整限定符（单位、时间窗口等）？
7. 是否**跳过**了表1基线平衡检查行（规则6）？
8. 对于交互效应，是否避免了冗余的三重报告（规则7）？
9. 是否通过阅读方法部分填写了`statistical_method`？
10. 是否从模型描述或表注中填写了`adjustment_variables`？
11. 对于RCT/干预研究，是否**优先**组间差异（规则4b）？**同时**是否保留了只报告组内变化或 group means 的次要终点（规则4b）？
12. 是否为每个 edge 标注了 `priority`（规则8），且次要终点用的是 "secondary"（不是 "exploratory"）？
13. 同 (X, Y) 出现多次时，Y 或 subgroup 中是否包含时间点/亚组/模型区分符（规则 4c）？
14. X 字段是否包含对照/参照组（"vs Control" / "vs 7h reference" 等）？（核心概念节）
15. 若论文用 baseline + follow-up + time×group 交互，`statistical_method` 是否为 LMM/GEE/ANCOVA 而非 linear？（规则 9）
16. `n` 是否为当前 edge 实际对应的样本量（亚组 n），而非全样本量？（规则 10）
17. **分类暴露的每个非参照层次是否都生成了独立 edge**（规则 4a）？例如 sleep duration 五分类（≤5 / 5–6 / 7 ref / 8–9 / ≥10）应有 4 个 edges，不是 1 个。**这是最常见的漏召回来源。**
18. Y 字段是否包含测量形式（change / incidence / absolute）+ 时间窗 + 单位（规则 4d）？例如 RCT 的 Y 是 `"HbA1c change (V3-V1, %)"` 而非 `"HbA1c (%)"`。
