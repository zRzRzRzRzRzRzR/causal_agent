import argparse
import json

from src.llm_client import GLMClient
from src.ocr import get_pdf_text
from src.ocr import init_extractor as init_ocr
from src.pipeline import EdgeExtractionPipeline


def main():
    parser = argparse.ArgumentParser(
        description="Evidence Edge Extraction Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "step",
        choices=["full", "classify", "edges"],
        help="Pipeline step to run",
    )
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument("--api-key", default=None, help="Override API key")
    parser.add_argument("--base-url", default=None, help="Override base URL")
    parser.add_argument(
        "--type",
        choices=["interventional", "causal", "mechanistic", "associational"],
        default=None,
        help="Force evidence type (skip Step 0)",
    )
    parser.add_argument("--output", "-o", default=None, help="Output directory")
    parser.add_argument("--ocr-dir", default="./cache_ocr", help="OCR cache dir")
    parser.add_argument("--dpi", type=int, default=200, help="PDF→image DPI")
    parser.add_argument(
        "--no-validate-pages",
        action="store_true",
        help="Skip page validation during OCR",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON output",
    )

    args = parser.parse_args()

    # Initialize client
    client = GLMClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )

    # Initialize pipeline
    pipeline = EdgeExtractionPipeline(
        client=client,
        ocr_text_func=get_pdf_text,
        ocr_init_func=init_ocr,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
    )

    # Run
    if args.step == "full":
        result = pipeline.run(
            pdf_path=args.pdf,
            force_type=args.type,
            output_dir=args.output or ".",
        )
    else:
        result = pipeline.run_single_step(
            pdf_path=args.pdf,
            step=args.step,
            evidence_type=args.type,
        )

    # Print to stdout
    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
