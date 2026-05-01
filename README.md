# 证据边缘提取工具 3.35

从学术论文 PDF 中自动提取因果关系边，并映射到 HPP（Human Phenotype Project）数据字典，生成符合统一模板的 JSON 输出。

**当前版本** (3.31–3.35 累积) 提供两套模式：

- `--workflow-mode legacy`（默认）：行为同 3.30，所有保护性逻辑（占位符过滤、Pi 软共识、IMRAD 召回、Phase C fill-only autofix、equation_type 强校验等）始终启用。
- `--workflow-mode evidence_first`：在 legacy 之上叠加 Step 1.6 研究价值筛选、Step 2.1 确定性尺度转换、Step 4 Phase A 新增 6 条 hard rules（CI 包点估计/grade 降级/no-quant 丢弃/equation_type↔model 一致性/statistic_type 语义/evidence_text 追溯）、case series / 单臂研究通用支持。

完整版本历史见 `CHANGELOG.md`（如果有），或 `git log` 看 commit。

---

## 核心流程

```
PDF 论文
   │
   ▼
┌──────────────────────────┐
│  Step 0: 分类             │  → interventional / causal / mechanistic / associational
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  Step 1: 提取边           │  → 枚举所有 X→Y 统计关系边
│  + baseline 过滤          │  → 移除 Table 1 不显著的人口统计行
│  + 模糊去重               │  → 基于 token Jaccard overlap 移除近重复边
└────────┬─────────────────┘
         │  edges: [{X, Y, Z, estimate, ci, ...}]
         ▼
┌──────────────────────────┐
│  Step 1.5: 预验证（无 LLM）│  → Phase A: 数值硬核验（论文原文中能否找到）
│                          │  → Phase B: 确定性推导 equation_type/model/mu/theta
│                          │  → 预计算 theta_hat 和 CI（对数尺度转换）
└────────┬─────────────────┘
         │  每条边附带 _prevalidation 元数据
         ▼
┌──────────────────────────┐
│  Step 2: 填充模板         │  → HPP RAG 检索（每条边独立检索相关字段）
│  + 预验证值强制覆盖       │  → LLM 单次调用填充模板（无重试）
│  + 数值硬核验（post-S2）  │  → 论文 anchor 数集比对，幻觉值置 null
│  + 语义验证（仅报告）      │  → 10 项语义检查，结果写入 _validation
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  Step 2.5: 强模型补值     │  → 可选：用更强/更贵模型重提取 null 效应值
│  (strong_client)          │  → 触发条件：reported_effect_value/theta_hat/CI 为 null
│                          │  → 补提值仍须通过 hard-match 才会写入
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  Step 3: 审查             │  → 3a: HPP Rerank（LLM 精排映射候选）
│                          │  → 3b: 跨边一致性检查 + 模糊重复检测
│                          │  → 3c: Spot-check（LLM 核对抽样数值）
│                          │  → 3d: 质量报告 + action items
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  Step 4: 内容审计         │  → Phase A: 确定性检查（协变量/数值/HPP 幻觉检测）
│                          │  → Phase A 自动修复（移除幻觉协变量、多余字段）
│                          │  → Phase B: LLM 审计（Y 标签、协变量语义、样本量）
│                          │  → Phase C: 把 Phase B 的 suggested_fix 安全应用回边
│                          │    （fill-only 默认；--phase-c-aggressive 显式开覆盖）
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  最终 schema 清洗         │  → 删除内部元数据、规范化 dataset ID、
│                          │    限制字段白名单、mu.type 前缀标准化
└────────┬─────────────────┘
         │
         ▼
   edges.json + step3_review.json + step4_audit.json
```

### 数据流概览

