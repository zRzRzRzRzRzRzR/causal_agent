import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.llm_client import GLMClient
from src.ocr import get_pdf_text
from src.ocr import init_extractor as init_ocr
from src.pipeline import EdgeExtractionPipeline


def process_single_pdf(
    pdf_path: Path,
    pipeline: EdgeExtractionPipeline,
    output_dir: Path,
    force_type: str = None,
) -> dict:
    t0 = time.time()
    status = "success"
    error_msg = None
    n_edges = 0

    try:
        edges = pipeline.run(
            pdf_path=str(pdf_path),
            force_type=force_type,
            output_dir=str(output_dir),
        )
        n_edges = len(edges) if edges else 0

    except Exception as e:
        status = "failed"
        error_msg = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)

    elapsed = round(time.time() - t0, 1)

    return {
        "pdf": pdf_path.name,
        "status": status,
        "n_edges": n_edges,
        "elapsed_sec": elapsed,
        "error": error_msg,
    }


def main():
    parser = argparse.ArgumentParser(description="Batch evidence edge extraction")
    parser.add_argument("-i", "--input-dir", default="./evidence_card")
    parser.add_argument("-o", "--output-dir", default="./output")
    parser.add_argument(
        "--type",
        choices=["interventional", "causal", "mechanistic", "associational"],
        default=None,
    )
    parser.add_argument("--ocr-dir", default="./cache_ocr")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--no-validate-pages", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-workers", type=int, default=1)

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    pdf_files = sorted(input_dir.glob("*.pdf"))
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'='*60}", file=sys.stderr)
    print(f"Batch Evidence Edge Extraction", file=sys.stderr)
    print(f"  Input:  {input_dir.resolve()} ({len(pdf_files)} PDFs)", file=sys.stderr)
    print(f"  Output: {output_dir.resolve()}", file=sys.stderr)
    print(f"  Type:   {args.type or 'auto-classify'}", file=sys.stderr)
    print(f"  Workers: {args.max_workers}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    client = GLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

    pipeline = EdgeExtractionPipeline(
        client=client,
        ocr_text_func=get_pdf_text,
        ocr_init_func=init_ocr,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
    )

    results = []
    t_start = time.time()

    if args.max_workers <= 1:
        for idx, pdf_path in enumerate(pdf_files, 1):
            print(f"\n{'─'*50}", file=sys.stderr)
            print(f"[{idx}/{len(pdf_files)}] {pdf_path.name}", file=sys.stderr)
            summary = process_single_pdf(pdf_path, pipeline, output_dir, args.type)
            results.append(summary)
            tag = "OK" if summary["status"] == "success" else "FAIL"
            print(
                f"  [{tag}] {summary['n_edges']} edges, {summary['elapsed_sec']}s",
                file=sys.stderr,
            )
    else:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_to_pdf = {
                executor.submit(
                    process_single_pdf, p, pipeline, output_dir, args.type
                ): p
                for p in pdf_files
            }
            for future in as_completed(future_to_pdf):
                pdf_path = future_to_pdf[future]
                try:
                    summary = future.result()
                except Exception as e:
                    summary = {
                        "pdf": pdf_path.name,
                        "status": "failed",
                        "n_edges": 0,
                        "elapsed_sec": 0,
                        "error": str(e),
                    }
                results.append(summary)
                tag = "OK" if summary["status"] == "success" else "FAIL"
                print(
                    f"  [{tag}] {pdf_path.name}: {summary['n_edges']} edges, "
                    f"{summary['elapsed_sec']}s",
                    file=sys.stderr,
                )

    total_elapsed = round(time.time() - t_start, 1)
    n_success = sum(1 for r in results if r["status"] == "success")
    n_failed = len(results) - n_success

    batch_summary = {
        "total_pdfs": len(pdf_files),
        "success": n_success,
        "failed": n_failed,
        "total_edges": sum(r["n_edges"] for r in results),
        "total_elapsed_sec": total_elapsed,
        "details": results,
    }

    summary_path = output_dir / "_batch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(batch_summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}", file=sys.stderr)
    print(
        f"Complete: {n_success}/{len(pdf_files)} succeeded, "
        f"{batch_summary['total_edges']} total edges, {total_elapsed}s",
        file=sys.stderr,
    )
    print(f"  Summary: {summary_path}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    sys.exit(1 if n_failed == len(pdf_files) else 0)


if __name__ == "__main__":
    main()
