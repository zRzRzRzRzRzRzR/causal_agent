import copy
import json
import math
import re
import sys
from typing import Any, Dict, List, Tuple

import json5

# Upper bound for plausible ratio values (HR/OR/RR).
# Values above this on "log" scale are assumed to already be log-transformed.
# In epidemiology, ratios > 50 are virtually nonexistent.
MAX_PLAUSIBLE_RATIO = 50


def load_template(template_path: str) -> Dict:
    with open(template_path, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = raw.split("\n")
    cleaned = []
    for line in lines:
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
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if k != "_comment"}
    elif isinstance(obj, list):
        return [strip_comments(item) for item in obj]
    return obj


def get_clean_skeleton(template: Dict) -> Dict:
    """
    Get a clean skeleton from the template (no _comment keys).
    Strictly follows the template structure — does NOT inject fields
    that the template doesn't have.
    """
    skeleton = strip_comments(copy.deepcopy(template))

    # ── Ensure equation_formula is a dict (not string) ──
    ef = skeleton.get("equation_formula", {})
    if isinstance(ef, str):
        skeleton["equation_formula"] = {"formula": ef}

    # ── Ensure study_cohort sub-fields have is_reported ──
    cohort = skeleton.get("study_cohort", {})
    if isinstance(cohort, dict):
        for field_name in (
            "sample_size",
            "age",
            "sex",
            "disease_indication",
            "study_design",
            "country_or_region",
            "data_source",
            "follow_up_duration",
        ):
            fd = cohort.get(field_name)
            if isinstance(fd, dict):
                fd.setdefault("is_reported", False)
                fd.setdefault("value", "")

    return skeleton


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

    year = paper_info.get("year")
    # Guard against None, "NA", "None", empty string, non-integer
    if year is None or str(year).strip().lower() in ("", "na", "none", "yyyy", "null"):
        # Fallback: try to extract year from DOI (e.g. "10.1001/jama.2023.12345")
        doi = str(paper_info.get("doi", "") or "")
        import re as _re

        doi_year_match = _re.search(r"(19|20)\d{2}", doi)
        if doi_year_match:
            year = doi_year_match.group(0)
        else:
            # Fallback: try pdf_name (often contains year)
            name_year_match = _re.search(r"(19|20)\d{2}", pdf_name)
            if name_year_match:
                year = name_year_match.group(0)
            else:
                year = "YYYY"
    else:
        year = str(year).strip()

    author = str(paper_info.get("first_author") or "AUTHOR").strip()
    short_title = paper_info.get("short_title") or ""
    study_tag = f"{author}{short_title}".replace(" ", "")
    idx = edge.get("edge_index", 1)
    result["edge_id"] = f"EV-{year}-{study_tag}#{idx}"

    lit = result.get("literature_estimate", {})

    estimate = edge.get("estimate")
    if estimate is not None:
        # Robust parsing: handle Unicode minus signs (U+2212, en-dash U+2013)
        est_str = str(estimate).replace("\u2212", "-").replace("\u2013", "-").strip()
        try:
            lit["theta_hat"] = float(est_str)
        except (ValueError, TypeError):
            pass

    ci = edge.get("ci")
    if isinstance(ci, list) and len(ci) == 2:
        parsed_ci = []
        for v in ci:
            if v is not None:
                v_str = str(v).replace("\u2212", "-").replace("\u2013", "-").strip()
                try:
                    parsed_ci.append(float(v_str))
                except (ValueError, TypeError):
                    parsed_ci.append(None)
            else:
                parsed_ci.append(None)
        if any(v is not None for v in parsed_ci):
            lit["ci"] = parsed_ci

    p_val = edge.get("p_value")
    if p_val is not None:
        lit["p_value"] = p_val

    otype = edge.get("outcome_type")
    if otype:
        result.setdefault("epsilon", {}).setdefault("o", {})["type"] = otype

    alpha = result.setdefault("epsilon", {}).setdefault("alpha", {})
    if evidence_type == "interventional":
        alpha["id_strategy"] = "rct"
    elif evidence_type == "causal":
        alpha["id_strategy"] = "observational"
    elif evidence_type == "associational":
        alpha["id_strategy"] = "observational"

    if evidence_type == "interventional":
        lit.setdefault("design", "RCT")

    return result


