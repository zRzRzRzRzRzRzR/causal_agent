"""
Medical Literature Evidence Card Extraction Tool - Command Line Interface

Usage:
  python main.py full paper.pdf --output ./output
  python main.py classify paper.pdf
  python main.py paths paper.pdf --type interventional
  python main.py full paper.pdf --type interventional --skip-hpp --output ./output
"""

import argparse
import json

from src.llm_client import GLMClient
from src.ocr import get_pdf_text
from src.ocr import init_extractor as init_ocr
from src.pipeline import EvidenceCardPipeline


def main():
    parser = argparse.ArgumentParser(
        description="Medical Literature Evidence Card Extraction Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("step", choices=["full", "classify", "paths", "card", "hpp"])
    parser.add_argument("pdf", help="PDF file path")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--type", choices=["interventional", "causal", "mechanistic", "associational"]
    )
    parser.add_argument("--target", default=None)
    parser.add_argument("--output", "-o", default=None)
    parser.add_argument("--skip-hpp", action="store_true")
    parser.add_argument("--ocr-dir", default="./cache_ocr")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--no-validate-pages", action="store_true")
    parser.add_argument("--pretty", action="store_true", default=True)

    args = parser.parse_args()

    client = GLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

    pipeline = EvidenceCardPipeline(
        client,
        ocr_text_func=get_pdf_text,
        ocr_init_func=init_ocr,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
    )

    if args.step == "full":
        result = pipeline.run(
            pdf_path=args.pdf,
            force_type=args.type,
            skip_hpp=args.skip_hpp,
            output_dir=args.output or ".",
        )
    else:
        result = pipeline.run_single_step(
            pdf_path=args.pdf,
            step=args.step,
            evidence_type=args.type,
            target_path=args.target,
        )

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
