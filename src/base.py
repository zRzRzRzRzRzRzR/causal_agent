import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_client import GLMClient
from ocr import get_pdf_text

# prompt 文件就在 src/ 同级目录下（step0_classify.md, step1_paths.md, ...）
# 按类型名映射到对应的 prompt 文件前缀


class BaseExtractor:
    """所有类型提取器的基类"""

    # 子类覆盖
    EVIDENCE_TYPE: str = ""
    PROMPT_DIR: str = ""

    def __init__(self, client: GLMClient):
        self.client = client
        self._prompt_cache: Dict[str, str] = {}

    def load_prompt(self, step_name: str) -> str:
        """加载 prompt 文件（与 .py 同目录下的 .md 文件）"""
        if step_name not in self._prompt_cache:
            prompt_file = Path(__file__).parent / f"{step_name}.md"
            if not prompt_file.exists():
                raise FileNotFoundError(f"Prompt 文件不存在: {prompt_file}")
            self._prompt_cache[step_name] = prompt_file.read_text(encoding="utf-8")
        return self._prompt_cache[step_name]

    def get_pdf_text(self, pdf_path: str, max_chars: int = 200000) -> str:
        """获取 PDF 文本并截断"""
        text = get_pdf_text(pdf_path)
        return text[:max_chars]

    def _build_prompt(self, template: str, pdf_text: str, **kwargs) -> str:
        """构建完整 prompt"""
        # 替换模板变量
        for key, value in kwargs.items():
            template = template.replace(f"{{{key}}}", str(value))
        return f"{template}\n\n**论文内容如下：**\n\n{pdf_text}"

    def extract_paths(self, pdf_path: str) -> List[Any]:
        """Step 1: 提取路径/对比（子类实现）"""
        raise NotImplementedError

    def extract_evidence_card(self, pdf_path: str, target_path: str) -> List[Dict]:
        """Step 2: 提取证据卡（子类实现）"""
        raise NotImplementedError

    def extract_hpp_mapping(self, evidence_card: Dict) -> Dict:
        """Step 3: HPP 字段映射（子类可覆盖）"""
        prompt_template = self.load_prompt("step3_hpp")
        card_json = json.dumps(evidence_card, ensure_ascii=False, indent=2)
        prompt = f"{prompt_template}\n\n**证据卡 JSON：**\n\n{card_json}"

        result = self.client.call_json(prompt)
        # 合并 hpp_mapping 到原始卡片
        if isinstance(result, dict) and "hpp_mapping" in result:
            evidence_card["hpp_mapping"] = result["hpp_mapping"]
        return evidence_card

    def run_full_pipeline(self, pdf_path: str) -> List[Dict]:
        """运行完整的 Step1 → Step2 → Step3 流程"""
        import sys

        # Step 1: 提取路径
        print(f"[{self.EVIDENCE_TYPE}] Step 1: 提取路径...", file=sys.stderr)
        paths = self.extract_paths(pdf_path)
        print(f"  发现 {len(paths)} 条路径", file=sys.stderr)

        all_cards = []
        for i, path in enumerate(paths):
            path_str = path if isinstance(path, str) else json.dumps(path, ensure_ascii=False)
            print(f"\n[{self.EVIDENCE_TYPE}] Step 2: 提取证据卡 [{i+1}/{len(paths)}]: {path_str[:80]}", file=sys.stderr)

            # Step 2: 提取证据卡
            cards = self.extract_evidence_card(pdf_path, path_str)
            if isinstance(cards, dict):
                cards = [cards]

            # Step 3: HPP 映射
            for j, card in enumerate(cards):
                print(f"  Step 3: HPP 映射 [{j+1}/{len(cards)}]...", file=sys.stderr)
                try:
                    card = self.extract_hpp_mapping(card)
                except Exception as e:
                    print(f"  HPP 映射失败: {e}", file=sys.stderr)

                all_cards.append(card)

        return all_cards


class Classifier:
    """文献类型分类器"""

    def __init__(self, client: GLMClient):
        self.client = client

    def classify(self, pdf_path: str) -> Dict[str, Any]:
        """Step 0: 判断文献类型"""
        prompt_path = Path(__file__).parent / "step0_classify.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")

        pdf_text = get_pdf_text(pdf_path)[:200000]
        full_prompt = f"{prompt_template}\n\n**论文内容如下：**\n\n{pdf_text}"

        return self.client.call_json(full_prompt)
