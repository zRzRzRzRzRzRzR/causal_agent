import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.llm_client import GLMClient
from src.ocr import get_pdf_text
from src.ocr import init_extractor as init_ocr
from src.pipeline import EdgeExtractionPipeline


def is_file_completed(file_path: Path, output_dir: Path) -> bool:
    """
    Check whether a file has already been fully processed.
    A file is considered complete if its output sub-directory contains edges.json.
    """
    stem = file_path.stem
    edges_file = output_dir / stem / "edges.json"
    return edges_file.exists()


def process_single_file(
    file_path: Path,
    pipeline: EdgeExtractionPipeline,
    output_dir: Path,
    force_type: str = None,
    resume: bool = False,
) -> dict:
    t0 = time.time()

    # ── File-level skip: if edges.json already exists, skip entirely ──
    if resume and is_file_completed(file_path, output_dir):
        # Read existing edges count for accurate reporting
        edges_file = output_dir / file_path.stem / "edges.json"
        with open(edges_file, "r", encoding="utf-8") as f:
            existing_edges = json.load(f)
        n_edges = len(existing_edges) if isinstance(existing_edges, list) else 0

        return {
            "file": file_path.name,
            "status": "skipped",
            "n_edges": n_edges,
            "elapsed_sec": 0,
            "error": None,
        }

    edges = pipeline.run(
        pdf_path=str(file_path),
        force_type=force_type,
        output_dir=str(output_dir),
        resume=resume,
    )
    n_edges = len(edges) if edges else 0

    elapsed = round(time.time() - t0, 1)

    return {
        "file": file_path.name,
        "status": "success",
        "n_edges": n_edges,
        "elapsed_sec": elapsed,
        "error": None,
    }


def collect_files_from_dir(directory: Path) -> list[Path]:
    """Collect input files from a single directory."""
    return sorted(directory.glob("*.pdf"))


def collect_batches(input_dir: Path) -> dict[str, list[Path]]:
    """
    Detect directory structure and collect files.

    - If input_dir contains sub-folders with target files → treat each sub-folder as a batch
    - If input_dir directly contains target files → single batch named '_root'
    """
    batches = {}

    # Check sub-directories first
    subdirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    for subdir in subdirs:
        files = collect_files_from_dir(subdir)
        if files:
            batches[subdir.name] = files

    # If no sub-folder batches found, fall back to flat mode
    if not batches:
        files = collect_files_from_dir(input_dir)
        if files:
            batches["_root"] = files

    return batches


