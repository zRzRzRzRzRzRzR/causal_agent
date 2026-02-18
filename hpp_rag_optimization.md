# HPP Mapping 优化方案：从全量注入到 RAG 检索

## 1. 问题诊断

### 1.1 当前架构问题

```
Step2 Prompt = prompt_template + template_json (含全量 data_dict) + paper_text
```

**问题**：
- `pheno_ai_data_dictionaries_simplified.json` 有 35 个 dataset、2779 个字段、~77K 字符（约 2 万 tokens）
- 每条 edge 的 step2 调用都把全量字典塞进 prompt → 29 条 edge = 58 万 tokens 浪费在字典上
- GLM 模型上下文有限，字典 + 论文文本 + prompt 可能超限或挤压推理空间
- 大量不相关字段干扰 LLM 的映射判断（如论文讨论 BMI 和吸烟，却看到 fundus、ECG、lipidomics 等无关字段）

### 1.2 UnicodeDecodeError

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x80 in position 530
```

这个错误发生在 `_load_prompt` 读取 `.md` 文件时，说明 prompts 目录下某个 `.md` 文件包含非 UTF-8 编码的内容（可能是从 Word/PDF 复制过来的特殊字符）。需要检查并修复。

---

## 2. RAG 方案设计

### 2.1 总体思路

```
                    ┌─────────────┐
                    │  Step 1     │
                    │ 提取 edges  │
                    └──────┬──────┘
                           │ edges: [{X, Y, Z, ...}]
                           ▼
                    ┌─────────────┐
                    │ HPP Mapper  │  ← 新增独立模块
                    │ (RAG 检索)  │
                    └──────┬──────┘
                           │ 每条 edge 只附带相关字段
                           ▼
                    ┌─────────────┐
                    │  Step 2     │
                    │ 填充模板    │  ← prompt 更短、更精准
                    └─────────────┘
