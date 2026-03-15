#!/usr/bin/env python3
"""
extract_error_patterns.py -- Extract error patterns from GT-annotated JSON files.

Usage:
    python extract_error_patterns.py reference/case_1/18644373_edges_verified.json
    python extract_error_patterns.py reference/     # process all cases
    python extract_error_patterns.py reference/ -o reference/error_patterns.json

Input: *_edges_verified.json / .jsonc files with // comment annotations (✅/⚠️/❌)
Output: error_patterns.json -- structured catalog of error patterns

This script:
  1. Parses annotated JSON (stripping JS-style comments)
  2. Extracts all ❌ and ⚠️ annotations with their field context
  3. Categorizes errors into pattern types
  4. Outputs a structured error catalog for Step 4 prompt tuning
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Parse annotated JSON with comments
# ---------------------------------------------------------------------------

# Regex to detect edge summary lines like:
#   // EV-2000-McPhillips#1 | ✅ 17 正确 | ⚠️ 4 待确认 | ❌ 2 错误
# These are NOT field-level annotations; skip them.
_EDGE_SUMMARY_RE = re.compile(
    r"EV-\S+\s*\|.*[✅⚠️❌].*\d+\s*(正确|待确认|错误)"
)

# Regex to detect "no verification needed" lines like:
#   // 🔵 无需验证（自定义框架分类）
#   // 🔵 epsilon 全部无需验证（自定义坐标编码）
_SKIP_RE = re.compile(r"🔵|无需验证|不逐字验证")


def _strip_js_comments(text: str) -> Tuple[str, List[Dict]]:
    """
    Strip // comments from JSON text while extracting annotations.

    Returns:
        (clean_json_str, annotations)

    Each annotation is:
        {line, type, text, context_field, edge_id}
    """
    lines = text.split("\n")
    clean_lines = []
    annotations = []

    last_field = ""
    current_edge_id = ""

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track field names from JSON keys (before checking comments)
        field_match = re.match(r'\s*"(\w+)"\s*:', line)
        if field_match:
            last_field = field_match.group(1)

        # Track edge_id for context
        edge_id_match = re.search(r'"edge_id"\s*:\s*"([^"]+)"', line)
        if edge_id_match:
            current_edge_id = edge_id_match.group(1)

        # Match // comment
        comment_match = re.search(r'//\s*(.*)', line)

        if comment_match:
            comment_text = comment_match.group(1).strip()

            # Skip edge summary lines (e.g. "EV-xxx | ✅ 17 正确 | ⚠️ 4 ...")
            if _EDGE_SUMMARY_RE.search(comment_text):
                clean_line = line[:comment_match.start()].rstrip()
                clean_lines.append(clean_line)
                continue

            # Skip "no verification needed" / "skip" lines
            if _SKIP_RE.search(comment_text):
                clean_line = line[:comment_match.start()].rstrip()
                clean_lines.append(clean_line)
                continue

            # Determine annotation type
            ann_type = "info"
            if "❌" in comment_text:
                ann_type = "error"
            elif "⚠️" in comment_text:
                ann_type = "warning"
            elif "✅" in comment_text:
                ann_type = "correct"
            else:
                # Not a structured annotation, skip
                clean_line = line[:comment_match.start()].rstrip()
                clean_lines.append(clean_line)
                continue

            # Try to extract field name from the JSON content before the comment
            field_part = line[:comment_match.start()].strip().rstrip(",")
            inline_field_match = re.match(r'"(\w+)"', field_part)
            if inline_field_match:
                last_field = inline_field_match.group(1)

            annotations.append({
                "line": i + 1,
                "type": ann_type,
                "text": comment_text,
                "context_field": last_field,
                "edge_id": current_edge_id,
                "raw_line": stripped,
            })

            # Remove comment from line for clean JSON
            clean_line = line[:comment_match.start()].rstrip()
            clean_lines.append(clean_line)
        else:
            clean_lines.append(line)

    return "\n".join(clean_lines), annotations


def parse_gt_json(gt_path: str) -> Tuple[List[Dict], List[Dict]]:
    """
    Parse an annotated JSON file with // comment annotations.

    Returns:
        (edges_data, annotations)
    """
    with open(gt_path, "r", encoding="utf-8") as f:
        raw = f.read()

    clean_json, annotations = _strip_js_comments(raw)

    # Try to parse the clean JSON
    try:
        data = json.loads(clean_json)
    except json.JSONDecodeError:
        # Try harder: remove trailing commas before } or ]
        clean_json = re.sub(r",\s*([}\]])", r"\1", clean_json)
        # Remove empty lines that might cause issues
        clean_json = re.sub(r"\n\s*\n", "\n", clean_json)
        try:
            data = json.loads(clean_json)
        except json.JSONDecodeError as e:
            print(f"  WARNING: Failed to parse JSON from {gt_path}: {e}", file=sys.stderr)
            print(f"  Annotations still extracted ({len(annotations)} found)", file=sys.stderr)
            return [], annotations

    if isinstance(data, list):
        return data, annotations
    return [data], annotations


# ---------------------------------------------------------------------------
# Categorize errors
# ---------------------------------------------------------------------------

ERROR_CATEGORIES = {
    "covariate_hallucination": [
        r"不存在于本论文",
        r"论文.*无协变量调整",
        r"未在.*方法.*中找到",
        r"未找到",
        r"not found in paper",
    ],
    "numeric_hallucination": [
        r"数值捏造",
        r"数值不存在",
        r"不存在于.*Table",
        r"不存在于PDF",
        r"fabricated",
    ],
    "ci_hallucination": [
        r"CI捏造",
        r"CI.*不存在",
        r"CI未在PDF中.*找到",
        r"CI.*NOT found",
    ],
    "y_label_mismatch": [
        r"Y标签错误",
        r"匹配.*组.*非",
        r"CI匹配.*组.*非",
        r"数值来自.*而非",
        r"label.*mismatch",
    ],
    "sample_data_error": [
        r"数据错误",
        r"数据来自初始.*非最终",
        r"应为.*而非",
        r"sample.*error",
        r"初始.*非最终",
    ],
    "hpp_variable_leakage": [
        r"HPP变量泄漏",
        r"HPP数据集变量.*与论文无关",
        r"HPP.*leak",
    ],
    "adjustment_semantic_error": [
        r"匹配变量.*非.*协变量",
        r"matching.*非.*covariate",
        r"ANOVA无协变量调整",
        r"分层变量.*非.*调整",
        r"matching variable",
        r"非ANOVA协变量",
        r"非回归协变量",
    ],
    "inconsistency": [
        r"不一致",
        r"inconsistent",
        r"mismatch",
        r"矛盾",
    ],
    "value_not_found": [
        r"未在PDF中.*找到",
        r"未在.*精确找到",
        r"not.*found.*PDF",
    ],
}


def categorize_error(text: str) -> str:
    """Categorize an error annotation into a pattern type."""
    for category, patterns in ERROR_CATEGORIES.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return category
    return "other"


# ---------------------------------------------------------------------------
# Extract patterns from annotations
# ---------------------------------------------------------------------------

def extract_patterns(
    annotations: List[Dict],
    edges: List[Dict],
    case_id: str,
) -> List[Dict]:
    """
    Extract structured error patterns from annotations.
    """
    patterns = []

    for ann in annotations:
        if ann["type"] not in ("error", "warning"):
            continue

        category = categorize_error(ann["text"])

        pattern = {
            "case_id": case_id,
            "edge_id": ann.get("edge_id", "unknown"),
            "annotation_type": ann["type"],
            "category": category,
            "field": ann["context_field"],
            "text": ann["text"],
            "line": ann["line"],
        }
        patterns.append(pattern)

    return patterns


# ---------------------------------------------------------------------------
# Aggregate patterns across cases
# ---------------------------------------------------------------------------

def aggregate_patterns(all_patterns: List[Dict]) -> Dict:
    """
    Aggregate error patterns across all cases into a catalog.
    """
    category_counts = Counter(p["category"] for p in all_patterns)
    error_count = sum(1 for p in all_patterns if p["annotation_type"] == "error")
    warning_count = sum(1 for p in all_patterns if p["annotation_type"] == "warning")

    # Group representative examples by category (errors only)
    examples_by_category = defaultdict(list)
    for p in all_patterns:
        if p["annotation_type"] == "error":
            examples_by_category[p["category"]].append({
                "case": p["case_id"],
                "edge_id": p["edge_id"],
                "field": p["field"],
                "text": p["text"],
            })

    # Keep top 3 unique examples per category
    top_examples = {}
    for cat, examples in examples_by_category.items():
        seen = set()
        unique = []
        for ex in examples:
            short = ex["text"][:80]
            if short not in seen:
                seen.add(short)
                unique.append(ex)
        top_examples[cat] = unique[:3]

    # Per-case summary
    cases = defaultdict(lambda: {"errors": 0, "warnings": 0, "categories": Counter()})
    for p in all_patterns:
        c = cases[p["case_id"]]
        if p["annotation_type"] == "error":
            c["errors"] += 1
        else:
            c["warnings"] += 1
        c["categories"][p["category"]] += 1

    return {
        "total_patterns": len(all_patterns),
        "total_errors": error_count,
        "total_warnings": warning_count,
        "num_cases": len(cases),
        "category_distribution": dict(category_counts.most_common()),
        "top_examples": top_examples,
        "per_case": {
            k: {
                "errors": v["errors"],
                "warnings": v["warnings"],
                "top_categories": dict(v["categories"].most_common(5)),
            }
            for k, v in cases.items()
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# File patterns to match as GT annotated files
_GT_FILE_PATTERNS = [
    "_edges_verified.json",
    "_edges_verified.jsonc",
    "gt.json",
    "gt.jsonc",
]


def _is_gt_file(fname: str) -> bool:
    """Check if filename matches any GT file pattern."""
    return any(fname.endswith(pat) for pat in _GT_FILE_PATTERNS)


def process_single_case(gt_path: str) -> Tuple[List[Dict], Dict]:
    """Process a single annotated JSON file."""
    case_id = Path(gt_path).parent.name
    if case_id in ("uploads", ".", "reference"):
        case_id = Path(gt_path).stem

    edges, annotations = parse_gt_json(gt_path)
    patterns = extract_patterns(annotations, edges, case_id)

    errors = [p for p in patterns if p["annotation_type"] == "error"]
    warnings = [p for p in patterns if p["annotation_type"] == "warning"]

    stats = {
        "case_id": case_id,
        "gt_file": Path(gt_path).name,
        "num_edges": len(edges),
        "num_annotations": len(annotations),
        "num_errors": len(errors),
        "num_warnings": len(warnings),
        "error_categories": dict(Counter(p["category"] for p in errors).most_common()),
        "warning_categories": dict(Counter(p["category"] for p in warnings).most_common()),
    }

    return patterns, stats


def main():
    parser = argparse.ArgumentParser(
        description="Extract error patterns from GT-annotated JSON files"
    )
    parser.add_argument(
        "target",
        help="Path to a single GT file or a directory containing case folders",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output path for error_patterns.json (default: <target_dir>/error_patterns.json)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed per-pattern output",
    )
    args = parser.parse_args()

    target = args.target
    all_patterns = []
    all_stats = []

    if os.path.isfile(target):
        patterns, stats = process_single_case(target)
        all_patterns.extend(patterns)
        all_stats.append(stats)
        print(f"Processed: {target}")
        print(f"  Edges: {stats['num_edges']}, Annotations: {stats['num_annotations']}")
        print(f"  Errors: {stats['num_errors']}, Warnings: {stats['num_warnings']}")
        print(f"  Error categories: {stats['error_categories']}")

    elif os.path.isdir(target):
        found = 0
        for root, dirs, files in sorted(os.walk(target)):
            for fname in sorted(files):
                if _is_gt_file(fname):
                    gt_path = os.path.join(root, fname)
                    patterns, stats = process_single_case(gt_path)
                    all_patterns.extend(patterns)
                    all_stats.append(stats)
                    found += 1
                    print(
                        f"  [{found:2d}] {stats['case_id']}: "
                        f"{stats['num_errors']} errors, "
                        f"{stats['num_warnings']} warnings "
                        f"({stats['num_edges']} edges)"
                    )
                    if args.verbose:
                        for cat, count in stats["error_categories"].items():
                            print(f"       {count:3d}x {cat}")

        if found == 0:
            print(f"No GT files found in {target}", file=sys.stderr)
            print(f"Looking for files matching: {_GT_FILE_PATTERNS}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Not found: {target}", file=sys.stderr)
        sys.exit(1)

    # Aggregate
    catalog = aggregate_patterns(all_patterns)
    catalog["per_case_stats"] = all_stats

    # Determine output path
    if args.output:
        output_path = args.output
    elif os.path.isdir(target):
        output_path = os.path.join(target, "error_patterns.json")
    else:
        output_path = os.path.join(os.path.dirname(target), "error_patterns.json")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"Total cases: {catalog['num_cases']}")
    print(f"Total errors: {catalog['total_errors']}")
    print(f"Total warnings: {catalog['total_warnings']}")
    print(f"Category distribution:")
    for cat, count in catalog["category_distribution"].items():
        pct = count / max(catalog["total_patterns"], 1) * 100
        print(f"  {count:3d}x ({pct:4.1f}%)  {cat}")
    print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()