```
Step 1.5 预验证输出
  → 每条边附带 _prevalidation（equation_type, model, mu, theta_hat, ci, id_strategy）
  ↓
Step 2 每条边的输出
  → LLM 填充模板结构 JSON
  → 预验证值强制覆盖（equation_type, model, mu, theta_hat, ci, adjustment_set）
  → 数值硬核验：从论文提取 anchor 数集，幻觉值置 null
  → 附加 _validation 元数据（语义检查结果，仅报告不触发重试）
  ↓
所有边汇总成列表
  ↓
Step 2.5 强模型补值（可选）
  → 对 reported_effect_value / theta_hat / CI 为 null 的边，用更强模型重提取
  → 补提值仍须通过 hard-match 验证
  ↓
Step 3a rerank → 原地修改 hpp_mapping.X 和 hpp_mapping.Y 的 dataset/field
Step 3b 一致性检查 + 模糊重复检测 → 只读
Step 3c 抽查数值 → 只读（批量失败时自动降级为逐边检查）
  ↓
Step 4 Phase A → 自动移除幻觉协变量和多余字段
Step 4 Phase B → LLM 审计，生成 issues 列表（每条带 suggested_fix）
Step 4 Phase C → 默认 ON，把 Phase B suggested_fix 安全写回边（fill-only：
                  只填 None / [] / [None,None] 等空值，不覆盖现有非空值）
  ↓
最终 schema 清洗 → 删除 _validation 等内部字段，执行白名单过滤
  ↓
edges.json         ← 最终输出
step3_review.json  ← Step 3 质量报告
step4_audit.json   ← Step 4 审计报告
```

### 单边约束

每条边 = **一个 X → 一个 Y**。同一论文中同一暴露变量对多个结局的效应会被拆分为多条独立的边，各自生成独立的 JSON 对象。

---

## 项目结构

```
.
├── src/
│   ├── __init__.py            # 公共导出
│   ├── pipeline.py            # 七步流水线（Step 0/1/1.5/2/2.5/3/4）+ 最终 schema 清洗
│   ├── llm_client.py          # LLM 客户端（OpenAI 兼容 API，含独立 vision client，支持 minimax reasoning_split）
│   ├── ocr.py                 # PDF → 图片 → GLM-OCR → Markdown
│   ├── hpp_mapper.py          # HPP 数据字典 RAG 检索模块
│   ├── template_utils.py      # 模板加载、合并、校验、自动修复
│   ├── edge_prevalidator.py   # Step 1.5 预验证：硬核验 + 确定性元数据推导 + reasoning_chain
│   ├── semantic_validator.py  # 语义正确性验证 + 公式检查 + 边去重 + 双方程一致性
│   ├── review.py              # Step 3 审查：rerank、一致性检查、spot-check、质量报告
│   ├── audit.py               # Step 4 内容审计：确定性 Phase A（A1-A13）+ LLM Phase B
│   └── gt_loader.py           # GT 参考数据加载：few-shot 示例 + 错误模式摘要
├── prompts/                   # LLM 提示词模板（.md 文件）
│   ├── step0_classify.md
│   ├── step1_edges.md
│   ├── step2_fill_template.md   # 含 parameters/reason 溯源填写规范
│   └── step4_content_audit.md   # 含 8 类审核模式（含溯源验证）
├── templates/                 # HPP 模板和数据字典
│   ├── hpp_mapping_template.json                    # 带溯源字段的模板（parameters, reason）
│   └── pheno_ai_data_dictionaries_simplified.json   # HPP 数据字典（35 datasets, ~2779 fields）
├── reference/                 # 参考 GT 数据（可选，存在时自动加载）
│   ├── case_1/
│   │   └── 10683000_combined.md           # 论文 OCR 全文（不注入 prompt）
│   ├── extract_error_patterns.py          # 离线工具：从标注 GT 提取错误模式
│   └── error_patterns.json                # 聚合错误模式（注入 Step 4 Phase B）
├── batch_run.py               # 唯一入口：批处理脚本（支持单文件 / 平铺 / 子文件夹 batch）
├── requirements.txt
└── .env                       # API 配置
```

---

