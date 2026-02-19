"""
pipeline.py — Edge extraction pipeline with semantic validation and retry.

Four-step pipeline:
  Step 0: Classify paper type (interventional/causal/mechanistic/associational)
  Step 1: Enumerate all X->Y statistical edges + deduplicate
  Step 2: Fill each edge into the HPP template (with retry on semantic errors)
  Step 3: Review, rerank, consistency check, spot-check, quality report

Key additions over the original:
  - Step 1 now includes fuzzy deduplication of extracted edges
  - Step 2 runs semantic validation after filling, and retries with a
    correction prompt if blocking errors are found (up to max_retries)
  - Step 3 includes fuzzy duplicate detection across filled edges
"""

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .hpp_mapper import HPPMapper, get_hpp_context
from .llm_client import GLMClient
from .review import (
    check_cross_edge_consistency,
    generate_quality_report,
    rerank_hpp_mapping,
    spot_check_values,
)
from .semantic_validator import (
    deduplicate_step1_edges,
    detect_fuzzy_duplicates_step3,
    format_issues_for_prompt,
    has_blocking_errors,
    validate_semantics,
)
from .template_utils import (
    build_filled_edge,
    load_template,
    prepare_template_for_prompt,
    prepare_template_with_comments,
)
from .utils import save_json

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SRC_DIR.parent
_PROMPTS_DIR = _PROJECT_DIR / "prompts"
_TEMPLATES_DIR = _PROJECT_DIR / "templates"
_DEFAULT_HPP_DICT = _TEMPLATES_DIR / "pheno_ai_data_dictionaries_simplified.json"
_DEFAULT_TEMPLATE = _TEMPLATES_DIR / "hpp_mapping_template.json"


