"""
pipeline.py -- Edge extraction pipeline v3.

Redesigned for speed and reliability:

  Step 0: Classify paper type
  Step 1: Enumerate all X->Y statistical edges + deduplicate
  Step 1.5: Pre-validate edges (hard check values in text + soft check
            equation metadata derivation) -- NO LLM calls needed
  Step 2: Fill each edge into HPP template (simplified, no retry loop --
          pre-validation provides equation_type/model/mu/theta)
  Step 2.5: Strong model recovery -- re-extract null effect values using a
            stronger/more expensive model. Triggered when reported_effect_value,
            theta_hat, or CI are null after Step 2.
  Step 3: Review with robust JSON parsing (no crash on LLM parse failures)
  Step 4: Content audit -- deterministic + LLM checks against paper text
          (covariate hallucination, numeric hallucination, Y-label mismatch,
          HPP variable leakage, sample data errors)

Key changes from v2:
  - Step 2.5 added: strong model recovery for null values
  - Step 4 added: post-extraction content audit with auto-fix
  - Phase A (deterministic): covariate/numeric/HPP leakage detection + auto-removal
  - Phase A new checks: A10 CI/effect_value logical constraint,
    A11 p-value format normalization, A12 formula-Z consistency,
    A13 Z mapping ghost detection
  - Phase B (LLM): Y-label cross-check, adjustment semantics, guided by
    error_patterns.json from reference GT cases
  - _final_schema_enforcement: Z consistency, CI/effect_value constraint,
    p-value normalization
"""

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .audit import run_step4_audit
from .edge_prevalidator import prevalidate_edges
from .hpp_mapper import HPPMapper, get_hpp_context
from .llm_client import GLMClient
from .review import (
    canonicalize_edge_ids,
    canonicalize_paper_titles,
    check_cross_edge_consistency,
    detect_placeholder_edges,
    filter_edges_by_priority,
    generate_quality_report,
    has_placeholder,
    reconcile_pi,
    rerank_hpp_mapping,
    spot_check_values,
)
from .semantic_validator import (
    deduplicate_step1_edges,
    detect_fuzzy_duplicates_step3,
    has_blocking_errors,
    validate_semantics,
)
from .template_utils import (
    build_filled_edge,
    load_template,
    prepare_template_for_prompt,
    prepare_template_with_comments,
)

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SRC_DIR.parent
_PROMPTS_DIR = _PROJECT_DIR / "prompts"
_TEMPLATES_DIR = _PROJECT_DIR / "templates"
_DEFAULT_HPP_DICT = _TEMPLATES_DIR / "pheno_ai_data_dictionaries_simplified.json"
_DEFAULT_TEMPLATE = _TEMPLATES_DIR / "hpp_mapping_template.json"
_REFERENCE_DIR = _PROJECT_DIR / "reference"
_DEFAULT_ERROR_PATTERNS = _REFERENCE_DIR / "error_patterns.json"


def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> Saved: {path}", file=sys.stderr)


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


# Step 0: Classify


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


# Step 1: Enumerate edges (with deduplication)


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
    """Return True if this edge is a baseline balance check row."""
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

    # step1 is the longest single output in the whole pipeline (dozens of
    # edges × verbose JSON per edge). Give it a larger budget than the
    # pipeline default so papers with many outcomes × many exposure levels
    # (e.g. 9 outcomes × 4 lifestyle-score levels = 36+ edges) don't get
    # truncated. Bumped from 32k to 65k on 2026-04-18 after Rassy hit the
    # cap silently.
    result = client.call_json(full_prompt, max_tokens=65536)

    edges = result.get("edges", [])
    paper_info = result.get("paper_info", {})

    # Filter baseline balance check rows
    filtered = [e for e in edges if not _is_baseline_check(e)]
    n_filtered = len(edges) - len(filtered)
    if n_filtered:
        print(
            f"[Step 1] Filtered {n_filtered} baseline balance check edges",
            file=sys.stderr,
        )

    # Fuzzy deduplication
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


# Step 1.5: Pre-validate edges (NO LLM calls)