## 环境配置

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入以下内容：
#   OPENAI_API_KEY=your_api_key
#   OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
#   DEFAULT_MODEL=glm-5
#   VISION_MODEL=glm-4.6v
```

---

## 运行方式

### 两条主命令

```bash
# A. 完整流程（生产配置）
python batch_run.py -i /mnt/yuxuan/nature_causal_pdf/S \
  --batches {00..10} -o output_0430_evfirst --max-workers 8 \
  --workflow-mode evidence_first \
  --ocr-dir /mnt/yuxuan/causal_agent/cache_ocr

# B. 只要"抽边" — 停在 Step 2.1，省 70-80% LLM 成本
python batch_run.py -i /mnt/yuxuan/nature_causal_pdf/S \
  --batches {00..10} -o output_0430_evfirst --max-workers 8 \
  --workflow-mode evidence_first \
  --stop-after step2_1 \
  --ocr-dir /mnt/yuxuan/causal_agent/cache_ocr
# 事后接续：去掉 --stop-after 重跑同一 -o，Step 2 走 step2_partial 缓存不重跑
```

| 项目 | A 完整 | B 只抽边 |
|---|---|---|
| 步骤 | 0 → 1 → 1.5 → 1.6 → 2 → 2.1 → 2.5 → 3 → 4 → final | 0 → 1 → 1.5 → 1.6 → 2 → 2.1 |
| LLM 成本 | 全 | 省 70-80% |
| edges.json 数字填充 | ✓ | ✓ |
| Step 3/4 审计 + Phase C 自动修 | ✓ | ✗ |

**目标发表 / 高准确率 → A；快速看边质量 / draft → B**。

### 输入目录结构

```
/mnt/nature_causal_pdf/S/
├── 80/                  # batch 子文件夹，--batches 80 跑这个
│   ├── 12345678.pdf
│   └── ...
└── 81/
```

输出按 batch 分目录：`output/<batch>/<paper_stem>/{edges.json, step*.json}`。也支持平铺模式（`-i` 目录下直接放 PDF）。

### CLI flags

| flag | 默认 | 说明 |
|---|---|---|
| `-i / -o / --max-workers / --batches` | — | 输入/输出/并发/批次 |
| `--ocr-dir` | `./cache_ocr` | OCR 缓存路径，强烈建议显式绝对路径 |
| `--resume / --no-resume` | ON | 跳过已完成步骤 |
| `--workflow-mode` | `legacy` | `evidence_first` 启用 Step 1.6 / 2.1 / 新 hard rules |
| `--stop-after STEP` | `all` | `step1 / step1_5 / step1_6 / step2 / step2_1 / step2_5 / step3 / step4 / step5 / all`。Step 2.5 前停止时保留 `step2_partial.json` 供 resume |
| `--phase-c-autofix / --no-phase-c-autofix` | ON | Phase C fill-only 自动填空缺失值 |
| `--phase-c-aggressive` | OFF | 允许 Phase C 覆盖非空值（高风险） |
| `--defer-hpp-mapping` | OFF | HPP 映射推迟到 Step 5 |
| `--final-renumber-edge-id` | OFF | edge_id 重排为 `#1..#N` |
| `--type` | auto | 跳过 Step 0，强制 `interventional/causal/mechanistic/associational` |
| `--hpp-dict` | auto | HPP 字典 JSON（默认 `templates/`） |
| `--reference-dir / --error-patterns` | auto | GT 参考目录与错误模式 |
| `--no-validate-pages` | — | 跳过 OCR 尾页 vision 过滤 |
| `--dpi` | 400 | PDF→图片 DPI |
| `--api-key / --base-url / --model` | env | LLM 配置覆盖 |

---

## 输出说明

每个 PDF 在输出目录下生成一个同名文件夹：

