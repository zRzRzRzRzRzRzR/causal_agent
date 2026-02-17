"""
PDF Text Extraction via GLM-OCR.

Workflow: PDF → Images → (optional page validation) → GLM-OCR → Markdown
Results cached to ocr_output_dir/{pdf_stem}/combined.md
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz
from glmocr import parse

from .llm_client import GLMClient


def pdf_to_images(
    pdf_path: str, output_dir: Optional[str] = None, dpi: int = 200
) -> List[str]:
    """Convert every page of a PDF into a PNG image."""
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="pdf_images_")
    else:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    pdf_name = Path(pdf_path).stem
    image_paths: List[str] = []
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


def _validate_content_pages(image_paths: List[str], client: GLMClient) -> List[int]:
    """Use vision model to detect non-content tail pages (references, appendix)."""
    total = len(image_paths)
    if total <= 3:
        return list(range(total))

    pages_to_check = min(5, total)
    check_images = image_paths[-pages_to_check:]

    prompt = (
        "Analyze these PDF page images. Determine if each page is main content or non-content.\n"
        "Non-content = References / Bibliography / Appendix / Acknowledgments / Blank pages.\n"
        "For each page reply: Page N: content/non-content\n"
        "Final line: from which page non-content starts (or 'all content')."
    )
    result = client.call_vision(check_images, prompt)

    valid_pages = list(range(total))
    if result:
        for i, line in enumerate(result.lower().split("\n")):
            if "non-content" in line or "非正文" in line:
                valid_pages = list(range(total - pages_to_check + i))
                break

    return valid_pages if valid_pages else list(range(total))


def _ocr_images(image_paths: List[str], output_dir: str) -> str:
    """Run glmocr.parse on images and write combined.md."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    results = parse(image_paths)

    parts: List[str] = []
    for i, result in enumerate(results):
        parts.append(f"<!-- Page {i + 1} -->\n\n")
        parts.append(result.markdown_result)
        parts.append("\n\n")

    combined_md_path = os.path.join(output_dir, "combined.md")
    with open(combined_md_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return combined_md_path


class PDFExtractor:
    """PDF → Markdown extractor with caching."""

    def __init__(
        self,
        ocr_output_dir: Optional[str] = None,
        client: Optional[GLMClient] = None,
        dpi: int = 200,
        validate_pages: bool = True,
    ):
        self.ocr_output_dir = ocr_output_dir or tempfile.mkdtemp(prefix="ocr_output_")
        self.client = client or GLMClient()
        self.dpi = dpi
        self.validate_pages = validate_pages

    def extract_text(self, pdf_path: str, force_rerun: bool = False) -> str:
        """Return the full PDF text as Markdown."""
        return self.extract_structured(pdf_path, force_rerun=force_rerun)["markdown"]

    def extract_structured(
        self, pdf_path: str, output_dir: Optional[str] = None, force_rerun: bool = False
    ) -> Dict[str, Any]:
        pdf_path = os.path.abspath(pdf_path)
        assert os.path.exists(pdf_path), f"PDF not found: {pdf_path}"

        pdf_stem = Path(pdf_path).stem
        base_dir = output_dir or self.ocr_output_dir
        final_dir = os.path.join(base_dir, pdf_stem)
        combined_md_path = os.path.join(final_dir, "combined.md")

        # --- Cache hit ---
        if not force_rerun and os.path.exists(combined_md_path):
            md = Path(combined_md_path).read_text(encoding="utf-8")
            if md.strip():
                pc = md.count("<!-- Page ")
                print(f"[OCR] Cache hit: {combined_md_path} ({pc} pages)")
                return {
                    "markdown": md,
                    "output_dir": final_dir,
                    "total_pages": pc,
                    "content_pages": pc,
                    "combined_md_path": combined_md_path,
                }

        # --- Full OCR pipeline ---
        print(f"[OCR] Step 1/3: PDF -> images (DPI={self.dpi}) ...")
        image_paths = pdf_to_images(pdf_path, dpi=self.dpi)
        total = len(image_paths)
        print(f"       {total} pages total")

        if self.validate_pages and total > 3:
            print("[OCR] Step 2/3: Filtering non-content pages ...")
            valid_idx = _validate_content_pages(image_paths, self.client)
            valid_images = [image_paths[i] for i in valid_idx]
            excluded = total - len(valid_images)
            if excluded:
                print(f"       Excluded {excluded} tail pages")
        else:
            print("[OCR] Step 2/3: Skipped page validation")
            valid_images = image_paths

        print("[OCR] Step 3/3: GLM-OCR recognizing ...")
        _ocr_images(valid_images, output_dir=final_dir)
        print(f"       Done -> {combined_md_path}")

        md = Path(combined_md_path).read_text(encoding="utf-8")
        return {
            "markdown": md,
            "output_dir": final_dir,
            "total_pages": total,
            "content_pages": len(valid_images),
            "combined_md_path": combined_md_path,
        }


# ---- Module-level singleton ----
_default_extractor: Optional[PDFExtractor] = None


def init_extractor(**kwargs) -> PDFExtractor:
    global _default_extractor
    _default_extractor = PDFExtractor(**kwargs)
    return _default_extractor


def get_extractor() -> PDFExtractor:
    global _default_extractor
    if _default_extractor is None:
        _default_extractor = PDFExtractor()
    return _default_extractor


def get_pdf_text(pdf_path: str, force_rerun: bool = False) -> str:
    return get_extractor().extract_text(pdf_path, force_rerun=force_rerun)
