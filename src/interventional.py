"""Interventional (干预/RCT) 证据卡提取器"""
import json
from typing import Any, Dict, List

from base import BaseExtractor


class InterventionalExtractor(BaseExtractor):
    EVIDENCE_TYPE = "interventional"
    PROMPT_DIR = "interventional"

    def extract_paths(self, pdf_path: str) -> List[Dict]:
        """Step 1: 提取介入对比路径"""
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
        """Step 2: 以对比为单位构建完整证据卡（对齐 HPP 模板）"""
        prompt_template = self.load_prompt("step2_card")
        pdf_text = self.get_pdf_text(pdf_path)

        prompt = self._build_prompt(
            prompt_template, pdf_text, target_path=target_path
        )

        result = self.client.call_json(
            prompt,
            max_tokens=32768,
        )

        if isinstance(result, list):
            return result
        return [result]
