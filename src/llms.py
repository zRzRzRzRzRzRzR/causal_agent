from openai import OpenAI
import os
import base64
import tempfile
from pathlib import Path
from typing import List
import fitz
from glmocr import parse


def pdf_to_images(pdf_path: str, output_dir: str = None, dpi: int = 200) -> List[str]:
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="pdf_images_")
    else:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    pdf_name = Path(pdf_path).stem
    image_paths = []
    doc = fitz.open(pdf_path)

    for page_num in range(len(doc)):
        page = doc[page_num]
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix)
        image_path = os.path.join(output_dir, f"{pdf_name}_page_{page_num + 1:03d}.png")
        pix.save(image_path)
        image_paths.append(image_path)

    doc.close()
    return image_paths


def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_large_model(messages, api_key=None, base_url=None, model=None):
    if api_key is None:
        api_key = os.getenv("OPENAI_API_KEY")
    if base_url is None:
        base_url = os.getenv("OPENAI_BASE_URL")

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=1.0,
        max_tokens=16384,
        stream=False,
    )
    if response.choices[0].message.content.strip():
        return response.choices[0].message.content.strip()
    return {}


def call_vision_model(
    images: List[str],
    prompt: str,
    api_key: str = None,
    base_url: str = None,
    model: str = None,
) -> str:
    from openai import OpenAI

    api_key = api_key or os.getenv("OPENAI_API_KEY")
    base_url = base_url or os.getenv("OPENAI_BASE_URL")
    model = model or os.getenv("VISION_MODEL", "glm-4.6v")

    client = OpenAI(api_key=api_key, base_url=base_url)
    content = []

    for img_path in images:
        img_base64 = image_to_base64(img_path)
        suffix = Path(img_path).suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(suffix, "image/png")

        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{img_base64}"},
            }
        )

    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=1.0,
        max_tokens=16384,
    )
    return response.choices[0].message.content.strip()