```
output/
└── paper_name/
    ├── step0_classification.json     # 论文分类结果
    ├── step1_edges.json              # Step 1 提取的边列表（已去重）+ 论文元数据
    ├── step1_5_prevalidation.json    # Step 1.5 预验证报告（硬核验 + 软核验 + equation_type 分布）
    ├── edges.json                    # 最终输出（经 Step 2/2.5/3/4 + schema 清洗后的干净 JSON）
    ├── step2_5_recovery.json         # Step 2.5 补值统计（可选，仅 strong_client 启用时生成）
    ├── step3_review.json             # Step 3 质量报告（一致性检查、spot-check、action items）
    └── step4_audit.json              # Step 4 审计报告（Phase A + Phase B issues + 自动修复记录）
```

批量处理额外生成 `_batch_summary.json`。

### 边的数据结构示例（evidence_first 模式）

**示例 1：model_effect 边**（来自 GWAS / MR / Cox / 回归类论文，绝大多数）

来源：`output/.../80/27089180/edges.json` 中的 `EV-2016-Day#3`，论文 "Physical and neurobehavioral determinants of reproductive onset and behavior" (Day 2016 Nat Genet)。

```json
{
  "edge_id": "EV-2016-Day#3",
  "paper_title": "Physical_and_neurobehavioral_determinants_of_reproductive_onset_and_behavior",
  "equation_type": "E1",
  "epsilon": {
    "rho": {
      "X": "Earlier puberty timing (1 year decrease, genetically predicted)",
      "Y": "Number of children (men)",
      "Z": []
    },
    "Pi": "adult_general",
    "mu": {"core": {"family": "difference", "scale": "identity", "type": "BETA"}},
    "alpha": {"id_strategy": "MR", "assumptions": ["exchangeability", "consistency"], "status": "partially_identified"}
  },
  "literature_estimate": {
    "theta_hat": 0.37,                         // ← Step 2.1 算出来的 (BETA→identity)
    "ci": [0.25, 0.49],                        // ← 同上 (identity scale 不变)
    "p_value": 5.8e-08,
    "n": 125667,
    "design": "MR",
    "model": "MR_IVW"
  },
  "equation_formula_reported": {
    "reported_effect_value": 0.37,             // ← 论文原始 BETA
    "reported_ci": [0.25, 0.49],
    "reported_p": 5.8e-08,
    "effect_measure": "BETA",
    "model_type": "MR_IVW"
  },
  "_step1_evidence": {                         // ← evidence_first 才有
    "statistic_type": "model_effect",
    "evidence_text": "Genetically predicted earlier puberty timing promoted earlier...",
    "source_context": "Results section 'Biological determinants of AFS'"
  }
}
```

**示例 2：descriptive_estimate 边**（来自单臂 / case series 类论文）

来源：`output/.../80/10784580/edges.json` 中的 `EV-2000-Morgner#1`，论文 "Helicobacter heilmannii-Associated Primary Gastric Lymphoma" (case series, n=5)。

```json
{
  "edge_id": "EV-2000-Morgner#1",
  "paper_title": "Helicobacter_heilmannii-Associated_Primary_Gastric_Lymphoma",
  "equation_type": "E1",
  "epsilon": {
    "rho": {"X": "H._heilmannii_eradication_(omeprazole+amoxicillin+clarithromycin)", "Y": "Complete_lymphoma_remission", "Z": []},
    "Pi": "case_series_population",
    "mu": {"core": {"family": "difference", "scale": "identity", "type": "proportion"}},
    "alpha": {"id_strategy": "descriptive", "assumptions": [], "status": "not_identified"}
  },
  "literature_estimate": {
    "theta_hat": null,                         // ← Step 2.1 强制 null：单臂没有 theta
    "ci": [null, null],
    "p_value": null,
    "n": 5,
    "design": "case_series",
    "model": "descriptive"
  },
  "equation_formula_reported": {
    "reported_effect_value": 1.0,              // ← 5/5 患者达到 remission，比例 = 1.0 (100%)
    "reported_ci": [null, null],
    "reported_p": null,
    "effect_measure": "proportion",
    "model_type": "descriptive"
  },
  "_step1_evidence": {
    "statistic_type": "proportion",            // ← 关键！告诉下游"这是描述性比例不是 model effect"
    "evidence_text": "5/5 patients (100%) achieved complete lymphoma remission after triple therapy",
    "source_context": "Results, paragraph 2"
  }
}
```

