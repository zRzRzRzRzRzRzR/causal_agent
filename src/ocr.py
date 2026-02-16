"""
PDF Text Extraction Module — Based on GLM-OCR

Workflow:
  1. PDF → Images (pdf_to_images)
  2. GLM vision model identifies main content pages, excludes references/appendices (optional)
  3. GLM-OCR recognizes content, outputs Markdown
  4. Results cached to ocr_output_dir/{pdf_stem}/combined.md

Cache strategy:
  If combined.md exists and is non-empty → read directly, skip OCR.
  force_rerun=True → force re-run.
"""

import os
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional

import fitz
from glmocr import parse

from llm_client import GLMClient


def pdf_to_images(pdf_path: str, output_dir: str = None, dpi: int = 200) -> List[str]:
    """Convert PDF pages to images."""
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


def _validate_content_pages(
    image_paths: List[str],
    client: GLMClient,
) -> List[int]:
    """Use vision model to filter non-content pages at the end (references/appendices etc.)"""
    total_pages = len(image_paths)
    if total_pages <= 3:
        return list(range(total_pages))

    pages_to_check = min(5, total_pages)
    check_images = image_paths[-pages_to_check:]

    prompt = """
Analyze these PDF page images and determine if each page belongs to the main content.

Non-content pages include:
- References / Bibliography
- Appendix / Acknowledgments
- Blank pages / Back cover / Advertisement pages

For each page, respond:
Page 1: content/non-content - brief reason
...

Final line: from which page the non-content starts (or "all content").
"""

    result = client.call_vision(check_images, prompt)

    valid_pages = list(range(total_pages))
    if result:
        for i, line in enumerate(result.lower().split("\n")):
            if "non-content" in line or "非正文" in line:
                valid_pages = list(range(total_pages - pages_to_check + i))
                break

    return valid_pages if valid_pages else list(range(total_pages))


def _ocr_images(image_paths: List[str], output_dir: str) -> str:
    """Call glmocr.parse to recognize and concatenate into combined.md"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    results = parse(image_paths)

    full_markdown = []
    for i, result in enumerate(results):
        full_markdown.append(f"<!-- Page {i + 1} -->\n\n")
        full_markdown.append(result.markdown_result)
        full_markdown.append("\n\n")

    combined_md_path = os.path.join(output_dir, "combined.md")
    with open(combined_md_path, "w", encoding="utf-8") as f:
        f.write("".join(full_markdown))

    return combined_md_path


class PDFExtractor:
    """
    PDF to Markdown Extractor (GLM-OCR)

    Parameters
    ----------
    ocr_output_dir : str
        OCR output/cache directory.
    client : GLMClient | None
        Shared GLMClient instance for vision calls.
    dpi : int
        PDF to image resolution, default 200.
    validate_pages : bool
        Whether to call vision model to filter non-content pages, default True.
    """

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
        """Extract full PDF text as Markdown"""
        return self.extract_structured(pdf_path, force_rerun=force_rerun)["markdown"]

    def extract_structured(
        self,
        pdf_path: str,
        output_dir: Optional[str] = None,
        force_rerun: bool = False,
    ) -> Dict[str, Any]:
        """
        Extract PDF to structured result.

        Returns: {"markdown", "output_dir", "total_pages", "content_pages", "combined_md_path"}
        """
        pdf_path = os.path.abspath(pdf_path)
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        pdf_stem = Path(pdf_path).stem
        base_dir = output_dir or self.ocr_output_dir
        final_output_dir = os.path.join(base_dir, pdf_stem)
        combined_md_path = os.path.join(final_output_dir, "combined.md")

        # Cache hit
        if not force_rerun and os.path.exists(combined_md_path):
            md_content = Path(combined_md_path).read_text(encoding="utf-8")
            if md_content.strip():
                page_count = md_content.count("<!-- Page ")
                print(f"[OCR] Cache hit: {combined_md_path} ({page_count} pages)")
                return {
                    "markdown": md_content,
                    "output_dir": final_output_dir,
                    "total_pages": page_count,
                    "content_pages": page_count,
                    "combined_md_path": combined_md_path,
                }

        # GLM-OCR workflow
        print(f"[OCR] Step 1/3: PDF -> images (DPI={self.dpi})...")
        image_paths = pdf_to_images(pdf_path, dpi=self.dpi)
        total_pages = len(image_paths)
        print(f"         Total {total_pages} pages")

        if self.validate_pages and total_pages > 3:
            print("[OCR] Step 2/3: Identifying content pages...")
            valid_indices = _validate_content_pages(image_paths, self.client)
            valid_images = [image_paths[i] for i in valid_indices]
            excluded = total_pages - len(valid_images)
            if excluded > 0:
                print(f"         Excluded {excluded} non-content pages at the end")
        else:
            print("[OCR] Step 2/3: Skipping page validation")
            valid_images = image_paths

        print("[OCR] Step 3/3: GLM-OCR recognizing...")
        _ocr_images(valid_images, output_dir=final_output_dir)
        print(f"         Completed -> {combined_md_path}")

        md_content = Path(combined_md_path).read_text(encoding="utf-8")
        return {
            "markdown": md_content,
            "output_dir": final_output_dir,
            "total_pages": total_pages,
            "content_pages": len(valid_images),
            "combined_md_path": combined_md_path,
        }


# Module-level singleton + convenience functions
_default_extractor: Optional[PDFExtractor] = None


def init_extractor(**kwargs) -> PDFExtractor:
    """Initialize module-level singleton, called once when pipeline starts"""
    global _default_extractor
    _default_extractor = PDFExtractor(**kwargs)
    return _default_extractor


def get_extractor() -> PDFExtractor:
    global _default_extractor
    if _default_extractor is None:
        _default_extractor = PDFExtractor()
    return _default_extractor


def get_pdf_text(pdf_path: str, force_rerun: bool = False) -> str:
    """Convenience function: other modules call this interface to get PDF text"""
    return get_extractor().extract_text(pdf_path, force_rerun=force_rerun)


def get_pdf_structured(pdf_path: str, force_rerun: bool = False) -> Dict[str, Any]:
    """Convenience function: get structured results"""
    return get_extractor().extract_structured(pdf_path, force_rerun=force_rerun)
