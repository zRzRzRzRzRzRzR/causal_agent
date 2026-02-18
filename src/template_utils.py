"""
template_utils.py — Template-first approach for edge filling.

Aligned with the new hpp_mapping_template.json which has a clean,
flat structure:
  edge_id, paper_title, paper_abstract, equation_type, equation_formula,
  epsilon{Pi, iota, o, tau, mu, alpha, rho},
  literature_estimate{theta_hat, ci, ...},
  hpp_mapping{X, Y, M?, X2?}

Key changes from v1:
  - Removed schema_version, equation_version, provenance, pi,
    modeling_directives, equation_inference_hints (not in new template)
  - Template comments (_comment) are stripped; no _hint/_meta system needed
  - LLM output can add extra keys (mapping_notes, composite_components,
    reported_HR, group_means, notes) — we merge them flexibly
  - hpp_mapping uses underscore dataset IDs (e.g. "009_sleep")
  - M and X2 in hpp_mapping are conditional on equation_type (E4/E6)
"""

import copy
import json
import math
import re
import sys
from typing import Any, Dict, List, Tuple

import json5


def load_template(template_path: str) -> Dict:
    """
    Load hpp_mapping_template.json, stripping JS-style comments.
    The template uses // comments which aren't valid JSON, so we strip them.
    """
    with open(template_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # Strip single-line // comments (but not inside strings)
    lines = raw.split("\n")
    cleaned = []
    for line in lines:
        # Simple approach: remove // comments not inside quotes
        # Find // that's not inside a string value
        in_string = False
        escape_next = False
        cut_pos = None
        for i, ch in enumerate(line):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
            if not in_string and i < len(line) - 1 and line[i : i + 2] == "//":
                cut_pos = i
                break
        if cut_pos is not None:
            cleaned.append(line[:cut_pos])
        else:
            cleaned.append(line)
    return json5.loads("\n".join(cleaned))


def strip_comments(obj: Any) -> Any:
    """Remove _comment keys recursively."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if k != "_comment"}
    elif isinstance(obj, list):
        return [strip_comments(item) for item in obj]
    return obj


def get_clean_skeleton(template: Dict) -> Dict:
    """Get a clean skeleton from the template, removing _comment keys."""
    return strip_comments(copy.deepcopy(template))


def prefill_skeleton(
    skeleton: Dict,
    edge: Dict,
    paper_info: Dict,
    evidence_type: str,
    pdf_name: str,
) -> Dict:
    """
    Pre-fill fields that are deterministic (known from step1 output or
    paper metadata). Reduces LLM burden and eliminates copy errors.

    Fills: edge_id, paper_title (from pdf_name if not available),
           literature_estimate partial values, epsilon.o.type
    """
    result = copy.deepcopy(skeleton)

    # ---- edge_id ----
    year = paper_info.get("year", "YYYY")
    author = str(paper_info.get("first_author", "AUTHOR"))
    # Capitalize first letter, keep rest as-is for readability
    short_title = paper_info.get("short_title", "")
    study_tag = f"{author}{short_title}".replace(" ", "")
    idx = edge.get("edge_index", 1)
    result["edge_id"] = f"EV_{year}_{study_tag}#{idx}"

    # ---- paper_title from paper_info if available ----
    if paper_info.get("short_title"):
        # Will be overwritten by LLM with full title
        pass

    # ---- literature_estimate (from step1 edge) ----
    lit = result.get("literature_estimate", {})

    estimate = edge.get("estimate")
    if estimate is not None:
        try:
            lit["theta_hat"] = float(estimate)
        except (ValueError, TypeError):
            pass

    ci = edge.get("ci")
    if isinstance(ci, list) and len(ci) == 2:
        parsed_ci = []
        for v in ci:
            try:
                parsed_ci.append(float(v) if v is not None else None)
            except (ValueError, TypeError):
                parsed_ci.append(None)
        if any(v is not None for v in parsed_ci):
            lit["ci"] = parsed_ci

    p_val = edge.get("p_value")
    if p_val is not None:
        lit["p_value"] = p_val

    # ---- outcome type -> epsilon.o.type ----
    otype = edge.get("outcome_type")
    if otype:
        result.setdefault("epsilon", {}).setdefault("o", {})["type"] = otype

    # ---- alpha.id_strategy from evidence_type ----
    alpha = result.setdefault("epsilon", {}).setdefault("alpha", {})
    if evidence_type == "interventional":
        alpha["id_strategy"] = "rct"
    elif evidence_type == "causal":
        alpha["id_strategy"] = "observational"
    elif evidence_type == "associational":
        alpha["id_strategy"] = "observational"

    # ---- literature_estimate.design from evidence_type ----
    if evidence_type == "interventional":
        lit.setdefault("design", "RCT")

    return result


# ---------------------------------------------------------------------------
# 3. Deep-merge LLM output into the skeleton (flexible merge)
# ---------------------------------------------------------------------------


def merge_with_template(skeleton: Dict, llm_output: Dict) -> Dict:
    """
    Recursively merge llm_output INTO skeleton.

    Rules (updated for new template):
      - skeleton defines the base structure (all keys preserved)
      - llm_output values overwrite skeleton values if present & meaningful
      - Extra keys in llm_output ARE allowed (e.g., mapping_notes,
        composite_components, reported_HR, group_means, notes)
      - This is more permissive than v1 — we accept LLM extensions
      - Placeholder strings from the template are overwritten
    """
    result = copy.deepcopy(skeleton)
    _recursive_merge(result, llm_output)
    return result


def _is_placeholder(val: Any) -> bool:
    """Check if a value is a template placeholder."""
    if val is None:
        return False
    if isinstance(val, str):
        return (
            val in ("...", "")
            or val.startswith("在此处")
            or val.startswith("论文")
            or val.startswith("暴露")
            or val.startswith("结局")
            or val.startswith("协变量")
            or "E1/E2" in val
        )
    return False


def _recursive_merge(target: Dict, source: Dict) -> None:
    """
    In-place recursive merge of source into target.
    Allows extra keys from source (LLM can add mapping_notes, etc.)
    """
    if not isinstance(source, dict):
        return

    # First: update existing keys
    for key in list(target.keys()):
        if key.startswith("_"):
            continue
        if key not in source:
            continue

        src_val = source[key]
        tgt_val = target[key]

        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            _recursive_merge(tgt_val, src_val)
        elif isinstance(tgt_val, list):
            if isinstance(src_val, list) and len(src_val) > 0:
                if not (len(src_val) == 1 and src_val[0] == "..."):
                    target[key] = src_val
        else:
            # Replace if source provides meaningful value, or if target is placeholder
            if src_val is not None and not _is_placeholder(src_val):
                target[key] = src_val
            elif src_val is None and _is_placeholder(tgt_val):
                target[key] = None

    # Second: add extra keys from source that aren't in target
    # This allows LLM to add mapping_notes, composite_components, reported_HR, etc.
    for key in source:
        if key.startswith("_"):
            continue
        if key not in target:
            target[key] = copy.deepcopy(source[key])


# ---------------------------------------------------------------------------
# 4. Validation
# ---------------------------------------------------------------------------

_CRITICAL_FIELDS = [
    "edge_id",
    "equation_type",
    ("epsilon", "o", "type"),
    ("epsilon", "o", "name"),
    ("epsilon", "rho", "X"),
    ("epsilon", "rho", "Y"),
    ("epsilon", "iota", "core", "name"),
    ("epsilon", "mu", "core", "family"),
    ("epsilon", "mu", "core", "type"),
    ("epsilon", "alpha", "id_strategy"),
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
        elif _is_placeholder(val):
            issues.append(f"UNFILLED: {path_str} still has placeholder")

    # Check equation_type is valid
    eq_type = edge_json.get("equation_type")
    if eq_type and eq_type not in ("E1", "E2", "E3", "E4", "E5", "E6"):
        issues.append(f"INVALID: equation_type='{eq_type}' not in E1-E6")

    # Check naming consistency: rho.X should relate to iota.core.name
    rho_x = _get_nested(edge_json, ("epsilon", "rho", "X"))
    iota_name = _get_nested(edge_json, ("epsilon", "iota", "core", "name"))
    if rho_x and iota_name:
        # Allow some flexibility — iota.core.name may be more descriptive
        # Just warn if they're completely unrelated
        rho_tokens = set(re.split(r"[_\-/\s.()]+", str(rho_x).lower()))
        iota_tokens = set(re.split(r"[_\-/\s.()]+", str(iota_name).lower()))
        overlap = rho_tokens & iota_tokens
        if len(overlap) < 1 and len(rho_tokens) > 1:
            issues.append(f"WARNING: rho.X and iota.core.name may be inconsistent")

    # Check rho.Y == o.name
    rho_y = _get_nested(edge_json, ("epsilon", "rho", "Y"))
    o_name = _get_nested(edge_json, ("epsilon", "o", "name"))
    if rho_y and o_name and rho_y != o_name:
        # Soft warning — allow minor differences
        if rho_y.lower().replace("_", " ") != o_name.lower().replace("_", " "):
            issues.append(f"WARNING: rho.Y='{rho_y}' != o.name='{o_name}'")

    # Check theta_hat is numeric (not string)
    theta = _get_nested(edge_json, ("literature_estimate", "theta_hat"))
    if theta is not None and not isinstance(theta, (int, float)):
        issues.append(
            f"TYPE_ERROR: theta_hat should be numeric, got {type(theta).__name__}"
        )

    # E4 requires M mapping
    if eq_type == "E4":
        m_ds = _get_nested(edge_json, ("hpp_mapping", "M", "dataset"))
        if not m_ds or m_ds == "N/A":
            issues.append("WARNING: E4 equation but hpp_mapping.M is missing")

    # E6 requires X2 mapping
    if eq_type == "E6":
        x2_ds = _get_nested(edge_json, ("hpp_mapping", "X2", "dataset"))
        if not x2_ds or x2_ds == "N/A":
            issues.append("WARNING: E6 equation but hpp_mapping.X2 is missing")

    # Separate hard errors from warnings
    hard_errors = [i for i in issues if not i.startswith("WARNING")]
    is_valid = len(hard_errors) == 0
    return is_valid, issues


def compute_fill_rate(edge_json: Dict) -> float:
    """
    Compute what fraction of leaf values are filled (not placeholder, not None).
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
        return 1, 1
    else:
        is_filled = obj is not None and not _is_placeholder(obj) and obj != ""
        return 1, (1 if is_filled else 0)


# ---------------------------------------------------------------------------
# 5. Auto-fix common LLM mistakes
# ---------------------------------------------------------------------------


def auto_fix(edge_json: Dict) -> Dict:
    """
    Apply deterministic fixes for common LLM output issues:
      - Normalize dataset IDs: replace '-' with '_' (template uses '-', examples use '_')
      - Ensure theta_hat is numeric
      - Fix hpp_mapping status=missing -> dataset/field = 'N/A'
      - Remove conditional fields (M, X2) if not needed by equation_type
      - Infer mu.core.scale from mu.core.type if inconsistent
    """
    # --- Normalize dataset IDs in hpp_mapping: replace '-' with '_' ---
    hm = edge_json.get("hpp_mapping", {})
    _normalize_dataset_ids(hm)

    # --- theta_hat: try to coerce string to number ---
    lit = edge_json.get("literature_estimate", {})
    theta = lit.get("theta_hat")
    if isinstance(theta, str):
        try:
            lit["theta_hat"] = float(theta)
        except (ValueError, TypeError):
            lit["theta_hat"] = None

    # --- Auto-compute theta_hat from reported ratio if on log scale ---
    mu = edge_json.get("epsilon", {}).get("mu", {}).get("core", {})
    if mu.get("scale") == "log" and mu.get("family") == "ratio":
        # If LLM provided theta_hat as the raw ratio, convert to log
        theta = lit.get("theta_hat")
        reported_ratio = (
            lit.get("reported_HR") or lit.get("reported_OR") or lit.get("reported_RR")
        )
        if theta is not None and reported_ratio is not None:
            # Check if theta looks like a raw ratio (> 0.01 and matches reported)
            try:
                if abs(float(theta) - float(reported_ratio)) < 0.01:
                    # theta_hat was given as the ratio, not log — convert
                    lit["theta_hat"] = round(math.log(float(reported_ratio)), 4)
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        elif theta is None and reported_ratio is not None:
            try:
                lit["theta_hat"] = round(math.log(float(reported_ratio)), 4)
            except (ValueError, TypeError, ZeroDivisionError):
                pass

    # --- CI: auto-convert to log scale if needed ---
    if mu.get("scale") == "log" and mu.get("family") == "ratio":
        reported_ci = (
            lit.get("reported_CI_HR")
            or lit.get("reported_CI_OR")
            or lit.get("reported_CI_RR")
        )
        ci = lit.get("ci")
        if reported_ci and isinstance(reported_ci, list) and len(reported_ci) == 2:
            # If ci matches reported_ci (ratio scale), convert to log
            if ci and isinstance(ci, list) and len(ci) == 2:
                try:
                    if ci[0] is not None and abs(ci[0] - reported_ci[0]) < 0.01:
                        lit["ci"] = [
                            (
                                round(math.log(reported_ci[0]), 4)
                                if reported_ci[0] and reported_ci[0] > 0
                                else None
                            ),
                            (
                                round(math.log(reported_ci[1]), 4)
                                if reported_ci[1] and reported_ci[1] > 0
                                else None
                            ),
                        ]
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            elif ci is None:
                try:
                    lit["ci"] = [
                        (
                            round(math.log(reported_ci[0]), 4)
                            if reported_ci[0] and reported_ci[0] > 0
                            else None
                        ),
                        (
                            round(math.log(reported_ci[1]), 4)
                            if reported_ci[1] and reported_ci[1] > 0
                            else None
                        ),
                    ]
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

    # --- Remove M/X2 from hpp_mapping if not needed ---
    eq_type = edge_json.get("equation_type", "")
    if eq_type != "E4" and "M" in hm:
        del hm["M"]
    if eq_type != "E6" and "X2" in hm:
        del hm["X2"]

    # --- Infer mu scale from type ---
    mu_type = mu.get("type", "")
    if mu_type in ("HR", "OR", "RR", "logHR", "logOR", "logRR"):
        mu["family"] = "ratio"
        mu["scale"] = "log"
    elif mu_type in ("MD", "BETA", "RD", "SMD"):
        mu["family"] = "difference"
        mu["scale"] = "identity"

    return edge_json


def _normalize_dataset_ids(mapping: Dict) -> None:
    """Replace '-' with '_' in all dataset IDs within hpp_mapping."""
    for key, val in mapping.items():
        if isinstance(val, dict):
            if "dataset" in val and isinstance(val["dataset"], str):
                val["dataset"] = val["dataset"].replace("-", "_")
            # Recurse for composite_components etc.
            _normalize_dataset_ids(val)


# ---------------------------------------------------------------------------
# 6. Prepare template for LLM prompt
# ---------------------------------------------------------------------------


def prepare_template_for_prompt(template: Dict) -> str:
    """
    Prepare the template for inclusion in the LLM prompt.
    The new template has // comments in the JSON file which serve as hints.
    We include them as-is for the LLM to read (they're excellent guidance),
    but we also provide a clean JSON version the LLM should output.
    """
    # For the prompt, we show the clean JSON skeleton (no comments)
    clean = strip_comments(template)
    return json.dumps(clean, indent=2, ensure_ascii=False)


def prepare_template_with_comments(template_path: str) -> str:
    """
    Read the raw template file including // comments for the LLM prompt.
    The comments serve as inline hints explaining each field.
    """
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 7. Full pipeline: skeleton -> prefill -> merge -> fix -> validate
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
      1. Get clean skeleton from template (strip _comment)
      2. Pre-fill deterministic fields from edge/paper metadata
      3. Merge LLM output into the skeleton (allows extra keys)
      4. Auto-fix common mistakes
      5. Validate and compute fill rate

    Returns:
      (filled_edge, is_valid, issues, fill_rate)
    """
    # Step 1: clean skeleton
    clean_skeleton = get_clean_skeleton(annotated_template)

    # Step 2: pre-fill known values
    prefilled = prefill_skeleton(
        clean_skeleton, edge, paper_info, evidence_type, pdf_name
    )

    # Step 3: merge LLM output (flexible — allows extra keys)
    merged = merge_with_template(prefilled, llm_output)

    # Step 4: auto-fix common issues
    fixed = auto_fix(merged)

    # Step 5: validate
    is_valid, issues = validate_filled_edge(fixed)
    fill_rate = compute_fill_rate(fixed)

    if issues:
        hard = [i for i in issues if not i.startswith("WARNING")]
        warns = [i for i in issues if i.startswith("WARNING")]
        if hard:
            print(f"  [Validate] {len(hard)} errors:", file=sys.stderr)
            for iss in hard[:5]:
                print(f"    ✗ {iss}", file=sys.stderr)
        if warns:
            print(f"  [Validate] {len(warns)} warnings:", file=sys.stderr)
            for w in warns[:3]:
                print(f"    ⚠ {w}", file=sys.stderr)
    print(
        f"  [Validate] fill_rate={fill_rate:.1%}, valid={is_valid}",
        file=sys.stderr,
    )

    return fixed, is_valid, issues, fill_rate
