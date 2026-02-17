"""
GLM LLM Client - Unified interface for text and vision calls.

Reads config from .env:
  OPENAI_API_KEY, OPENAI_BASE_URL, DEFAULT_MODEL,
  DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, VISION_MODEL
"""

import base64
import json
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_API_KEY = os.getenv("OPENAI_API_KEY", "")
_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
_DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "glm-5")
_DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.1"))
_DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "16384"))
_VISION_MODEL = os.getenv("VISION_MODEL", "glm-4.6v")


class GLMClient:
    """Thin wrapper around OpenAI-compatible API for GLM models."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or _API_KEY
        self.base_url = base_url or _BASE_URL
        self.model = model or _DEFAULT_MODEL
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------
    def call(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
        """Send a single-turn chat completion and return the text."""
        messages: List[Dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    # ------------------------------------------------------------------
    # JSON call - forces JSON output and parses
    # ------------------------------------------------------------------
    def call_json(
        self,
        prompt: str,
        system_prompt: str = "你是医学文献分析专家。请严格以 JSON 格式输出。",
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> Any:
        """Call LLM with JSON response format and parse the result."""
        raw = self.call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return json.loads(raw)

    # ------------------------------------------------------------------
    # Vision call
    # ------------------------------------------------------------------
    def call_vision(
        self,
        images: List[str],
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        """Call vision model with base64-encoded images."""
        vision_model = model or _VISION_MODEL

        content: List[Dict] = []
        for img_path in images:
            img_b64 = self._image_to_base64(img_path)
            suffix = img_path.rsplit(".", 1)[-1].lower()
            media_type = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            }.get(suffix, "image/png")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{img_b64}"},
                }
            )
        content.append({"type": "text", "text": prompt})

        response = self.client.chat.completions.create(
            model=vision_model,
            messages=[{"role": "user", "content": content}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _image_to_base64(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
