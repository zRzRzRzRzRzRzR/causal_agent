from typing import Dict, List
from base import BaseExtractor


class CausalExtractor(BaseExtractor):
    EVIDENCE_TYPE = "causal"
    PROMPT_DIR = "causal"

    def extract_paths(self, pdf_path: str) -> List[Dict]:
        """Step 1: Extract causal comparison paths"""
        prompt_template = self.load_prompt("step1_paths")
        pdf_text = self.get_pdf_text(pdf_path)
        prompt = self._build_prompt(prompt_template, pdf_text)

        result = self.client.call_json(prompt)
        if isinstance(result, dict) and "paths" in result:
            return result["paths"]
        if isinstance(result, list):
            return result
        return [result]

    def extract_evidence_card(self, pdf_path: str, target_path: str) -> List[Dict]:
        """Step 2: Build evidence cards by causal comparison"""
        prompt_template = self.load_prompt("step2_card")
        pdf_text = self.get_pdf_text(pdf_path)

        prompt = self._build_prompt(
            prompt_template, pdf_text, target_path=target_path
        )

        result = self.client.call_json(prompt, max_tokens=32768)
        if isinstance(result, list):
            return result
        return [result]
