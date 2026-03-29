import base64
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_API_KEY = os.getenv("OPENAI_API_KEY")
_BASE_URL = os.getenv("OPENAI_BASE_URL")
_DEFAULT_MODEL = os.getenv("DEFAULT_MODEL")
_DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE"))
_DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS"))
_VISION_MODEL = os.getenv("VISION_MODEL")
_VISION_API_KEY = os.getenv("VISION_API_KEY")
_VISION_BASE_URL = os.getenv("VISION_BASE_URL")


class GLMClient:
    """Thin wrapper around OpenAI-compatible API for GLM models."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        vision_api_key: Optional[str] = None,
        vision_base_url: Optional[str] = None,
    ):
        self.api_key = api_key or _API_KEY
        self.base_url = base_url or _BASE_URL
        self.model = model or _DEFAULT_MODEL
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        self.vision_api_key = vision_api_key or _VISION_API_KEY
        self.vision_base_url = vision_base_url or _VISION_BASE_URL
        self.vision_client = OpenAI(
            api_key=self.vision_api_key, base_url=self.vision_base_url
        )

        print(
            f"[LLM] Initialized GLMClient with model={self.model}, base_url={self.base_url}"
        )
        print(f"[LLM] Vision client with base_url={vision_base_url}")

    def call(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        response_format: Optional[Dict[str, str]] = None,
        thinking: bool = True,
        max_retries: int = 10,
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

        if not thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
            except Exception as e:
                last_error = e
                is_rate_limit = "429" in str(e) or "RateLimitError" in type(e).__name__
                if attempt < max_retries:
                    wait = 5.0 if is_rate_limit else 2.0
                    print(
                        f"[LLM] {'限流' if is_rate_limit else '调用失败'}"
                        f" (第 {attempt}/{max_retries} 次)，"
                        f"{wait:.0f}s 后重试"
                    )
                    time.sleep(wait)
                else:
                    raise

    def call_json(
        self,
        prompt: str,
        system_prompt: str = "请你遵循我的指令，请严格以 JSON 格式输出。不要生成任何额外的内容。",
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        thinking: bool = True,
        max_retries: int = 3,
    ) -> Any:
        last_raw = ""
        for attempt in range(1, max_retries + 1):
            raw = self.call(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                thinking=thinking,
            )
            last_raw = raw

            parsed = self._try_parse_json(raw)
            if parsed is not None:
                return parsed

            print(f"[LLM] JSON 解析失败，第 {attempt}/{max_retries} 次重试...")

        raise ValueError(
            f"模型输出经过 {max_retries} 次尝试仍无法解析为 JSON：\n{last_raw}"
        )

    @staticmethod
    def _try_parse_json(raw: str) -> Any:
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
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        return None

    def call_vision(
        self,
        images: List[str],
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        max_retries: int = 10,
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

        for attempt in range(1, max_retries + 1):
            try:
                response = self.vision_client.chat.completions.create(
                    model=vision_model,
                    messages=[{"role": "user", "content": content}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                is_rate_limit = "429" in str(e) or "RateLimitError" in type(e).__name__
                if attempt < max_retries:
                    wait = 5.0 if is_rate_limit else 2.0
                    print(
                        f"[LLM] Vision {'限流' if is_rate_limit else '调用失败'}"
                        f" (第 {attempt}/{max_retries} 次)，"
                        f"{wait:.0f}s 后重试"
                    )
                    time.sleep(wait)
                else:
                    raise

    @staticmethod
    def _image_to_base64(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
