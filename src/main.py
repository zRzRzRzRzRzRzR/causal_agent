#!/usr/bin/env python3
"""
Medical Literature Evidence Card Extraction Tool - Command Line Interface

Usage:
  # Full pipeline (auto-classify -> extract paths -> evidence cards -> HPP mapping)
  python main.py full paper.pdf --output ./output

  # Classify only
  python main.py classify paper.pdf

  # Extract paths only (specify type)
  python main.py paths paper.pdf --type interventional

  # Extract evidence cards only (specify path)
  python main.py card paper.pdf --type interventional --target "Late dinner vs Early dinner -> Glucose AUC"

  # Full pipeline (force type, skip HPP mapping)
  python main.py full paper.pdf --type interventional --skip-hpp --output ./output
"""
import argparse
import json

from llm_client import GLMClient
from pipeline import EvidenceCardPipeline


def main():
    parser = argparse.ArgumentParser(
        description="Medical Literature Evidence Card Extraction Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "step",
        choices=["full", "classify", "paths", "card", "hpp"],
        help="Step: full (full pipeline), classify, paths (extract paths), card (evidence card), hpp (HPP mapping)",
    )
    parser.add_argument("pdf", help="PDF file path")
    parser.add_argument("--model", help="Model name (overrides DEFAULT_MODEL in .env)")
    parser.add_argument("--api-key", help="API Key (overrides OPENAI_API_KEY in .env)")
    parser.add_argument(
        "--base-url", help="API Base URL (overrides OPENAI_BASE_URL in .env)"
    )
    parser.add_argument(
        "--type",
        choices=["interventional", "causal", "mechanistic", "associational"],
        help="Force document type (skip auto-classification)",
    )
    parser.add_argument("--target", help="Target path (for card/hpp steps)")
    parser.add_argument("--output", "-o", help="Output directory")
    parser.add_argument("--skip-hpp", action="store_true", help="Skip HPP mapping")
    parser.add_argument("--ocr-dir", default="./cache_ocr", help="OCR cache directory")
    parser.add_argument(
        "--dpi", type=int, default=200, help="PDF to image DPI (default: 200)"
    )
    parser.add_argument(
        "--no-validate-pages",
        action="store_true",
        help="Skip OCR content page validation",
    )
    parser.add_argument(
        "--force-ocr", action="store_true", help="Force re-run OCR (ignore cache)"
    )
    parser.add_argument(
        "--pretty", action="store_true", default=True, help="Pretty JSON output"
    )

    args = parser.parse_args()

    # Initialize client (all defaults come from .env via llm_client module)
    client = GLMClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    pipeline = EvidenceCardPipeline(
        client,
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

    # Output to stdout
    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