**关键区别**：

| 字段 | 示例 1 (model_effect) | 示例 2 (descriptive) |
|---|---|---|
| `_step1_evidence.statistic_type` | `model_effect` | `proportion` |
| `literature_estimate.theta_hat` | `0.37` (Step 2.1 计算) | `null` (Step 2.1 强制清空) |
| `literature_estimate.ci` | `[0.25, 0.49]` | `[null, null]` |
| `literature_estimate.design` | `MR` | `case_series` |
| `epsilon.alpha.id_strategy` | `MR` | `descriptive` |
| `epsilon.alpha.assumptions` | `[exchangeability, consistency]` | `[]` |
| `epsilon.alpha.status` | `partially_identified` | `not_identified` |
| `equation_formula_reported.reported_effect_value` | `0.37` | `1.0` ✅（数据**没丢**） |

**结论**：

- **示例 1 全字段都有数字**——`theta_hat / ci / reported_effect_value` 都填好。
- **示例 2 `theta_hat=null` 但原始值 `reported_effect_value=1.0` 在 `equation_formula_reported` 里**——单臂研究因果识别上没有"theta"，但论文报告的真实数据没有任何丢失。下游消费时优先看 `reported_effect_value`。

### `_validation` 元数据（中间态）

Step 2 填充后，每条边附带验证元数据（最终输出前会被剥离）：

```json
{
  "_validation": {
    "is_format_valid": true,
    "is_semantically_valid": true,
    "retries_used": 0,
    "fill_rate": 0.85,
    "semantic_issues": [],
    "format_issues": [],
    "prevalidation": {
      "equation_type": "E2",
      "model": "Cox",
      "hard_check_passed": true
    }
  }
}
```

> **注意**：`_validation`、`_prevalidation`、`_hard_match`、`_step2_edge_index` 等都是 pipeline 内部的中间状态。最终写入 `edges.json` 前，`_final_schema_enforcement` 会剥离所有以 `_` 开头的字段。

---

## Reference GT（可选）

Pipeline 自动检测 `reference/` 目录下的人工验证 GT 数据，用途：

- **Step 2 few-shot**：注入 1-2 个代表性 edge 作 LLM 示例
- **Step 4 Phase B**：注入历史错误模式分布（从 GT 标注提取）

目录结构：

```
reference/
├── case_1/
│   ├── 10683000_combined.md            # 论文 OCR
│   └── 10683000_edges_verified.json    # 验证过的 edges
├── extract_error_patterns.py           # 离线工具
└── error_patterns.json                 # 错误模式库
```

`reference/` 不存在或为空时 pipeline 正常运行（无 few-shot / 无错误模式参考）。新增 GT case：人工核对 `edges.json` → 复制 OCR + edges 到 `reference/case_N/` → 跑 `extract_error_patterns.py` 更新模式库。

---

## 关键模块速查