def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts directory."""
    path = _PROMPTS_DIR / f"{name}.md"
    assert path.exists(), f"Prompt file not found: {path}"
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "latin1"):
        decoded = (
            raw.decode(enc, errors="strict") if enc != "latin1" else raw.decode(enc)
        )
        return decoded
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Step 0: Classify
# ---------------------------------------------------------------------------


def step0_classify(client: GLMClient, pdf_text: str) -> Dict[str, Any]:
    """Classify paper into: interventional / causal / mechanistic / associational."""
    prompt_template = _load_prompt("step0_classify")
    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text}"
    result = client.call_json(full_prompt)

    print(
        f"[Step 0] Classification: {result.get('primary_category')} "
        f"(confidence: {result.get('confidence', 'N/A')})",
        file=sys.stderr,
    )
    return result


# ---------------------------------------------------------------------------
# Step 1: Enumerate edges (with deduplication)
# ---------------------------------------------------------------------------


_BASELINE_DEMO_KEYWORDS = {
    "age",
    "sex",
    "gender",
    "bmi",
    "weight",
    "height",
    "ethnicity",
    "race",
    "waist",
    "% female",
    "% male",
    "systolic",
    "diastolic",
    "years",
}


def _is_baseline_check(edge: Dict) -> bool:
    """Return True if this edge is a baseline balance check row (not a real finding)."""
    if edge.get("significant", True):
        return False
    source = str(edge.get("source", "")).lower()
    if "table 1" not in source and "supplementary" not in source:
        return False
    y = str(edge.get("Y", "")).lower()
    return any(kw in y for kw in _BASELINE_DEMO_KEYWORDS)


def step1_enumerate_edges(
    client: GLMClient, pdf_text: str, evidence_type: str
) -> Dict[str, Any]:
    """Extract all X->Y statistical edges from the paper, then deduplicate."""
    prompt_template = _load_prompt("step1_edges")
    prompt_template = prompt_template.replace("{evidence_type}", evidence_type)
    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text}"

    result = client.call_json(full_prompt, max_tokens=32768)

    edges = result.get("edges", [])
    paper_info = result.get("paper_info", {})

    # --- Filter baseline balance check rows ---
    filtered = [e for e in edges if not _is_baseline_check(e)]
    n_filtered = len(edges) - len(filtered)
    if n_filtered:
        print(
            f"[Step 1] Filtered {n_filtered} baseline balance check edges",
            file=sys.stderr,
        )

    # --- Fuzzy deduplication ---
    unique_edges, removed_dups = deduplicate_step1_edges(filtered)
    if removed_dups:
        print(
            f"[Step 1] Deduplicated: removed {len(removed_dups)} duplicate edges",
            file=sys.stderr,
        )
        for dup in removed_dups:
            print(
                f"    Removed #{dup['removed_index']+1} (kept #{dup['kept_index']+1}): "
                f"{dup['reason']}",
                file=sys.stderr,
            )

    result["edges"] = unique_edges
    result["removed_duplicates"] = removed_dups

    print(
        f"[Step 1] Found {len(unique_edges)} unique edges from "
        f"{paper_info.get('first_author', '?')} {paper_info.get('year', '?')}",
        file=sys.stderr,
    )
    for i, e in enumerate(unique_edges):
        sig = "+" if e.get("significant") else "-"
        print(
            f"  [{i+1}] {sig} {e.get('X', '?')} -> {e.get('Y', '?')}"
            f"  ({e.get('source', '')})",
            file=sys.stderr,
        )
    return result


# ---------------------------------------------------------------------------
# Step 2: Fill one edge (with semantic validation + retry)
# ---------------------------------------------------------------------------


def _build_correction_prompt(
    original_json: Dict,
    semantic_issues: List[Dict],
    format_issues: List[str],
) -> str:
    """
    Build a correction prompt that tells the LLM what went wrong
    and asks it to fix the specific fields.
    """
    lines = [
        "Your previous output has semantic and/or structural errors. "
        "Please fix the following issues and return the corrected JSON.\n",
    ]

    # Semantic issues (from validate_semantics)
    if semantic_issues:
        lines.append("## Semantic errors detected:\n")
        lines.append(format_issues_for_prompt(semantic_issues))
        lines.append("")

    # Format issues (from validate_filled_edge)
    hard_format = [i for i in format_issues if not i.startswith("WARNING")]
    if hard_format:
        lines.append("## Format/structural errors:\n")
        for i, issue in enumerate(hard_format, 1):
            lines.append(f"{i}. {issue}")
        lines.append("")

    lines.append("## Your previous output (to be corrected):\n")
    lines.append("```json")
    lines.append(json.dumps(original_json, ensure_ascii=False, indent=2))
    lines.append("```\n")

    lines.append(
        "Please return a COMPLETE corrected JSON object. "
        "Fix all the errors listed above. "
        "Do NOT add // comments. Output valid JSON only."
    )

    return "\n".join(lines)


def step2_fill_one_edge(
    client: GLMClient,
    pdf_text: str,
    edge: Dict,
    paper_info: Dict,
    evidence_type: str,
    annotated_template: Dict,
    pdf_name: str,
    template_path: Optional[str] = None,
    hpp_dict_path: Optional[str] = None,
    max_retries: int = 2,
) -> Dict:
    """
    Fill a single edge into the HPP template.
    After filling, runs semantic validation. If blocking errors are found,
    sends a correction prompt back to the LLM and retries (up to max_retries).
    """
    prompt_template = _load_prompt("step2_fill_template")
    template_json = prepare_template_for_prompt(annotated_template)

    if template_path:
        template_with_hints = prepare_template_with_comments(template_path)
    else:
        template_with_hints = template_json

    replacements = {
        "{edge_index}": str(edge.get("edge_index", 1)),
        "{X}": str(edge.get("X", "")),
        "{C}": str(edge.get("C", "")),
        "{Y}": str(edge.get("Y", "")),
        "{subgroup}": str(edge.get("subgroup", "overall")),
        "{outcome_type}": str(edge.get("outcome_type", "")),
        "{effect_scale}": str(edge.get("effect_scale", "")),
        "{estimate}": str(edge.get("estimate", "null")),
        "{ci}": str(edge.get("ci", [None, None])),
        "{p_value}": str(edge.get("p_value", "null")),
        "{source}": str(edge.get("source", "")),
        "{first_author}": str(paper_info.get("first_author", "")),
        "{year}": str(paper_info.get("year", "")),
        "{doi}": str(paper_info.get("doi", "")),
        "{evidence_type}": evidence_type,
        "{template_json}": template_with_hints,
    }

    if hpp_dict_path:
        hpp_context = get_hpp_context(edge, dict_path=hpp_dict_path)
        print(
            f"         [HPP] Retrieved ~{len(hpp_context)//4} tokens of field context",
            file=sys.stderr,
        )
    else:
        hpp_context = (
            "(HPP data dictionary not configured. "
            "Fill hpp_mapping based on available information.)"
        )
    replacements["{hpp_context}"] = hpp_context

    for placeholder, value in replacements.items():
        prompt_template = prompt_template.replace(placeholder, value)

    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text}"

    # --- Initial LLM call ---
    llm_output = client.call_json(full_prompt, max_tokens=32768)
    filled, is_valid, format_issues, fill_rate = build_filled_edge(
        annotated_template=annotated_template,
        llm_output=llm_output,
        edge=edge,
        paper_info=paper_info,
        evidence_type=evidence_type,
        pdf_name=pdf_name,
    )

    # --- Semantic validation ---
    semantic_issues = validate_semantics(filled, evidence_type=evidence_type)

    if semantic_issues:
        n_err = sum(1 for i in semantic_issues if i["severity"] == "error")
        n_warn = sum(1 for i in semantic_issues if i["severity"] == "warning")
        print(
            f"  [Semantic] {n_err} errors, {n_warn} warnings",
            file=sys.stderr,
        )
        for iss in semantic_issues:
            level = "X" if iss["severity"] == "error" else "!"
            print(f"    [{level}] {iss['check']}: {iss['message']}", file=sys.stderr)

    # --- Retry loop if blocking errors exist ---
    attempt = 0
    while has_blocking_errors(semantic_issues) and attempt < max_retries:
        attempt += 1
        print(
            f"  [Retry {attempt}/{max_retries}] Sending correction prompt ...",
            file=sys.stderr,
        )

        correction_prompt = _build_correction_prompt(
            original_json=filled,
            semantic_issues=semantic_issues,
            format_issues=format_issues,
        )

        # Append paper text for context (truncated to leave room for correction)
        correction_full = (
            f"{correction_prompt}\n\n---\n\n**Paper (for reference)**\n\n" f"{pdf_text}"
        )

        llm_output_retry = client.call_json(correction_full, max_tokens=32768)
        filled, is_valid, format_issues, fill_rate = build_filled_edge(
            annotated_template=annotated_template,
            llm_output=llm_output_retry,
            edge=edge,
            paper_info=paper_info,
            evidence_type=evidence_type,
            pdf_name=pdf_name,
        )

        semantic_issues = validate_semantics(filled, evidence_type=evidence_type)

        if semantic_issues:
            n_err = sum(1 for i in semantic_issues if i["severity"] == "error")
            n_warn = sum(1 for i in semantic_issues if i["severity"] == "warning")
            print(
                f"  [Retry {attempt}] After correction: {n_err} errors, {n_warn} warnings",
                file=sys.stderr,
            )
        else:
            print(f"  [Retry {attempt}] All semantic issues resolved", file=sys.stderr)

    # Attach validation metadata to the edge for downstream inspection
    filled["_validation"] = {
        "semantic_issues": semantic_issues,
        "format_issues": format_issues,
        "fill_rate": fill_rate,
        "retries_used": attempt,
        "is_format_valid": is_valid,
        "is_semantically_valid": not has_blocking_errors(semantic_issues),
    }

    return filled


# ---------------------------------------------------------------------------
# Step 3: Review (with fuzzy duplicate detection)
# ---------------------------------------------------------------------------


def step3_review(
    edges: List[Dict],
    pdf_text: str,
    client: GLMClient,
    hpp_dict_path: Optional[str] = None,
    enable_rerank: bool = True,
    enable_spot_check: bool = True,
    spot_check_sample: int = 5,
) -> Tuple[List[Dict], Dict]:
    print(f"\n[Step 3] Reviewing {len(edges)} edges ...", file=sys.stderr)

    all_rerank_changes: List[Dict] = []
    if enable_rerank and hpp_dict_path:
        print("  [3a] Reranking HPP mappings ...", file=sys.stderr)
        with open(hpp_dict_path, "r", encoding="utf-8") as f:
            raw_dict = json.load(f)
        mapper = HPPMapper(raw_dict)

        for i, edge in enumerate(edges):
            changes = rerank_hpp_mapping(edge, mapper, client)
            all_rerank_changes.append(changes)
            if changes:
                print(
                    f"    Edge #{i + 1}: reranked {list(changes.keys())}",
                    file=sys.stderr,
                )
        n = sum(len(c) for c in all_rerank_changes)
        print(f"    {n} mapping(s) updated", file=sys.stderr)
    else:
        print("  [3a] Rerank skipped", file=sys.stderr)

    # 3b. Cross-edge consistency (original exact-match checks)
    print("  [3b] Cross-edge consistency ...", file=sys.stderr)
    consistency_issues = check_cross_edge_consistency(edges)

    # 3b+. Fuzzy duplicate detection (new)
    fuzzy_dups = detect_fuzzy_duplicates_step3(edges)
    if fuzzy_dups:
        print(
            f"    [3b+] Found {len(fuzzy_dups)} fuzzy duplicate pairs",
            file=sys.stderr,
        )
        for dup in fuzzy_dups:
            print(f"      {dup['message']}", file=sys.stderr)
    consistency_issues.extend(fuzzy_dups)

    ne = sum(1 for x in consistency_issues if x.get("severity") == "error")
    nw = sum(1 for x in consistency_issues if x.get("severity") == "warning")
    print(f"    {ne} errors, {nw} warnings", file=sys.stderr)

    # 3c. Spot-check
    spot_checks: List[Dict] = []
    if enable_spot_check:
        ns = min(spot_check_sample, len(edges))
        print(f"  [3c] Spot-checking {ns} edges ...", file=sys.stderr)
        spot_checks = spot_check_values(
            edges, pdf_text, client, sample_size=spot_check_sample
        )
        verdicts = Counter(c.get("verdict", "?") for c in spot_checks)
        print(f"    Results: {dict(verdicts)}", file=sys.stderr)
    else:
        print("  [3c] Spot-check skipped", file=sys.stderr)

    # 3d. Quality report
    print("  [3d] Generating quality report ...", file=sys.stderr)
    report = generate_quality_report(
        edges, consistency_issues, spot_checks, all_rerank_changes
    )

    s = report["summary"]
    print(
        f"\n  [Step 3 Summary]\n"
        f"    Valid: {s['valid_edges']}/{s['total_edges']}\n"
        f"    Avg fill: {s['avg_fill_rate']:.1%}\n"
        f"    Errors: {s['validation_errors']}, "
        f"Warnings: {s['validation_warnings']}\n"
        f"    Consistency: {s['consistency_issues']}\n"
        f"    Spot-check: {s['spot_check_verdicts']}\n"
        f"    Rerank: {s['rerank_changes']}",
        file=sys.stderr,
    )
    for action in report.get("action_items", []):
        print(f"    {action}", file=sys.stderr)

    return edges, report


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------


class EdgeExtractionPipeline:
    """
    Four-step pipeline:
      Step 0: Classify paper type
      Step 1: Enumerate all statistical edges (with deduplication)
      Step 2: Fill each edge into the HPP template (with semantic validation + retry)
      Step 3: Review, rerank, consistency check, spot-check, quality report
    """

    def __init__(
        self,
        client: GLMClient,
        ocr_text_func: Callable[[str], str],
        ocr_init_func: Optional[Callable] = None,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
        hpp_dict_path: Optional[str] = None,
        template_path: Optional[str] = None,
        # Step 2 retry options
        max_retries: int = 2,
        # Step 3 options
        enable_step3: bool = True,
        enable_rerank: bool = True,
        enable_spot_check: bool = True,
        spot_check_sample: int = 5,
    ):
        self.client = client
        self.ocr_text_func = ocr_text_func
        self.template_path = template_path or str(_DEFAULT_TEMPLATE)
        self.annotated_template = load_template(self.template_path)
        self.max_retries = max_retries
        print(f"[Pipeline] Template: {self.template_path}", file=sys.stderr)

        if hpp_dict_path:
            self.hpp_dict_path = hpp_dict_path
        elif _DEFAULT_HPP_DICT.exists():
            self.hpp_dict_path = str(_DEFAULT_HPP_DICT)
        else:
            self.hpp_dict_path = None
        print(f"[Pipeline] HPP dict: {self.hpp_dict_path}", file=sys.stderr)

        # Step 3 flags
        self.enable_step3 = enable_step3
        self.enable_rerank = enable_rerank
        self.enable_spot_check = enable_spot_check
        self.spot_check_sample = spot_check_sample

        if ocr_init_func is not None:
            ocr_init_func(
                ocr_output_dir=ocr_output_dir,
                client=client,
                dpi=ocr_dpi,
                validate_pages=ocr_validate_pages,
            )

    def _get_pdf_text(self, pdf_path: str) -> str:
        return self.ocr_text_func(pdf_path)

    def run(
        self,
        pdf_path: str,
        force_type: Optional[str] = None,
        output_dir: Optional[str] = None,
        resume: bool = False,
    ) -> List[Dict]:
        pdf_name = Path(pdf_path).stem
        base_dir = Path(output_dir) if output_dir else None

        pdf_dir = None
        if base_dir:
            pdf_dir = base_dir / pdf_name
            pdf_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[Pipeline] Processing: {pdf_name}", file=sys.stderr)
        if pdf_dir:
            print(f"[Pipeline] Output folder: {pdf_dir}", file=sys.stderr)
        if resume:
            print(f"[Pipeline] Resume mode: will skip completed steps", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        pdf_text = self._get_pdf_text(pdf_path)

        # -- Step 0: Classify (skip if cached) --
        step0_cached = False
        if resume and pdf_dir and (pdf_dir / "step0_classification.json").exists():
            with open(
                pdf_dir / "step0_classification.json", "r", encoding="utf-8"
            ) as f:
                classification = json.load(f)
            evidence_type = classification.get("primary_category", "associational")
            print(f"[Step 0] CACHED: {evidence_type}", file=sys.stderr)
            step0_cached = True
        elif force_type:
            evidence_type = force_type
            classification = {"primary_category": force_type, "forced": True}
            print(f"[Step 0] Forced type: {evidence_type}", file=sys.stderr)
        else:
            print("\n[Step 0] Classifying paper ...", file=sys.stderr)
            classification = step0_classify(self.client, pdf_text)
            evidence_type = classification.get("primary_category", "associational")

        if pdf_dir and not step0_cached:
            save_json(pdf_dir / "step0_classification.json", classification)

        # -- Step 1: Enumerate edges (skip if cached) --
        step1_cached = False
        if resume and pdf_dir and (pdf_dir / "step1_edges.json").exists():
            with open(pdf_dir / "step1_edges.json", "r", encoding="utf-8") as f:
                step1_result = json.load(f)
            edges_list = step1_result.get("edges", [])
            paper_info = step1_result.get("paper_info", {})
            print(
                f"[Step 1] CACHED: {len(edges_list)} edges from "
                f"{paper_info.get('first_author', '?')} {paper_info.get('year', '?')}",
                file=sys.stderr,
            )
            step1_cached = True
        else:
            print("\n[Step 1] Enumerating edges ...", file=sys.stderr)
            step1_result = step1_enumerate_edges(self.client, pdf_text, evidence_type)
            edges_list = step1_result.get("edges", [])
            paper_info = step1_result.get("paper_info", {})
            if pdf_dir:
                save_json(pdf_dir / "step1_edges.json", step1_result)

        # -- Step 2: Fill templates (with semantic validation + retry) --
        print(
            f"\n[Step 2] Filling templates for {len(edges_list)} edges ...",
            file=sys.stderr,
        )
        all_filled_edges: List[Dict] = []
        total_retries = 0

        for i, edge in enumerate(edges_list):
            idx = edge.get("edge_index", i + 1)
            y_short = str(edge.get("Y", ""))
            print(
                f"\n  [{idx}/{len(edges_list)}] Filling: -> {y_short} ...",
                file=sys.stderr,
            )

            filled = step2_fill_one_edge(
                client=self.client,
                pdf_text=pdf_text,
                edge=edge,
                paper_info=paper_info,
                evidence_type=evidence_type,
                annotated_template=self.annotated_template,
                pdf_name=pdf_name,
                template_path=self.template_path,
                hpp_dict_path=self.hpp_dict_path,
                max_retries=self.max_retries,
            )

            all_filled_edges.append(filled)

            eid = filled.get("edge_id", f"#{idx}")
            eq = filled.get("equation_type", "?")
            validation = filled.get("_validation", {})
            retries = validation.get("retries_used", 0)
            total_retries += retries
            sem_valid = validation.get("is_semantically_valid", "?")
            print(
                f"         Done: {eid} (equation_type={eq}, "
                f"semantic_valid={sem_valid}, retries={retries})",
                file=sys.stderr,
            )

        if total_retries > 0:
            print(
                f"\n[Step 2] Total retries across all edges: {total_retries}",
                file=sys.stderr,
            )

        # -- Step 3: Review & Quality Assessment --
        quality_report = None
        if self.enable_step3 and all_filled_edges:
            all_filled_edges, quality_report = step3_review(
                edges=all_filled_edges,
                pdf_text=pdf_text,
                client=self.client,
                hpp_dict_path=self.hpp_dict_path,
                enable_rerank=self.enable_rerank,
                enable_spot_check=self.enable_spot_check,
                spot_check_sample=self.spot_check_sample,
            )
            if pdf_dir:
                save_json(pdf_dir / "step3_review.json", quality_report)

        # -- Save final edges --
        if pdf_dir:
            output_file = pdf_dir / "edges.json"
            save_json(output_file, all_filled_edges)
            print(
                f"\n[Pipeline] Saved {len(all_filled_edges)} edges to: {output_file}",
                file=sys.stderr,
            )

        # -- Summary --
        print(f"\n{'='*60}", file=sys.stderr)
        print(
            f"[Pipeline] Complete: {len(all_filled_edges)} edges extracted",
            file=sys.stderr,
        )
        if quality_report:
            s = quality_report["summary"]
            print(
                f"  Quality: {s['valid_edges']}/{s['total_edges']} valid, "
                f"avg fill {s['avg_fill_rate']:.0%}",
                file=sys.stderr,
            )
        sem_pass = sum(
            1
            for e in all_filled_edges
            if e.get("_validation", {}).get("is_semantically_valid", False)
        )
        print(
            f"  Semantic: {sem_pass}/{len(all_filled_edges)} passed all checks",
            file=sys.stderr,
        )
        print(f"{'='*60}\n", file=sys.stderr)

        return all_filled_edges

    def run_single_step(
        self,
        pdf_path: str,
        step: str,
        evidence_type: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Any:
        """Run a single pipeline step (classify, edges, or review)."""
        pdf_text = self._get_pdf_text(pdf_path)

        if step == "classify":
            return step0_classify(self.client, pdf_text)

        if step == "edges":
            if not evidence_type:
                classification = step0_classify(self.client, pdf_text)
                evidence_type = classification["primary_category"]
            return step1_enumerate_edges(self.client, pdf_text, evidence_type)

        if step == "review":
            pdf_name = Path(pdf_path).stem
            base = Path(output_dir or ".")
            edges_path = base / pdf_name / "edges.json"
            assert edges_path.exists(), f"Run 'full' first. Not found: {edges_path}"
            with open(edges_path, "r", encoding="utf-8") as f:
                edges = json.load(f)

            updated, report = step3_review(
                edges=edges,
                pdf_text=pdf_text,
                client=self.client,
                hpp_dict_path=self.hpp_dict_path,
                enable_rerank=self.enable_rerank,
                enable_spot_check=self.enable_spot_check,
                spot_check_sample=self.spot_check_sample,
            )

            save_json(edges_path, updated)
            save_json(base / pdf_name / "step3_review.json", report)
            return report

        raise ValueError(f"Unknown step: {step}. Valid: classify, edges, review")
