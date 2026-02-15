"""GLM LLM 客户端封装"""
import os
import json
import base64
import re
from typing import Optional, Dict, Any

from openai import OpenAI
from config import ZHIPU_API_KEY, ZHIPU_BASE_URL, DEFAULT_MODEL, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS


class GLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", ZHIPU_API_KEY)
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", ZHIPU_BASE_URL)
        self.model = model
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        print(self.api_key, self.base_url)
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

    def call_json(
        self,
        prompt: str,
        system_prompt: str = "你是医学文献分析专家，严格按照 JSON 格式输出。",
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Any:
        """调用 LLM 并解析 JSON 响应"""
        response = self.call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return self._parse_json(response)

    @staticmethod
    def _parse_json(text: str) -> Any:
        """尝试多种方式解析 JSON"""
        # 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 提取 ```json ... ``` 块
        match = re.search(r'```json\s*([\s\S]*?)```', text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 提取最外层 { } 或 [ ]
        for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
            match = re.search(pattern, text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        raise ValueError(f"无法解析模型响应为 JSON: {text[:500]}")

    def encode_pdf(self, pdf_path: str) -> str:
        with open(pdf_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
