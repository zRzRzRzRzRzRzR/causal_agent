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
from src.xml_reader import extract_text_from_xml


def process_single_file(
    file_path: Path,
    pipeline: EdgeExtractionPipeline,
    output_dir: Path,
    force_type: str = None,
    resume: bool = False,
) -> dict:
    t0 = time.time()
    status = "success"
    error_msg = None
    n_edges = 0

    try:
        edges = pipeline.run(
            pdf_path=str(file_path),
            force_type=force_type,
            output_dir=str(output_dir),
            resume=resume,
        )
        n_edges = len(edges) if edges else 0

    except Exception as e:
        status = "failed"
        error_msg = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)

    elapsed = round(time.time() - t0, 1)

    return {
        "file": file_path.name,
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
    parser.add_argument(
        "--hpp-dict",
        default=None,
        help="Path to HPP data dictionary JSON",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries per edge when semantic validation fails (default: 2)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip steps whose output already exists (step0/step1 cache)",
    )
    # ── NEW: XML input support ──
    parser.add_argument(
        "--xml",
        action="store_true",
        help="Input files are JATS/NLM XML (.nxml/.xml) — skip OCR entirely",
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    # ── Collect input files based on format ──
    if args.xml:
        input_files = sorted(
            list(input_dir.glob("*.nxml")) + list(input_dir.glob("*.xml"))
        )
        fmt_label = "XML"
    else:
        input_files = sorted(input_dir.glob("*.pdf"))
        fmt_label = "PDF"

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'='*60}", file=sys.stderr)
    print(f"Batch Evidence Edge Extraction", file=sys.stderr)
    print(
        f"  Input:  {input_dir.resolve()} ({len(input_files)} {fmt_label}s)",
        file=sys.stderr,
    )
    print(f"  Output: {output_dir.resolve()}", file=sys.stderr)
    print(f"  Format: {fmt_label}", file=sys.stderr)
    print(f"  Type:   {args.type or 'auto-classify'}", file=sys.stderr)
    print(f"  Workers: {args.max_workers}", file=sys.stderr)
    print(f"  Resume:  {args.resume}", file=sys.stderr)
    print(f"  Max retries: {args.max_retries}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    client = GLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

    # ── Select text extraction strategy ──
    if args.xml:
        text_func = extract_text_from_xml
        init_func = None
    else:
        text_func = get_pdf_text
        init_func = init_ocr

    pipeline = EdgeExtractionPipeline(
        client=client,
        ocr_text_func=text_func,
        ocr_init_func=init_func,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
        hpp_dict_path=args.hpp_dict,
        max_retries=args.max_retries,
    )

    results = []
    t_start = time.time()

    if args.max_workers <= 1:
        for idx, file_path in enumerate(input_files, 1):
            print(f"\n{'─'*50}", file=sys.stderr)
            print(f"[{idx}/{len(input_files)}] {file_path.name}", file=sys.stderr)
            summary = process_single_file(
                file_path, pipeline, output_dir, args.type, args.resume
            )
            results.append(summary)
            tag = "OK" if summary["status"] == "success" else "FAIL"
            print(
                f"  [{tag}] {summary['n_edges']} edges, {summary['elapsed_sec']}s",
                file=sys.stderr,
            )
    else:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_to_file = {
                executor.submit(
                    process_single_file,
                    p,
                    pipeline,
                    output_dir,
                    args.type,
                    args.resume,
                ): p
                for p in input_files
            }
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    summary = future.result()
                except Exception as e:
                    summary = {
                        "file": file_path.name,
                        "status": "failed",
                        "n_edges": 0,
                        "elapsed_sec": 0,
                        "error": str(e),
                    }
                results.append(summary)
                tag = "OK" if summary["status"] == "success" else "FAIL"
                print(
                    f"  [{tag}] {file_path.name}: {summary['n_edges']} edges, "
                    f"{summary['elapsed_sec']}s",
                    file=sys.stderr,
                )

    total_elapsed = round(time.time() - t_start, 1)
    n_success = sum(1 for r in results if r["status"] == "success")
    n_failed = len(results) - n_success

    batch_summary = {
        "total_files": len(input_files),
        "format": fmt_label,
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
        f"Complete: {n_success}/{len(input_files)} succeeded, "
        f"{batch_summary['total_edges']} total edges, {total_elapsed}s",
        file=sys.stderr,
    )
    print(f"  Summary: {summary_path}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    sys.exit(1 if n_failed == len(input_files) else 0)


if __name__ == "__main__":
    main()