def step1_5_prevalidate(
    edges: List[Dict],
    pdf_text: str,
    evidence_type: str,
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Pre-validate all edges between Step 1 and Step 2.
    Derives equation_type, model, mu, theta_hat on correct scale.
    Verifies reported values appear in paper text.

    Returns:
        (enriched_edges, validation_report)
    """
    print(f"\n[Step 1.5] Pre-validating {len(edges)} edges ...", file=sys.stderr)

    enriched, report = prevalidate_edges(edges, pdf_text, evidence_type)

    # Print summary
    print(
        f"  Hard check: {report['hard_check_passed']} passed, "
        f"{report['hard_check_failed']} failed",
        file=sys.stderr,
    )
    if report["hard_check_missing_values"]:
        for item in report["hard_check_missing_values"][:5]:
            print(
                f"    Edge #{item['edge_index']}: missing {item['missing']}",
                file=sys.stderr,
            )

    eq_dist = report["equation_type_distribution"]
    print(f"  Equation types: {dict(eq_dist)}", file=sys.stderr)

    n_soft = len(report["soft_check_issues"])
    if n_soft:
        print(f"  Soft check: {n_soft} issue(s)", file=sys.stderr)
        for iss in report["soft_check_issues"][:5]:
            print(
                f"    Edge #{iss.get('edge_index', '?')}: {iss['message']}",
                file=sys.stderr,
            )

    return enriched, report


# Step 2: Fill one edge (simplified -- no retry loop)


def extract_anchor_numbers(pdf_text: str) -> Set[str]:
    """
    Extract ALL numbers that appear in the paper text.
    Returns a set of string representations for fast lookup.

    This is the "ground truth" set — any numeric value in the
    final output must trace back to one of these numbers.
    """
    text_clean = pdf_text.replace("\n", " ").replace("\t", " ")

    # Match numbers in various formats: 0.40, 158, 14.7, <0.001, etc.
    raw_numbers = re.findall(r"(?<![a-zA-Z])(\d+\.?\d*)", text_clean)

    anchor_set = set()
    for n in raw_numbers:
        anchor_set.add(n)
        # Also add common transformations
        try:
            val = float(n)
            # Add various string representations
            anchor_set.add(f"{val:.1f}")
            anchor_set.add(f"{val:.2f}")
            anchor_set.add(f"{val:.3f}")
            if val == int(val) and abs(val) < 100000:
                anchor_set.add(str(int(val)))
            # For values like 0.40, also store "0.4" and ".40"
            if 0 < abs(val) < 1:
                anchor_set.add(f"{val:.2f}".lstrip("0"))
                anchor_set.add(f"{val:.3f}".lstrip("0"))
        except ValueError:
            pass

    return anchor_set


def hard_match_value(val: Any, anchor_set: Set[str], pdf_text: str) -> bool:
    """
    Check if a numeric value can be traced to the paper.

    For ratio measures (OR/HR/RR), also checks if exp(val)
    appears in the paper (since theta_hat is on log scale).
    """
    if val is None:
        return True

    try:
        num = float(val)
    except (ValueError, TypeError):
        return True  # Non-numeric, skip

    # Direct match
    candidates = [
        f"{num:.1f}",
        f"{num:.2f}",
        f"{num:.3f}",
        f"{num:g}",
    ]
    if num == int(num) and abs(num) < 100000:
        candidates.append(str(int(num)))
    if 0 < abs(num) < 1:
        candidates.append(f"{num:.2f}".lstrip("0"))
        candidates.append(f"{num:.3f}".lstrip("0"))

    for c in candidates:
        if c in anchor_set:
            return True

    # Try exp() for log-scale values
    try:
        exp_val = math.exp(num)
        exp_candidates = [
            f"{exp_val:.1f}",
            f"{exp_val:.2f}",
            f"{exp_val:.3f}",
            f"{exp_val:g}",
        ]
        if 0 < exp_val < 1:
            exp_candidates.append(f"{exp_val:.2f}".lstrip("0"))
        for c in exp_candidates:
            if c in anchor_set:
                return True
    except (OverflowError, ValueError):
        pass

    # Fallback: search in raw text
    text_collapsed = pdf_text.replace(" ", "").replace("\n", "")
    for c in candidates:
        if c in text_collapsed:
            return True

    return False


def post_step2_hard_match(
    filled: Dict,
    anchor_set: Set[str],
    pdf_text: str,
    strict: bool = False,
) -> Dict:
    """
    After Step 2 LLM fills the template, verify all numeric values
    against the paper's anchor numbers.

    Behavior:
    - strict=False (default): MARK untraceable values in
      filled["_hard_match"]["marked"] but leave the values intact.
      Safer at scale: OCR hiccups, rounding, or unusual number
      formatting will produce false "not traceable" hits, and
      nullifying them would discard real extractions.
    - strict=True: NULLIFY untraceable values. Use this only when you
      have high confidence the anchor_set captures the paper's full
      numeric surface (e.g., well-OCR'd clean PDFs, small corpora).
    """
    marked = []
    nullified = []

    def _record(detail: str, clear_fn):
        if strict:
            clear_fn()
            nullified.append(detail)
        else:
            marked.append(detail)

    efr = filled.get("equation_formula_reported", {})
    lit = filled.get("literature_estimate", {})
    mu = filled.get("epsilon", {}).get("mu", {}).get("core", {})

    # Check reported_effect_value
    rev = efr.get("reported_effect_value")
    if rev is not None and not hard_match_value(rev, anchor_set, pdf_text):
        _record(
            f"reported_effect_value={rev} (not traceable in paper)",
            lambda: efr.__setitem__("reported_effect_value", None),
        )

    # Check reported_ci
    rci = efr.get("reported_ci", [])
    if isinstance(rci, list):
        new_ci = list(rci)
        for idx, bound in enumerate(rci):
            if bound is not None and not hard_match_value(bound, anchor_set, pdf_text):

                def _clear(i=idx):
                    new_ci[i] = None

                _record(f"reported_ci[{idx}]={bound} (not traceable)", _clear)
        if strict:
            efr["reported_ci"] = new_ci

    # Check theta_hat (only for non-log scales, since log is derived)
    theta = lit.get("theta_hat")
    if theta is not None and mu.get("scale") != "log":
        if not hard_match_value(theta, anchor_set, pdf_text):
            _record(
                f"theta_hat={theta} (difference scale, not traceable)",
                lambda: lit.__setitem__("theta_hat", None),
            )

    # For log scale: verify the original-scale value exists
    if theta is not None and mu.get("scale") == "log":
        try:
            original = math.exp(theta)
            if not hard_match_value(original, anchor_set, pdf_text):
                _record(
                    f"theta_hat={theta} (exp={original:.4f}) original scale "
                    f"not traceable",
                    lambda: lit.__setitem__("theta_hat", None),
                )
        except (OverflowError, ValueError):
            pass

    # Check literature_estimate.ci on non-log scale
    lit_ci = lit.get("ci", [])
    if isinstance(lit_ci, list) and mu.get("scale") != "log":
        new_ci = list(lit_ci)
        for idx, bound in enumerate(lit_ci):
            if bound is not None and not hard_match_value(bound, anchor_set, pdf_text):

                def _clear(i=idx):
                    new_ci[i] = None

                _record(f"lit.ci[{idx}]={bound} (not traceable)", _clear)
        if strict:
            lit["ci"] = new_ci

    if marked or nullified:
        filled.setdefault("_hard_match", {})
        if marked:
            filled["_hard_match"]["marked"] = marked
        if nullified:
            filled["_hard_match"]["nullified"] = nullified

    return filled


# Step 2: Fill one edge (simplified -- no retry loop)


def _is_fill_marker(v: Any) -> bool:
    """True if v is a string carrying the template's `<<FILL_ME:…>>` marker."""
    if not isinstance(v, str):
        return False
    return "<<FILL_ME" in v or "FILL_ME:" in v


def _clean_fill_markers(filled: Dict) -> Dict[str, int]:
    """
    Sweep an edge produced by Step 2 and replace any `<<FILL_ME:…>>` markers
    the LLM left behind:
      - hpp_mapping.{X|Y|Z[*]}.{dataset|field|name}  → ""  + status='missing'
      - paper_title / paper_abstract                 → ""
      - equation_type (top-level) FILL_ME or slash list → recover from
            literature_estimate.equation_type if that one is a single E1..E6,
            else ""
      - epsilon.rho.{X|Y}, epsilon.iota.core.name, epsilon.o.name → ""

    Returns counters of what was cleaned, e.g. {"hpp_mapping": 3, "top": 1}.
    Mutates `filled` in place.
    """
    cleaned: Dict[str, int] = {"hpp_mapping": 0, "top": 0, "rho_iota_o": 0}

    # hpp_mapping: X / Y
    hm = filled.get("hpp_mapping", {}) or {}
    for role in ("X", "Y"):
        mapping = hm.get(role)
        if isinstance(mapping, dict):
            hit = False
            for k in ("dataset", "field", "name"):
                if _is_fill_marker(mapping.get(k)):
                    mapping[k] = ""
                    hit = True
            if hit or _is_fill_marker(mapping.get("status")):
                mapping["status"] = "missing"
                cleaned["hpp_mapping"] += 1

    # hpp_mapping: Z list
    z_list = hm.get("Z")
    if isinstance(z_list, list):
        for z in z_list:
            if isinstance(z, dict):
                hit = False
                for k in ("dataset", "field", "name"):
                    if _is_fill_marker(z.get(k)):
                        z[k] = ""
                        hit = True
                if hit or _is_fill_marker(z.get("status")):
                    z["status"] = "missing"
                    cleaned["hpp_mapping"] += 1

    # equation_type: FILL_ME or slash list ("E1/E2/E3/...") rescue
    eq_top = filled.get("equation_type", "")
    if _is_fill_marker(eq_top) or "/" in str(eq_top):
        lit_eq = filled.get("literature_estimate", {}).get("equation_type", "")
        if isinstance(lit_eq, str) and lit_eq in {"E1", "E2", "E3", "E4", "E5", "E6"}:
            filled["equation_type"] = lit_eq
        else:
            filled["equation_type"] = ""
        cleaned["top"] += 1

    # paper_title / paper_abstract
    for k in ("paper_title", "paper_abstract"):
        if _is_fill_marker(filled.get(k)):
            filled[k] = ""
            cleaned["top"] += 1

    # epsilon.rho.{X,Y}, epsilon.iota.core.name, epsilon.o.name
    eps = filled.get("epsilon", {}) or {}
    rho = eps.get("rho", {}) or {}
    for k in ("X", "Y"):
        if _is_fill_marker(rho.get(k)):
            rho[k] = ""
            cleaned["rho_iota_o"] += 1
    iota_core = (eps.get("iota", {}) or {}).get("core", {}) or {}
    if _is_fill_marker(iota_core.get("name")):
        iota_core["name"] = ""
        cleaned["rho_iota_o"] += 1
    o = eps.get("o", {}) or {}
    if _is_fill_marker(o.get("name")):
        o["name"] = ""
        cleaned["rho_iota_o"] += 1

    return cleaned


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
    gt_fewshot_context: Optional[str] = None,  # NEW: GT few-shot examples
    enable_hard_match: bool = False,
) -> Dict:
    """
    Fill a single edge into the HPP template.

    Uses pre-validated equation metadata from Step 1.5 (_prevalidation key)
    to guide the LLM. No retry loop -- pre-validation already ensures
    equation_type/model/mu consistency.
    """
    prompt_template = _load_prompt("step2_fill_template")
    template_json = prepare_template_for_prompt(annotated_template)

    if template_path:
        template_with_hints = prepare_template_with_comments(template_path)
    else:
        template_with_hints = template_json

    # Extract pre-validation data
    preval = edge.get("_prevalidation", {})

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

    # Inject pre-validated equation metadata as guidance
    preval_guidance = _build_prevalidation_guidance(edge, preval)

    # Build GT few-shot section (if available)
    gt_fewshot_section = ""
    if gt_fewshot_context:
        gt_fewshot_section = f"---\n\n" f"{gt_fewshot_context}\n\n"

    full_prompt = (
        f"{prompt_template}\n\n"
        f"---\n\n"
        f"## Pre-validated equation metadata (suggested reference)\n\n"
        f"{preval_guidance}\n\n"
        f"{gt_fewshot_section}"
        f"---\n\n**Paper**\n\n{pdf_text}"
    )

    # Single LLM call -- no retry loop
    llm_output = client.call_json(full_prompt)
    if not isinstance(llm_output, dict):
        llm_output = {}

    filled, is_valid, format_issues, fill_rate = build_filled_edge(
        annotated_template=annotated_template,
        llm_output=llm_output,
        edge=edge,
        paper_info=paper_info,
        evidence_type=evidence_type,
        pdf_name=pdf_name,
    )

    # Sweep <<FILL_ME:...>> markers the LLM left in the output. We do this
    # BEFORE step3_review's placeholder check so a partially-mapped edge
    # (e.g. real X/Y/theta_hat but no HPP field) survives as a valid edge
    # with status='missing' instead of being dropped wholesale.
    cleaned_counts = _clean_fill_markers(filled)
    if any(cleaned_counts.values()):
        print(
            f"  [Step 2] Cleaned FILL_ME markers: {cleaned_counts}",
            file=sys.stderr,
        )

    # Preserve Step 1's priority tag through Step 2 so the Step 3 priority
    # filter actually has something to act on (the template doesn't include
    # priority, so build_filled_edge would otherwise drop it).
    step1_priority = edge.get("priority")
    if step1_priority:
        filled["priority"] = step1_priority

    # Apply pre-validated overrides (deterministic corrections)
    filled = _apply_prevalidation_overrides(filled, preval)

    # Hard-match numeric tracing is opt-in (off by default for scale safety).
    if enable_hard_match:
        anchor_set = extract_anchor_numbers(pdf_text)
        filled = post_step2_hard_match(filled, anchor_set, pdf_text, strict=False)

        hm = filled.get("_hard_match", {})
        if hm.get("marked"):
            print(
                f"  [Hard-Match] Marked {len(hm['marked'])} values as untraceable "
                f"(not nullified — see _hard_match.marked)",
                file=sys.stderr,
            )
            for change in hm["marked"]:
                print(f"    {change}", file=sys.stderr)
        if hm.get("nullified"):
            print(
                f"  [Hard-Match] Nullified {len(hm['nullified'])} "
                f"values not found in paper",
                file=sys.stderr,
            )
            for change in hm["nullified"]:
                print(f"    {change}", file=sys.stderr)

    # Run semantic validation (for reporting only, no retry)
    semantic_issues = validate_semantics(filled, evidence_type=evidence_type)

    if semantic_issues:
        n_err = sum(1 for i in semantic_issues if i["severity"] == "error")
        n_warn = sum(1 for i in semantic_issues if i["severity"] == "warning")
        if n_err:
            print(
                f"  [Semantic] {n_err} errors, {n_warn} warnings (post-override)",
                file=sys.stderr,
            )

    filled["_validation"] = {
        "semantic_issues": semantic_issues,
        "format_issues": format_issues,
        "fill_rate": fill_rate,
        "retries_used": 0,
        "is_format_valid": is_valid,
        "is_semantically_valid": not has_blocking_errors(semantic_issues),
        "prevalidation": {
            "equation_type": preval.get("equation_type"),
            "model": preval.get("model"),
            "hard_check_passed": preval.get("hard_check", {}).get("passed", True),
        },
    }

    return filled


# Step 2.5: Strong Model Recovery for null values


def step2_5_recover_nulls(
    client_strong: GLMClient,
    pdf_text: str,
    edges: List[Dict],
    anchor_set: Optional[Set[str]] = None,
    enable_hard_match: bool = False,
) -> List[Dict]:
    """
    Step 2.5: Use a stronger model to recover null effect values.

    Trigger conditions (any):
    1. reported_effect_value is null
    2. theta_hat is null
    3. reported_ci exists but reported_effect_value is null (logical contradiction)
    4. All of CI, effect_value, p_value are null (entire edge is empty)

    When enable_hard_match=True, recovered values are gated by the
    anchor_set (trace-to-paper check). Otherwise recovered values are
    accepted as-is — at scale this avoids false rejections from OCR
    or formatting drift.
    """
    # Lazy import to avoid a cycle on module load.
    from .review import (
        _select_relevant_chunks,
        _spot_check_keywords,
        select_results_and_tables,
    )

    # For long papers (>30K chars) we can't fit the whole text into the
    # recovery prompt — pre-build the Results/Tables-biased excerpt and
    # then keyword-refine per edge.
    long_paper = len(pdf_text) > 30000
    if long_paper:
        results_pool = select_results_and_tables(pdf_text, max_total_chars=40000)
    else:
        results_pool = pdf_text

    for i, edge in enumerate(edges):
        efr = edge.get("equation_formula_reported", {})
        lit = edge.get("literature_estimate", {})
        rho = edge.get("epsilon", {}).get("rho", {})
        mu = edge.get("epsilon", {}).get("mu", {}).get("core", {})

        rev = efr.get("reported_effect_value")
        rci = efr.get("reported_ci", [None, None])
        theta = lit.get("theta_hat")
        p_val = lit.get("p_value")

        ci_exists = isinstance(rci, list) and any(v is not None for v in rci)

        needs_recovery = (
            rev is None
            or theta is None
            or (ci_exists and rev is None)
            or (rev is None and theta is None and p_val is None)
        )

        if not needs_recovery:
            continue

        edge_id = edge.get("edge_id", f"#{i+1}")
        x_name = rho.get("X", "?")
        y_name = rho.get("Y", "?")
        source = edge.get("_prevalidation", {}).get("hard_check", {})

        print(
            f"  [Recovery] Edge {edge_id}: {x_name} → {y_name} "
            f"(rev={'null' if rev is None else rev}, "
            f"theta={'null' if theta is None else theta})",
            file=sys.stderr,
        )

        # For long papers, pull the most keyword-relevant ~28K out of the
        # Results/Tables-biased pool; for short papers, pass the whole thing.
        if long_paper:
            kw = _spot_check_keywords(edge, theta if theta is not None else 0.0)
            paper_excerpt = _select_relevant_chunks(
                results_pool, kw, max_total_chars=28000
            )
            excerpt_label = (
                f"--- 论文原文（关键词召回，约 {len(paper_excerpt)} chars / "
                f"{len(pdf_text)} chars 全文） ---"
            )
        else:
            paper_excerpt = pdf_text
            excerpt_label = "--- 论文原文 ---"

        prompt = (
            f"你是医学统计学专家。请从论文中精确提取以下统计效应的数值。\n\n"
            f"**要提取的关系**: {x_name} → {y_name}\n"
            f"**效应量类型**: {efr.get('effect_measure', '?')}\n"
            f"**模型类型**: {efr.get('model_type', '?')}\n"
            f"**来源位置提示**: {edge.get('_prevalidation', {}).get('hard_check', {}).get('checks', [])}\n\n"
            f"请在论文中找到这个效应的：\n"
            f"1. 效应值（HR/OR/RR/beta/MD 等，论文原始尺度）\n"
            f"2. 95% CI（如有）\n"
            f"3. P 值（如有，用 float 表示，如 < 0.001 则写 0.001）\n"
            f"4. 来源位置（Table X / Figure X / 正文第 X 页）\n\n"
            f"如果论文确实没有报告某个值，写 null。\n"
            f"不要计算，不要推导，只提取论文直接报告的数值。\n\n"
            f"输出 JSON：\n"
            f'{{"effect_value": ..., "ci": [lower, upper], '
            f'"p_value": ..., "source_location": "...", '
            f'"effect_type": "HR/OR/beta/MD/..."}}\n\n'
            f"{excerpt_label}\n{paper_excerpt}"
        )

        try:
            result = client_strong.call_json(prompt, max_tokens=4096)
            _apply_recovery_result(
                edge,
                result,
                anchor_set,
                pdf_text,
                mu,
                enable_hard_match=enable_hard_match,
            )
        except Exception as e:
            print(
                f"    [Recovery] Failed: {e}",
                file=sys.stderr,
            )

    return edges


def _apply_recovery_result(
    edge: Dict,
    result: Dict,
    anchor_set: Optional[Set[str]],
    pdf_text: str,
    mu: Dict,
    enable_hard_match: bool = False,
) -> None:
    """
    Apply recovery result to edge.

    When enable_hard_match=True and anchor_set is provided, recovered
    values are gated by hard_match_value. Otherwise accept as-is —
    at scale, OCR/rounding drift produces false rejections.
    """
    efr = edge.get("equation_formula_reported", {})
    lit = edge.get("literature_estimate", {})

    new_val = result.get("effect_value")
    new_ci = result.get("ci")
    new_p = result.get("p_value")
    recovered = []

    def _accept(v) -> bool:
        if v is None:
            return False
        if enable_hard_match and anchor_set is not None:
            return hard_match_value(v, anchor_set, pdf_text)
        return True

    # Recover reported_effect_value
    if new_val is not None and efr.get("reported_effect_value") is None:
        if _accept(new_val):
            efr["reported_effect_value"] = new_val
            recovered.append(f"reported_effect_value={new_val}")
            # Sync theta_hat
            if mu.get("scale") == "log":
                try:
                    val_f = float(new_val)
                    if val_f > 0:
                        lit["theta_hat"] = round(math.log(val_f), 4)
                        recovered.append(f"theta_hat={lit['theta_hat']}")
                except (ValueError, TypeError):
                    pass
            elif mu.get("scale") == "identity":
                try:
                    lit["theta_hat"] = float(new_val)
                    recovered.append(f"theta_hat={new_val}")
                except (ValueError, TypeError):
                    pass

    # Recover CI
    if new_ci and isinstance(new_ci, list) and len(new_ci) == 2:
        curr_ci = efr.get("reported_ci", [None, None])
        if curr_ci in (None, [None, None]):
            valid_ci = []
            for b in new_ci:
                if _accept(b):
                    valid_ci.append(b)
                else:
                    valid_ci.append(None)
            if any(v is not None for v in valid_ci):
                efr["reported_ci"] = valid_ci
                recovered.append(f"reported_ci={valid_ci}")
                # Sync lit CI
                if mu.get("scale") == "log":
                    log_ci = [None, None]
                    for idx, b in enumerate(valid_ci):
                        if b is not None:
                            try:
                                bf = float(b)
                                if bf > 0:
                                    log_ci[idx] = round(math.log(bf), 4)
                            except (ValueError, TypeError):
                                pass
                    lit["ci"] = log_ci
                else:
                    lit["ci"] = [float(b) if b is not None else None for b in valid_ci]

    # Recover P value (normalize to float)
    if new_p is not None and lit.get("p_value") is None:
        if isinstance(new_p, (int, float)):
            lit["p_value"] = new_p
            recovered.append(f"p_value={new_p}")
        elif isinstance(new_p, str):
            import re as _re

            match = _re.match(r"^[<≤]\s*(\d*\.?\d+)$", new_p.strip())
            if match:
                p_float = float(match.group(1))
                lit["p_value"] = p_float
                recovered.append(f"p_value={p_float}")
            else:
                try:
                    p_float = float(new_p.strip())
                    lit["p_value"] = p_float
                    recovered.append(f"p_value={p_float}")
                except ValueError:
                    pass

    # Enforce: if reported_ci exists but reported_effect_value is still null,
    # clear the CI (logical invariant)
    rci = efr.get("reported_ci", [None, None])
    ci_exists = isinstance(rci, list) and any(v is not None for v in rci)
    if ci_exists and efr.get("reported_effect_value") is None:
        efr["reported_ci"] = [None, None]
        lit["ci"] = [None, None]
        recovered.append("cleared orphan CI (no effect_value)")

    if recovered:
        print(
            f"    [Recovery] Recovered: {', '.join(recovered)}",
            file=sys.stderr,
        )


def _build_prevalidation_guidance(edge: Dict, preval: Dict) -> str:
    """Build a guidance block for the LLM based on pre-validated metadata."""
    if not preval:
        return "(No pre-validation data available)"

    lines = []
    lines.append(
        f"- **suggested equation_type**: `{preval.get('equation_type', '?')}` "
        f"(derived from effect_scale={edge.get('effect_scale', '?')}, "
        f"outcome_type={edge.get('outcome_type', '?')})"
    )
    lines.append(f"- **suggested model**: `{preval.get('model', '?')}`")

    mu = preval.get("mu", {})
    lines.append(
        f"- **suggested mu**: family=`{mu.get('family', '?')}`, "
        f"type=`{mu.get('type', '?')}`, scale=`{mu.get('scale', '?')}`"
    )

    theta = preval.get("theta_hat")
    if theta is not None:
        lines.append(f"- **theta_hat** (pre-computed on correct scale): `{theta}`")

    ci = preval.get("ci")
    if ci and any(v is not None for v in ci):
        lines.append(f"- **ci** (pre-computed on correct scale): `{ci}`")

    reported = preval.get("reported_value")
    if reported is not None:
        lines.append(f"- **reported_value** (original scale): `{reported}`")

    reported_ci = preval.get("reported_ci")
    if reported_ci:
        lines.append(f"- **reported_ci** (original scale): `{reported_ci}`")

    lines.append(f"- **id_strategy**: `{preval.get('id_strategy', '?')}`")
    lines.append(f"- **formula_skeleton**: `{preval.get('formula_skeleton', '?')}`")

    # Include reasoning chain so LLM can reference it in its own `reason` field
    reasoning = preval.get("reasoning_chain", [])
    if reasoning:
        lines.append("\n**Derivation reasoning** (for your `reason` field reference):")
        for step in reasoning:
            lines.append(f"  - {step}")

    lines.append(
        "\n**NOTE**: The equation_type, model, and mu above are SUGGESTIONS "
        "derived from effect_scale and outcome_type. You MUST verify them "
        "against the paper's actual statistical method and formula structure. "
        "If the paper uses a different model (e.g. Poisson reporting HR, "
        "or logistic regression with interaction terms), use the correct type. "
        "theta_hat and ci are pre-computed and will be applied automatically."
    )

    return "\n".join(lines)


def _apply_prevalidation_overrides(filled: Dict, preval: Dict) -> Dict:
    """
    Apply deterministic post-processing to the LLM-filled edge.

    We NO LONGER override equation_type, model, or mu — those are determined
    by the LLM based on the paper's actual statistical method and formula.

    We DO override:
      - theta_hat and ci (log-scale conversion is deterministic math)
      - id_strategy (derived from evidence_type, no ambiguity)
      - adjustment_variables (from Step 1 extraction)
      - Dual-check field sync (lit.equation_type ↔ top-level)
    """
    if not preval:
        return filled

    lit = filled.setdefault("literature_estimate", {})

    # ── Override theta_hat and ci (deterministic log-scale conversion) ──
    if preval.get("theta_hat") is not None:
        lit["theta_hat"] = preval["theta_hat"]

    if preval.get("ci") and any(v is not None for v in preval["ci"]):
        lit["ci"] = preval["ci"]

    # ── Override id_strategy (derived from evidence_type, no ambiguity) ──
    if preval.get("id_strategy"):
        alpha = filled.setdefault("epsilon", {}).setdefault("alpha", {})
        alpha["id_strategy"] = preval["id_strategy"]

    # ── Sync dual-check fields ──
    # literature_estimate.equation_type MUST match top-level equation_type
    final_eq_type = filled.get("equation_type")
    if final_eq_type:
        lit["equation_type"] = final_eq_type

    # literature_estimate.equation_formula SHOULD match top-level equation_formula.formula
    top_formula = filled.get("equation_formula", {})
    if isinstance(top_formula, dict) and top_formula.get("formula"):
        lit["equation_formula"] = top_formula["formula"]

    # ── Pre-populate adjustment_variables if LLM left them empty ──
    adj_vars = preval.get("adjustment_variables", [])
    if adj_vars:
        rho = filled.setdefault("epsilon", {}).setdefault("rho", {})
        if not rho.get("Z") or rho.get("Z") == ["..."]:
            rho["Z"] = adj_vars
        lit_adj = lit.get("adjustment_set", [])
        if not lit_adj or lit_adj == ["..."]:
            lit["adjustment_set"] = adj_vars

    return filled


def _final_schema_enforcement(edge: Dict) -> None:
    """
    Final deterministic cleanup to ensure output matches GT schema exactly.
    Called once before saving to disk. Mutates edge in place.
    """
    import re as _re

    # 1. Strip _validation and any other internal keys
    for key in list(edge.keys()):
        if key.startswith("_"):
            del edge[key]

    # 1a. Normalize priority to a known whitelist value. Anything else
    # falls back to "secondary" (the safe default) so the Step 3 priority
    # filter behaves predictably. We keep the field on the edge — the
    # downstream filter_edges_by_priority needs it to do anything.
    _PRIORITY_WHITELIST = {"primary", "secondary", "exploratory"}
    p = edge.get("priority")
    if p is not None:
        p = str(p).strip().lower()
        edge["priority"] = p if p in _PRIORITY_WHITELIST else "secondary"

    # 1b. Fix edge_id format: EV-YEAR-Author#N (hyphen, not underscore)
    eid = edge.get("edge_id", "")
    if eid:
        # Normalize: EV_2001_Scurr#1 -> EV-2001-Scurr#1
        # Pattern: EV followed by separator then year then separator then name#num
        eid = _re.sub(r"^EV[_\-](\d{4})[_\-]", r"EV-\1-", eid)
        # Also fix EV-NA-... or EV-None-... (year was missing from step1)
        eid = _re.sub(r"^EV-(NA|None|null|YYYY)-", r"EV-YYYY-", eid)
        edge["edge_id"] = eid

    # 2. hpp_mapping: enforce strict structure
    hm = edge.get("hpp_mapping", {})

    _ALLOWED_MAPPING_KEYS = {"name", "dataset", "field", "status"}
    _ALLOWED_HPP_TOP_KEYS = {"X", "Y", "Z", "M", "X2"}

    # Strip forbidden fields from X, Y mapping objects
    for role in ("X", "Y"):
        mapping = hm.get(role)
        if isinstance(mapping, dict):
            for k in list(mapping.keys()):
                if k not in _ALLOWED_MAPPING_KEYS:
                    del mapping[k]

    # Backfill missing 'name' in hpp_mapping X/Y from epsilon.rho
    rho = edge.get("epsilon", {}).get("rho", {})
    iota_name = edge.get("epsilon", {}).get("iota", {}).get("core", {}).get("name", "")
    o_name = edge.get("epsilon", {}).get("o", {}).get("name", "")
    for role, fallback in [
        ("X", rho.get("X") or iota_name),
        ("Y", rho.get("Y") or o_name),
    ]:
        mapping = hm.get(role)
        if isinstance(mapping, dict):
            if not mapping.get("name") and fallback:
                mapping["name"] = fallback
            # Ensure all 4 required keys exist
            mapping.setdefault("name", "")
            mapping.setdefault("dataset", "")
            mapping.setdefault("field", "")
            mapping.setdefault("status", "missing")

    # Strip forbidden fields from Z list items
    z_list = hm.get("Z")
    if isinstance(z_list, list):
        for z_item in z_list:
            if isinstance(z_item, dict):
                for k in list(z_item.keys()):
                    if k not in _ALLOWED_MAPPING_KEYS:
                        del z_item[k]

    # Ensure M and X2 always present
    eq_type = edge.get("equation_type", "")
    if "M" not in hm:
        hm["M"] = None
    if "X2" not in hm:
        hm["X2"] = None
    if eq_type != "E4":
        hm["M"] = None
    if eq_type != "E6":
        hm["X2"] = None

    # Strip non-standard top-level hpp_mapping keys
    for k in list(hm.keys()):
        if k not in _ALLOWED_HPP_TOP_KEYS:
            del hm[k]

    # 3. Normalize dataset IDs to hyphen format
    def _fix_ds(obj):
        if isinstance(obj, dict):
            if "dataset" in obj and isinstance(obj["dataset"], str):
                obj["dataset"] = _re.sub(r"^(\d+)[_]", r"\1-", obj["dataset"])
            for v in obj.values():
                _fix_ds(v)
        elif isinstance(obj, list):
            for item in obj:
                _fix_ds(item)

    _fix_ds(hm)

    # 4. literature_estimate: strip extra fields AND sync dual-check fields
    _ALLOWED_LIT_KEYS = {
        "theta_hat",
        "ci",
        "ci_level",
        "p_value",
        "n",
        "design",
        "grade",
        "model",
        "adjustment_set",
        "equation_type",
        "equation_formula",
    }
    lit = edge.get("literature_estimate", {})
    for k in list(lit.keys()):
        if k not in _ALLOWED_LIT_KEYS:
            del lit[k]

    # ── FIX: Ensure literature_estimate.equation_type == top-level equation_type ──
    if eq_type:
        lit["equation_type"] = eq_type

    # ── FIX: Ensure literature_estimate.equation_formula syncs with top-level ──
    top_formula = edge.get("equation_formula", {})
    if isinstance(top_formula, dict) and top_formula.get("formula"):
        lit["equation_formula"] = top_formula["formula"]

    # ── NOTE: model is NOT forced to match equation_type anymore ──
    # The LLM determines model and equation_type from the paper's actual method.
    # Semantic validation (semantic_validator.py) will report inconsistencies
    # but will NOT override the LLM's choices.

    # 4b. equation_formula_reported: strip extra fields — STRICTLY follow template
    _ALLOWED_EFR_KEYS = {
        "equation",
        "source",
        "model_type",
        "link_function",
        "effect_measure",
        "reported_effect_value",
        "reported_ci",
        "reported_p",
        "X",
        "Y",
        "Z",
    }
    efr = edge.get("equation_formula_reported", {})
    if isinstance(efr, dict):
        for k in list(efr.keys()):
            if k not in _ALLOWED_EFR_KEYS:
                del efr[k]

    # 4c. equation_formula: ONLY "formula" key per template
    ef = edge.get("equation_formula", {})
    if isinstance(ef, dict):
        _ALLOWED_EF_KEYS = {"formula"}
        for k in list(ef.keys()):
            if k not in _ALLOWED_EF_KEYS:
                del ef[k]
    elif isinstance(ef, str):
        edge["equation_formula"] = {"formula": ef}

    # 5. Normalize mu.core.type to use log prefix for ratio measures
    mu = edge.get("epsilon", {}).get("mu", {}).get("core", {})
    mu_type = mu.get("type", "")
    if mu_type in ("HR", "OR", "RR") and mu.get("scale") == "log":
        mu["type"] = f"log{mu_type}"

    # 6. Z consistency: if rho.Z is empty, all Z-related fields must be empty
    rho_z = rho.get("Z", [])
    if not rho_z or rho_z == ["..."]:
        rho["Z"] = []
        lit["adjustment_set"] = []
        if isinstance(efr, dict):
            efr["Z"] = []
        hm["Z"] = []
    else:
        # rho.Z has values — strip placeholder entries from hpp_mapping.Z
        if isinstance(hm.get("Z"), list):
            _placeholder_pats = ("协变量", "...", "covariate_name", "变量")
            hm["Z"] = [
                z
                for z in hm["Z"]
                if isinstance(z, dict)
                and z.get("name")
                and z["name"] not in ("...", "")
                and not any(p in z.get("name", "") for p in _placeholder_pats)
            ]

    # 7. reported_ci / reported_effect_value logical constraint:
    #    CI exists → effect_value must exist; otherwise clear both
    if isinstance(efr, dict):
        _rev = efr.get("reported_effect_value")
        _rci = efr.get("reported_ci", [None, None])
        _ci_exists = (
            isinstance(_rci, list)
            and len(_rci) == 2
            and any(v is not None for v in _rci)
        )
        if _ci_exists and _rev is None:
            efr["reported_ci"] = [None, None]

    # 8. Normalize reported_p / p_value: "< 0.001" → float 0.001
    for _container, _key in [(efr, "reported_p"), (lit, "p_value")]:
        if not isinstance(_container, dict):
            continue
        _pval = _container.get(_key)
        if isinstance(_pval, str):
            _cleaned = _pval.strip()
            _p_match = _re.match(r"^[<≤]\s*(\d*\.?\d+)$", _cleaned)
            if _p_match:
                try:
                    _container[_key] = float(_p_match.group(1))
                except ValueError:
                    pass
            else:
                try:
                    _container[_key] = float(_cleaned)
                except ValueError:
                    pass  # Non-numeric string like "NS", leave as-is

    # 9. Math-consistency normalizer: effect_measure ↔ link_function only.
    # Low-risk: these are mathematical identities (HR lives on log scale,
    # MD lives on identity). We do NOT override model_type / equation_type /
    # Z decisions here — those are semantic choices for the LLM.
    if isinstance(efr, dict):
        _em = efr.get("effect_measure")
        _lf = efr.get("link_function")
        if _em in ("HR", "OR", "RR") and _lf not in ("log", "logit"):
            efr["link_function"] = "log" if _em in ("HR", "RR") else "logit"
        if _em in ("MD", "BETA", "SMD") and _lf != "identity":
            efr["link_function"] = "identity"


def _is_edge_content_empty(edge: Dict) -> bool:
    """
    Return True if the edge has no usable quantitative content AND is not
    explicitly flagged as qualitative. Used by filter_low_quality_edges.
    """
    efr = edge.get("equation_formula_reported", {}) or {}
    lit = edge.get("literature_estimate", {}) or {}

    def _has_any_num(val):
        if val is None:
            return False
        if isinstance(val, list):
            return any(v is not None for v in val)
        return True

    if (
        _has_any_num(efr.get("reported_effect_value"))
        or _has_any_num(efr.get("reported_ci", [None, None]))
        or _has_any_num(lit.get("theta_hat"))
        or _has_any_num(lit.get("ci", [None, None]))
        or _has_any_num(lit.get("p_value"))
    ):
        return False

    # Preserve explicitly-qualitative edges (paper reports a finding but
    # no numeric estimate is available).
    if edge.get("has_numeric_estimate") is False:
        return False

    return True


def filter_low_quality_edges(edges: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Drop edges that:
      * contain template placeholder strings (e.g. '论文完整标题'), OR
      * carry no numeric effect, CI, theta_hat, or p-value
        (unless has_numeric_estimate is explicitly False).

    Safety: if every edge would be dropped on the numeric grounds alone
    we keep them (same rule as filter_edges_by_priority); but placeholder
    edges are dropped even in that fallback path because they cannot be
    salvaged by downstream review.
    """
    kept: List[Dict] = []
    dropped: List[Dict] = []
    placeholder_dropped: List[Dict] = []
    for e in edges:
        if has_placeholder(e):
            placeholder_dropped.append(e)
            continue
        if _is_edge_content_empty(e):
            dropped.append(e)
        else:
            kept.append(e)

    if not kept:
        # Numeric-empty fallback: keep them, but still throw out placeholder edges.
        return edges, placeholder_dropped

    return kept, dropped + placeholder_dropped


# Step 3: Review (with robust JSON parsing)


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

    pre_issues: List[Dict] = []
    dropped_by_review: List[Dict] = []

    # 3pre-0. Detect placeholder leaks (Chinese template skeleton, "TBD",
    # "E1/E2/E3/...", etc). These edges should never reach rerank /
    # spot_check — rerank confabulates a candidate for "暴露变量名称"
    # and spot_check wastes a sample slot.
    placeholder_issues = detect_placeholder_edges(edges)
    if placeholder_issues:
        bad_idx = {
            ix for iss in placeholder_issues for ix in iss.get("edge_indices", [])
        }
        for i in sorted(bad_idx):
            dropped_by_review.append(
                {
                    "edge_id": edges[i].get("edge_id", f"#{i+1}"),
                    "edge_index": i + 1,
                    "reason": "placeholder_leak",
                }
            )
        print(
            f"  [3pre-0] {len(bad_idx)} edge(s) carry template placeholders — "
            f"flagged as errors and excluded from rerank/spot_check",
            file=sys.stderr,
        )
        pre_issues.extend(placeholder_issues)
        edges = [e for i, e in enumerate(edges) if i not in bad_idx]

    # 3pre-1. Canonicalize paper_title and edge_id across the paper so the
    # downstream consistency checks don't re-flag pure formatting noise.
    pre_issues.extend(canonicalize_paper_titles(edges))
    pre_issues.extend(canonicalize_edge_ids(edges))

    # 3pre-1a. Force a single Pi across the paper. With a 14-value whitelist,
    # the LLM picks legitimate-but-different labels per edge (e.g. GERD paper
    # gets {gi_disease, adult_general, other}); reconcile to the most-specific
    # majority before population_inconsistency check fires.
    pre_issues.extend(reconcile_pi(edges))

    # 3pre-2. Filter exploratory edges by priority
    kept, removed = filter_edges_by_priority(edges)
    if removed:
        for e in removed:
            dropped_by_review.append(
                {
                    "edge_id": e.get("edge_id", "?"),
                    "reason": f"priority={e.get('priority', '?')}",
                }
            )
        print(
            f"  [3pre-2] Filtered {len(removed)} exploratory edges "
            f"({len(kept)} kept)",
            file=sys.stderr,
        )
        edges = kept

    all_rerank_changes: List[Dict] = []
    if enable_rerank and hpp_dict_path:
        print("  [3a] Reranking HPP mappings ...", file=sys.stderr)
        with open(hpp_dict_path, "r", encoding="utf-8") as f:
            raw_dict = json.load(f)
        mapper = HPPMapper(raw_dict)

        for i, edge in enumerate(edges):
            try:
                changes = rerank_hpp_mapping(edge, mapper, client)
                all_rerank_changes.append(changes)
                if changes:
                    print(
                        f"    Edge #{i + 1}: reranked {list(changes.keys())}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"    Edge #{i + 1}: rerank failed ({e})",
                    file=sys.stderr,
                )
                all_rerank_changes.append({})
        n = sum(len(c) for c in all_rerank_changes)
        print(f"    {n} mapping(s) updated", file=sys.stderr)
    else:
        print("  [3a] Rerank skipped", file=sys.stderr)

    # 3b. Cross-edge consistency
    print("  [3b] Cross-edge consistency ...", file=sys.stderr)
    consistency_issues = check_cross_edge_consistency(edges)
    if pre_issues:
        consistency_issues = pre_issues + consistency_issues

    # 3b+. Fuzzy duplicate detection
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

    # 3c. Spot-check (with safe JSON parsing)
    spot_checks: List[Dict] = []
    if enable_spot_check:
        # Auto-scale sample size for big papers — checking 5 of 46 edges is
        # statistically meaningless, and the new keyword-chunk retrieval
        # makes individual checks cheap enough to afford a few more.
        effective_sample = max(spot_check_sample, min(10, len(edges) // 5))
        ns = min(effective_sample, len(edges))
        print(
            f"  [3c] Spot-checking {ns} edges "
            f"(requested={spot_check_sample}, scaled={effective_sample}) ...",
            file=sys.stderr,
        )
        try:
            spot_checks = _safe_spot_check(
                edges, pdf_text, client, sample_size=effective_sample
            )
            verdicts = Counter(c.get("verdict", "?") for c in spot_checks)
            print(f"    Results: {dict(verdicts)}", file=sys.stderr)
        except Exception as e:
            print(f"    Spot-check failed: {e}", file=sys.stderr)
            spot_checks = [{"status": "error", "reason": str(e)}]
    else:
        print("  [3c] Spot-check skipped", file=sys.stderr)

    # 3d. Quality report
    print("  [3d] Generating quality report ...", file=sys.stderr)
    report = generate_quality_report(
        edges, consistency_issues, spot_checks, all_rerank_changes
    )

    # Surface what review threw out so it shows up at the top of the
    # report instead of buried in consistency_issues. Empty list means
    # "review kept everything"; non-empty entries cite reason per edge.
    report["summary"]["dropped_edges_by_review"] = len(dropped_by_review)
    report["dropped_edges_by_review"] = dropped_by_review

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


def _safe_spot_check(
    edges: List[Dict],
    pdf_text: str,
    client: GLMClient,
    sample_size: int = 5,
) -> List[Dict]:
    """
    Wrapper around spot_check_values with robust JSON parsing.
    Falls back to individual edge checks if batch fails.
    """
    try:
        return spot_check_values(edges, pdf_text, client, sample_size=sample_size)
    except json.JSONDecodeError:
        print(
            "    [WARN] Batch spot-check JSON parse failed. Trying individual ...",
            file=sys.stderr,
        )
        # Fallback: check edges one at a time
        results = []
        checkable = []
        for i, e in enumerate(edges):
            lit = e.get("literature_estimate", {})
            theta = lit.get("theta_hat")
            if theta is not None and isinstance(theta, (int, float)):
                checkable.append((i, e, theta))

        # Lazy import to avoid a cycle on module load.
        from .review import _select_relevant_chunks, _spot_check_keywords

        for idx, (i, e, theta_val) in enumerate(checkable[:sample_size]):
            rho = e.get("epsilon", {}).get("rho", {})
            keywords = _spot_check_keywords(e, theta_val)
            excerpt = _select_relevant_chunks(pdf_text, keywords, max_total_chars=14000)
            try:
                prompt = (
                    f"Verify: {rho.get('X', '?')} -> {rho.get('Y', '?')}\n"
                    f"Extracted theta_hat (log scale): {theta_val}\n"
                    f'Reply JSON: {{"verdict": "correct/incorrect/not_found", '
                    f'"correct_value": null}}\n\n'
                    f"Paper (keyword-selected excerpt, "
                    f"{len(excerpt)} chars of {len(pdf_text)} total):\n{excerpt}"
                )
                result = client.call_json(prompt)
                result["edge_index"] = i
                result["edge_id"] = e.get("edge_id", "?")
                results.append(result)
            except Exception as ex:
                results.append(
                    {
                        "edge_index": i,
                        "edge_id": e.get("edge_id", "?"),
                        "verdict": "error",
                        "note": str(ex),
                    }
                )

        return (
            results if results else [{"status": "error", "reason": "All checks failed"}]
        )


# Pipeline class


class EdgeExtractionPipeline:
    """
    Seven-step pipeline:
      Step 0: Classify paper type
      Step 1: Enumerate all statistical edges (with deduplication)
      Step 1.5: Pre-validate (hard + soft check, no LLM)
      Step 2: Fill each edge into HPP template (simplified, no retry)
      Step 2.5: Strong model recovery for null effect values (optional)
      Step 3: Review, rerank, consistency check, spot-check, quality report
      Step 4: Content audit (deterministic Phase A + LLM Phase B)
    """

    def __init__(
        self,
        client: GLMClient,
        ocr_text_func: Callable[[str], str],
        ocr_init_func: Optional[Callable] = None,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 400,
        ocr_validate_pages: bool = True,
        hpp_dict_path: Optional[str] = None,
        template_path: Optional[str] = None,
        # Step 2 retry options (kept for API compat but no longer used)
        max_retries: int = 0,
        # Step 3 options
        enable_step3: bool = True,
        enable_rerank: bool = True,
        enable_spot_check: bool = True,
        spot_check_sample: int = 5,
        # Step 4 options
        enable_step4: bool = True,
        enable_step4_llm: bool = True,
        step4_max_edges_per_call: int = 5,
        # Phase C deterministic autofix using Phase B suggested_fix.
        # Off by default — only flip on once you've eyeballed the diff
        # on a small batch and decided LLM corrections are net-positive.
        enable_phase_c_autofix: bool = False,
        error_patterns_path: Optional[str] = None,
        # Reference GT options (NEW)
        reference_dir: Optional[str] = None,
        # Strong model for null recovery (Step 2.5)
        strong_client: Optional[GLMClient] = None,
        # Hard-match numeric tracing — off by default at scale since
        # OCR hiccups and rounding produce false "not traceable" hits
        # that would mark or discard valid extractions. Turn on for
        # small, clean corpora where you want the extra safety.
        enable_hard_match: bool = False,
    ):
        self.client = client
        self.strong_client = strong_client
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

        # Step 4 flags
        self.enable_step4 = enable_step4
        self.enable_step4_llm = enable_step4_llm
        self.step4_max_edges_per_call = step4_max_edges_per_call
        self.enable_phase_c_autofix = enable_phase_c_autofix

        # Hard-match flag (off by default; see constructor docstring above)
        self.enable_hard_match = enable_hard_match
        if error_patterns_path:
            self.error_patterns_path = error_patterns_path
        elif _DEFAULT_ERROR_PATTERNS.exists():
            self.error_patterns_path = str(_DEFAULT_ERROR_PATTERNS)
        else:
            self.error_patterns_path = None
        if self.enable_step4:
            print(
                f"[Pipeline] Step 4 enabled "
                f"(LLM={'on' if self.enable_step4_llm else 'off'}, "
                f"patterns={self.error_patterns_path})",
                file=sys.stderr,
            )

        # Reference GT directory (NEW)
        if reference_dir:
            self.reference_dir = reference_dir
        elif _REFERENCE_DIR.exists():
            self.reference_dir = str(_REFERENCE_DIR)
        else:
            self.reference_dir = None

        # Pre-load GT few-shot context (once, reused across edges)
        self._gt_fewshot_cache: Dict[str, str] = {}  # eq_type -> context
        if self.reference_dir:
            try:
                from .gt_loader import load_gt_cases

                self._gt_cases = load_gt_cases(self.reference_dir, max_cases=3)
                if self._gt_cases:
                    n_c = len(self._gt_cases)
                    n_e = sum(len(edges) for _, edges in self._gt_cases)
                    print(
                        f"[Pipeline] Loaded {n_e} GT edges from {n_c} reference cases",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[Pipeline] Failed to load GT cases: {e}", file=sys.stderr)
                self._gt_cases = []
        else:
            self._gt_cases = []

        if self.strong_client:
            print(
                f"[Pipeline] Strong model enabled for Step 2.5 null recovery",
                file=sys.stderr,
            )

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

        # -- Step 0: Classify --
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

        # -- Step 1: Enumerate edges --
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
            try:
                step1_result = step1_enumerate_edges(
                    self.client, pdf_text, evidence_type
                )
            except Exception as exc:
                # Never let step1 die silently — leave a breadcrumb so the
                # paper can be retried and we know which step blew up.
                if pdf_dir:
                    save_json(
                        pdf_dir / "_step1_failed.json",
                        {
                            "error": str(exc),
                            "type": type(exc).__name__,
                            "evidence_type": evidence_type,
                            "hint": (
                                "Most common cause: LLM output hit max_tokens "
                                "and the truncated JSON failed to parse 3×. "
                                "Re-run this single paper; if it fails again, "
                                "raise step1's max_tokens further."
                            ),
                        },
                    )
                print(
                    f"[Step 1] FAILED: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                raise
            edges_list = step1_result.get("edges", [])
            paper_info = step1_result.get("paper_info", {})
            if pdf_dir:
                save_json(pdf_dir / "step1_edges.json", step1_result)

        # -- Step 1.5: Pre-validate edges (NO LLM calls) --
        edges_list, preval_report = step1_5_prevalidate(
            edges_list, pdf_text, evidence_type
        )
        if pdf_dir:
            save_json(pdf_dir / "step1_5_prevalidation.json", preval_report)

        # -- Step 2: Fill templates (simplified, no retry) --
        print(
            f"\n[Step 2] Filling templates for {len(edges_list)} edges ...",
            file=sys.stderr,
        )
        all_filled_edges: List[Dict] = []

        # Resume support: load partially completed Step 2 results.
        #
        # Staleness guard: a partial from a previous run may reference
        # an outdated edge set. We refuse to load the partial if any of
        # the following hold:
        #   - the user did not pass resume=True
        #   - step1_edges.json is newer than step2_partial.json
        #     (Step 1 was rerun, edge IDs may have shifted)
        #   - none of the partial's _step2_edge_index values fall inside
        #     the current Step 1 edge index range
        # This prevents the 38365951-style case where a stale 1-edge
        # partial overwrites a 6-edge fresh run.
        step2_partial_path = pdf_dir / "step2_partial.json" if pdf_dir else None
        step2_cached_edges: Dict[int, Dict] = {}  # edge_index -> filled_edge

        def _partial_is_stale() -> Tuple[bool, str]:
            if not step2_partial_path or not step2_partial_path.exists():
                return True, "no partial file"
            try:
                step1_path = pdf_dir / "step1_edges.json" if pdf_dir else None
                if step1_path and step1_path.exists():
                    if step1_path.stat().st_mtime > step2_partial_path.stat().st_mtime:
                        return True, "step1_edges.json newer than partial"
                with open(step2_partial_path, "r", encoding="utf-8") as f:
                    sample = json.load(f)
                if not isinstance(sample, list):
                    return True, "partial is not a list"
                # Compare against current edge count: any cached _step2_edge_index
                # must be within [0, len(edges_list)).
                max_cached_idx = max(
                    (
                        ce.get("_step2_edge_index", -1)
                        for ce in sample
                        if isinstance(ce, dict)
                    ),
                    default=-1,
                )
                if max_cached_idx >= len(edges_list):
                    return (
                        True,
                        f"partial references edge_index {max_cached_idx} "
                        f"but only {len(edges_list)} step1 edges exist",
                    )
                return False, ""
            except Exception as e:
                return True, f"failed to read: {e}"

        if resume and step2_partial_path and step2_partial_path.exists():
            stale, reason = _partial_is_stale()
            if stale:
                print(
                    f"  [Step 2] Ignoring step2_partial.json " f"(stale: {reason})",
                    file=sys.stderr,
                )
                try:
                    step2_partial_path.unlink()
                except OSError:
                    pass
            else:
                try:
                    with open(step2_partial_path, "r", encoding="utf-8") as f:
                        cached_list = json.load(f)
                    for ce in cached_list:
                        ce_idx = ce.get("_step2_edge_index")
                        if ce_idx is not None:
                            step2_cached_edges[ce_idx] = ce
                    print(
                        f"  [Step 2] RESUME: loaded {len(step2_cached_edges)} "
                        f"cached edges from step2_partial.json",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(
                        f"  [Step 2] Failed to load partial cache: {e}",
                        file=sys.stderr,
                    )
        elif step2_partial_path and step2_partial_path.exists():
            # Not in resume mode but partial exists — almost certainly
            # leftover from a previous crashed run. Delete so it can't
            # confuse any later resume.
            try:
                step2_partial_path.unlink()
                print(
                    "  [Step 2] Removed stale step2_partial.json from "
                    "a previous run (no resume requested)",
                    file=sys.stderr,
                )
            except OSError:
                pass

        # Build GT few-shot context once (reuse across edges)
        gt_fewshot_context = None
        if self._gt_cases:
            try:
                from .gt_loader import build_fewshot_context

                gt_fewshot_context = build_fewshot_context(
                    self._gt_cases,
                    max_edges=2,
                    equation_type_filter=None,  # will be set per-edge below
                )
            except Exception as e:
                print(f"  [GT] Failed to build few-shot context: {e}", file=sys.stderr)

        for i, edge in enumerate(edges_list):
            idx = edge.get("edge_index", i + 1)
            y_short = str(edge.get("Y", ""))[:60]
            eq_pre = edge.get("_prevalidation", {}).get("equation_type", "?")

            # Check if this edge is already cached (resume mode)
            if idx in step2_cached_edges:
                filled = step2_cached_edges[idx]
                all_filled_edges.append(filled)
                eid = filled.get("edge_id", f"#{idx}")
                print(
                    f"\n  [{idx}/{len(edges_list)}] CACHED: -> {y_short} " f"({eid})",
                    file=sys.stderr,
                )
                continue

            print(
                f"\n  [{idx}/{len(edges_list)}] Filling: -> {y_short} "
                f"(pre-validated: {eq_pre}) ...",
                file=sys.stderr,
            )

            # Per-edge: try to get equation_type-specific GT example
            edge_gt_context = gt_fewshot_context
            if self._gt_cases and eq_pre != "?":
                try:
                    from .gt_loader import build_fewshot_context as _bfc

                    typed_ctx = _bfc(
                        self._gt_cases,
                        max_edges=1,
                        equation_type_filter=eq_pre,
                    )
                    if typed_ctx:
                        edge_gt_context = typed_ctx
                except Exception:
                    pass  # fallback to generic context

            try:
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
                    gt_fewshot_context=edge_gt_context,  # NEW
                    enable_hard_match=self.enable_hard_match,
                )
            except Exception as e:
                print(
                    f"         [ERROR] Edge #{idx} failed: {e}",
                    file=sys.stderr,
                )
                # Save progress so far before potentially crashing
                if step2_partial_path and all_filled_edges:
                    save_json(step2_partial_path, all_filled_edges)
                    print(
                        f"         [CHECKPOINT] Saved {len(all_filled_edges)} "
                        f"edges to step2_partial.json (edge #{idx} failed)",
                        file=sys.stderr,
                    )
                raise  # Re-raise to let caller decide

            # Tag with edge index for resume support
            filled["_step2_edge_index"] = idx
            all_filled_edges.append(filled)

            eid = filled.get("edge_id", f"#{idx}")
            eq = filled.get("equation_type", "?")
            validation = filled.get("_validation", {})
            sem_valid = validation.get("is_semantically_valid", "?")
            print(
                f"         Done: {eid} (equation_type={eq}, semantic_valid={sem_valid})",
                file=sys.stderr,
            )

            # Incremental save after each edge (crash-safe)
            if step2_partial_path:
                save_json(step2_partial_path, all_filled_edges)

        # Clean up partial file after successful completion
        if step2_partial_path and step2_partial_path.exists():
            step2_partial_path.unlink()
            print(
                "  [Step 2] All edges filled, removed step2_partial.json",
                file=sys.stderr,
            )

        # -- Step 2.5: Strong Model Recovery for null values --
        if self.strong_client and all_filled_edges:
            # anchor_set only needed when hard_match is enabled
            anchor_set = (
                extract_anchor_numbers(pdf_text) if self.enable_hard_match else None
            )
            null_count_before = sum(
                1
                for e in all_filled_edges
                if e.get("equation_formula_reported", {}).get("reported_effect_value")
                is None
                or e.get("literature_estimate", {}).get("theta_hat") is None
            )
            if null_count_before > 0:
                print(
                    f"\n[Step 2.5] Recovering {null_count_before} null values "
                    f"with strong model ...",
                    file=sys.stderr,
                )
                all_filled_edges = step2_5_recover_nulls(
                    self.strong_client,
                    pdf_text,
                    all_filled_edges,
                    anchor_set=anchor_set,
                    enable_hard_match=self.enable_hard_match,
                )
                null_count_after = sum(
                    1
                    for e in all_filled_edges
                    if e.get("equation_formula_reported", {}).get(
                        "reported_effect_value"
                    )
                    is None
                    or e.get("literature_estimate", {}).get("theta_hat") is None
                )
                print(
                    f"  Recovered: {null_count_before - null_count_after}/"
                    f"{null_count_before} null values filled",
                    file=sys.stderr,
                )
                if pdf_dir:
                    save_json(
                        pdf_dir / "step2_5_recovery.json",
                        {
                            "null_before": null_count_before,
                            "null_after": null_count_after,
                            "recovered": null_count_before - null_count_after,
                        },
                    )

        # -- Step 3: Review --
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

        # -- Step 4: Content Audit --
        audit_report = None
        if self.enable_step4 and all_filled_edges:
            all_filled_edges, audit_report = run_step4_audit(
                edges=all_filled_edges,
                pdf_text=pdf_text,
                client=self.client if self.enable_step4_llm else None,
                max_edges_per_llm_call=self.step4_max_edges_per_call,
                error_patterns_path=self.error_patterns_path,  # NEW
                enable_phase_c_autofix=self.enable_phase_c_autofix,
            )
            if pdf_dir:
                save_json(pdf_dir / "step4_audit.json", audit_report)

        # -- Save final edges --
        # Final cleanup: strip internal metadata and enforce schema
        for edge in all_filled_edges:
            edge.pop("_validation", None)
            _final_schema_enforcement(edge)

        # Drop edges that carry no numeric content after cleanup
        kept_edges, dropped_empty = filter_low_quality_edges(all_filled_edges)
        if dropped_empty:
            print(
                f"[Pipeline] Dropped {len(dropped_empty)} edges with no numeric "
                f"content (effect value / CI / theta_hat / p_value all null)",
                file=sys.stderr,
            )
        all_filled_edges = kept_edges

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
        if audit_report:
            s4 = audit_report["summary"]
            print(
                f"  Audit: {s4['phase_a_issues']} Phase A issues, "
                f"{s4['phase_a_fixes']} auto-fixed, "
                f"{s4['phase_b_issues']} Phase B issues, "
                f"{s4['edges_with_errors']} edges with errors",
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
        """Run a single pipeline step."""
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

        if step == "audit":
            pdf_name = Path(pdf_path).stem
            base = Path(output_dir or ".")
            edges_path = base / pdf_name / "edges.json"
            assert edges_path.exists(), f"Run 'full' first. Not found: {edges_path}"
            with open(edges_path, "r", encoding="utf-8") as f:
                edges = json.load(f)

            updated, report = run_step4_audit(
                edges=edges,
                pdf_text=pdf_text,
                client=self.client if self.enable_step4_llm else None,
                max_edges_per_llm_call=self.step4_max_edges_per_call,
                error_patterns_path=self.error_patterns_path,
                enable_phase_c_autofix=self.enable_phase_c_autofix,
            )

            save_json(edges_path, updated)
            save_json(base / pdf_name / "step4_audit.json", report)
            return report

        raise ValueError(f"Unknown step: {step}. Valid: classify, edges, review, audit")
