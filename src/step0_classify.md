# Step 0: 文献类型分类

你是医学信息学研究员。请阅读提供的论文内容，判断其研究类型并给出分类依据。

## 分类规则（按优先级）

1) **interventional**（干预/RCT/临床试验）
   触发信号：
   - PubMed Publication Type含 "Randomized Controlled Trial"/"Clinical Trial"
   - 方法/摘要含：randomized, double-blind, placebo, allocation, trial, NCT注册号, crossover
   - 有明确的干预组 vs 对照组设计

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

## 冲突消解规则
- 同时具备"干预"和"因果（观察性）"信号 → **causal**
- 同时具备"因果"和"机制（中介）"信号 → **mechanistic**（secondary_tags加"mediation"）
- 仅有机制/中介而无因果识别 → **mechanistic**
- 其余 → **associational**

## 输出格式（仅JSON）
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
