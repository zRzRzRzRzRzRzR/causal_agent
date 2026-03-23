# 证据边缘提取工具 3.20

从学术论文 PDF 中自动提取因果关系边，并映射到 HPP（Human Phenotype Project）数据字典，生成符合统一模板的 JSON 输出。

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
│  + 语义验证（仅报告）      │  → 10 项语义检查，结果写入 _validation
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
  → 附加 _validation 元数据（语义检查结果，仅报告不触发重试）
  ↓
所有边汇总成列表
  ↓
Step 3a rerank → 原地修改 hpp_mapping.X 和 hpp_mapping.Y 的 dataset/field
Step 3b 一致性检查 + 模糊重复检测 → 只读
Step 3c 抽查数值 → 只读（批量失败时自动降级为逐边检查）
  ↓
Step 4 Phase A → 自动移除幻觉协变量和多余字段
Step 4 Phase B → LLM 审计，生成 issues 列表
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
│   ├── pipeline.py            # 六步流水线（Step 0/1/1.5/2/3/4）+ 最终 schema 清洗
│   ├── llm_client.py          # LLM 客户端（OpenAI 兼容 API）
│   ├── ocr.py                 # PDF → 图片 → GLM-OCR → Markdown
│   ├── xml_reader.py          # JATS/NLM XML 文本提取（跳过 OCR）
│   ├── hpp_mapper.py          # HPP 数据字典 RAG 检索模块
│   ├── template_utils.py      # 模板加载、合并、校验、自动修复
│   ├── edge_prevalidator.py   # Step 1.5 预验证：硬核验 + 确定性元数据推导 + reasoning_chain
│   ├── semantic_validator.py  # 语义正确性验证 + 公式检查 + 边去重 + 双方程一致性
│   ├── review.py              # Step 3 审查：rerank、一致性检查、spot-check、质量报告
│   ├── audit.py               # Step 4 内容审计：确定性 Phase A（含 A8/A9）+ LLM Phase B
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
│   │   ├── 10683000_combined.md           # 论文 OCR 全文（不注入 prompt）
│   │   └── 10683000_edges_verified.json   # 人工验证的 GT edges（注入 Step 2 few-shot）
│   ├── case_2/
│   │   └── ...
│   ├── extract_error_patterns.py          # 离线工具：从标注 GT 提取错误模式
│   └── error_patterns.json                # 聚合错误模式（注入 Step 4 Phase B）
├── batch_run.py               # 批处理脚本（支持子文件夹 batch 模式）
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

### 单个 PDF 处理

```bash
# 完整流程（Step 0 + 1 + 1.5 + 2 + 3 + 4）
python src/main.py full paper.pdf -o ./output

# 指定 HPP 数据字典（启用 RAG 检索 + LLM Rerank）
python src/main.py full paper.pdf -o ./output \
  --hpp-dict templates/pheno_ai_data_dictionaries_simplified.json

# 跳过分类，强制指定类型
python src/main.py full paper.pdf --type associational -o ./output

# 单独运行某个步骤
python src/main.py classify paper.pdf
python src/main.py edges paper.pdf
python src/main.py audit paper.pdf -o ./output     # 单独运行 Step 4
```

### 批量处理

支持两种输入目录结构：

1. **平铺模式**：`-i ./pdfs`，目录下直接放 PDF 文件
2. **子文件夹模式**：`-i /mnt/nature_causal_pdf/S`，目录下按编号分子文件夹，每个子文件夹视为一个 batch

```
/mnt/nature_causal_pdf/S/
├── 98/
│   ├── 11041398.pdf
│   ├── 15765398.pdf
│   └── 16837098.pdf
└── 99/
    ├── 15781099.pdf
    ├── 16214599.pdf
    └── 16227999.pdf
```

输出也会按 batch 分目录：

```
output/
├── 98/
│   ├── 11041398/
│   │   ├── edges.json
│   │   └── ...
│   └── ...
├── 99/
│   └── ...
└── _batch_summary.json
```

