# 证据边缘提取工具 2.19

从学术论文 PDF 中自动提取因果关系边，并映射到 HPP（Human Phenotype Project）数据字典，生成符合统一模板的 JSON 输出。

---

## 核心流程

```
PDF 论文
   │
   ▼
┌──────────────────┐
│  Step 0: 分类     │  → interventional / causal / mechanistic / associational
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Step 1: 提取边   │  → 枚举所有 X→Y 统计关系边
└────────┬─────────┘
         │  edges: [{X, Y, Z, estimate, ci, ...}]
         ▼
┌──────────────────┐
│  HPP RAG 检索     │  → 为每条边检索相关的 HPP 数据集字段（轻量级，非全量注入）
└────────┬─────────┘
         │  每条边附带精简的 hpp_context（~1-3K tokens）
         ▼
┌──────────────────┐
│  Step 2: 填充模板  │  → 用论文信息 + HPP 映射上下文填充 HPP 模板
└────────┬─────────┘
         │
         ▼
   edges.json（每条边一个完整的 HPP JSON 对象）
```

### 单边约束

每条边 = **一个 X → 一个 Y**。同一论文中同一暴露变量对多个结局的效应会被拆分为多条独立的边，各自生成独立的 JSON 对象。

---

## 项目结构

```
.
├── src/
│   ├── __init__.py          # 公共导出
│   ├── main.py              # CLI 入口
│   ├── pipeline.py          # 三步流水线（Step 0/1/2）
│   ├── llm_client.py        # LLM 客户端（OpenAI 兼容 API）
│   ├── ocr.py               # PDF → 图片 → GLM-OCR → Markdown
│   ├── hpp_mapper.py         # HPP 数据字典 RAG 检索模块
│   ├── template_utils.py     # 模板加载、合并、校验、自动修复
│   └── utils.py             # 工具函数（PDF 转图、base64、保存 JSON）
├── prompts/                  # LLM 提示词模板（.md 文件）
│   ├── step0_classify.md
│   ├── step1_edges.md
│   └── step2_fill_template.md
├── templates/                # HPP 模板和数据字典
│   ├── hpp_mapping_template.json                    # 带 // 注释的模板（LLM 阅读用）
│   └── pheno_ai_data_dictionaries_simplified.json   # HPP 数据字典（35 datasets, ~2779 fields）
├── batch_run.py              # 批处理脚本
├── requirements.txt
└── .env                      # API 配置
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
# 完整流程（Step 0 + 1 + 2）
python src/main.py full paper.pdf -o ./output

# 指定 HPP 数据字典（启用 RAG 检索）
python src/main.py full paper.pdf -o ./output \
  --hpp-dict templates/pheno_ai_data_dictionaries_simplified.json

# 跳过分类，强制指定类型
python src/main.py full paper.pdf --type associational -o ./output

# 单独运行某个步骤
python src/main.py classify paper.pdf
python src/main.py edges paper.pdf
```

### 批量处理

```bash
# 默认：处理 ./evidence_card 目录下的所有 PDF
python batch_run.py

# 自定义输入/输出目录 + 并发
python batch_run.py -i ./pdfs -o ./results --max-workers 3

# 强制指定类型
python batch_run.py --type interventional
```

### 常用参数

| 参数                    | 说明                         |
|-----------------------|----------------------------|
| `--model`             | 覆盖默认 LLM 模型名               |
| `--api-key`           | 覆盖环境变量中的 API Key           |
| `--base-url`          | 覆盖环境变量中的 Base URL          |
| `--hpp-dict`          | HPP 数据字典 JSON 路径（启用 RAG）   |
| `--ocr-dir`           | OCR 缓存目录（默认 `./cache_ocr`） |
| `--dpi`               | PDF 转图片 DPI（默认 200）        |
| `--no-validate-pages` | 跳过 OCR 尾页过滤                |

---

## 输出说明

每个 PDF 在输出目录下生成一个同名文件夹：

```
output/
└── paper_name/
    ├── step0_classification.json   # 论文分类结果
    ├── step1_edges.json            # Step 1 提取的边列表 + 论文元数据
    └── edges.json                  # Step 2 完整的 HPP 模板 JSON（数组，每条边一个对象）
```

批量处理额外生成 `_batch_summary.json`。

---

## 关键模块详解

### HPP RAG 检索（`hpp_mapper.py`）

**解决的问题**：HPP 数据字典有 35 个 dataset、2779 个字段（~77K 字符 / ~20K tokens）。如果每条边的 Step 2 调用都注入全量字典，会导致大量
token 浪费、上下文拥挤、无关字段干扰映射判断。

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
4. **灵活合并**：LLM 输出覆盖模板占位符，允许添加额外字段（`mapping_notes`、`reported_HR` 等）
5. **自动修复**：数据集 ID 下划线规范化、theta_hat 对数尺度转换、CI 自动转换、M/X2 条件删除
6. **校验**：关键字段完整性检查、命名一致性检查、类型检查

---

## 已知限制和 TODO

### 当前限制

- **上下文长度**：超长论文（>50 页）可能超出 LLM 上下文限制，当前截断到 500K 字符
- **HPP 映射精度**：基于关键词的 RAG 检索对复合变量、罕见指标的召回率有限
- **编码兼容性**：prompt 文件需确保 UTF-8 编码（已在 `_load_prompt` 中增加容错）

### TODO

- [ ] **LLM Rerank**：对 RAG 检索的 top-N 候选用 LLM 做精排（可选，每条边额外 ~500 tokens）
- [ ] **Step 2 拆分**：将模板填充拆为 2a（论文信息提取）+ 2b（HPP 映射），每步 prompt 更短更聚焦
- [ ] **评估框架**：对比 RAG vs 全量注入的映射准确率；对比不同 LLM 的填充质量
- [ ] **增量同义词表**：根据实际运行中的 `status=missing` 反馈扩展同义词
- [ ] **向量检索升级**：对字段描述做 embedding，在关键词检索效果不佳时回退到语义检索
