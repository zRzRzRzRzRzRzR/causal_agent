# 医学文献证据卡提取工具

使用 GLM 模型 (智谱AI) 从医学 PDF 文献中提取结构化证据卡。

## 安装

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt
```

## 配置

1. 获取智谱AI API Key: https://open.bigmodel.cn/
2. 复制 `.env.example` 为 `.env`
3. 填入你的 API Key

```bash
cp .env.example .env
# 编辑 .env，填入 ZHIPU_API_KEY
```

或直接修改 `config.py` 中的 `ZHIPU_API_KEY`

## 使用

### 1. 文献分类

判断文献属于哪种类型 (interventional/causal/mechanistic/associational):

```bash
python extract.py paper.pdf --step classify
```

### 2. 提取机制通路

对于 mechanistic 类型，提取所有 X→M→Y 通路:

```bash
python extract.py paper.pdf --step paths
```

### 3. 完整提取

自动执行分类、提取通路、生成证据卡:

```bash
python extract.py paper.pdf --step extract
```

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--model` | GLM 模型名称 | glm-4.7 |
| `--api-key` | 覆盖 API Key | 从环境变量读取 |
| `--step` | 执行步骤 | classify |

## 支持的 GLM 模型

| 模型 | 说明 |
|------|------|
| `glm-4.7` | 最新版本 (默认) |
| `glm-4-flash` | 快速/便宜 |
| `glm-4-plus` | 通用模型 |
| `glm-4-air` | 轻量级 |

## 项目结构

```
causal_agent/
├── config.py          # GLM API 配置
├── extract.py         # 主脚本
├── requirements.txt   # 依赖
├── prompt/
│   └── extract.md     # LLM 提示词模板
├── Evidence Card/     # 输出目录
└── .env.example       # 环境变量模板
```
