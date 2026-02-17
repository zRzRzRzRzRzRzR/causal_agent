# Step 0: 论文研究类型分类

你是医学信息学研究员。请仔细阅读下面的论文全文，判断它属于哪种研究类型。

## 四种类型及判断规则（按优先级排列）

### 1. interventional（干预性研究 / RCT / 临床试验）
判断依据——论文中出现以下**任一**信号：
- 方法部分含有关键词：randomized, double-blind, placebo, allocation, clinical trial, crossover
- 有 NCT 注册号或试验注册信息
- 明确描述了干预组 vs 对照组的分组设计
- PubMed 文献类型标注为 "Randomized Controlled Trial" 或 "Clinical Trial"

### 2. causal（观察性因果推断）
判断依据——论文中出现以下**任一**信号：
- Mendelian randomization (MR), instrumental variable, 2SLS
- target-trial emulation, front-door/back-door criterion
- difference-in-differences, regression discontinuity
- propensity score, IPTW, g-formula, TMLE, negative control

### 3. mechanistic（机制/中介/通路分析）
判断依据——论文中出现以下**任一**信号：
- mediation analysis, indirect effect, ACME, ADE
- 明确的 X → M → Y 通路分析
- 论文核心目的是阐释某一生物机制（如通过何种中间变量起作用）

### 4. associational（相关/描述/一般观察性研究）
判断依据——以上三种都不满足时归为此类：
- 队列/横断面研究，仅报告调整后的 OR / HR / β / C statistic
- 没有因果推断方法，没有干预设计
- 仅描述关联性

## 冲突消解
- 如果同时有"干预"和"观察性因果"信号 → **causal**
- 如果同时有"因果"和"中介"信号 → **mechanistic**
- 如果仅有中介分析但无因果识别 → **mechanistic**
- 其余 → **associational**

## 输出格式（严格 JSON，不要输出任何其他内容）

```json
{
  "primary_category": "interventional 或 causal 或 mechanistic 或 associational",
  "secondary_tags": ["可选的补充标签，如 prediction, MR, mediation"],
  "category_signals": [
    "信号1：在论文中发现的具体证据",
    "信号2：在论文中发现的具体证据"
  ],
  "confidence": "high 或 medium 或 low",
  "rationale": "用1-2句话简要说明你为什么选择这个类型"
}
```

注意：
- `category_signals` 必须引用论文中的具体文字或位置，不要泛泛而谈
- 如果无法确定，confidence 标为 low 并在 rationale 中说明原因
