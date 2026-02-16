"""GLM LLM Client Wrapper

Reads configuration from environment variables (loaded via .env):
  - OPENAI_API_KEY
  - OPENAI_BASE_URL
  - DEFAULT_MODEL
  - DEFAULT_TEMPERATURE
  - DEFAULT_MAX_TOKENS
"""
import os
import json
import base64
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Read all config from environment
_API_KEY = os.getenv("OPENAI_API_KEY", "")
_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
_DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "glm-5")
_DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "1.0"))
_DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "16384"))

EVIDENCE_TYPES = ["interventional", "causal", "mechanistic", "associational"]


class GLMClient:
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

    def call(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
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
        system_prompt: str = "You are a medical literature analysis expert. Output strictly in JSON format.",
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> Any:
        """Call LLM and parse JSON response"""
        response = self.call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return self._parse_json(response)

    def call_vision(
        self,
        images: list,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 1.0,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        """Call vision model with images (base64 encoded)"""
        vision_model = model or os.getenv("VISION_MODEL", "glm-4.6v")
        content = []
        for img_path in images:
            img_base64 = self._image_to_base64(img_path)
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
                    "image_url": {"url": f"data:{media_type};base64,{img_base64}"},
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
    def _parse_json(text: str) -> Any:
        return json.loads(text)

    @staticmethod
    def _image_to_base64(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def encode_pdf(self, pdf_path: str) -> str:
        with open(pdf_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