```

### 2.2 核心模块：`hpp_mapper.py`

```python
"""
hpp_mapper.py - 基于关键词/语义匹配的 HPP 字段检索器

不用向量数据库，用轻量级的关键词 + LLM rerank 方案。
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class FieldCandidate:
    dataset_id: str       # e.g., "055-lifestyle_and_environment"
    field_name: str       # e.g., "smoking_current_status"
    score: float          # 匹配得分
    match_reason: str     # 匹配原因


class HPPFieldIndex:
    """对数据字典建立倒排索引，支持快速检索。"""

    def __init__(self, dict_path: str):
        with open(dict_path, "r", encoding="utf-8") as f:
            self.raw_dict = json.load(f)

        # 构建倒排索引: token -> [(dataset_id, field_name), ...]
        self.inverted_index: Dict[str, List[Tuple[str, str]]] = {}
        # 构建正排: (dataset_id, field_name) -> dataset description
        self.field_registry: Dict[str, Dict] = {}

        self._build_index()

    def _build_index(self):
        for dataset_id, info in self.raw_dict.items():
            fields = info.get("tabular_field_name", [])
            # 用 dataset 名称本身也作为检索依据
            dataset_tokens = self._tokenize(dataset_id)

            for field in fields:
                key = f"{dataset_id}::{field}"
                self.field_registry[key] = {
                    "dataset_id": dataset_id,
                    "field_name": field,
                }

                # 对字段名分词并建立倒排
                tokens = self._tokenize(field) | dataset_tokens
                for token in tokens:
                    if token not in self.inverted_index:
                        self.inverted_index[token] = []
                    self.inverted_index[token].append((dataset_id, field))

    @staticmethod
    def _tokenize(text: str) -> set:
        """将 field_name/dataset_id 拆分为检索 token。"""
        # 分割下划线、连字符、数字前缀
        text = re.sub(r"^\d+[-_]", "", text)  # 去掉 "055-" 前缀
        parts = re.split(r"[_\-/\s]+", text.lower())
        # 过滤太短的 token
        return {p for p in parts if len(p) > 2}

    def search(self, query: str, top_k: int = 30) -> List[FieldCandidate]:
        """基于关键词匹配检索候选字段。"""
        query_tokens = self._tokenize(query)
        # 同义词扩展
        query_tokens = self._expand_synonyms(query_tokens)

        # 计算每个 (dataset, field) 的命中次数
        hit_count: Dict[str, int] = {}
        hit_tokens: Dict[str, set] = {}

        for token in query_tokens:
            matches = self.inverted_index.get(token, [])
            for dataset_id, field in matches:
                key = f"{dataset_id}::{field}"
                hit_count[key] = hit_count.get(key, 0) + 1
                if key not in hit_tokens:
                    hit_tokens[key] = set()
                hit_tokens[key].add(token)

        # 排序: 命中 token 数 / 总 query token 数
        candidates = []
        for key, count in sorted(hit_count.items(), key=lambda x: -x[1]):
            info = self.field_registry[key]
            score = count / max(len(query_tokens), 1)
            candidates.append(FieldCandidate(
                dataset_id=info["dataset_id"],
                field_name=info["field_name"],
                score=score,
                match_reason=f"matched tokens: {hit_tokens[key]}"
            ))

        return candidates[:top_k]

    @staticmethod
    def _expand_synonyms(tokens: set) -> set:
        """领域同义词扩展。"""
        SYNONYMS = {
            "bmi": {"body", "mass", "index", "anthropometrics", "weight", "obesity"},
            "smoking": {"tobacco", "smoke", "cigarette", "smoker"},
            "alcohol": {"drinking", "ethanol", "drink"},
            "diet": {"dietary", "food", "nutrition", "fruit", "vegetable", "meat"},
            "exercise": {"physical", "activity", "sport", "fitness"},
            "hypertension": {"blood", "pressure", "systolic", "diastolic"},
            "diabetes": {"glucose", "insulin", "hba1c", "glycated"},
            "heart": {"cardiac", "cardiovascular", "ischemic", "coronary"},
            "kidney": {"renal", "nephro"},
            "cancer": {"tumor", "carcinoma", "adenocarcinoma", "malignant"},
            "sleep": {"insomnia", "apnea", "circadian"},
            "mood": {"depression", "anxiety", "mental", "psychological"},
            "weight": {"bmi", "obesity", "overweight", "body"},
            "mortality": {"death", "survival", "died"},
            "age": {"years", "born", "birth"},
            "sex": {"gender", "male", "female"},
            "ethnicity": {"race", "ethnic", "country", "birth"},
            "deprivation": {"socioeconomic", "townsend", "income"},
        }
        expanded = set(tokens)
        for token in tokens:
            if token in SYNONYMS:
                expanded |= SYNONYMS[token]
        return expanded


class HPPMapper:
    """为每条 edge 检索相关的 HPP 字段，生成精简的映射上下文。"""

    def __init__(self, dict_path: str, client=None):
        self.index = HPPFieldIndex(dict_path)
        self.client = client  # 可选：用 LLM 做 rerank
        self.raw_dict = self.index.raw_dict

    def get_context_for_edge(
        self,
        edge: Dict,
        paper_info: Dict = None,
        max_datasets: int = 8,
        max_fields_per_dataset: int = 15,
    ) -> str:
        """
        为单条 edge 生成精简的 HPP 映射上下文。

        返回一个 markdown 字符串，只包含与这条 edge 相关的数据集和字段。
        """
        # 1. 从 edge 中提取需要映射的变量名
        queries = self._extract_mapping_queries(edge)

        # 2. 对每个变量检索候选字段
        all_candidates: Dict[str, List[FieldCandidate]] = {}
        relevant_datasets = set()

        for role, query in queries.items():
            candidates = self.index.search(query, top_k=20)
            all_candidates[role] = candidates
            for c in candidates[:10]:
                relevant_datasets.add(c.dataset_id)

        # 3. 构建精简上下文
        context_parts = []
        context_parts.append("## Available HPP Datasets and Fields\n")
        context_parts.append(
            "Below are the datasets and fields most likely relevant to this edge. "
            "Use these to fill the `hpp_mapping` section.\n"
        )

        # 只输出相关 dataset 的字段
        for ds_id in sorted(relevant_datasets)[:max_datasets]:
            fields = self.raw_dict.get(ds_id, {}).get("tabular_field_name", [])
            context_parts.append(f"\n### `{ds_id}`")
            context_parts.append(
                f"Fields ({len(fields)} total, showing relevant): "
                + ", ".join(fields[:max_fields_per_dataset])
            )
            if len(fields) > max_fields_per_dataset:
                context_parts.append(f"  ... and {len(fields) - max_fields_per_dataset} more")

        # 4. 附加检索建议
        context_parts.append("\n\n## Retrieval Suggestions\n")
        for role, candidates in all_candidates.items():
            if not candidates:
                context_parts.append(f"- **{role}**: No matching fields found → status=missing")
                continue
            top3 = candidates[:3]
            suggestions = "; ".join(
                f"`{c.dataset_id}`.`{c.field_name}` (score={c.score:.2f})"
                for c in top3
            )
            context_parts.append(f"- **{role}**: {suggestions}")

        return "\n".join(context_parts)

    def _extract_mapping_queries(self, edge: Dict) -> Dict[str, str]:
        """从 edge 提取各角色的检索查询。"""
        queries = {}

        # X (exposure)
        x_name = edge.get("X", "")
        if x_name:
            queries["X"] = x_name

        # Y (outcome)
        y_name = edge.get("Y", "")
        if y_name:
            queries["Y"] = y_name

        # C / Z (covariates) - 如果有的话
        covariates = edge.get("C", "") or edge.get("Z", "")
        if isinstance(covariates, list):
            for i, z in enumerate(covariates[:5]):
                queries[f"Z_{z}"] = str(z)
        elif covariates:
            queries["Z"] = str(covariates)

        return queries


# ============================================================
# 集成到 pipeline 的方式
# ============================================================

def create_hpp_context(
    edge: Dict,
    dict_path: str,
    client=None,
) -> str:
    """
    便捷函数：为单条 edge 生成 HPP 映射上下文。
    在 pipeline.step2_fill_one_edge 中调用。
    """
    mapper = HPPMapper(dict_path=dict_path, client=client)
    return mapper.get_context_for_edge(edge)
```

### 2.3 Pipeline 集成改造

修改 `pipeline.py` 中的 `step2_fill_one_edge`：

```python
# === 修改前 ===
# template_json_str 包含完整数据字典（~2万tokens）
template_json_str = json.dumps(clean_tmpl, indent=2, ensure_ascii=False)

# === 修改后 ===
# 1. template 不再包含数据字典
template_json_str = json.dumps(clean_tmpl, indent=2, ensure_ascii=False)

# 2. 用 RAG 检索只获取相关字段（~1-2千 tokens）
from .hpp_mapper import create_hpp_context
hpp_context = create_hpp_context(
    edge=edge,
    dict_path="path/to/pheno_ai_data_dictionaries_simplified.json",
    client=client,
)

# 3. 注入到 prompt
replacements["{hpp_context}"] = hpp_context
```

对应 prompt 模板修改：

```markdown
## HPP Field Mapping Reference

{hpp_context}

## Instructions

Based on the above available fields, fill the `hpp_mapping` section.
For each variable (X, Y, Z), determine:
- `field`: the dataset ID (e.g., "002-anthropometrics")
- `dataset`: the specific field name(s) within that dataset
- `status`: one of "exact" | "close" | "tentative" | "missing"
- `notes`: brief justification for the mapping decision
```

---

## 3. Token 消耗对比

| 场景 | 每条 Edge 的字典 Tokens | 29 条 Edge 总计 |
|------|------------------------|----------------|
| **当前：全量注入** | ~20,000 | ~580,000 |
| **RAG 方案** | ~1,500-3,000 | ~50,000-87,000 |
| **节省** | ~85-90% | ~500K tokens |

---

## 4. 可选增强：LLM Rerank

如果关键词匹配不够精准，可以加一个轻量 LLM rerank 步骤：

```python
def rerank_with_llm(self, role: str, query: str,
                     candidates: List[FieldCandidate],
                     client: GLMClient) -> List[FieldCandidate]:
    """用 LLM 对 top-N 候选做精排。"""
    candidate_text = "\n".join(
        f"{i+1}. {c.dataset_id} / {c.field_name}"
        for i, c in enumerate(candidates[:10])
    )
    prompt = f"""