```bash
# 平铺模式（向后兼容）：处理 ./evidence_card 目录下的所有 PDF
python batch_run.py

# 子文件夹模式：自动识别子文件夹为 batch
python batch_run.py -i /mnt/nature_causal_pdf/S -o ./results

# 每个 batch 最多处理 5 个 PDF
python batch_run.py -i /mnt/nature_causal_pdf/S --batch-size 5

# 只处理指定的子文件夹
python batch_run.py -i /mnt/nature_causal_pdf/S --batches 98 99

# 组合：只跑 98，每批 3 个，4 个并发 worker
python batch_run.py -i /mnt/nature_causal_pdf/S --batches 98 --batch-size 3 --max-workers 4

# 强制指定类型
python batch_run.py --type interventional
```

### 常用参数

| 参数                    | 说明                                          |
|-----------------------|---------------------------------------------|
| `-i`, `--input-dir`   | 输入目录，支持平铺 PDF 或含子文件夹（默认 `./evidence_card`） |
| `-o`, `--output-dir`  | 输出目录（默认 `./output`）                         |
| `--batch-size`        | 每个子文件夹最多处理 N 个 PDF（0 = 不限制，默认 0）           |
| `--batches`           | 只处理指定的子文件夹（如 `--batches 98 99`，默认全部）        |
| `--max-workers`       | 并发线程数（默认 1，即串行）                             |
| `--model`             | 覆盖默认 LLM 模型名                                |
| `--api-key`           | 覆盖环境变量中的 API Key                            |
| `--base-url`          | 覆盖环境变量中的 Base URL                           |
| `--hpp-dict`          | HPP 数据字典 JSON 路径（启用 RAG + Rerank）           |
| `--ocr-dir`           | OCR 缓存目录（默认 `./cache_ocr`）                  |
| `--dpi`               | PDF 转图片 DPI（默认 200）                         |
| `--no-validate-pages` | 跳过 OCR 尾页过滤                                 |
| `--xml`               | 输入为 JATS/NLM XML 格式，跳过 OCR                  |
| `--resume`            | 跳过已有缓存的步骤（step0/step1）                      |
| `--reference-dir`     | GT 参考数据目录（默认自动检测 `./reference/`）             |
| `--error-patterns`    | 错误模式 JSON 路径（默认自动检测 `./reference/error_patterns.json`） |

---

## 输出说明

每个 PDF 在输出目录下生成一个同名文件夹：

```
output/
└── paper_name/
    ├── step0_classification.json     # 论文分类结果
    ├── step1_edges.json              # Step 1 提取的边列表（已去重）+ 论文元数据
    ├── step1_5_prevalidation.json    # Step 1.5 预验证报告（硬核验 + 软核验 + equation_type 分布）
    ├── edges.json                    # 最终输出（经 Step 2/3/4 + schema 清洗后的干净 JSON）
    ├── step3_review.json             # Step 3 质量报告（一致性检查、spot-check、action items）
    └── step4_audit.json              # Step 4 审计报告（Phase A + Phase B issues + 自动修复记录）
```

批量处理额外生成 `_batch_summary.json`。

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

> **注意**：`_validation` 是 pipeline 内部的中间状态。最终写入 `edges.json` 前，`_final_schema_enforcement` 会剥离所有以 `_` 开头的字段。

---

## Reference GT 参考数据

Pipeline 支持从 `reference/` 目录加载人工验证的 GT（Ground Truth）数据，用于两个目的：

1. **Step 2 few-shot 示例**：从 GT edges 中选取 1-2 个代表性示例注入 LLM prompt，指导 `parameters`、`reason` 等溯源字段的填写格式
2. **Step 4 错误模式参考**：将历史高频错误模式（从 GT 标注中提取）注入 Phase B 审计 prompt，提醒 LLM 重点关注

### 目录结构

```
reference/
├── case_1/
│   ├── 10683000_combined.md           # 论文 OCR 全文（仅存档，不注入 prompt）
│   └── 10683000_edges_verified.json   # 人工验证的 GT edges
├── case_2/
│   └── ...
├── extract_error_patterns.py          # 离线工具：从标注 GT 提取错误模式
└── error_patterns.json                # 聚合后的错误模式目录
```

### GT edges 文件格式

`*_edges_verified.json` 是一个 JSON 数组，每个元素是一个完整的 edge 对象（与 `edges.json` 结构相同）。这些 edge 经过人工逐字段核对，确保 `parameters[].source`、`reason`、数值、协变量等全部正确。

