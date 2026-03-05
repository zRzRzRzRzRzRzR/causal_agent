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


def collect_files_from_dir(directory: Path, xml_mode: bool) -> list[Path]:
    """Collect input files from a single directory."""
    if xml_mode:
        return sorted(list(directory.glob("*.nxml")) + list(directory.glob("*.xml")))
    else:
        return sorted(directory.glob("*.pdf"))


def collect_batches(input_dir: Path, xml_mode: bool) -> dict[str, list[Path]]:
    """
    Detect directory structure and collect files.

    - If input_dir contains sub-folders with target files → treat each sub-folder as a batch
    - If input_dir directly contains target files → single batch named '_root'
    """
    batches = {}

    # Check sub-directories first
    subdirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    for subdir in subdirs:
        files = collect_files_from_dir(subdir, xml_mode)
        if files:
            batches[subdir.name] = files

    # If no sub-folder batches found, fall back to flat mode
    if not batches:
        files = collect_files_from_dir(input_dir, xml_mode)
        if files:
            batches["_root"] = files

    return batches


def process_batch(
    batch_name: str,
    files: list[Path],
    pipeline: EdgeExtractionPipeline,
    output_dir: Path,
    force_type: str,
    resume: bool,
    max_workers: int,
) -> list[dict]:
    """Process all files in one batch (sub-folder), return list of result dicts."""
    # Each batch gets its own output sub-folder (unless _root)
    if batch_name == "_root":
        batch_output = output_dir
    else:
        batch_output = output_dir / batch_name
    batch_output.mkdir(parents=True, exist_ok=True)

    results = []

    if max_workers <= 1:
        for idx, file_path in enumerate(files, 1):
            print(f"    [{idx}/{len(files)}] {file_path.name}", file=sys.stderr)
            summary = process_single_file(
                file_path, pipeline, batch_output, force_type, resume
            )
            # Add batch info
            summary["batch"] = batch_name
            results.append(summary)
            tag = "OK" if summary["status"] == "success" else "FAIL"
            print(
                f"      [{tag}] {summary['n_edges']} edges, {summary['elapsed_sec']}s",
                file=sys.stderr,
            )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {
                executor.submit(
                    process_single_file,
                    p,
                    pipeline,
                    batch_output,
                    force_type,
                    resume,
                ): p
                for p in files
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
                summary["batch"] = batch_name
                results.append(summary)
                tag = "OK" if summary["status"] == "success" else "FAIL"
                print(
                    f"      [{tag}] {file_path.name}: {summary['n_edges']} edges, "
                    f"{summary['elapsed_sec']}s",
                    file=sys.stderr,
                )

    return results


def main():
    parser = argparse.ArgumentParser(description="Batch evidence edge extraction")
    parser.add_argument(
        "-i",
        "--input-dir",
        default="./evidence_card",
        help="Parent dir containing sub-folders of PDFs, or a flat dir of PDFs",
    )
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
    parser.add_argument(
        "--xml",
        action="store_true",
        help="Input files are JATS/NLM XML (.nxml/.xml) — skip OCR entirely",
    )
    # ── NEW: batch control ──
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Global max files to process across all batches in order (0 = no limit, process all)",
    )
    parser.add_argument(
        "--batches",
        nargs="*",
        default=None,
        help="Only process these sub-folder names (e.g. --batches 98 99). Default: all.",
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fmt_label = "XML" if args.xml else "PDF"

    # ── Collect batches (sub-folder → files) ──
    all_batches = collect_batches(input_dir, args.xml)

    # Filter batches if --batches specified
    if args.batches is not None:
        selected = set(args.batches)
        all_batches = {k: v for k, v in all_batches.items() if k in selected}

    # Apply --batch-size as a global cap across all batches (folders processed in order)
    if args.batch_size > 0:
        capped: dict[str, list[Path]] = {}
        remaining = args.batch_size
        for k, v in all_batches.items():
            take = v[:remaining]
            if take:
                capped[k] = take
            remaining -= len(take)
            if remaining <= 0:
                break
        all_batches = capped

    total_files = sum(len(v) for v in all_batches.values())

    print(f"{'='*60}", file=sys.stderr)
    print(f"Batch Evidence Edge Extraction", file=sys.stderr)
    print(f"  Input:   {input_dir.resolve()}", file=sys.stderr)
    print(f"  Output:  {output_dir.resolve()}", file=sys.stderr)
    print(f"  Format:  {fmt_label}", file=sys.stderr)
    print(
        f"  Batches: {len(all_batches)} ({', '.join(all_batches.keys())})",
        file=sys.stderr,
    )
    print(f"  Total files: {total_files}", file=sys.stderr)
    print(f"  Batch size limit: {args.batch_size or 'unlimited'}", file=sys.stderr)
    print(f"  Type:    {args.type or 'auto-classify'}", file=sys.stderr)
    print(f"  Workers: {args.max_workers}", file=sys.stderr)
    print(f"  Resume:  {args.resume}", file=sys.stderr)
    print(f"  Max retries: {args.max_retries}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    if total_files == 0:
        print("No input files found. Exiting.", file=sys.stderr)
        sys.exit(0)

    client = GLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

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

    all_results = []
    batch_summaries = {}
    t_start = time.time()

    for batch_idx, (batch_name, files) in enumerate(all_batches.items(), 1):
        print(f"\n{'━'*60}", file=sys.stderr)
        print(
            f"  BATCH [{batch_idx}/{len(all_batches)}]: {batch_name} "
            f"({len(files)} {fmt_label}s)",
            file=sys.stderr,
        )
        print(f"{'━'*60}", file=sys.stderr)

        t_batch = time.time()
        batch_results = process_batch(
            batch_name=batch_name,
            files=files,
            pipeline=pipeline,
            output_dir=output_dir,
            force_type=args.type,
            resume=args.resume,
            max_workers=args.max_workers,
        )
        batch_elapsed = round(time.time() - t_batch, 1)

        all_results.extend(batch_results)

        n_ok = sum(1 for r in batch_results if r["status"] == "success")
        n_edges = sum(r["n_edges"] for r in batch_results)
        batch_summaries[batch_name] = {
            "files": len(files),
            "success": n_ok,
            "failed": len(files) - n_ok,
            "total_edges": n_edges,
            "elapsed_sec": batch_elapsed,
        }
        print(
            f"  Batch {batch_name} done: {n_ok}/{len(files)} OK, "
            f"{n_edges} edges, {batch_elapsed}s",
            file=sys.stderr,
        )

    # ── Global summary ──
    total_elapsed = round(time.time() - t_start, 1)
    n_success = sum(1 for r in all_results if r["status"] == "success")
    n_failed = len(all_results) - n_success

    global_summary = {
        "total_files": total_files,
        "total_batches": len(all_batches),
        "format": fmt_label,
        "success": n_success,
        "failed": n_failed,
        "total_edges": sum(r["n_edges"] for r in all_results),
        "total_elapsed_sec": total_elapsed,
        "batch_summaries": batch_summaries,
        "details": all_results,
    }

    summary_path = output_dir / "_batch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(global_summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}", file=sys.stderr)
    print(
        f"Complete: {n_success}/{total_files} succeeded across "
        f"{len(all_batches)} batches, "
        f"{global_summary['total_edges']} total edges, {total_elapsed}s",
        file=sys.stderr,
    )
    print(f"  Summary: {summary_path}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    sys.exit(1 if n_failed == total_files else 0)


if __name__ == "__main__":
    main()