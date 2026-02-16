import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_client import GLMClient
from ocr import get_pdf_text

# prompt files are located in the same directory as src/ (step0_classify.md, step1_paths.md, ...)
# Map to corresponding prompt file prefixes by type name


class BaseExtractor:
    """Base class for all extractors"""

    # Override in subclasses
    EVIDENCE_TYPE: str = ""
    PROMPT_DIR: str = ""

    def __init__(self, client: GLMClient):
        self.client = client
        self._prompt_cache: Dict[str, str] = {}

    def load_prompt(self, step_name: str) -> str:
        """Load prompt file (.md file in the same directory as .py)"""
        if step_name not in self._prompt_cache:
            prompt_file = Path(__file__).parent / f"{step_name}.md"
            if not prompt_file.exists():
                raise FileNotFoundError(f"Prompt 文件不存在: {prompt_file}")
            self._prompt_cache[step_name] = prompt_file.read_text(encoding="utf-8")
        return self._prompt_cache[step_name]

    def get_pdf_text(self, pdf_path: str, max_chars: int = 200000) -> str:
        """Get PDF text and truncate"""
        text = get_pdf_text(pdf_path)
        return text[:max_chars]

    def _build_prompt(self, template: str, pdf_text: str, **kwargs) -> str:
        """Build complete prompt"""
        # Replace template variables
        for key, value in kwargs.items():
            template = template.replace(f"{{{key}}}", str(value))
        return f"{template}\n\n**Paper content follows:**\n\n{pdf_text}"

    def extract_paths(self, pdf_path: str) -> List[Any]:
        """Step 1: Extract paths/comparisons (implemented by subclasses)"""
        raise NotImplementedError

    def extract_evidence_card(self, pdf_path: str, target_path: str) -> List[Dict]:
        """Step 2: Extract evidence cards (implemented by subclasses)"""
        raise NotImplementedError

    def extract_hpp_mapping(self, evidence_card: Dict) -> Dict:
        """Step 3: HPP field mapping (can be overridden by subclasses)"""
        prompt_template = self.load_prompt("step3_hpp")
        card_json = json.dumps(evidence_card, ensure_ascii=False, indent=2)
        prompt = f"{prompt_template}\n\n**证据卡 JSON：**\n\n{card_json}"

        result = self.client.call_json(prompt)
        # Merge hpp_mapping into original card
        if isinstance(result, dict) and "hpp_mapping" in result:
            evidence_card["hpp_mapping"] = result["hpp_mapping"]
        return evidence_card

    def run_full_pipeline(self, pdf_path: str) -> List[Dict]:
        """Run complete Step1 -> Step2 -> Step3 flow"""
        import sys

        # Step 1: Extract paths
        print(f"[{self.EVIDENCE_TYPE}] Step 1: Extracting paths...", file=sys.stderr)
        paths = self.extract_paths(pdf_path)
        print(f"  Found {len(paths)} paths", file=sys.stderr)

        all_cards = []
        for i, path in enumerate(paths):
            path_str = path if isinstance(path, str) else json.dumps(path, ensure_ascii=False)
            print(f"\n[{self.EVIDENCE_TYPE}] Step 2: Extracting evidence card [{i+1}/{len(paths)}]: {path_str[:80]}", file=sys.stderr)

            # Step 2: Extract evidence card
            cards = self.extract_evidence_card(pdf_path, path_str)
            if isinstance(cards, dict):
                cards = [cards]

            # Step 3: HPP mapping
            for j, card in enumerate(cards):
                print(f"  Step 3: HPP mapping [{j+1}/{len(cards)}]...", file=sys.stderr)
                try:
                    card = self.extract_hpp_mapping(card)
                except Exception as e:
                    print(f"  HPP 映射失败: {e}", file=sys.stderr)

                all_cards.append(card)

        return all_cards


class Classifier:
    """Document type classifier"""

    def __init__(self, client: GLMClient):
        self.client = client

    def classify(self, pdf_path: str) -> Dict[str, Any]:
        """Step 0: Determine document type"""
        prompt_path = Path(__file__).parent / "step0_classify.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")

        pdf_text = get_pdf_text(pdf_path)[:200000]
        full_prompt = f"{prompt_template}\n\n**Paper content follows:**\n\n{pdf_text}"

        return self.client.call_json(full_prompt)
