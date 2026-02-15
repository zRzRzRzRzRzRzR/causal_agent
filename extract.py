import os
import json
import base64
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from ocr import PDFExtractor
from openai import OpenAI
load_dotenv()

from config import ZHIPU_API_KEY, ZHIPU_BASE_URL, DEFAULT_MODEL, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS


def get_pdf_text(pdf_path: str) -> str:
    extractor = PDFExtractor()
    result = extractor.extract_structured(pdf_path)
    return result.get("markdown", result.get("text", ""))

class GLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ):
        self.api_key = api_key or os.getenv("ZHIPU_API_KEY", ZHIPU_API_KEY)
        self.base_url = base_url or os.getenv("ZHIPU_BASE_URL", ZHIPU_BASE_URL)
        self.model = model
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def call(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if response_format:
            kwargs["response_format"] = response_format

        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def call_with_pdf(
        self,
        prompt: str,
        pdf_path: str,
        system_prompt: Optional[str] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        pdf_base64 = self._encode_pdf(pdf_path)

        user_message = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:application/pdf;base64,{pdf_base64}"
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }
        messages.append(user_message)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    def _encode_pdf(self, pdf_path: str) -> str:
        """将 PDF 文件编码为 base64"""
        with open(pdf_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def extract_text_from_pdf(self, pdf_path: str) -> str:
        return get_pdf_text(pdf_path)


class EvidenceExtractor:
    def __init__(self, glm_client: GLMClient):
        self.client = glm_client
        self.prompts = self._load_prompts()

    def _load_prompts(self) -> Dict[str, str]:
        """从 prompt/extract.md 加载提示词"""
        prompt_path = Path(__file__).parent / "prompt" / "extract.md"
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 简单解析提示词（实际使用时可以按需解析）
        return {
            "full_content": content,
            "step0_classify": self._extract_section(content, "第0步"),
            "mechanistic_step1": self._extract_section(content, "类型一：Mechanistic", "Step 1:"),
            "mechanistic_step2": self._extract_section(content, "类型一：Mechanistic", "Step 2:"),
        }

    def _extract_section(self, content: str, section_marker: str, subsection: str = "") -> str:
        """从 Markdown 内容中提取指定章节"""
        # 简化版提取，实际可以更精确
        lines = content.split("\n")
        result = []
        in_section = False

        for i, line in enumerate(lines):
            if section_marker in line:
                in_section = True
                continue

            if in_section:
                if subsection and subsection not in lines[i-1] if i > 0 else "":
                    continue
                if line.startswith("## ") and section_marker not in line:
                    break
                result.append(line)

        return "\n".join(result)

    def classify_paper(self, pdf_path: str) -> Dict[str, Any]:
        """
        Step 0: 判断文献类型

        Returns:
            分类结果字典
        """
        prompt = """
你是医学信息学研究员。请阅读提供的PDF论文，判断其研究类型并给出分类依据。

**分类规则（按优先级）：**

1) **interventional**（干预/RCT/临床试验）
   触发信号：
   - PubMed Publication Type含 "Randomized Controlled Trial"/"Clinical Trial"
   - 方法/摘要含：randomized, double-blind, placebo, allocation, trial, NCT注册号
   - 有明确的干预组vs对照组设计

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

**冲突消解规则：**
- 同时具备"干预"和"因果（观察性）"信号 → **causal**
- 同时具备"因果"和"机制（中介）"信号 → **mechanistic**（secondary_tags加"mediation"）
- 仅有机制/中介而无因果识别 → **mechanistic**
- 其余 → **associational**

**输出格式（仅JSON）：**
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
"""

        pdf_text = self.client.extract_text_from_pdf(pdf_path)
        full_prompt = f"{prompt}\n\n**论文内容如下：**\n\n{pdf_text[:200000]}"  # 限制长度

        response = self.client.call(
            prompt=full_prompt,
            system_prompt="你是医学文献分析专家，严格按照 JSON 格式输出。",
            response_format={"type": "json_object"}
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # 尝试提取 JSON
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                return json.loads(match.group())
            raise ValueError(f"无法解析模型响应: {response}")

    def extract_mechanistic_paths(self, pdf_path: str) -> List[str]:
        """
        Mechanistic 类型 - Step 1: 提取机制通路

        Returns:
            通路列表
        """
        prompt = """
你是一名医学信息学研究员和严谨的信息抽取器。现在请你从提供的本地PDF论文中读取并提取该论文中所有被研究或提出的机制通路。

**任务：**
识别论文中涉及的所有因果机制链路（X→M→…→Y），其中：
- X 是论文关注的锚点暴露
- Y 是主要结局（疾病或生理指标）
- M 是中介变量（可能有多个，表示X影响Y的中间机制）

**提取内容：**
- 只提取论文正文和附录中明确提到的所有 X→M→…→Y 通路序列
- 保证顺序和符号与论文描述一致
- 包括直接的 X→Y 关系，或多级链条如 X→M1→M2→Y
- 如果论文讨论了多个独立通路，请逐一提取每条通路
- 不要遗漏附录或图表中提及的机制路径

**信息来源：**
只能参考提供的PDF内容，禁止使用任何外部知识或编造内容。

**输出格式（仅JSON数组）：**
```json
[
  "BMI → KDM-BA Acceleration → Cardiovascular Disease",
  "Waist Circumference → KDM-BA Acceleration → Cardiovascular Disease",
  "TyG Index → KDM-BA Acceleration → Stroke"
]
```
"""

        pdf_text = self.client.extract_text_from_pdf(pdf_path)
        full_prompt = f"{prompt}\n\n**论文内容如下：**\n\n{pdf_text[:200000]}"

        response = self.client.call(
            prompt=full_prompt,
            system_prompt="你是医学文献分析专家，严格按照 JSON 格式输出。",
            response_format={"type": "json_object"}
        )

        try:
            data = json.loads(response)
            if isinstance(data, dict) and "paths" in data:
                return data["paths"]
            if isinstance(data, list):
                return data
            return data
        except json.JSONDecodeError:
            match = re.search(r'\[[\s\S]*\]', response)
            if match:
                return json.loads(match.group())
            raise ValueError(f"无法解析模型响应: {response}")

    def extract_evidence_card(
        self,
        pdf_path: str,
        evidence_type: str,
        target: str,
    ) -> Dict[str, Any]:

        prompt = f"""
你是医学信息学研究员 + 严谨的信息抽取器，专注于从PDF文献中提取机制/中介分析研究的结构化证据。

**核心原则：**
- 仅使用PDF内容：正文 + 补充材料，**禁止**联网或使用任何外部信息
- 不确定即 null：对未明确的信息**禁止编造或推测**
- 完整性优先：提取**所有**符合条件的变量和效应值
- 可复现性：所有数值必须可追溯到原文**具体位置**

**目标机制通路：** {target}

**必提取模块：**

1) **paper（文献信息）**
   - title: 论文完整标题
   - journal: 期刊名称
   - year: 发表年份（整数）
   - pmid: PubMed ID（PDF中明确给出，否则 null）
   - doi: DOI号（PDF中明确给出，否则 null）
   - registry: 临床试验注册号（如NCTxxxxxxx，否则 null）
   - abstract: 基于论文摘要的英文总结（≤150词）

2) **provenance（证据溯源）**
   - figure_table: 如 ["Table 2 p.6", "Fig 3 p.5"]
   - pages: 整数数组
   - supplement: 是否使用补充材料（boolean）

3) **design（研究设计）**
   - type: "prospective cohort" | "retrospective cohort" | "cross-sectional" | "case-control" | ...
   - analysis: "causal mediation analysis" | "SEM" | "path analysis" | ...
   - n_total: 总样本量（整数）

4) **population（人群特征）**
   - eligibility_signature:
     - age: 年龄范围/中位数
     - sex: "both" | "male" | "female"
     - disease: 疾病状态
     - key_inclusions: 纳入标准数组
     - key_exclusions: 排除标准数组

5) **variables（变量定义）**
   - nodes: 数组，每个变量包含：
     - node_id: "local:变量简称"
     - label: 完整变量名
     - type: "state" | "event" | "intervention"
     - unit: 单位（如 "kg/m²", "years"）
     - system_tags: 系统标签数组

6) **roles（角色分配）**
   - X: 暴露变量node_id数组
   - M: 中介变量node_id数组
   - Y: 结局变量node_id数组
   - Z: 协变量node_id数组

7) **mediation_equations（中介方程）**
   针对每条X→M→Y通路，提取：
   - path: "X_label → M_label → Y_label"
   - total_effect: {{"estimate": 数值, "ci_lower": 数值, "ci_upper": 数值, "p": 数值, "scale": "OR|HR|RR|β"}}
   - direct_effect: 同上（NDE）
   - indirect_effect: 同上（NIE/ACME）
   - proportion_mediated: {{"estimate": 百分比, "ci_lower": 数值, "ci_upper": 数值}}

8) **identification（识别假设）**
   - assumptions: 中介分析假设数组（如sequential ignorability）

**输出格式（仅JSON）：**
见下方完整模板...
"""

        # 添加 JSON 模板
        json_template = """
```json
{
  "schema_version": "1",
  "evidence_id": "EV-[年份]-[暴露简称]-[中介简称]-[结局简称]",
  "paper": {
    "title": "",
    "journal": "",
    "year": 0,
    "pmid": null,
    "doi": "",
    "registry": null,
    "abstract": ""
  },
  "provenance": {
    "figure_table": [],
    "pages": [],
    "supplement": false
  },
  "design": {
    "type": "",
    "analysis": "",
    "estimand": null,
    "n_total": 0,
    "n_arms": null,
    "randomization": null,
    "blinding": null,
    "itt": null
  },
  "population": {
    "eligibility_signature": {
      "age": "",
      "sex": "",
      "disease": "",
      "key_inclusions": [],
      "key_exclusions": []
    }
  },
  "transport_signature": {
    "center": "",
    "era": "",
    "geo": "",
    "care_setting": "",
    "data_source": ""
  },
  "time_semantics": {
    "exposure_window": "",
    "baseline_window": "",
    "assessment_timepoints": [],
    "follow_up_duration": "",
    "effect_lag": "",
    "effect_duration": "",
    "temporal_level": "coarse|fine"
  },
  "variables": {
    "nodes": [
      {
        "node_id": "local:VAR_NAME",
        "label": "Full Variable Name",
        "type": "state|event|intervention",
        "unit": "unit_string",
        "unit_ucum": "ucum_code",
        "system_tags": ["TAG1", "TAG2"],
        "ontology": {
          "UMLS": "CUI_or_null"
        }
      }
    ]
  },
  "roles": {
    "X": ["local:EXPOSURE"],
    "M": ["local:MEDIATOR"],
    "Y": ["local:OUTCOME"],
    "Z": ["local:COVARIATE1", "local:COVARIATE2"]
  },
  "mediation_equations": [
    {
      "path": "Exposure → Mediator → Outcome",
      "contrast": "per 1-SD increase | Q4 vs Q1 | ...",
      "total_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "direct_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "indirect_effect": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "p": 0.0,
        "scale": "OR|HR|RR|β|RD"
      },
      "proportion_mediated": {
        "estimate": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0
      },
      "provenance": "Table X p.Y"
    }
  ],
  "identification": {
    "assumptions": [
      "Sequential ignorability assumption",
      "Temporal ordering assumption",
      "No exposure-induced mediator-outcome confounding"
    ]
  }
}
```
"""

        pdf_text = self.client.extract_text_from_pdf(pdf_path)
        full_prompt = f"{prompt}\n\n{json_template}\n\n**论文内容如下：**\n\n{pdf_text[:200000]}"

        response = self.client.call(
            prompt=full_prompt,
            system_prompt="你是医学文献分析专家，严格按照 JSON 格式输出。",
            response_format={"type": "json_object"}
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                return json.loads(match.group())
            raise ValueError(f"无法解析模型响应: {response}")


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="医学文献证据卡提取工具")
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="GLM 模型名称")
    parser.add_argument("--api-key", help="智谱AI API Key")
    parser.add_argument("--step", choices=["classify", "paths", "extract"], default="classify",
                        help="执行步骤: classify(分类), paths(提取通路), extract(完整提取)")
    args = parser.parse_args()
    client = GLMClient(api_key=args.api_key, model=args.model)
    extractor = EvidenceExtractor(client)

    if args.step == "classify":
        result = extractor.classify_paper(args.pdf)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.step == "paths":
        paths = extractor.extract_mechanistic_paths(args.pdf)
        print(json.dumps(paths, ensure_ascii=False, indent=2))

    elif args.step == "extract":
        classification = extractor.classify_paper(args.pdf)
        print(f"文献类型: {classification['primary_category']}", file=sys.stderr)

        if classification["primary_category"] == "mechanistic":
            paths = extractor.extract_mechanistic_paths(args.pdf)
            print(f"发现 {len(paths)} 条机制通路", file=sys.stderr)

            for i, path in enumerate(paths):
                print(f"\n提取证据卡 [{i+1}/{len(paths)}]: {path}", file=sys.stderr)
                evidence = extractor.extract_evidence_card(args.pdf, "mechanistic", path)
                output_file = Path(args.pdf).stem + f"_evidence_{i}.json"
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(evidence, f, ensure_ascii=False, indent=2)
                print(f"已保存: {output_file}")
        else:
            print(f"类型 {classification['primary_category']} 的提取功能待实现", file=sys.stderr)


if __name__ == "__main__":
    import sys
    main()
