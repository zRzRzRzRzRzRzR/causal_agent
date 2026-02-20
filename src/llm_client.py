import base64
import json
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_API_KEY = os.getenv("OPENAI_API_KEY", "")
_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
_DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "glm-5")
_DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "1.0"))
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
        print(
            f"[LLM] Initialized GLMClient with model={self.model}, base_url={self.base_url}"
        )

    def call(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        response_format: Optional[Dict[str, str]] = None,
    ) -> str:
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

    def call_json(
        self,
        prompt: str,
        system_prompt: str = "请你遵循我的指令，请严格以 JSON 格式输出。不要生成任何额外的内容。",
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> Any:
        raw = self.call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(raw)

        except json.JSONDecodeError:
            pass

        cleaned = raw.strip()

        code_block_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if code_block_match:
            cleaned = code_block_match.group(1).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if json_match:
            candidate = json_match.group(1)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise ValueError(f"模型输出无法解析为 JSON：\n{raw}")

    def call_vision(
        self,
        images: List[str],
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
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

    @staticmethod
    def _image_to_base64(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