### 数据流

```
reference/case_*/..._edges_verified.json
        │
        ▼  gt_loader.load_gt_cases()
        │  选择 1-2 个代表性 edge，裁剪为关键字段（~3K chars/edge）
        │
        ▼  注入 Step 2 prompt
   "## GT 参考示例（人工验证过的正确输出）
    ### 示例 1: EV-2000-Vermeulen#1
    ```json { equation_formula, parameters, reason, ... } ```"

reference/error_patterns.json
        │
        ▼  gt_loader.load_error_patterns()
        │  提取分类分布 + 每类 top 2 示例
        │
        ▼  注入 Step 4 Phase B prompt
   "## 历史错误模式分布（从 N 篇 GT 论文中提取）
    - covariate_hallucination: 12次 (28.6%)
    - numeric_hallucination: 8次 (19.0%)"
```

### 使用方式

**完全可选**——如果 `reference/` 目录不存在或为空，pipeline 正常运行，只是 Step 2 没有 few-shot 示例、Step 4 没有错误模式参考。

```bash
# 自动检测（reference/ 在项目根目录下）
python batch_run.py -i ./pdfs -o ./output

# 显式指定路径
python batch_run.py -i ./pdfs -o ./output \
  --reference-dir ./my_gt_data \
  --error-patterns ./my_gt_data/error_patterns.json
```

### 新增 GT case 的流程

```bash
# 1. 运行 pipeline 提取某篇论文
python src/main.py full paper.pdf -o ./output

# 2. 人工核对 output/paper_name/edges.json，修正错误
#    （可在 JSON 中添加 // 注释标注 ✅/⚠️/❌）

# 3. 创建 reference case 目录
mkdir -p reference/case_N
cp cache_ocr/paper_name/combined.md reference/case_N/
cp output/paper_name/edges.json reference/case_N/paper_name_edges_verified.json

# 4.（可选）如果有标注错误的 .jsonc 文件，重新提取错误模式
python reference/extract_error_patterns.py reference/ -o reference/error_patterns.json
```

`extract_error_patterns.py` 是一个独立的离线 CLI 工具，从带 ❌/⚠️ 标注的 `_edges_verified.jsonc` 文件中提取结构化错误模式，聚合后输出 `error_patterns.json`。Pipeline 运行时**不会**自动调用它——你需要在新增 GT case 后手动运行一次来更新错误模式库。

---

## 关键模块详解

### Step 1.5 预验证（`edge_prevalidator.py`）

**解决的问题**：原有 pipeline 在 Step 2 依赖 LLM 推导 equation_type、model、mu 等元数据，容易出错且需要昂贵的重试循环。v3 将这些确定性推导前移到 Step 1.5，完全无需 LLM 调用。

**两阶段验证**：

Phase A（硬核验）对每条边的报告数值（estimate、CI、p_value）在论文原文中做字符串匹配，支持多种数值格式（整数、小数、前导零省略等），未找到的数值标记为 missing。

Phase B（软核验）基于 edge 的 effect_scale、outcome_type、statistical_method 等字段确定性推导元数据，推导优先级为：特殊类型关键词（mediation→E4、interaction→E6、longitudinal→E3） > statistical_method > effect_scale > outcome_type。同时预计算 theta_hat 和 CI 到正确尺度（比率指标自动 log 变换）。

**reasoning_chain**：每一步推导决策都记录到 `reasoning_chain` 列表中（如 `"[eq_type] statistical_method='cox' → (E2, Cox) via method_to_eq"`），随 `_prevalidation` 写入每条边。Step 2 会将 reasoning_chain 注入 LLM prompt，作为 LLM 填写 `reason` 字段的参考；同时方便调试时回溯 equation_type 判断错误的根因。

推导结果写入 `edge["_prevalidation"]`，Step 2 的 LLM 调用会将这些值作为 guidance 注入 prompt，且在 LLM 输出后通过 `_apply_prevalidation_overrides` 强制覆盖，确保关键字段不受 LLM 幻觉影响。

### Step 2 模板填充（`pipeline.py` + `template_utils.py`）

