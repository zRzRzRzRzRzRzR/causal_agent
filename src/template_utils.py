"""
template_utils.py — Template-first approach for edge filling.

Instead of trusting the LLM to reproduce the full template structure,
this module:
  1. Builds a clean skeleton from the annotated template (strip _hint keys)
  2. Pre-fills known values from edge/paper metadata before sending to LLM
  3. Merges LLM output INTO the skeleton (skeleton is the authority on structure)
  4. Validates critical fields and computes fill-rate

This guarantees every output matches the template schema exactly.
"""

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. Strip hint / meta keys from the annotated template
# ---------------------------------------------------------------------------

def strip_hints(obj: Any) -> Any:
    """
    Recursively remove all keys starting with '_hint' or '_meta'
    from dicts, producing a clean skeleton suitable for output.
    """
    if isinstance(obj, dict):
        return {
            k: strip_hints(v)
            for k, v in obj.items()
            if not k.startswith("_hint") and not k.startswith("_meta")
        }
    elif isinstance(obj, list):
        return [strip_hints(item) for item in obj]
    return obj


def strip_all_underscored(obj: Any) -> Any:
    """
    Recursively remove ALL keys starting with '_' (hints, notes, meta).
    Used when preparing the template for the LLM prompt — keeps only real fields.
    """
    if isinstance(obj, dict):
        return {
            k: strip_all_underscored(v)
            for k, v in obj.items()
            if not k.startswith("_")
        }
    elif isinstance(obj, list):
        return [strip_all_underscored(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# 2. Build a pre-filled skeleton from known edge + paper metadata
# ---------------------------------------------------------------------------

def prefill_skeleton(
    template: Dict,
    edge: Dict,
    paper_info: Dict,
    evidence_type: str,
    pdf_name: str,
) -> Dict:
    """
    Deep-copy the template and pre-fill fields that are deterministic
    (known from step1 output or paper metadata). This reduces the LLM's
    burden and eliminates common copy errors.
    """
    skeleton = copy.deepcopy(template)

    # ---- edge_id ----
    year = paper_info.get("year", "YYYY")
    author = str(paper_info.get("first_author", "AUTHOR")).upper()
    idx = edge.get("edge_index", 1)
    skeleton["edge_id"] = f"EV_{year}_{author}#{idx}"

    # ---- schema / equation version (fixed) ----
    skeleton["schema_version"] = "1.1"
    skeleton["equation_version"] = "1.0"

    # ---- provenance ----
    skeleton["provenance"] = {
        "pdf_name": pdf_name,
        "page": None,
        "table_or_figure": edge.get("source", None),
        "extractor": "llm",
    }

    # ---- pi (paper-level, partially filled) ----
    doi = paper_info.get("doi", "")
    ref_str = f"{paper_info.get('first_author', '')} {year} DOI:{doi}" if doi else f"{paper_info.get('first_author', '')} {year}"
    pi = skeleton.get("pi", {})
    pi["ref"] = ref_str
    pi["source"] = "pdf_extraction"

    # ---- literature_estimate (from step1 edge) ----
    lit = skeleton.get("literature_estimate", {})
    estimate = edge.get("estimate")
    if estimate is not None:
        try:
            lit["theta_hat"] = float(estimate)
        except (ValueError, TypeError):
            lit["theta_hat"] = None

    ci = edge.get("ci")
    if isinstance(ci, list) and len(ci) == 2:
        parsed_ci = []
        for v in ci:
            try:
                parsed_ci.append(float(v) if v is not None else None)
            except (ValueError, TypeError):
                parsed_ci.append(None)
        lit["ci"] = parsed_ci

    p_val = edge.get("p_value")
    if p_val is not None:
        lit["p_value"] = p_val

    # ---- outcome type -> epsilon.o.type ----
    o = skeleton.get("epsilon", {}).get("o", {})
    otype = edge.get("outcome_type")
    if otype:
        o["type"] = otype

    return skeleton


# ---------------------------------------------------------------------------
# 3. Deep-merge LLM output into the skeleton
# ---------------------------------------------------------------------------

def merge_with_template(skeleton: Dict, llm_output: Dict) -> Dict:
    """
    Recursively merge llm_output INTO skeleton.

    Rules:
      - skeleton defines the structure (all keys preserved)
      - llm_output values overwrite skeleton values if present
      - Extra keys in llm_output that are NOT in skeleton are ignored
      - Keys starting with '_' in skeleton are preserved as-is (no overwrite)
      - List values: if llm_output provides a non-empty list, use it;
        otherwise keep skeleton's default
    """
    result = copy.deepcopy(skeleton)
    _recursive_merge(result, llm_output)
    return result


def _recursive_merge(target: Dict, source: Dict) -> None:
    """In-place recursive merge of source into target."""
    if not isinstance(source, dict):
        return

    for key in target:
        # Skip internal/hint keys — don't let LLM overwrite them
        if key.startswith("_"):
            continue

        if key not in source:
            continue

        src_val = source[key]
        tgt_val = target[key]

        # Both dicts: recurse
        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            _recursive_merge(tgt_val, src_val)

        # Target is list: replace if source provides non-empty content
        elif isinstance(tgt_val, list):
            if isinstance(src_val, list) and len(src_val) > 0:
                # Don't accept lists that are just ["..."] (unfilled placeholder)
                if not (len(src_val) == 1 and src_val[0] == "..."):
                    target[key] = src_val

        # Scalar: replace if source provides a meaningful value
        else:
            if src_val is not None and src_val != "...":
                target[key] = src_val
            # Allow explicit null from LLM (e.g., theta_hat = null when unknown)
            elif src_val is None and tgt_val == "...":
                target[key] = None


# ---------------------------------------------------------------------------
# 4. Validation
# ---------------------------------------------------------------------------

_CRITICAL_FIELDS = [
    "schema_version",
    "edge_id",
    "equation_type",
    ("epsilon", "o", "type"),
    ("epsilon", "rho", "X"),
    ("epsilon", "rho", "Y"),
    ("hpp_mapping", "X", "dataset"),
    ("hpp_mapping", "X", "field"),
    ("hpp_mapping", "Y", "dataset"),
    ("hpp_mapping", "Y", "field"),
]


def _get_nested(d: Dict, path) -> Any:
    """Get a value from a nested dict using a tuple path."""
    if isinstance(path, str):
        return d.get(path)
    current = d
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def validate_filled_edge(edge_json: Dict) -> Tuple[bool, List[str]]:
    """
    Check that critical fields are present and non-placeholder.
    Returns (is_valid, list_of_issues).
    """
    issues = []

    for field_path in _CRITICAL_FIELDS:
        val = _get_nested(edge_json, field_path)
        path_str = field_path if isinstance(field_path, str) else ".".join(field_path)

        if val is None:
            issues.append(f"MISSING: {path_str} is null")
        elif val == "...":
            issues.append(f"UNFILLED: {path_str} still has placeholder '...'")
        elif isinstance(val, str) and val.startswith(".."):
            issues.append(f"UNFILLED: {path_str} = '{val}'")

    # Check naming consistency: rho.X == iota.core.name
    rho_x = _get_nested(edge_json, ("epsilon", "rho", "X"))
    iota_name = _get_nested(edge_json, ("epsilon", "iota", "core", "name"))
    if rho_x and iota_name and rho_x != iota_name:
        issues.append(
            f"INCONSISTENCY: rho.X='{rho_x}' != iota.core.name='{iota_name}'"
        )

    # Check rho.Y == o.name
    rho_y = _get_nested(edge_json, ("epsilon", "rho", "Y"))
    o_name = _get_nested(edge_json, ("epsilon", "o", "name"))
    if rho_y and o_name and rho_y != o_name:
        issues.append(
            f"INCONSISTENCY: rho.Y='{rho_y}' != o.name='{o_name}'"
        )

    # Check theta_hat is numeric (not string)
    theta = _get_nested(edge_json, ("literature_estimate", "theta_hat"))
    if theta is not None and not isinstance(theta, (int, float)):
        issues.append(f"TYPE_ERROR: theta_hat should be numeric, got {type(theta).__name__}: '{theta}'")

    is_valid = len(issues) == 0
    return is_valid, issues


def compute_fill_rate(edge_json: Dict) -> float:
    """
    Compute what fraction of leaf values are filled (not '...', not None).
    Ignores keys starting with '_'.
    """
    total, filled = _count_leaves(edge_json)
    return filled / max(total, 1)


def _count_leaves(obj: Any) -> Tuple[int, int]:
    """Return (total_leaves, filled_leaves)."""
    if isinstance(obj, dict):
        total = 0
        filled = 0
        for k, v in obj.items():
            if k.startswith("_"):
                continue
            t, f = _count_leaves(v)
            total += t
            filled += f
        return total, filled
    elif isinstance(obj, list):
        if len(obj) == 0:
            return 1, 0
        if len(obj) == 1 and obj[0] == "...":
            return 1, 0
        # Non-empty meaningful list counts as filled
        return 1, 1
    else:
        # Leaf scalar
        is_filled = obj is not None and obj != "..." and obj != ""
        return 1, (1 if is_filled else 0)


# ---------------------------------------------------------------------------
# 5. Auto-fix common LLM mistakes
# ---------------------------------------------------------------------------

def auto_fix(edge_json: Dict) -> Dict:
    """
    Apply deterministic fixes for common LLM output issues:
      - Enforce naming consistency (rho.X -> iota.core.name, rho.Y -> o.name)
      - Replace spaces with underscores in variable names
      - Fix hpp_mapping missing status -> dataset/field = 'N/A'
      - Infer equation_type from hints if missing
      - Set correct enabled flags in modeling_directives
      - Ensure schema_version and equation_version are present
    """
    # --- Underscores in variable names ---
    rho = edge_json.get("epsilon", {}).get("rho", {})
    for key in ["X", "Y", "X1", "X2"]:
        val = rho.get(key)
        if isinstance(val, str):
            rho[key] = val.replace(" ", "_")

    iota = edge_json.get("epsilon", {}).get("iota", {})
    iota_name = iota.get("core", {}).get("name")
    if isinstance(iota_name, str):
        iota["core"]["name"] = iota_name.replace(" ", "_")

    o = edge_json.get("epsilon", {}).get("o", {})
    o_name = o.get("name")
    if isinstance(o_name, str):
        o["name"] = o_name.replace(" ", "_")

    # --- Naming consistency: rho.X -> iota.core.name ---
    rho_x = rho.get("X")
    iota_core_name = iota.get("core", {}).get("name")
    if rho_x and iota_core_name and rho_x != iota_core_name:
        # Prefer rho.X as the canonical name
        iota["core"]["name"] = rho_x

    # --- Naming consistency: rho.Y -> o.name ---
    rho_y = rho.get("Y")
    o_name = o.get("name")
    if rho_y and o_name and rho_y != o_name:
        o["name"] = rho_y

    # --- Equation type inference ---
    hints = edge_json.get("equation_inference_hints", {})
    eq_type = edge_json.get("equation_type")

    if not eq_type:
        if hints.get("has_joint_intervention"):
            eq_type = "E6"
        elif hints.get("has_counterfactual_query"):
            eq_type = "E5"
        elif hints.get("has_mediator"):
            eq_type = "E4"
        elif hints.get("has_survival_outcome"):
            eq_type = "E2"
        elif hints.get("has_longitudinal_timepoints"):
            eq_type = "E3"
        else:
            eq_type = "E1"
        edge_json["equation_type"] = eq_type

    # --- Modeling directives: set enabled flags ---
    md = edge_json.get("modeling_directives", {})
    eq_num = eq_type.replace("E", "e") if eq_type else "e1"
    for key in ["e1", "e2", "e3", "e4", "e5", "e6"]:
        if key in md and isinstance(md[key], dict):
            md[key]["enabled"] = (key == eq_num)

    # --- HPP mapping: missing -> N/A ---
    hm = edge_json.get("hpp_mapping", {})
    for role in ["X", "Y", "X1", "X2"]:
        role_data = hm.get(role)
        if isinstance(role_data, dict) and role_data.get("status") == "missing":
            for fld in ["dataset", "field"]:
                if role_data.get(fld) in ("missing", "...", "", None):
                    role_data[fld] = "N/A"

    # --- Fixed fields ---
    edge_json.setdefault("schema_version", "1.1")
    edge_json.setdefault("equation_version", "1.0")

    # --- theta_hat: try to coerce string to number ---
    lit = edge_json.get("literature_estimate", {})
    theta = lit.get("theta_hat")
    if isinstance(theta, str):
        try:
            lit["theta_hat"] = float(theta)
        except (ValueError, TypeError):
            lit["theta_hat"] = None

    return edge_json


# ---------------------------------------------------------------------------
# 6. Full pipeline: skeleton -> prefill -> merge -> fix -> validate
# ---------------------------------------------------------------------------

def build_filled_edge(
    annotated_template: Dict,
    llm_output: Dict,
    edge: Dict,
    paper_info: Dict,
    evidence_type: str,
    pdf_name: str,
) -> Tuple[Dict, bool, List[str], float]:
    """
    Full template-first pipeline:
      1. Strip hints from annotated template to get clean skeleton
      2. Pre-fill deterministic fields from edge/paper metadata
      3. Merge LLM output into the skeleton
      4. Auto-fix common mistakes
      5. Validate and compute fill rate

    Returns:
      (filled_edge, is_valid, issues, fill_rate)
    """
    # Step 1: clean skeleton (no _hint keys)
    clean_skeleton = strip_hints(annotated_template)

    # Step 2: pre-fill known values
    prefilled = prefill_skeleton(
        clean_skeleton, edge, paper_info, evidence_type, pdf_name
    )

    # Step 3: merge LLM output (skeleton is authority on structure)
    merged = merge_with_template(prefilled, llm_output)

    # Step 4: auto-fix common issues
    fixed = auto_fix(merged)

    # Step 5: validate
    is_valid, issues = validate_filled_edge(fixed)
    fill_rate = compute_fill_rate(fixed)

    if issues:
        print(f"  [Validate] {len(issues)} issues found:", file=sys.stderr)
        for iss in issues[:5]:
            print(f"    - {iss}", file=sys.stderr)
    print(
        f"  [Validate] fill_rate={fill_rate:.1%}, valid={is_valid}",
        file=sys.stderr,
    )

    return fixed, is_valid, issues, fill_rate


def prepare_template_for_prompt(annotated_template: Dict) -> str:
    """
    Prepare the annotated template for inclusion in the LLM prompt.
    Keeps _hint fields (they guide the LLM) but strips _meta.
    """
    cleaned = copy.deepcopy(annotated_template)
    # Remove _meta (internal docs) but keep _hint (LLM guidance)
    cleaned.pop("_meta", None)
    return json.dumps(cleaned, indent=2, ensure_ascii=False)
