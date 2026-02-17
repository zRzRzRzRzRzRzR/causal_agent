# 证据边缘提取工具

从学术论文 PDF 中自动提取因果关系边的工具。使用 LLM 从 PDF 中提取证据关系，并生成符合 HPP 统一模板的 JSON 输出。

## 核心流程

- **Step 0**: 论文分类（interventional / causal / mechanistic / associational）
- **Step 1**: 枚举所有 X→Y 关系边
- **Step 2**: 为每条边填充 HPP 模板

## 环境配置

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量（参考 .env.example）
cp .env.example .env
# 编辑 .env，填入 API_KEY 和 BASE_URL
```

## 运行方式

### 单个 PDF 处理

```bash
# 完整流程（Step 0 + 1 + 2）
python src/main.py full paper.pdf -o ./output

# 跳过分类，强制指定类型
python src/main.py full paper.pdf --type interventional -o ./output

# 单独运行 Step 0（分类）
python src/main.py classify paper.pdf

# 单独运行 Step 1（提取边）
python src/main.py edges paper.pdf
```

### 批量处理

```bash
# 默认：处理 ./evidence_card 目录下的所有 PDF，输出到 ./output
python batch_run.py

# 自定义输入/输出目录
python batch_run.py -i ./pdfs -o ./results

# 强制指定类型（跳过分类）
python batch_run.py --type interventional

# 并发处理（默认单线程）
python batch_run.py --max-workers 3
```

## 输出说明

| 文件名                                | 内容                     |
|------------------------------------|------------------------|
| `{pdf名}_step0_classification.json` | Step 0 分类结果            |
| `{pdf名}_step1_edges.json`          | S tep 1 边列表            |
| `{pdf名}_edges.json`                | Step 2 完整的 HPP 模板 JSON |

批量处理会额外生成：
- `_batch_summary.json`：批处理汇总（成功/失败数、总边数、耗时等）

## 常用参数

| 参数                    | 说明                         |
|-----------------------|----------------------------|
| `--model `            | 覆盖默认的 LLM 模型名              |
| `--api-key`           | 覆盖环境变量中的 API Key           |
| `--base-url`          | 覆盖环境变量中的 Base URL          |
| `--ocr-dir`           | OCR 缓存目录（默认 `./cache_ocr`） |
| `--dpi`               | PDF 转图片的 DPI（默认 200）       |
| `--no-validate-pages` | 跳过 OCR 页面验证                |

## 项目结构

```
.
├── src/
│   ├── main.py          # CLI 入口
│   ├── pipeline.py      # 提取流水线
│   ├── llm_client.py    # LLM 客户端
│   └── ocr.py           # OCR 模块
├── prompts/             # LLM 提示词模板
├── templates/           # HPP 映射模板
├── batch_run.py         # 批处理脚本
└── requirements.txt
```