# 3. Deep-merge LLM output into the skeleton (flexible merge)


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
            if src_val is not None and not _is_placeholder(src_val):
                target[key] = src_val
            elif src_val is None and _is_placeholder(tgt_val):
                target[key] = None

    for key in source:
        if key.startswith("_"):
            continue
        if key not in target:
            target[key] = copy.deepcopy(source[key])


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


def auto_fix(edge_json: Dict) -> Dict:
    """
    Apply deterministic fixes for common LLM output issues:
      - Normalize dataset IDs: replace '_' between number prefix and name with '-'
        (e.g. "055_lifestyle_and_environment" -> "055-lifestyle_and_environment")
      - Ensure theta_hat is numeric
      - Fix hpp_mapping status=missing -> dataset/field = 'N/A'
      - Ensure M and X2 keys always exist in hpp_mapping (null if not E4/E6)
      - Strip forbidden extra fields from hpp_mapping, literature_estimate
      - Strip top-level _validation key
      - Infer mu.core.scale from mu.core.type if inconsistent
      - Normalize mu.core.type to use log prefix for ratio measures
    """
    edge_json.pop("_validation", None)

    hm = edge_json.get("hpp_mapping", {})
    _normalize_dataset_ids(hm)

    eq_type = edge_json.get("equation_type", "")
    if eq_type != "E4":
        hm["M"] = None
    elif "M" not in hm:
        hm["M"] = None
    if eq_type != "E6":
        hm["X2"] = None
    elif "X2" not in hm:
        hm["X2"] = None

    _ALLOWED_MAPPING_KEYS = {"name", "dataset", "field", "status"}
    for role in ("X", "Y"):
        mapping = hm.get(role)
        if isinstance(mapping, dict):
            extra_keys = set(mapping.keys()) - _ALLOWED_MAPPING_KEYS
            for k in extra_keys:
                del mapping[k]

    rho = edge_json.get("epsilon", {}).get("rho", {})
    iota_name = (
        edge_json.get("epsilon", {}).get("iota", {}).get("core", {}).get("name", "")
    )
    o_name = edge_json.get("epsilon", {}).get("o", {}).get("name", "")

    for role, fallback_name in [
        ("X", rho.get("X") or iota_name),
        ("Y", rho.get("Y") or o_name),
    ]:
        mapping = hm.get(role)
        if isinstance(mapping, dict):
            # Ensure 'name' exists — most common LLM omission
            if not mapping.get("name") and fallback_name:
                mapping["name"] = fallback_name
            # Ensure all 4 required keys exist
            mapping.setdefault("name", "")
            mapping.setdefault("dataset", "")
            mapping.setdefault("field", "")
            mapping.setdefault("status", "missing")
    # Strip Z entries too
    z_list = hm.get("Z")
    if isinstance(z_list, list):
        for z_item in z_list:
            if isinstance(z_item, dict):
                extra_keys = set(z_item.keys()) - _ALLOWED_MAPPING_KEYS
                for k in extra_keys:
                    del z_item[k]
    # Strip any non-standard top-level keys from hpp_mapping
    _ALLOWED_HPP_KEYS = {"X", "Y", "Z", "M", "X2"}
    extra_hpp_keys = set(hm.keys()) - _ALLOWED_HPP_KEYS
    for k in extra_hpp_keys:
        del hm[k]

    # STRICTLY follow template: only these keys exist in the template
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
    lit = edge_json.get("literature_estimate", {})
    extra_lit_keys = set(lit.keys()) - _ALLOWED_LIT_KEYS
    for k in extra_lit_keys:
        del lit[k]

    # STRICTLY follow template: only these keys exist in the template
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
    efr = edge_json.get("equation_formula_reported", {})
    if isinstance(efr, dict):
        extra_efr_keys = set(efr.keys()) - _ALLOWED_EFR_KEYS
        for k in extra_efr_keys:
            del efr[k]

    # Template only has "formula" key, no "parameters"
    ef = edge_json.get("equation_formula")
    if isinstance(ef, str):
        edge_json["equation_formula"] = {"formula": ef}
    elif isinstance(ef, dict):
        ef.setdefault("formula", "")
        # Strip keys not in template (e.g. "parameters" added by LLM)
        _ALLOWED_EF_KEYS = {"formula"}
        for k in list(ef.keys()):
            if k not in _ALLOWED_EF_KEYS:
                del ef[k]
    elif ef is None:
        edge_json["equation_formula"] = {"formula": ""}

    if "equation_type" not in lit:
        lit["equation_type"] = edge_json.get("equation_type", "")
    if "equation_formula" not in lit:
        ef_obj = edge_json.get("equation_formula", {})
        if isinstance(ef_obj, dict):
            lit["equation_formula"] = ef_obj.get("formula", "")
        else:
            lit["equation_formula"] = ""

    cohort = edge_json.get("study_cohort", {})
    if isinstance(cohort, dict):
        for field_name in (
            "sample_size",
            "age",
            "sex",
            "disease_indication",
            "study_design",
            "country_or_region",
            "data_source",
            "follow_up_duration",
        ):
            fd = cohort.get(field_name)
            if isinstance(fd, dict):
                fd.setdefault("is_reported", False)
                fd.setdefault("value", "")

    theta = lit.get("theta_hat")
    if isinstance(theta, str):
        # Robust parsing: handle Unicode minus signs
        theta_str = str(theta).replace("\u2212", "-").replace("\u2013", "-").strip()
        try:
            lit["theta_hat"] = float(theta_str)
        except (ValueError, TypeError):
            lit["theta_hat"] = None

    mu = edge_json.get("epsilon", {}).get("mu", {}).get("core", {})
    if mu.get("scale") == "log" and mu.get("family") == "ratio":
        theta = lit.get("theta_hat")
        # Deterministic log transform: if mu says log scale and theta is
        # a raw ratio (positive value that is NOT already on log scale),
        # convert it. We detect "raw ratio" by checking if the value is
        # positive — log-scale values are centered around 0 and can be negative,
        # while raw ratios (HR/OR/RR) are always positive.
        if theta is not None and isinstance(theta, (int, float)) and theta > 0:
            # A positive theta on log scale would mean exp(theta) > 1.
            # But raw ratios like 0.84 are also positive.
            # Key insight: if theta is between 0.01 and 50, it's almost
            # certainly a raw ratio. True log-scale values > 3 (i.e., ratio > 20)
            # are extremely rare in epidemiology.
            # Check: is exp(theta) a plausible ratio? If theta=0.84, exp(0.84)=2.32 — plausible.
            # But theta=-0.17 is already on log scale (negative, no ambiguity).
            # So for positive theta: always convert if < 50 (safe upper bound).
            if theta < MAX_PLAUSIBLE_RATIO:
                try:
                    lit["theta_hat"] = round(math.log(theta), 6)
                except (ValueError, ZeroDivisionError):
                    pass

    if mu.get("scale") == "log" and mu.get("family") == "ratio":
        ci = lit.get("ci")
        if ci and isinstance(ci, list) and len(ci) == 2:
            new_ci = [None, None]
            needs_transform = False
            for i, bound in enumerate(ci):
                if bound is not None and isinstance(bound, (int, float)):
                    # Same logic: positive values on ratio scale need log transform
                    if bound > 0:
                        needs_transform = True
                        try:
                            new_ci[i] = round(math.log(bound), 6)
                        except (ValueError, ZeroDivisionError):
                            new_ci[i] = None
                    else:
                        # Already on log scale (negative or zero)
                        new_ci[i] = bound
                else:
                    new_ci[i] = bound
            if needs_transform:
                lit["ci"] = new_ci

    mu_type = mu.get("type", "")
    if mu_type in ("HR", "OR", "RR") and mu.get("scale") == "log":
        mu["type"] = f"log{mu_type}"
    if mu_type in ("logHR", "logOR", "logRR"):
        mu["family"] = "ratio"
        mu["scale"] = "log"
    elif mu_type in ("HR", "OR", "RR"):
        mu["family"] = "ratio"
        mu["scale"] = "log"
        mu["type"] = f"log{mu_type}"
    elif mu_type in ("MD", "BETA", "RD", "SMD"):
        mu["family"] = "difference"
        mu["scale"] = "identity"

    rho_z = rho.get("Z", [])
    if not rho_z or rho_z == ["..."]:
        rho["Z"] = []
        lit["adjustment_set"] = []
        efr = edge_json.get("equation_formula_reported", {})
        if isinstance(efr, dict):
            efr["Z"] = []
        hm["Z"] = []
    else:
        # Strip placeholder Z entries from hpp_mapping.Z
        if isinstance(hm.get("Z"), list):
            _ph_pats = ("协变量", "...", "covariate_name", "变量")
            hm["Z"] = [
                z
                for z in hm["Z"]
                if isinstance(z, dict)
                and z.get("name")
                and z["name"] not in ("...", "")
                and not any(p in z.get("name", "") for p in _ph_pats)
            ]

    # CI exists → effect_value must exist; otherwise clear CI
    efr = edge_json.get("equation_formula_reported", {})
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

    import re as _re_p

    for _cont, _key in [
        (efr, "reported_p"),
        (lit, "p_value"),
    ]:
        if not isinstance(_cont, dict):
            continue
        _pv = _cont.get(_key)
        if isinstance(_pv, str):
            _cl = _pv.strip()
            _pm = _re_p.match(r"^[<≤]\s*(\d*\.?\d+)$", _cl)
            if _pm:
                try:
                    _cont[_key] = float(_pm.group(1))
                except ValueError:
                    pass
            else:
                try:
                    _cont[_key] = float(_cl)
                except ValueError:
                    pass  # Non-numeric like "NS", leave as-is

    return edge_json