def filter_completed_files(
    batches: dict[str, list[Path]], output_dir: Path
) -> tuple[dict[str, list[Path]], int]:
    """
    Remove already-completed files from each batch.
    Returns (filtered_batches, n_skipped).
    """
    filtered = {}
    n_skipped = 0
    for batch_name, files in batches.items():
        if batch_name == "_root":
            batch_output = output_dir
        else:
            batch_output = output_dir / batch_name

        pending = []
        for f in files:
            if is_file_completed(f, batch_output):
                n_skipped += 1
            else:
                pending.append(f)

        if pending:
            filtered[batch_name] = pending

    return filtered, n_skipped


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
            tag = (
                "SKIP"
                if summary["status"] == "skipped"
                else ("OK" if summary["status"] == "success" else "FAIL")
            )
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
                summary = future.result()
                summary["batch"] = batch_name
                results.append(summary)
                tag = (
                    "SKIP"
                    if summary["status"] == "skipped"
                    else ("OK" if summary["status"] == "success" else "FAIL")
                )
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
    parser.add_argument("--dpi", type=int, default=400)
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
        help="Skip files whose edges.json already exists, and skip steps whose cache exists",
    )
    # ── Batch control ──
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Max NEW files to process across all batches (0 = no limit). "
        "Already-completed files are excluded from this count when --resume is set.",
    )
    parser.add_argument(
        "--batches",
        nargs="*",
        default=None,
        help="Only process these sub-folder names (e.g. --batches 98 99). Default: all.",
    )
    parser.add_argument(
        "--reference-dir",
        default=None,
        help="Path to reference GT directory (default: auto-detect ./reference/)",
    )
    parser.add_argument(
        "--error-patterns",
        default=None,
        help="Path to error_patterns.json (default: auto-detect ./reference/error_patterns.json)",
    )
    parser.add_argument(
        "--no_validate_pages",
        action="store_true",
        help="Skip using vision model to cut paper references",
    )
    parser.add_argument(
        "--phase-c-autofix",
        action="store_true",
        help=(
            "Enable Step 4 Phase C: deterministic autofix using Phase B "
            "suggested_fix. Off by default. With this flag alone, runs in "
            "fill-only mode — only fills missing/empty values "
            "(None / '' / [] / [None,None]) and never overwrites a "
            "non-empty value, eliminating the risk of LLM 'corrections' "
            "stomping on correct data."
        ),
    )
    parser.add_argument(
        "--phase-c-aggressive",
        action="store_true",
        help=(
            "Allow Phase C to also overwrite existing non-empty values "
            "(e.g. n: 5800 → 5792, model_type: linear → ANCOVA). Risky — "
            "Phase B's LLM correctly identifies many real errors but also "
            "occasionally overwrites correct values with wrong ones. Off "
            "by default. Has no effect unless --phase-c-autofix is also set."
        ),
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_batches = collect_batches(input_dir)
    if args.batches is not None:
        selected = set(args.batches)
        all_batches = {k: v for k, v in all_batches.items() if k in selected}

    total_found = sum(len(v) for v in all_batches.values())
    n_skipped_completed = 0
    if args.resume:
        all_batches, n_skipped_completed = filter_completed_files(
            all_batches, output_dir
        )

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
    print(f"  Format:  {"PDF"}", file=sys.stderr)
    print(
        f"  Batches: {len(all_batches)} ({', '.join(all_batches.keys()) if all_batches else 'none'})",
        file=sys.stderr,
    )
    print(f"  Total found: {total_found}", file=sys.stderr)
    if args.resume:
        print(f"  Already completed (skipped): {n_skipped_completed}", file=sys.stderr)
    print(f"  To process: {total_files}", file=sys.stderr)
    print(f"  Batch size limit: {args.batch_size or 'unlimited'}", file=sys.stderr)
    print(f"  Type:    {args.type or 'auto-classify'}", file=sys.stderr)
    print(f"  Workers: {args.max_workers}", file=sys.stderr)
    print(f"  Resume:  {args.resume}", file=sys.stderr)
    print(f"  Max retries: {args.max_retries}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    if total_files == 0:
        if n_skipped_completed > 0:
            print(
                f"All {n_skipped_completed} files already completed. Nothing to do.",
                file=sys.stderr,
            )
        else:
            print("No input files found. Exiting.", file=sys.stderr)
        sys.exit(0)

    client = GLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

    pipeline = EdgeExtractionPipeline(
        client=client,
        ocr_text_func=get_pdf_text,
        ocr_init_func=init_ocr,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
        hpp_dict_path=args.hpp_dict,
        max_retries=args.max_retries,
        reference_dir=args.reference_dir,
        error_patterns_path=args.error_patterns,
        enable_phase_c_autofix=args.phase_c_autofix,
        phase_c_aggressive=args.phase_c_aggressive,
    )

    all_results = []
    batch_summaries = {}
    t_start = time.time()

    for batch_idx, (batch_name, files) in enumerate(all_batches.items(), 1):
        print(f"\n{'━'*60}", file=sys.stderr)
        print(
            f"  BATCH [{batch_idx}/{len(all_batches)}]: {batch_name} "
            f"({len(files)} {"PDF"}s)",
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
        n_skip = sum(1 for r in batch_results if r["status"] == "skipped")
        n_edges = sum(r["n_edges"] for r in batch_results)
        batch_summaries[batch_name] = {
            "files": len(files),
            "success": n_ok,
            "skipped": n_skip,
            "failed": len(files) - n_ok - n_skip,
            "total_edges": n_edges,
            "elapsed_sec": batch_elapsed,
        }
        print(
            f"  Batch {batch_name} done: {n_ok}/{len(files)} OK, "
            f"{n_skip} skipped, {n_edges} edges, {batch_elapsed}s",
            file=sys.stderr,
        )

    # ── Global summary ──
    total_elapsed = round(time.time() - t_start, 1)
    n_success = sum(1 for r in all_results if r["status"] == "success")
    n_skipped = sum(1 for r in all_results if r["status"] == "skipped")
    n_failed = len(all_results) - n_success - n_skipped

    global_summary = {
        "total_files_found": total_found,
        "total_files_processed": total_files,
        "total_batches": len(all_batches),
        "format": "PDF",
        "success": n_success,
        "skipped": n_skipped + n_skipped_completed,
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
        f"Complete: {n_success}/{total_files} succeeded, "
        f"{n_skipped} skipped in-run, {n_skipped_completed} pre-filtered, "
        f"{n_failed} failed across {len(all_batches)} batches, "
        f"{global_summary['total_edges']} total edges, {total_elapsed}s",
        file=sys.stderr,
    )
    print(f"  Summary: {summary_path}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    sys.exit(1 if n_failed == total_files else 0)


if __name__ == "__main__":
    main()
