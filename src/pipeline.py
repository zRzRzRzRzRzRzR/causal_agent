"""
pipeline.py -- Edge extraction pipeline v3.

Redesigned for speed and reliability:

  Step 0: Classify paper type
  Step 1: Enumerate all X->Y statistical edges + deduplicate
  Step 1.5: Pre-validate edges (hard check values in text + soft check
            equation metadata derivation) -- NO LLM calls needed
  Step 2: Fill each edge into HPP template (simplified, no retry loop --
          pre-validation provides equation_type/model/mu/theta)
  Step 3: Review with robust JSON parsing (no crash on LLM parse failures)
  Step 4: Content audit -- deterministic + LLM checks against paper text
          (covariate hallucination, numeric hallucination, Y-label mismatch,
          HPP variable leakage, sample data errors)

Key changes from v2:
  - Step 4 added: post-extraction content audit with auto-fix
  - Phase A (deterministic): covariate/numeric/HPP leakage detection + auto-removal
  - Phase B (LLM): Y-label cross-check, adjustment semantics, guided by
    error_patterns.json from reference GT cases
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
    check_cross_edge_consistency,
    generate_quality_report,
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

    result = client.call_json(full_prompt)

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


# ---------------------------------------------------------------------------
# Step 1.5: Pre-validate edges (NO LLM calls)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Step 2: Fill one edge (simplified -- no retry loop)
# ---------------------------------------------------------------------------


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
) -> Dict:
    """
    After Step 2 LLM fills the template, verify all numeric values
    against the paper's anchor numbers. Nullify any that can't be traced.

    This is the CRITICAL fix — it prevents hallucinated numbers
    from surviving into the final output.
    """
    changes = []

    efr = filled.get("equation_formula_reported", {})
    lit = filled.get("literature_estimate", {})
    mu = filled.get("epsilon", {}).get("mu", {}).get("core", {})

    # Check reported_effect_value
    rev = efr.get("reported_effect_value")
    if rev is not None and not hard_match_value(rev, anchor_set, pdf_text):
        changes.append(f"reported_effect_value={rev} -> null (not in paper)")
        efr["reported_effect_value"] = None

    # Check reported_ci
    rci = efr.get("reported_ci", [])
    if isinstance(rci, list):
        new_ci = []
        for bound in rci:
            if bound is not None and not hard_match_value(bound, anchor_set, pdf_text):
                changes.append(f"reported_ci bound={bound} -> null")
                new_ci.append(None)
            else:
                new_ci.append(bound)
        efr["reported_ci"] = new_ci

    # Check theta_hat (only for non-log scales, since log is derived)
    theta = lit.get("theta_hat")
    if theta is not None and mu.get("scale") != "log":
        if not hard_match_value(theta, anchor_set, pdf_text):
            changes.append(
                f"theta_hat={theta} -> null (difference scale, not in paper)"
            )
            lit["theta_hat"] = None

    # For log scale: verify the original-scale value exists
    if theta is not None and mu.get("scale") == "log":
        try:
            original = math.exp(theta)
            if not hard_match_value(original, anchor_set, pdf_text):
                changes.append(
                    f"theta_hat={theta} (exp={original:.4f}) -> null "
                    f"(original scale not in paper)"
                )
                lit["theta_hat"] = None
        except (OverflowError, ValueError):
            pass

    # Check literature_estimate.ci on non-log scale
    lit_ci = lit.get("ci", [])
    if isinstance(lit_ci, list) and mu.get("scale") != "log":
        new_ci = []
        for bound in lit_ci:
            if bound is not None and not hard_match_value(bound, anchor_set, pdf_text):
                changes.append(f"lit ci bound={bound} -> null")
                new_ci.append(None)
            else:
                new_ci.append(bound)
        lit["ci"] = new_ci

    if changes:
        # Store changes in internal metadata for logging
        filled.setdefault("_hard_match", {})["nullified"] = changes

    return filled


# ---------------------------------------------------------------------------
# Step 2: Fill one edge (simplified -- no retry loop)
# ---------------------------------------------------------------------------


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
        f"## Pre-validated equation metadata (use these values)\n\n"
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

    # Apply pre-validated overrides (deterministic corrections)
    filled = _apply_prevalidation_overrides(filled, preval)

    # --- Hard-match gate: nullify any hallucinated numbers ---
    anchor_set = extract_anchor_numbers(pdf_text)
    filled = post_step2_hard_match(filled, anchor_set, pdf_text)

    if filled.get("_hard_match", {}).get("nullified"):
        print(
            f"  [Hard-Match] Nullified {len(filled['_hard_match']['nullified'])} "
            f"values not found in paper",
            file=sys.stderr,
        )
        for change in filled["_hard_match"]["nullified"]:
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


def _build_prevalidation_guidance(edge: Dict, preval: Dict) -> str:
    """Build a guidance block for the LLM based on pre-validated metadata."""
    if not preval:
        return "(No pre-validation data available)"

    lines = []
    lines.append(
        f"- **equation_type**: `{preval.get('equation_type', '?')}` "
        f"(derived from effect_scale={edge.get('effect_scale', '?')}, "
        f"outcome_type={edge.get('outcome_type', '?')})"
    )
    lines.append(f"- **model**: `{preval.get('model', '?')}`")

    mu = preval.get("mu", {})
    lines.append(
        f"- **mu**: family=`{mu.get('family', '?')}`, "
        f"type=`{mu.get('type', '?')}`, scale=`{mu.get('scale', '?')}`"
    )

    theta = preval.get("theta_hat")
    if theta is not None:
        lines.append(f"- **theta_hat** (on correct scale): `{theta}`")

    ci = preval.get("ci")
    if ci and any(v is not None for v in ci):
        lines.append(f"- **ci** (on correct scale): `{ci}`")

    reported = preval.get("reported_value")
    if reported is not None:
        lines.append(f"- **reported_value** (original scale): `{reported}`")

    reported_ci = preval.get("reported_ci")
    if reported_ci:
        lines.append(f"- **reported_ci** (original scale): `{reported_ci}`")

    lines.append(f"- **id_strategy**: `{preval.get('id_strategy', '?')}`")
    lines.append(f"- **formula_skeleton**: `{preval.get('formula_skeleton', '?')}`")

    # NEW: Include reasoning chain so LLM can reference it in its own `reason` field
    reasoning = preval.get("reasoning_chain", [])
    if reasoning:
        lines.append("\n**Derivation reasoning** (for your `reason` field reference):")
        for step in reasoning:
            lines.append(f"  - {step}")

    lines.append(
        "\n**IMPORTANT**: Use the pre-validated equation_type, model, mu, "
        "theta_hat, and ci values above. Do NOT re-derive these -- they have "
        "been verified against the paper."
    )

    return "\n".join(lines)


def _apply_prevalidation_overrides(filled: Dict, preval: Dict) -> Dict:
    """
    Force pre-validated values into the filled edge.
    This ensures equation_type, model, mu, theta_hat, and ci are correct
    even if the LLM ignored the guidance.

    Also ensures consistency between top-level equation_type and
    literature_estimate.equation_type (dual-check fields).
    """
    if not preval:
        return filled

    # Override equation_type (top-level)
    eq_type = preval.get("equation_type")
    if eq_type:
        filled["equation_type"] = eq_type

    # Override model
    model = preval.get("model")
    if model:
        lit = filled.setdefault("literature_estimate", {})
        lit["model"] = model

    # Override mu
    if preval.get("mu"):
        mu_core = (
            filled.setdefault("epsilon", {}).setdefault("mu", {}).setdefault("core", {})
        )
        mu_core.update(preval["mu"])

    # Override theta_hat and ci
    lit = filled.setdefault("literature_estimate", {})
    if preval.get("theta_hat") is not None:
        lit["theta_hat"] = preval["theta_hat"]

    if preval.get("ci") and any(v is not None for v in preval["ci"]):
        lit["ci"] = preval["ci"]

    # NOTE: Do NOT add reported_HR/reported_OR/reported_CI_* to literature_estimate.
    # The GT schema only allows: theta_hat, ci, ci_level, p_value, n, design, grade, model, adjustment_set.

    # Override id_strategy
    if preval.get("id_strategy"):
        alpha = filled.setdefault("epsilon", {}).setdefault("alpha", {})
        alpha["id_strategy"] = preval["id_strategy"]

    # ── FIX: Sync dual-check fields ──
    # literature_estimate.equation_type MUST match top-level equation_type
    final_eq_type = filled.get("equation_type")
    if final_eq_type:
        lit["equation_type"] = final_eq_type

    # literature_estimate.equation_formula SHOULD match top-level equation_formula.formula
    top_formula = filled.get("equation_formula", {})
    if isinstance(top_formula, dict) and top_formula.get("formula"):
        lit["equation_formula"] = top_formula["formula"]

    # ── FIX: Ensure model is consistent with equation_type ──
    # If the LLM wrote model="mediation" but equation_type is E1, override model
    if final_eq_type and model:
        from .semantic_validator import MODEL_TO_EQUATION_TYPE

        allowed_eqs = MODEL_TO_EQUATION_TYPE.get(model, set())
        if allowed_eqs and final_eq_type not in allowed_eqs:
            # Model is inconsistent with equation_type — re-derive model
            from .edge_prevalidator import EQUATION_TYPE_TO_MODEL

            effect_scale = str(filled.get("_prevalidation_effect_scale", ""))
            new_model_key = f"{final_eq_type}_{effect_scale}"
            new_model = EQUATION_TYPE_TO_MODEL.get(new_model_key)
            if not new_model:
                # Fallback: derive from equation_type alone
                _eq_to_default_model = {
                    "E1": "linear",
                    "E2": "Cox",
                    "E3": "LMM",
                    "E4": "mediation",
                    "E5": "counterfactual",
                    "E6": "interaction_model",
                }
                new_model = _eq_to_default_model.get(final_eq_type, "linear")
            lit["model"] = new_model
            print(
                f"  [Override] model '{model}' inconsistent with eq_type '{final_eq_type}', "
                f"corrected to '{new_model}'",
                file=sys.stderr,
            )

    # Pre-populate adjustment_variables into rho.Z and adjustment_set if LLM left them empty
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

    # ── FIX: Ensure literature_estimate.model is consistent with equation_type ──
    model = lit.get("model", "")
    if model and eq_type:
        _MODEL_TO_ALLOWED_EQ = {
            "logistic": {"E1"},
            "linear": {"E1"},
            "poisson": {"E1"},
            "ANCOVA": {"E1"},
            "IVW": {"E1"},
            "t-test": {"E1"},
            "Cox": {"E2"},
            "parametric_survival": {"E2"},
            "KM": {"E2"},
            "LMM": {"E3"},
            "GEE": {"E3"},
            "mixed": {"E3"},
            "mediation": {"E4"},
            "path_analysis": {"E4"},
            "counterfactual": {"E5"},
            "S-learner": {"E5"},
            "T-learner": {"E5"},
            "interaction_model": {"E6"},
            "factorial": {"E6"},
        }
        allowed_eqs = _MODEL_TO_ALLOWED_EQ.get(model, set())
        if allowed_eqs and eq_type not in allowed_eqs:
            _eq_to_default = {
                "E1": "linear",
                "E2": "Cox",
                "E3": "LMM",
                "E4": "mediation",
                "E5": "counterfactual",
                "E6": "interaction_model",
            }
            lit["model"] = _eq_to_default.get(eq_type, "linear")

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


# ---------------------------------------------------------------------------
# Step 3: Review (with robust JSON parsing)
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
        ns = min(spot_check_sample, len(edges))
        print(f"  [3c] Spot-checking {ns} edges ...", file=sys.stderr)
        try:
            spot_checks = _safe_spot_check(
                edges, pdf_text, client, sample_size=spot_check_sample
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

        for idx, (i, e, theta_val) in enumerate(checkable[:sample_size]):
            rho = e.get("epsilon", {}).get("rho", {})
            try:
                prompt = (
                    f"Verify: {rho.get('X', '?')} -> {rho.get('Y', '?')}\n"
                    f"Extracted theta_hat (log scale): {theta_val}\n"
                    f'Reply JSON: {{"verdict": "correct/incorrect/not_found", '
                    f'"correct_value": null}}\n\n'
                    f"Paper (first 15000 chars):\n{pdf_text[:15000]}"
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


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------


class EdgeExtractionPipeline:
    """
    Six-step pipeline:
      Step 0: Classify paper type
      Step 1: Enumerate all statistical edges (with deduplication)
      Step 1.5: Pre-validate (hard + soft check, no LLM)
      Step 2: Fill each edge into HPP template (simplified, no retry)
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
        error_patterns_path: Optional[str] = None,
        # Reference GT options (NEW)
        reference_dir: Optional[str] = None,
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

        # Step 4 flags
        self.enable_step4 = enable_step4
        self.enable_step4_llm = enable_step4_llm
        self.step4_max_edges_per_call = step4_max_edges_per_call
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
            step1_result = step1_enumerate_edges(self.client, pdf_text, evidence_type)
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
            )

            all_filled_edges.append(filled)

            eid = filled.get("edge_id", f"#{idx}")
            eq = filled.get("equation_type", "?")
            validation = filled.get("_validation", {})
            sem_valid = validation.get("is_semantically_valid", "?")
            print(
                f"         Done: {eid} (equation_type={eq}, semantic_valid={sem_valid})",
                file=sys.stderr,
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
            )
            if pdf_dir:
                save_json(pdf_dir / "step4_audit.json", audit_report)

        # -- Save final edges --
        # Final cleanup: strip internal metadata and enforce schema
        for edge in all_filled_edges:
            edge.pop("_validation", None)
            _final_schema_enforcement(edge)

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
            )

            save_json(edges_path, updated)
            save_json(base / pdf_name / "step4_audit.json", report)
            return report

        raise ValueError(f"Unknown step: {step}. Valid: classify, edges, review, audit")