def _normalize_dataset_ids(mapping: Dict) -> None:
    """
    Normalize dataset IDs in hpp_mapping to use hyphen format.
    Converts '055_lifestyle_and_environment' -> '055-lifestyle_and_environment'
    (hyphen between numeric prefix and name, underscores within name preserved).
    """
    import re as _re

    for key, val in list(mapping.items()):
        if isinstance(val, dict):
            if "dataset" in val and isinstance(val["dataset"], str):
                ds = val["dataset"]
                # Pattern: digits followed by underscore/hyphen then name
                # Normalize to: digits-name (hyphen after prefix)
                ds = _re.sub(r"^(\d+)[_\-]", r"\1-", ds)
                val["dataset"] = ds
            _normalize_dataset_ids(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    if "dataset" in item and isinstance(item["dataset"], str):
                        ds = item["dataset"]
                        ds = _re.sub(r"^(\d+)[_\-]", r"\1-", ds)
                        item["dataset"] = ds


def prepare_template_for_prompt(template: Dict) -> str:
    """
    Prepare the template for inclusion in the LLM prompt.
    The new template has // comments in the JSON file which serve as hints.
    We include them as-is for the LLM to read (they're excellent guidance),
    but we also provide a clean JSON version the LLM should output.

    Ensures the skeleton includes all fields the prompt instructs the LLM
    to fill (parameters, reason, etc.) even if the template file doesn't
    have them.
    """
    # Use get_clean_skeleton which adds missing fields
    clean = get_clean_skeleton(template)
    return json.dumps(clean, indent=2, ensure_ascii=False)


def prepare_template_with_comments(template_path: str) -> str:
    """
    Read the raw template file including // comments for the LLM prompt.
    The comments serve as inline hints explaining each field.
    """
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


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