| 模块 | 文件 | 作用 |
|---|---|---|
| Step 1.5 预验证 | `edge_prevalidator.py` | 无 LLM，确定性推导 equation_type / model / mu / theta_hat 候选；硬核验论文中的 estimate/CI/p_value 字符串匹配 |
| Step 2 模板填充 | `pipeline.py` + `template_utils.py` | 单次 LLM 调用填完整模板，无重试。预验证值强制覆盖；增量保存 `step2_partial.json` |
| Step 2.5 强模型补值 | `pipeline.py` | 可选。`strong_client` 配置时对 null 字段重抽，仍走 hard-match。evidence_first 强制 hard_match + 拒绝 confidence=low |
| 语义验证 | `semantic_validator.py` | 10 项检查（model↔eq_type、公式关键词、mu 内部一致性、theta_hat 量级、M/X2 条件字段、Z↔adjustment_set 等），仅报告不重试 |
| 边去重 | `semantic_validator.py` | 两级：Step 1 级 token Jaccard 0.75；Step 3 级 0.70 |
| Step 3 审查 | `review.py` | 占位符过滤、paper_title/edge_id canonicalize、Pi 软共识（无白名单）、HPP rerank 含降级保护、跨边一致性归一化、IMRAD 结构 spot-check |
| Step 4 内容审计 | `audit.py` | Phase A 19 项确定性检查（A1-A13 + A14-A19）+ Phase B LLM few-shot 审计 + Phase C 安全门 7 道的 fill-only autofix |
| Final schema 清洗 | `pipeline.py:_final_schema_enforcement` | 剥离 `_` 前缀字段；edge_id 标准化；字段白名单；Z 一致性；P 值规范化 |
| HPP RAG 检索 | `hpp_mapper.py` | 倒排索引 + 医学同义词扩展，每条边 ~1.5-3K tokens（vs 全量 20K），节省 85-90% |

### Step 4 Phase A 检查列表（速查）

| ID | 检查 |
|---|---|
| A1 | 协变量幻觉（Z 名必须在原文） |
| A2 | 数值幻觉（reported_value/CI/theta_hat 必须在原文） |
| A3 | study_cohort 中 is_reported=true 的值必须在原文 |
| A4 | X/Y 不能用 HPP 字段名替代论文原词 |
| A5 | literature_estimate / hpp_mapping 字段白名单 |
| A6 | 跨边 theta 重复（不同 Y 用了同一 theta = LLM 复制） |
| A7 | study_cohort 计算值检测 |
| A8 | parameters[].source 必须可追溯到 Table/Figure |
| A9 | 双方程一致性（efr.model_type ↔ lit.model、reported↔theta 尺度换算） |
| A10 | reported_ci 存在但 reported_effect_value=null → 清空两者 |
| A11 | p_value 字符串 `"< 0.001"` → float `0.001` |
| A12 | 公式无 γ 项时 Z 必须空 |
| A13 | rho.Z 空但 hpp_mapping.Z 有条目 → 清空 |
| **A14** | **CI 必须包住点估计**（reported_ci ⊃ reported_effect_value 等） |
| **A15** | **theta_hat 非空但 ci=null → 自动降 grade='C'** |
| **A16** | **theta+rev+p 全空 → 自动 drop edge** |
| **A17** | **equation_type↔model 一致**（E2 必须 Cox/KM/parametric…，E3 必须 LMM/GEE/ANCOVA…） |
| **A18** | **statistic_type 语义一致**（crude_rate/proportion 不能配 ratio+log；group_mean 不能带 theta；within_group_change 必须配 baseline） |
| **A19** | **evidence_text 必须含 reported_effect_value 数字（容差 0.5%）** |

A14-A19 是 evidence_first 模式新增；其中 A18-A19 仅 evidence_first（需要 `_step1_evidence` 字段）。

### Phase C 自动修复安全门（fill-only 默认）

```
1. severity == "error"（warning 永远不自动修）
2. suggested_fix 非空、非 "null" / "TBD" / ""
3. 类型相符（numeric / list / string 各自匹配）
4. 量级合理（number 改动 ≤ 100×）
5. 散文检测（含 should / extract from / 应 / 建议 等指示词的字符串拒）
6. 多选项检测（含 ` or ` / ` 或 ` 拒）
7. 句末标点（`.` / `。` 等结尾的字符串拒）
8. fill-only 模式额外：current_value 必须是 None / "" / [] / [None,None]
```

修复记录写入 `step4_audit.json:phase_c_fixes_applied`。`--phase-c-aggressive` 才允许覆盖现有非空值。