Given the variable "{query}" (role: {role}) from a medical research paper,
rank these candidate database fields by relevance (1=best match):

{candidate_text}

Reply as JSON: {{"rankings": [field_index, ...]}}
"""
    result = client.call_json(prompt)
    rankings = result.get("rankings", list(range(len(candidates))))
    return [candidates[i-1] for i in rankings if 0 < i <= len(candidates)]
```

这个 rerank 步骤是可选的，每条 edge 额外消耗约 500 tokens（比全量注入 2 万 tokens 便宜得多）。

---

## 5. 实施步骤

### Phase 1: 修复 Bug（立即）

```bash
# 1. 找到编码错误的文件
find prompts/ -name "*.md" -exec file {} \; | grep -v "UTF-8"

# 2. 转换编码
iconv -f GB2312 -t UTF-8 problematic_file.md > fixed.md

# 3. 或在代码中增加容错
def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    for enc in ["utf-8", "utf-8-sig", "gb2312", "latin1"]:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode: {path}")
```

### Phase 2: 实现 HPPFieldIndex（1-2天）

1. 创建 `src/hpp_mapper.py`
2. 建立倒排索引 + 同义词表
3. 单元测试：用 GT 中的 edge 验证检索召回率

### Phase 3: 改造 Pipeline（1天）

1. 修改 `step2_fill_one_edge`，注入 `{hpp_context}` 替代全量字典
2. 修改 prompt 模板，引导 LLM 基于检索结果做映射
3. 端到端测试

### Phase 4: 可选 - LLM Rerank + 评估（后续）

1. 对比 RAG vs 全量注入的映射准确率
2. 评估是否需要 rerank
3. 调优同义词表和检索参数

---

## 6. 额外建议

### 6.1 Step 2 Prompt 优化

当前 step2 prompt 承担了太多任务（填模板 + 做映射 + 判断方程类型），建议拆分为：

```
Step 2a: 纯论文信息提取（epsilon, literature_estimate 等）
Step 2b: HPP 映射（基于 RAG 检索结果）
Step 2c: 建模指令（equation_type, modeling_directives）
```

拆分好处：每步 prompt 更短、更聚焦，LLM 推理质量更高。

### 6.2 GT 文件问题

注意：你提供的 GT JSON 是 **sleep health 论文** 的（Zhang S 2025, USP→mortality），
不是 "Healthy Lifestyle Factors and Obesity" 论文（Rassy N 2023）的。
如果要评估模型在 obesity 论文上的表现，需要对应的 GT。