v3 的 Step 2 对每条边做**单次 LLM 调用**，不再有重试循环。核心逻辑：

1. 为每条边独立调用 HPP RAG 检索，获取精简的字段上下文（~1-3K tokens）
2. 如果 `reference/` 目录存在 GT cases，加载 1-2 个 equation_type 匹配的 few-shot 示例（~3-6K chars）
3. 将预验证 guidance（含 reasoning_chain）、GT few-shot、模板（含注释）、HPP 上下文、论文全文组装为 prompt
4. LLM 输出后，通过 `build_filled_edge` 合并到模板结构
5. 强制覆盖预验证值（equation_type、model、mu、theta_hat、ci、id_strategy、adjustment_set）
6. 运行语义验证（10 项检查），结果写入 `_validation` 但不触发重试

### 语义验证（`semantic_validator.py`）

10 项语义检查用于评估 LLM 输出的逻辑正确性（在 v3 中仅用于报告，不触发重试）：

| 编号 | 检查项 | 说明 |
|------|--------|------|
| 1 | model ↔ equation_type | Cox→E2、logistic→E1 等，基于 prompt 定义的对应表 |
| 2 | 公式关键词检查 | E2 公式应含 λ(t)/exp(β)，E1 应含 logit 等 |
| 3 | 公式矛盾检测 | E1 公式不应出现 hazard/λ₀(t) 等 Cox 特征 |
| 4 | 公式结构验证 | E2 必须有 hazard 形式，E4 必须引用 M，E6 必须有交互项 |
| 5 | mu.type ↔ equation_type | E2 通常→HR，E1→OR/BETA/MD 等 |
| 6 | mu 内部一致性 | type/family/scale 三者匹配（HR→ratio→log） |
| 7 | theta_hat 量级检查 | log scale 下 \|theta\| > 3 报警，检测未做 log 变换 |
| 8 | 条件字段 M/X2 | E4 必须有 M，E6 必须有 X2，其余不应有 |
| 9 | alpha ↔ evidence_type | id_strategy 与论文分类一致 |
| 10 | rho.Z ↔ adjustment_set | 两处协变量列表内容一致 |

### 边去重（`semantic_validator.py`）

**两级去重**：

1. **Step 1 级**（`deduplicate_step1_edges`）：提取边后立即做模糊去重，基于 (X, Y, subgroup, effect_scale, C) 五元组的 token Jaccard overlap（阈值 0.75），保留有数值估计的边
2. **Step 3 级**（`detect_fuzzy_duplicates_step3`）：对填充后的边做二次检测，使用 epsilon.rho.X/Y + subgroup + mu.type，阈值 0.70，结果进入 quality report

### Step 3 审查（`review.py`）

| 子步骤 | 功能 |
|--------|------|
| 3a | HPP Rerank：LLM 从 top-6 RAG 候选中精排最佳映射 |
| 3b | 跨边一致性：精确重复检测、metadata 一致性、model↔equation_type 跨边矛盾、theta 量级/符号检查 |
| 3b+ | 模糊重复检测：token overlap 近重复边警告 |
| 3c | Spot-check：LLM 核对抽样边的数值是否与论文原文一致（批量失败时自动降级为逐边检查） |
| 3d | 质量报告：汇总所有检查结果，生成 action items（含语义错误标记） |

### Step 4 内容审计（`audit.py`）

**解决的问题**：格式校验和语义验证无法捕捉**内容层面**的错误——例如 LLM 幻觉出论文中不存在的协变量，或将数值填到了错误的 Y 变量下。Step 4 在 Step 3 之后运行，专注于内容准确性。

**Phase A（确定性，无 LLM）**：

| 检查 | 说明 |
|------|------|
| A1 协变量幻觉 | Z / adjustment_set 中每个变量名必须在论文原文中出现（支持下划线替换、token 60% 匹配） |
| A2 数值幻觉 | reported_effect_value、reported_ci、theta_hat（非 log 尺度时）、CI 必须在原文中能找到 |
| A3 样本数据验证 | study_cohort 中标记为 is_reported 的数值必须在原文出现 |
| A4 HPP 变量泄漏 | X/Y 变量名必须来自论文而非 HPP 字典字段名 |
| A5 多余字段检测 | literature_estimate 和 hpp_mapping 中不允许的字段 |

