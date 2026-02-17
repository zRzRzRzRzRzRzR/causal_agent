#!/usr/bin/env python3
"""
PDF Extractor - Based on GLM-4.6V and GLM-OCR

Workflow:
1. Convert PDF to images
2. Use GLM-4.6V to validate content pages (exclude references, appendix, etc.)
3. Use GLM-OCR to recognize content
4. Save results to output directory
"""

import os
import argparse
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv
import tempfile
from glmocr import parse
from src.llms import call_vision_model, pdf_to_images

load_dotenv()


def validate_content_pages(
    image_paths: List[str], api_key: str = None, base_url: str = None, model: str = None
) -> List[int]:
    total_pages = len(image_paths)

    if total_pages <= 3:
        return list(range(total_pages))

    pages_to_check = min(5, total_pages)
    check_images = image_paths[-pages_to_check:]

    prompt = """
Analyze these PDF page images and determine if each page belongs to the main content.

Non-content pages include:
- References
- Appendix
- Acknowledgments
- Blank pages
- Back cover
- Advertisement pages

For each page, respond in this format:
Page 1: content/non-content - brief reason
Page 2: content/non-content - brief reason
...

Final line: summarize from which page the non-content starts (or "all content" if all pages are content)
"""

    result = call_vision_model(
        check_images, prompt, api_key=api_key, base_url=base_url, model=model
    )

    valid_pages = list(range(total_pages))

    if result:
        lines = result.lower().split("\n")
        for i, line in enumerate(lines):
            if "non-content" in line or "非正文" in line:
                actual_page_idx = total_pages - pages_to_check + i
                valid_pages = list(range(actual_page_idx))
                break

    return valid_pages if valid_pages else list(range(total_pages))


def ocr_images(image_paths: List[str], output_dir: str) -> str:
    """
    OCR recognize images and concatenate in page order

    Fix: Ensure processing each page in image_paths order, not relying on parse() return order
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Key fix: parse() processes all images at once
    results = parse(image_paths)

    # Key fix: Ensure results and image_paths order consistency
    # parse() should return results in input order, but for safety we match by index
    full_markdown = []

    for i, result in enumerate(results):
        # Page numbers start from 1
        page_num = i + 1

        # Add page marker
        full_markdown.append(f"<!-- Page {page_num} -->\n\n")

        # Add recognition result for this page
        full_markdown.append(result.markdown_result)

        # Add separator between pages
        full_markdown.append("\n\n")

    # Concatenate all pages
    combined_md_path = os.path.join(output_dir, "combined.md")
    with open(combined_md_path, "w", encoding="utf-8") as f:
        f.write("".join(full_markdown))

    print(f"      Saved combined markdown to: {combined_md_path}")

    return combined_md_path


class PDFExtractor:
    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        vision_model: str = None,
        dpi: int = 200,
        validate_pages: bool = True,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.vision_model = vision_model or os.getenv("VISION_MODEL", "glm-4.6v")
        self.dpi = dpi
        self.validate_pages = validate_pages

    def extract_structured(
        self, pdf_path: str, output_dir: str = None
    ) -> Dict[str, Any]:
        temp_dir = output_dir or tempfile.mkdtemp(prefix="ocr_output_")
        result = self.extract(pdf_path, output_dir=temp_dir)
        combined_md_path = os.path.join(result["output_dir"], "combined.md")
        if os.path.exists(combined_md_path):
            with open(combined_md_path, "r", encoding="utf-8") as f:
                markdown_content = f.read()
        else:
            markdown_content = ""

        return {
            "markdown": markdown_content,
            "output_dir": result["output_dir"],
            "total_pages": result["total_pages"],
            "content_pages": result["content_pages"],
        }

    def extract(self, pdf_path: str, output_dir: str) -> Dict[str, Any]:
        pdf_path = os.path.abspath(pdf_path)
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        pdf_name = Path(pdf_path).stem
        final_output_dir = os.path.join(output_dir, pdf_name)

        print(f"[1/3] Converting PDF to images (DPI={self.dpi})...")
        image_paths = pdf_to_images(pdf_path, dpi=self.dpi)
        print(f"      Total {len(image_paths)} pages")

        print("[2/3] Validating content pages...")
        valid_indices = validate_content_pages(
            image_paths,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.vision_model,
        )
        valid_images = [image_paths[i] for i in valid_indices]
        excluded_count = len(image_paths) - len(valid_images)
        print(f"      Excluded last {excluded_count} non-content pages")

        print("[3/3] Running OCR...")
        ocr_images(valid_images, output_dir=final_output_dir)

        print(f"Done! Results saved to: {final_output_dir}")

        return {
            "output_dir": final_output_dir,
            "total_pages": len(image_paths),
            "content_pages": len(valid_images),
            "excluded_pages": len(image_paths) - len(valid_images),
        }


def main():
    parser = argparse.ArgumentParser(
        description="PDF Extractor - Based on GLM-4.6V and GLM-OCR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pdf_extractor.py document.pdf -o ./output
  python pdf_extractor.py document.pdf -o ./output --no-validate
  python pdf_extractor.py document.pdf -o ./output --dpi 300 -v
        """,
    )

    parser.add_argument("pdf", help="PDF file path")
    parser.add_argument("-o", "--output", required=True, help="Output directory")
    parser.add_argument("--api-key", help="API Key (default: OPENAI_API_KEY env)")
    parser.add_argument(
        "--base-url", help="API Base URL (default: OPENAI_BASE_URL env)"
    )
    parser.add_argument(
        "--vision-model",
        default="glm-4.6v",
        help="Vision model name (default: glm-4.6v)",
    )
    parser.add_argument("--dpi", type=int, default=200, help="Image DPI (default: 200)")
    parser.add_argument(
        "--no-validate", action="store_true", help="Skip content page validation"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed info"
    )

    args = parser.parse_args()

    extractor = PDFExtractor(
        api_key=args.api_key,
        base_url=args.base_url,
        vision_model=args.vision_model,
        dpi=args.dpi,
        validate_pages=not args.no_validate,
    )

    result = extractor.extract(args.pdf, output_dir=args.output)

    if args.verbose:
        print("Details:")
        print(f"  Total pages: {result['total_pages']}")
        print(f"  Content pages: {result['content_pages']}")
        print(f"  Excluded pages: {result['excluded_pages']}")

    return 0


if __name__ == "__main__":
    exit(main())