Phase A 检测到的 `action=remove` 和 `action=nullify` 问题会自动修复：幻觉协变量从 rho.Z、adjustment_set、hpp_mapping.Z 中移除；幻觉数值置为 null；多余字段直接删除。

**Phase B（LLM 审计）**：

以 few-shot 方式调用 LLM 对照论文原文逐字段核验，重点检查 8 类错误模式：Y 标签混淆、协变量语义错误、样本量混淆、统计方法误判、HPP 变量泄漏、参数溯源虚假（A8：parameters[].source 中引用的 Table/数值不存在）、双方程不一致（A9：efr.model_type ≠ lit.model、equation_type 不一致、reported_effect_value ↔ theta_hat 换算错误）、reason 引用不实。

如果 `reference/error_patterns.json` 存在，Phase B 会在 prompt 开头注入历史错误模式分布（如"covariate_hallucination 28.6%"）和典型示例，引导 LLM 审核员重点关注高频错误类型。支持分批处理（`max_edges_per_llm_call` 控制每次 LLM 调用的边数）。

### 最终 Schema 清洗（`_final_schema_enforcement`）

在写入 `edges.json` 前，对每条边执行确定性清洗：

- 剥离所有 `_` 前缀的内部字段（`_validation`、`_prevalidation` 等）
- `hpp_mapping`：仅保留 X/Y/Z/M/X2 顶层 key，每个映射对象仅保留 name/dataset/field/status；E4 以外的边强制 M=null，E6 以外的边强制 X2=null
- `literature_estimate`：仅保留 theta_hat/ci/ci_level/p_value/n/design/grade/model/adjustment_set/equation_type/equation_formula/reason
- `equation_formula_reported`：仅保留 equation/source/model_type/link_function/effect_measure/reported_effect_value/reported_ci/reported_p/X/Y/Z/parameters/reason
- `equation_formula`：仅保留 formula/parameters
- Dataset ID 格式规范化（下划线 → 连字符，如 `021_medical` → `021-medical`）
- mu.core.type 标准化（HR + log scale → logHR）

### HPP RAG 检索（`hpp_mapper.py`）

**解决的问题**：HPP 数据字典有 35 个 dataset、2779 个字段（~77K 字符 / ~20K tokens）。如果每条边的 Step 2 调用都注入全量字典，会导致大量 token 浪费、上下文拥挤、无关字段干扰映射判断。

**方案**：轻量级关键词检索 + 同义词扩展，为每条边只检索相关的数据集和字段。

```
全量注入：每条边 ~20,000 tokens → 29 条边 = ~580K tokens
RAG 检索：每条边 ~1,500-3,000 tokens → 29 条边 = ~50-87K tokens
节省：~85-90%
```

**工作原理**：

1. **倒排索引**：对所有字段名和数据集名分词，建立 token → (dataset, field) 的倒排索引
2. **同义词扩展**：医学领域同义词表（如 `smoking` → `tobacco, cigarette, nicotine`；`kidney` → `renal, nephro, creatinine`）
3. **规则强制纳入**：疾病结局自动纳入 `021-medical_conditions`；生活方式变量自动纳入 `055-lifestyle_and_environment`
4. **评分排序**：直接匹配权重 2x，同义词匹配权重 1x
5. **输出**：精简的 Markdown 上下文（相关数据集及其字段 + 每个变量角色的映射建议）

### 模板系统（`template_utils.py`）

采用**模板优先**策略：

1. **加载模板**：读取 `hpp_mapping_template.json`（含 `//` 注释作为 LLM 提示）
2. **预填确定性字段**：`edge_id`、`literature_estimate` 部分值、`alpha.id_strategy` 等
3. **LLM 填充**：将模板 + 论文文本 + HPP 上下文发送给 LLM
4. **灵活合并**：LLM 输出覆盖模板占位符，允许添加额外字段
5. **自动修复**：数据集 ID 下划线规范化、theta_hat 对数尺度转换、CI 自动转换、M/X2 条件删除
6. **校验**：关键字段完整性检查、命名一致性检查、类型检查
