import copy
import json
import re
from typing import Any, Dict, List, Set, Tuple


def _number_appears_in_text(val: Any, text: str) -> bool:
    if val is None:
        return True
    try:
        num = float(val)
    except (ValueError, TypeError):
        if isinstance(val, str):
            clean = val.replace(" ", "")
            return clean in text.replace(" ", "")
        return True

    candidates = set()
    candidates.add(f"{num:.2f}")
    candidates.add(f"{num:.3f}")
    candidates.add(f"{num:.1f}")
    if num == int(num) and abs(num) < 10000:
        candidates.add(str(int(num)))
    if 0 < abs(num) < 1:
        candidates.add(f"{num:.2f}".lstrip("0"))
        candidates.add(f"{num:.3f}".lstrip("0"))
    if abs(num) >= 1:
        candidates.add(f"{num:g}")

    text_collapsed = text.replace(" ", "").replace("\n", "")
    for c in candidates:
        if c in text_collapsed:
            return True
    return False


def _term_appears_in_text(term: str, text: str, fuzzy: bool = True) -> bool:
    """
    Check if a variable name / term appears in the paper text.
    Handles underscores, case-insensitive matching, and common abbreviations.
    """
    if not term or not text:
        return False

    # Normalize: replace underscores with spaces, lowercase
    normalized = term.replace("_", " ").lower().strip()
    text_lower = text.lower()

    # Direct match
    if normalized in text_lower:
        return True

    # Try individual significant tokens (skip short ones)
    if fuzzy:
        tokens = [t for t in normalized.split() if len(t) > 2]
        if tokens:
            # Require at least 60% of tokens to appear
            found = sum(1 for t in tokens if t in text_lower)
            if found / len(tokens) >= 0.6:
                return True

    return False


def _check_covariate_hallucination(edge: Dict, pdf_text: str) -> List[Dict[str, Any]]:
    """
    A1: Check that each variable in Z / adjustment_set actually appears
    in the paper text. Hallucinated covariates are a top error pattern.
    """
    issues = []
    rho = edge.get("epsilon", {}).get("rho", {})
    z_list = rho.get("Z", []) or []
    lit = edge.get("literature_estimate", {})
    adj_set = lit.get("adjustment_set", []) or []

    # Merge both lists (should be identical but check both)
    all_covariates = set()
    for z in z_list:
        if isinstance(z, str):
            all_covariates.add(z)
    for a in adj_set:
        if isinstance(a, str):
            all_covariates.add(a)

    for cov in sorted(all_covariates):
        if not _term_appears_in_text(cov, pdf_text):
            issues.append(
                {
                    "check": "covariate_hallucination",
                    "severity": "error",
                    "field": "epsilon.rho.Z / literature_estimate.adjustment_set",
                    "variable": cov,
                    "message": (
                        f"Covariate '{cov}' not found in paper text. "
                        f"Likely hallucinated by LLM."
                    ),
                    "action": "remove",
                }
            )

    return issues


def _check_numeric_hallucination(edge: Dict, pdf_text: str) -> List[Dict[str, Any]]:
    """
    A2: Check that key numeric values appear in the paper text.

    HARDENED VERSION:
    - reported_effect_value and reported_ci are ALWAYS checked
    - theta_hat on difference scale is ERROR (not warning) if missing
    - For continuous outcomes without CI, verify group means exist in text
    """
    issues = []
    lit = edge.get("literature_estimate", {})
    efr = edge.get("equation_formula_reported", {})
    mu = edge.get("epsilon", {}).get("mu", {}).get("core", {})

    rev = efr.get("reported_effect_value")
    if rev is not None and not _number_appears_in_text(rev, pdf_text):
        issues.append(
            {
                "check": "numeric_hallucination",
                "severity": "error",  # UPGRADED from flag_for_review
                "field": "equation_formula_reported.reported_effect_value",
                "value": rev,
                "message": f"reported_effect_value={rev} not found in paper text. "
                f"BLOCKING: this value may be hallucinated.",
                "action": "nullify",  # NEW action: set to null
            }
        )

    rci = efr.get("reported_ci")
    if isinstance(rci, list):
        for i, bound in enumerate(rci):
            if bound is not None and not _number_appears_in_text(bound, pdf_text):
                label = "lower" if i == 0 else "upper"
                issues.append(
                    {
                        "check": "numeric_hallucination",
                        "severity": "error",
                        "field": f"equation_formula_reported.reported_ci[{i}]",
                        "value": bound,
                        "message": f"CI {label} bound={bound} not found in paper text.",
                        "action": "nullify",
                    }
                )

    theta = lit.get("theta_hat")
    if theta is not None:
        if mu.get("scale") == "log":
            # For log-scale: check the ORIGINAL scale value (exp(theta))
            import math

            try:
                original = round(math.exp(theta), 2)
                if not _number_appears_in_text(original, pdf_text):
                    # Also try with more decimal places
                    original_3 = round(math.exp(theta), 3)
                    if not _number_appears_in_text(original_3, pdf_text):
                        issues.append(
                            {
                                "check": "numeric_hallucination",
                                "severity": "error",
                                "field": "literature_estimate.theta_hat",
                                "value": theta,
                                "original_scale": original,
                                "message": (
                                    f"theta_hat={theta} (exp={original}) — "
                                    f"original-scale value not found in paper text."
                                ),
                                "action": "nullify",
                            }
                        )
            except (OverflowError, ValueError):
                pass
        else:
            # For difference scale: theta_hat itself should appear in text
            if not _number_appears_in_text(theta, pdf_text):
                issues.append(
                    {
                        "check": "numeric_hallucination",
                        "severity": "error",  # UPGRADED from warning
                        "field": "literature_estimate.theta_hat",
                        "value": theta,
                        "message": (
                            f"theta_hat={theta} not found in paper text "
                            f"(difference scale). BLOCKING: likely hallucinated."
                        ),
                        "action": "nullify",
                    }
                )

    ci = lit.get("ci")
    if isinstance(ci, list) and mu.get("scale") != "log":
        for i, bound in enumerate(ci):
            if bound is not None and not _number_appears_in_text(bound, pdf_text):
                label = "lower" if i == 0 else "upper"
                issues.append(
                    {
                        "check": "numeric_hallucination",
                        "severity": "error",  # UPGRADED from warning
                        "field": f"literature_estimate.ci[{i}]",
                        "value": bound,
                        "message": f"CI {label}={bound} not found in paper text.",
                        "action": "nullify",
                    }
                )

    return issues


def _check_cross_edge_theta_duplication(edges: List[Dict]) -> List[Dict[str, Any]]:
    """
    A6: Detect when multiple edges with DIFFERENT Y variables share
    the exact same theta_hat. This is a strong signal of LLM copy-paste.
    """
    issues = []
    theta_to_edges = {}

    for i, edge in enumerate(edges):
        lit = edge.get("literature_estimate", {})
        theta = lit.get("theta_hat")
        if theta is None:
            continue
        y_name = edge.get("epsilon", {}).get("rho", {}).get("Y", "")

        key = round(float(theta), 6) if isinstance(theta, (int, float)) else theta
        if key not in theta_to_edges:
            theta_to_edges[key] = []
        theta_to_edges[key].append((i, edge.get("edge_id", f"#{i+1}"), y_name))

    for theta_val, edge_list in theta_to_edges.items():
        if len(edge_list) < 2:
            continue
        # Check if Y variables are different
        y_names = set(info[2] for info in edge_list)
        if len(y_names) > 1:
            edge_ids = [info[1] for info in edge_list]
            for idx, eid, y in edge_list:
                issues.append(
                    {
                        "check": "cross_edge_theta_duplication",
                        "severity": "error",
                        "field": "literature_estimate.theta_hat",
                        "edge_id": eid,
                        "edge_index": idx,
                        "value": theta_val,
                        "message": (
                            f"theta_hat={theta_val} is identical across edges "
                            f"{edge_ids} but they have different Y variables "
                            f"({y_names}). Likely LLM copy-paste hallucination."
                        ),
                        "action": "nullify",
                    }
                )

    return issues


def _check_computed_cohort_values(edge: Dict, pdf_text: str) -> List[Dict[str, Any]]:
    """
    A7: For study_cohort fields marked is_reported=True, verify the
    EXACT string (not just individual numbers) can be traced to the paper.
    Catches LLM-computed averages that don't appear in the text.
    """
    issues = []
    cohort = edge.get("study_cohort", {})

    age_data = cohort.get("age", {})
    if isinstance(age_data, dict) and age_data.get("is_reported"):
        val = age_data.get("value", "")
        if isinstance(val, str):
            # Extract the core number(s)
            import re

            numbers = re.findall(r"[\d.]+", val)
            for num_str in numbers:
                try:
                    num = float(num_str)
                    if num > 1 and not _number_appears_in_text(num, pdf_text):
                        issues.append(
                            {
                                "check": "computed_cohort_value",
                                "severity": "error",
                                "field": "study_cohort.age.value",
                                "value": num_str,
                                "message": (
                                    f"Age value '{num_str}' in study_cohort.age "
                                    f"not found in paper text. May be an LLM-computed "
                                    f"average of per-group values."
                                ),
                                "action": "flag_for_review",
                            }
                        )
                except ValueError:
                    pass

    return issues


def _check_sample_data(edge: Dict, pdf_text: str) -> List[Dict[str, Any]]:
    """
    A3: Check that study_cohort numeric values appear in the paper.
    """
    issues = []
    cohort = edge.get("study_cohort", {})

    for field_name, field_data in cohort.items():
        if not isinstance(field_data, dict):
            continue
        val = field_data.get("value")
        if val is None or not field_data.get("is_reported", False):
            continue

        # Extract numbers from the value string
        if isinstance(val, str):
            numbers = re.findall(r"[\d.]+", val)
            for num_str in numbers:
                try:
                    num = float(num_str)
                    if num > 1 and not _number_appears_in_text(num, pdf_text):
                        issues.append(
                            {
                                "check": "sample_data_hallucination",
                                "severity": "warning",
                                "field": f"study_cohort.{field_name}.value",
                                "value": num_str,
                                "message": (
                                    f"Number '{num_str}' from "
                                    f"study_cohort.{field_name} not found in text."
                                ),
                                "action": "flag_for_review",
                            }
                        )
                except ValueError:
                    pass

    return issues


def _check_hpp_variable_leakage(edge: Dict, pdf_text: str) -> List[Dict[str, Any]]:
    """
    A4: Check that X and Y variable names actually come from the paper,
    not leaked from HPP dictionary field names.
    """
    issues = []
    rho = edge.get("epsilon", {}).get("rho", {})
    hm = edge.get("hpp_mapping", {})

    for role in ("X", "Y"):
        # Get the variable name from rho
        var_name = rho.get(role, "")
        if not var_name:
            continue

        # Check if it appears in the paper
        if not _term_appears_in_text(var_name, pdf_text, fuzzy=True):
            # Also check hpp_mapping -- if the name only matches HPP field
            hpp_entry = hm.get(role, {})
            if isinstance(hpp_entry, dict):
                hpp_field = hpp_entry.get("field", "")
                hpp_name = hpp_entry.get("name", "")
                issues.append(
                    {
                        "check": "hpp_variable_leakage",
                        "severity": "error",
                        "field": f"epsilon.rho.{role}",
                        "value": var_name,
                        "hpp_field": hpp_field,
                        "message": (
                            f"Variable name '{var_name}' (role={role}) not found "
                            f"in paper text. May be leaked from HPP dictionary "
                            f"(hpp field='{hpp_field}')."
                        ),
                        "action": "flag_for_review",
                    }
                )

    return issues


def _check_extra_fields(edge: Dict) -> List[Dict[str, Any]]:
    """
    A5: Detect fields that shouldn't exist in the final output.
    """
    issues = []
    lit = edge.get("literature_estimate", {})

    FORBIDDEN_LIT_FIELDS = {
        "subgroup",
        "control_reference",
        "reported_HR",
        "reported_CI_HR",
        "reported_OR",
        "reported_CI_OR",
        "reported_RR",
        "reported_CI_RR",
        "group_means",
        "notes",
        "reported_effect_value",
    }

    for field in FORBIDDEN_LIT_FIELDS:
        if field in lit:
            issues.append(
                {
                    "check": "extra_field",
                    "severity": "warning",
                    "field": f"literature_estimate.{field}",
                    "message": (
                        f"Field '{field}' should not be in literature_estimate."
                    ),
                    "action": "remove",
                }
            )

    # Check hpp_mapping for extra fields
    hm = edge.get("hpp_mapping", {})
    ALLOWED_HPP_FIELDS = {"name", "dataset", "field", "status"}

    for role in ("X", "Y"):
        entry = hm.get(role)
        if isinstance(entry, dict):
            extras = set(entry.keys()) - ALLOWED_HPP_FIELDS
            if extras:
                issues.append(
                    {
                        "check": "extra_field",
                        "severity": "warning",
                        "field": f"hpp_mapping.{role}",
                        "extra_keys": sorted(extras),
                        "message": (
                            f"hpp_mapping.{role} has extra fields: {sorted(extras)}. "
                            f"Only {sorted(ALLOWED_HPP_FIELDS)} allowed."
                        ),
                        "action": "remove",
                    }
                )

    return issues


def _check_parameter_source_traceability(
    edge: Dict, pdf_text: str
) -> List[Dict[str, Any]]:
    """
    A8: Verify that parameters[].source references can be found in paper text.
    Checks both equation_formula and equation_formula_reported.

    Two sub-checks:
      - Table/Figure references cited in source actually exist in paper
      - Numeric values cited in source actually appear in paper
    """
    issues = []
    for formula_key in ("equation_formula", "equation_formula_reported"):
        formula = edge.get(formula_key, {})
        if not isinstance(formula, dict):
            continue
        params = formula.get("parameters", [])
        if not isinstance(params, list):
            continue

        for p in params:
            if not isinstance(p, dict):
                continue
            source = p.get("source", "")
            symbol = p.get("symbol", "")

            if not source or "not reported" in source.lower() or "标准" in source:
                continue

            # Sub-check 1: Table/Figure reference exists in paper
            table_match = re.search(
                r"(Table\s*\d+|Figure\s*\d+)", source, re.IGNORECASE
            )
            if table_match:
                ref = table_match.group(1).lower().replace(" ", "")
                text_lower = pdf_text.lower().replace(" ", "")
                if ref not in text_lower:
                    issues.append(
                        {
                            "check": "parameter_source_ref_missing",
                            "severity": "warning",
                            "field": f"{formula_key}.parameters[{symbol}].source",
                            "value": source,
                            "message": (
                                f"Parameter '{symbol}' source references "
                                f"'{table_match.group(1)}' but not found in paper text."
                            ),
                            "action": "flag_for_review",
                        }
                    )

            # Sub-check 2: Numeric values cited in source exist in paper
            nums_in_source = re.findall(r"\d+\.\d+", source)
            for num_str in nums_in_source:
                try:
                    if not _number_appears_in_text(float(num_str), pdf_text):
                        issues.append(
                            {
                                "check": "parameter_source_num_missing",
                                "severity": "error",
                                "field": f"{formula_key}.parameters[{symbol}].source",
                                "value": num_str,
                                "message": (
                                    f"Parameter '{symbol}' source cites value {num_str} "
                                    f"but this number not found in paper text."
                                ),
                                "action": "flag_for_review",
                            }
                        )
                except (ValueError, TypeError):
                    pass

    return issues


def _check_reported_ci_effect_value_consistency(edge: Dict) -> List[Dict[str, Any]]:
    """
    A10: If reported_ci has values, reported_effect_value MUST also have a value.
    If reported_effect_value is null but reported_ci is not, both must be cleared.
    This is a logical invariant — you can't have CI without a point estimate.
    """
    issues = []
    efr = edge.get("equation_formula_reported", {})
    if not isinstance(efr, dict):
        return issues

    rev = efr.get("reported_effect_value")
    rci = efr.get("reported_ci", [None, None])

    ci_exists = (
        isinstance(rci, list) and len(rci) == 2 and any(v is not None for v in rci)
    )

    if ci_exists and rev is None:
        issues.append(
            {
                "check": "ci_without_effect_value",
                "severity": "error",
                "field": "equation_formula_reported.reported_ci",
                "message": (
                    f"reported_ci={rci} exists but reported_effect_value is null. "
                    f"CI cannot exist without a point estimate. Both will be cleared."
                ),
                "action": "clear_ci_and_effect",
            }
        )

    return issues


def _check_reported_p_format(edge: Dict) -> List[Dict[str, Any]]:
    """
    A11: Normalize reported_p format.
    - "< 0.001" / "<0.001" / "< .001" → float 0.001
    - "< 0.05" → float 0.05
    - "0.032" (string) → float 0.032
    For '<' prefixed p-values, the numeric part is the upper bound,
    which is what we store (the actual p is guaranteed to be smaller).
    """
    issues = []
    efr = edge.get("equation_formula_reported", {})
    lit = edge.get("literature_estimate", {})

    for container_name, container, field_key in [
        ("equation_formula_reported.reported_p", efr, "reported_p"),
        ("literature_estimate.p_value", lit, "p_value"),
    ]:
        p_val = container.get(field_key)

        if p_val is None:
            continue
        if isinstance(p_val, (int, float)):
            continue  # Already numeric, skip

        if isinstance(p_val, str):
            cleaned = p_val.strip()
            # Match patterns like "< 0.001", "<0.001", "< .001", "<.05", "≤0.05"
            match = re.match(r"^[<≤]\s*(\d*\.?\d+)$", cleaned)
            if match:
                try:
                    numeric_val = float(match.group(1))
                    issues.append(
                        {
                            "check": "reported_p_format",
                            "severity": "warning",
                            "field": container_name,
                            "value": p_val,
                            "message": (
                                f"p-value '{p_val}' uses '<' prefix. "
                                f"Normalizing to float {numeric_val} "
                                f"(actual p < {numeric_val})."
                            ),
                            "action": "normalize_p",
                            "normalized_value": numeric_val,
                            "field_key": field_key,
                            "container_path": container_name.rsplit(".", 1)[0],
                        }
                    )
                except ValueError:
                    pass
            else:
                # Try direct float conversion for string numbers like "0.032"
                try:
                    numeric_val = float(cleaned)
                    issues.append(
                        {
                            "check": "reported_p_format",
                            "severity": "warning",
                            "field": container_name,
                            "value": p_val,
                            "message": (
                                f"p-value '{p_val}' is a string. "
                                f"Converting to float {numeric_val}."
                            ),
                            "action": "normalize_p",
                            "normalized_value": numeric_val,
                            "field_key": field_key,
                            "container_path": container_name.rsplit(".", 1)[0],
                        }
                    )
                except ValueError:
                    pass  # Non-numeric string like "NS", leave as-is

    return issues


def _check_formula_z_consistency(edge: Dict) -> List[Dict[str, Any]]:
    """
    A12: Check that equation_formula_reported.equation actually contains
    covariate adjustment terms if Z is non-empty.
    If the formula has no Z/gamma/covariate symbols but Z list is filled,
    the LLM likely hallucinated Z from the HPP dictionary or baseline table.
    """
    issues = []
    efr = edge.get("equation_formula_reported", {})
    if not isinstance(efr, dict):
        return issues

    equation = efr.get("equation", "") or ""
    z_list = efr.get("Z", [])

    if not z_list or not equation:
        return issues

    # Formula markers that indicate covariate adjustment is present
    has_covariate_in_formula = bool(
        re.search(
            r"gamma|γ|\\gamma|covariate|adjust|\+ γ|\+ gamma|γ\^T|gamma\^T"
            r"|Z_[a-zA-Z]|covariates|confound",
            equation,
            re.IGNORECASE,
        )
    )

    if not has_covariate_in_formula and len(z_list) > 0:
        issues.append(
            {
                "check": "formula_z_inconsistency",
                "severity": "error",
                "field": "equation_formula_reported.Z",
                "message": (
                    f"Formula '{equation[:100]}...' contains no covariate "
                    f"adjustment terms (no gamma/γ/Z symbols), but Z={z_list}. "
                    f"Z should be empty for unadjusted models."
                ),
                "current_value": z_list,
                "action": "clear_z",
            }
        )

    return issues


def _check_z_mapping_consistency(edge: Dict) -> List[Dict[str, Any]]:
    """
    A13: If epsilon.rho.Z is empty, hpp_mapping.Z must also be empty.
    Catches ghost Z mappings with placeholder names.
    """
    issues = []
    rho_z = edge.get("epsilon", {}).get("rho", {}).get("Z", [])
    hm_z = edge.get("hpp_mapping", {}).get("Z", [])

    # rho.Z is empty but hpp_mapping.Z has entries
    if (not rho_z or rho_z == ["..."]) and isinstance(hm_z, list) and len(hm_z) > 0:
        issues.append(
            {
                "check": "z_mapping_ghost",
                "severity": "error",
                "field": "hpp_mapping.Z",
                "message": (
                    f"epsilon.rho.Z is empty but hpp_mapping.Z has "
                    f"{len(hm_z)} entries. "
                    f"hpp_mapping.Z must be empty when rho.Z is empty."
                ),
                "action": "clear_z",
            }
        )

    # Check for placeholder names in hpp_mapping.Z
    if isinstance(hm_z, list):
        placeholder_patterns = ("协变量", "...", "covariate_name", "变量")
        for z_item in hm_z:
            if isinstance(z_item, dict):
                name = z_item.get("name", "")
                if any(p in name for p in placeholder_patterns) or name == "":
                    issues.append(
                        {
                            "check": "z_mapping_placeholder",
                            "severity": "error",
                            "field": "hpp_mapping.Z",
                            "message": (
                                f"hpp_mapping.Z contains placeholder entry: "
                                f"name='{name}'. Must be removed."
                            ),
                            "action": "clear_z_placeholders",
                        }
                    )
                    break  # One issue is enough

    return issues


def _check_dual_equation_consistency(edge: Dict) -> List[Dict[str, Any]]:
    """
    A9: Verify consistency between equation_formula, equation_formula_reported,
    and literature_estimate dual-check fields.

    Checks:
      1. model_type consistency (efr.model_type == lit.model)
      2. equation_type consistency (top-level == lit.equation_type)
      3. reported_effect_value <-> theta_hat scale consistency
      4. X/Y name consistency (efr.X/Y == rho.X/Y)
    """
    issues = []
    import math as _math

    efr = edge.get("equation_formula_reported", {})
    if not isinstance(efr, dict):
        efr = {}
    lit = edge.get("literature_estimate", {})
    top_eq = edge.get("equation_type", "")
    mu = edge.get("epsilon", {}).get("mu", {}).get("core", {})

    # 1. model_type consistency
    efr_model = efr.get("model_type", "")
    lit_model = lit.get("model", "")
    if efr_model and lit_model:
        if efr_model.lower() != lit_model.lower():
            issues.append(
                {
                    "check": "dual_model_type_mismatch",
                    "severity": "error",
                    "field": "equation_formula_reported.model_type",
                    "message": (
                        f"equation_formula_reported.model_type='{efr_model}' != "
                        f"literature_estimate.model='{lit_model}'"
                    ),
                    "action": "flag_for_review",
                }
            )

    # 2. equation_type consistency
    lit_eq = lit.get("equation_type", "")
    if top_eq and lit_eq and top_eq != lit_eq:
        issues.append(
            {
                "check": "dual_equation_type_mismatch",
                "severity": "error",
                "field": "literature_estimate.equation_type",
                "message": (
                    f"Top-level equation_type='{top_eq}' != "
                    f"literature_estimate.equation_type='{lit_eq}'"
                ),
                "action": "flag_for_review",
            }
        )

    # 3. reported_effect_value <-> theta_hat scale consistency
    rev = efr.get("reported_effect_value")
    theta = lit.get("theta_hat")
    if rev is not None and theta is not None:
        try:
            rev_f = float(rev)
            theta_f = float(theta)
            if mu.get("scale") == "log" and rev_f > 0:
                expected = round(_math.log(rev_f), 4)
                if abs(theta_f - expected) > 0.02:
                    issues.append(
                        {
                            "check": "theta_reported_value_mismatch",
                            "severity": "error",
                            "field": "literature_estimate.theta_hat",
                            "message": (
                                f"theta_hat={theta_f} but "
                                f"ln(reported_effect_value={rev_f})={expected}"
                            ),
                            "action": "flag_for_review",
                        }
                    )
            elif mu.get("scale") == "identity":
                if abs(theta_f - rev_f) > 0.01:
                    issues.append(
                        {
                            "check": "theta_reported_value_mismatch",
                            "severity": "error",
                            "field": "literature_estimate.theta_hat",
                            "message": (
                                f"theta_hat={theta_f} != "
                                f"reported_effect_value={rev_f} on identity scale"
                            ),
                            "action": "flag_for_review",
                        }
                    )
        except (ValueError, TypeError):
            pass

    # 4. X/Y name consistency between efr and rho
    rho = edge.get("epsilon", {}).get("rho", {})
    for role in ("X", "Y"):
        efr_val = efr.get(role, "")
        rho_val = rho.get(role, "")
        if efr_val and rho_val and efr_val != rho_val:
            issues.append(
                {
                    "check": f"dual_{role}_name_mismatch",
                    "severity": "warning",
                    "field": f"equation_formula_reported.{role}",
                    "message": (
                        f"equation_formula_reported.{role}='{efr_val}' != "
                        f"epsilon.rho.{role}='{rho_val}'"
                    ),
                    "action": "flag_for_review",
                }
            )

    return issues


# ──────────────────────────────────────────────────────────────────────
# A14–A19: evidence_first hard rules. These run unconditionally for the
# rules that don't require Step 1 evidence fields, and gracefully no-op
# in legacy mode for the rules that consume `_step1_evidence`.
# ──────────────────────────────────────────────────────────────────────


def _check_ci_contains_point_estimate(edge: Dict) -> List[Dict[str, Any]]:
    """
    A14 — quant hard rule: a reported CI must straddle its point estimate.
    Catches LLM transcription errors where the CI was lifted from a
    different table row than the effect value.

    Checks:
      - equation_formula_reported.reported_ci contains reported_effect_value
      - literature_estimate.ci contains literature_estimate.theta_hat
    """
    issues: List[Dict[str, Any]] = []
    edge_id = edge.get("edge_id", "?")
    efr = edge.get("equation_formula_reported", {}) or {}
    lit = edge.get("literature_estimate", {}) or {}

    def _check_pair(point: Any, ci: Any, field_label: str) -> None:
        if point is None or not isinstance(ci, list) or len(ci) != 2:
            return
        lo, hi = ci
        if lo is None or hi is None:
            return
        try:
            point_f = float(point)
            lo_f = float(lo)
            hi_f = float(hi)
        except (TypeError, ValueError):
            return
        if lo_f > hi_f:
            issues.append(
                {
                    "edge_id": edge_id,
                    "check": "ci_bounds_inverted",
                    "severity": "error",
                    "field": field_label,
                    "message": (
                        f"{field_label}: CI lower={lo_f} > upper={hi_f} "
                        f"(swapped or transcription error)"
                    ),
                    "action": "swap_ci",
                    "ci_field": field_label,
                }
            )
            lo_f, hi_f = hi_f, lo_f
        # Use a small tolerance — extracted CIs sometimes round differently
        # from the point. 1% of the half-width is plenty.
        slack = max(abs(hi_f - lo_f) * 0.01, 1e-9)
        if not (lo_f - slack <= point_f <= hi_f + slack):
            issues.append(
                {
                    "edge_id": edge_id,
                    "check": "ci_does_not_contain_point",
                    "severity": "error",
                    "field": field_label,
                    "message": (
                        f"{field_label}={point_f} falls outside CI "
                        f"[{lo_f}, {hi_f}]"
                    ),
                }
            )

    _check_pair(
        efr.get("reported_effect_value"),
        efr.get("reported_ci"),
        "equation_formula_reported.reported_ci",
    )
    _check_pair(
        lit.get("theta_hat"),
        lit.get("ci"),
        "literature_estimate.ci",
    )
    return issues


def _check_grade_downgrade_when_no_ci(edge: Dict) -> List[Dict[str, Any]]:
    """
    A15 — quant hard rule: if theta_hat is present but ci is missing, the
    edge is "estimate without uncertainty" — downgrade grade to 'C'.
    """
    issues: List[Dict[str, Any]] = []
    lit = edge.get("literature_estimate", {}) or {}
    theta = lit.get("theta_hat")
    ci = lit.get("ci", [None, None])
    has_ci = isinstance(ci, list) and any(v is not None for v in ci)
    grade = lit.get("grade")

    if theta is not None and not has_ci and grade not in ("C", None):
        issues.append(
            {
                "edge_id": edge.get("edge_id", "?"),
                "check": "grade_downgrade_no_ci",
                "severity": "warning",
                "field": "literature_estimate.grade",
                "message": (
                    f"theta_hat={theta} but ci is null; downgrading "
                    f"grade from {grade!r} to 'C'."
                ),
                "action": "downgrade_grade",
                "before": grade,
                "after": "C",
            }
        )
    return issues


def _check_drop_when_no_theta_hat(edge: Dict) -> List[Dict[str, Any]]:
    """
    A16 — quant hard rule: edges with no theta_hat AND no reported_effect_value
    AND no p_value are not actionable evidence — record drop_reason.

    Marks the edge for removal via action="drop_edge". Edges explicitly
    flagged `has_numeric_estimate=false` from Step 1 are exempt — those
    are intentional qualitative records.
    """
    issues: List[Dict[str, Any]] = []
    if edge.get("has_numeric_estimate") is False:
        return issues

    lit = edge.get("literature_estimate", {}) or {}
    efr = edge.get("equation_formula_reported", {}) or {}
    theta = lit.get("theta_hat")
    rev = efr.get("reported_effect_value")
    pval = lit.get("p_value")
    if theta is None and rev is None and pval is None:
        issues.append(
            {
                "edge_id": edge.get("edge_id", "?"),
                "check": "no_quantitative_evidence",
                "severity": "error",
                "field": "literature_estimate",
                "message": (
                    "Edge has no theta_hat, no reported_effect_value, and no "
                    "p_value — not actionable evidence. Drop with reason."
                ),
                "action": "drop_edge",
                "drop_reason": "no_quantitative_evidence",
            }
        )
    return issues


# Map equation_type → required model_type tokens (case-insensitive substring).
# Empty set means "no constraint" (e.g. E5 / E6 cover too much variety).
_EQTYPE_TO_MODEL_TOKENS: Dict[str, Tuple[str, ...]] = {
    "E2": ("cox", "km", "kaplan", "parametric_survival", "fine-gray", "weibull", "aft"),
    "E3": ("lmm", "gee", "ancova", "mixed", "repeated", "rmanova", "change"),
}


def _check_equation_type_model_match(edge: Dict) -> List[Dict[str, Any]]:
    """
    A17 — semantic hard rule: the model_type must be plausible for the
    equation_type. E2 (survival) is meaningless if model is "linear" or
    "logistic"; E3 (longitudinal) is meaningless if model is "Cox".
    """
    issues: List[Dict[str, Any]] = []
    eq_type = str(edge.get("equation_type", "") or "")
    expected = _EQTYPE_TO_MODEL_TOKENS.get(eq_type)
    if not expected:
        return issues
    efr = edge.get("equation_formula_reported", {}) or {}
    lit = edge.get("literature_estimate", {}) or {}
    model_str = str(efr.get("model_type", "") or "") + " " + str(lit.get("model", "") or "")
    model_norm = model_str.lower()
    if model_norm.strip() == "":
        return issues  # nothing to compare against
    if not any(tok in model_norm for tok in expected):
        issues.append(
            {
                "edge_id": edge.get("edge_id", "?"),
                "check": "equation_type_model_mismatch",
                "severity": "error",
                "field": "equation_type",
                "message": (
                    f"equation_type={eq_type} requires model_type to "
                    f"contain one of {list(expected)}, got "
                    f"model_type={efr.get('model_type')!r} / "
                    f"model={lit.get('model')!r}"
                ),
            }
        )
    return issues


def _check_statistic_type_consistency(edge: Dict) -> List[Dict[str, Any]]:
    """
    A18 — semantic hard rule (evidence_first only): the Step 1
    `statistic_type` must be consistent with downstream packaging.

    Rules:
      - statistic_type='crude_rate' MUST NOT be packaged as Cox HR
        (mu.family='ratio' + mu.scale='log' is the giveaway).
      - statistic_type='group_mean' MUST NOT carry a non-null theta_hat
        on identity scale unless the LLM was actually given a between-
        group difference. Without a true MD, we cannot derive theta.
      - statistic_type='within_group_change' MUST NOT be presented as a
        between-group effect — control/reference must include "baseline"
        or be empty.
    """
    issues: List[Dict[str, Any]] = []
    sev = edge.get("_step1_evidence")
    if not isinstance(sev, dict):
        return issues  # legacy mode — no Step 1 evidence to check against
    st_type = sev.get("statistic_type")
    if not st_type or st_type in ("model_effect", "between_group_effect", "subgroup",
                                  "sensitivity", "unknown"):
        return issues

    edge_id = edge.get("edge_id", "?")
    mu = edge.get("epsilon", {}).get("mu", {}).get("core", {}) or {}
    lit = edge.get("literature_estimate", {}) or {}

    if st_type == "crude_rate":
        if mu.get("family") == "ratio" and mu.get("scale") == "log":
            issues.append(
                {
                    "edge_id": edge_id,
                    "check": "crude_rate_packaged_as_ratio",
                    "severity": "error",
                    "field": "epsilon.mu.core",
                    "message": (
                        "statistic_type='crude_rate' but mu is "
                        "(family=ratio, scale=log) — a crude incidence "
                        "rate cannot be a Cox HR. Step 1 likely "
                        "mis-labeled statistical_method."
                    ),
                }
            )

    if st_type == "group_mean":
        theta = lit.get("theta_hat")
        if theta is not None:
            issues.append(
                {
                    "edge_id": edge_id,
                    "check": "group_mean_packaged_as_difference",
                    "severity": "error",
                    "field": "literature_estimate.theta_hat",
                    "message": (
                        f"statistic_type='group_mean' but theta_hat={theta} "
                        f"— group means alone do not give a between-group "
                        f"effect. Either drop theta_hat or split into "
                        f"separate per-group records."
                    ),
                }
            )

    if st_type == "within_group_change":
        c_field = (edge.get("epsilon", {}).get("rho", {}).get("C")
                   or edge.get("C", "")
                   or "")
        c_norm = str(c_field).lower()
        if c_norm and "baseline" not in c_norm and "pre" not in c_norm:
            issues.append(
                {
                    "edge_id": edge_id,
                    "check": "within_group_change_misframed",
                    "severity": "error",
                    "field": "epsilon.rho.C",
                    "message": (
                        f"statistic_type='within_group_change' but "
                        f"control/reference={c_field!r} suggests a "
                        f"between-group framing. A within-group change "
                        f"compares the same group at two time points."
                    ),
                }
            )
    return issues


def _check_evidence_text_traceability(
    edge: Dict, pdf_text: str
) -> List[Dict[str, Any]]:
    """
    A19 — evidence hard rule (evidence_first only): when reported_effect_value
    is not null, evidence_text MUST be non-empty AND must contain a number
    matching the reported value (within rounding).

    Skipped in legacy mode (no _step1_evidence field). Best-effort — small
    rounding mismatches are OK.
    """
    issues: List[Dict[str, Any]] = []
    sev = edge.get("_step1_evidence")
    if not isinstance(sev, dict):
        return issues
    efr = edge.get("equation_formula_reported", {}) or {}
    rev = efr.get("reported_effect_value")
    if rev is None:
        return issues
    evid = sev.get("evidence_text", "") or ""
    if not str(evid).strip():
        issues.append(
            {
                "edge_id": edge.get("edge_id", "?"),
                "check": "evidence_text_missing",
                "severity": "warning",
                "field": "_step1_evidence.evidence_text",
                "message": (
                    f"reported_effect_value={rev} but evidence_text is "
                    f"empty — cannot verify provenance."
                ),
            }
        )
        return issues
    try:
        rev_f = float(rev)
    except (TypeError, ValueError):
        return issues
    # Look for the number in the evidence quote, allow ± half-tick of
    # last reported digit.
    import re as _re

    nums = [float(x) for x in _re.findall(r"-?\d+\.?\d*", str(evid))]
    if nums and not any(abs(n - rev_f) <= max(abs(rev_f) * 0.005, 0.005) for n in nums):
        issues.append(
            {
                "edge_id": edge.get("edge_id", "?"),
                "check": "evidence_text_does_not_contain_value",
                "severity": "warning",
                "field": "_step1_evidence.evidence_text",
                "message": (
                    f"reported_effect_value={rev_f} not found in "
                    f"evidence_text={evid[:80]!r} — possible mis-quote."
                ),
            }
        )
    return issues


def phase_a_audit(
    edges: List[Dict], pdf_text: str
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Run all Phase A (deterministic) checks on every edge.

    Returns:
        (edges_with_issues, phase_a_report)
    """
    all_issues: List[Dict] = []

    for i, edge in enumerate(edges):
        edge_id = edge.get("edge_id", f"#{i+1}")
        edge_issues: List[Dict] = []

        edge_issues.extend(_check_covariate_hallucination(edge, pdf_text))
        edge_issues.extend(_check_numeric_hallucination(edge, pdf_text))
        edge_issues.extend(_check_sample_data(edge, pdf_text))
        edge_issues.extend(_check_hpp_variable_leakage(edge, pdf_text))
        edge_issues.extend(_check_extra_fields(edge))
        edge_issues.extend(_check_computed_cohort_values(edge, pdf_text))
        edge_issues.extend(_check_parameter_source_traceability(edge, pdf_text))  # A8
        edge_issues.extend(_check_dual_equation_consistency(edge))  # A9
        edge_issues.extend(_check_reported_ci_effect_value_consistency(edge))  # A10
        edge_issues.extend(_check_reported_p_format(edge))  # A11
        edge_issues.extend(_check_formula_z_consistency(edge))  # A12
        edge_issues.extend(_check_z_mapping_consistency(edge))  # A13
        # Round 2 evidence_first quant + semantic + traceability checks.
        # These run unconditionally when the data exists; A18 / A19 no-op
        # silently in legacy mode (when _step1_evidence is absent).
        edge_issues.extend(_check_ci_contains_point_estimate(edge))  # A14
        edge_issues.extend(_check_grade_downgrade_when_no_ci(edge))  # A15
        edge_issues.extend(_check_drop_when_no_theta_hat(edge))  # A16
        edge_issues.extend(_check_equation_type_model_match(edge))  # A17
        edge_issues.extend(_check_statistic_type_consistency(edge))  # A18
        edge_issues.extend(_check_evidence_text_traceability(edge, pdf_text))  # A19

        for iss in edge_issues:
            iss["edge_id"] = edge_id
            iss["edge_index"] = i

        all_issues.extend(edge_issues)

    # Cross-edge checks (run on full set)
    cross_issues = _check_cross_edge_theta_duplication(edges)  # NEW
    all_issues.extend(cross_issues)

    # Summary
    severity_counts = {}
    check_counts = {}
    for iss in all_issues:
        sev = iss.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        chk = iss.get("check", "unknown")
        check_counts[chk] = check_counts.get(chk, 0) + 1

    report = {
        "phase": "A",
        "total_issues": len(all_issues),
        "by_severity": severity_counts,
        "by_check": check_counts,
        "issues": all_issues,
    }

    return edges, report


def apply_phase_a_fixes(
    edges: List[Dict],
    issues: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """
    Apply automatic fixes for Phase A issues.

    NEW action: "nullify" — set the field value to null.
    This is used for hallucinated numeric values that fail hard-match.

    Returns:
        (fixed_edges, applied_fixes)
    """
    fixed_edges = copy.deepcopy(edges)
    applied_fixes: List[Dict] = []

    # Group issues by edge_index
    issues_by_edge: Dict[int, List[Dict]] = {}
    for iss in issues:
        idx = iss.get("edge_index", -1)
        issues_by_edge.setdefault(idx, []).append(iss)

    for idx, edge_issues in issues_by_edge.items():
        if idx < 0 or idx >= len(fixed_edges):
            continue
        edge = fixed_edges[idx]

        for iss in edge_issues:
            action = iss.get("action")

            if action == "remove":
                check = iss["check"]
                if check == "covariate_hallucination":
                    var = iss["variable"]
                    rho_z = edge.get("epsilon", {}).get("rho", {}).get("Z", [])
                    if isinstance(rho_z, list) and var in rho_z:
                        rho_z.remove(var)
                        applied_fixes.append(
                            {
                                "edge_id": iss["edge_id"],
                                "action": "removed_from_rho_Z",
                                "variable": var,
                            }
                        )
                    adj = edge.get("literature_estimate", {}).get("adjustment_set", [])
                    if isinstance(adj, list) and var in adj:
                        adj.remove(var)
                        applied_fixes.append(
                            {
                                "edge_id": iss["edge_id"],
                                "action": "removed_from_adjustment_set",
                                "variable": var,
                            }
                        )
                    hm_z = edge.get("hpp_mapping", {}).get("Z", [])
                    if isinstance(hm_z, list):
                        edge["hpp_mapping"]["Z"] = [
                            z
                            for z in hm_z
                            if not (isinstance(z, dict) and z.get("name") == var)
                        ]
                elif check == "extra_field":
                    field_path = iss["field"]
                    parts = field_path.split(".")
                    if len(parts) == 2:
                        container = edge.get(parts[0], {})
                        if isinstance(container, dict) and parts[1] in container:
                            del container[parts[1]]
                            applied_fixes.append(
                                {
                                    "edge_id": iss["edge_id"],
                                    "action": "removed_extra_field",
                                    "field": field_path,
                                }
                            )
                    elif "extra_keys" in iss:
                        role = parts[-1] if len(parts) >= 2 else ""
                        entry = edge.get("hpp_mapping", {}).get(role, {})
                        if isinstance(entry, dict):
                            for key in iss["extra_keys"]:
                                entry.pop(key, None)
                            applied_fixes.append(
                                {
                                    "edge_id": iss["edge_id"],
                                    "action": "removed_extra_hpp_keys",
                                    "field": field_path,
                                    "keys": iss["extra_keys"],
                                }
                            )

            elif action == "nullify":
                # NEW: Set hallucinated values to null
                field_path = iss.get("field", "")

                if field_path == "equation_formula_reported.reported_effect_value":
                    efr = edge.get("equation_formula_reported", {})
                    old_val = efr.get("reported_effect_value")
                    efr["reported_effect_value"] = None
                    applied_fixes.append(
                        {
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "nullified_hallucinated_value",
                            "field": field_path,
                            "old_value": old_val,
                            "reason": iss["message"],
                        }
                    )

                elif field_path.startswith("equation_formula_reported.reported_ci"):
                    efr = edge.get("equation_formula_reported", {})
                    old_ci = efr.get("reported_ci")
                    efr["reported_ci"] = [None, None]
                    applied_fixes.append(
                        {
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "nullified_hallucinated_ci",
                            "field": "equation_formula_reported.reported_ci",
                            "old_value": old_ci,
                        }
                    )

                elif field_path == "literature_estimate.theta_hat":
                    lit = edge.get("literature_estimate", {})
                    old_theta = lit.get("theta_hat")
                    lit["theta_hat"] = None
                    applied_fixes.append(
                        {
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "nullified_hallucinated_theta",
                            "field": field_path,
                            "old_value": old_theta,
                            "reason": iss["message"],
                        }
                    )

                elif field_path.startswith("literature_estimate.ci"):
                    lit = edge.get("literature_estimate", {})
                    old_ci = lit.get("ci")
                    lit["ci"] = [None, None]
                    applied_fixes.append(
                        {
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "nullified_hallucinated_lit_ci",
                            "field": "literature_estimate.ci",
                            "old_value": old_ci,
                        }
                    )

            elif action == "clear_ci_and_effect":
                # A10: CI exists but effect_value is null → clear both
                efr = edge.get("equation_formula_reported", {})
                old_ci = efr.get("reported_ci")
                efr["reported_ci"] = [None, None]
                applied_fixes.append(
                    {
                        "edge_id": iss.get("edge_id", "?"),
                        "action": "cleared_orphan_ci",
                        "field": "equation_formula_reported.reported_ci",
                        "old_value": old_ci,
                        "reason": "CI cannot exist without point estimate",
                    }
                )

            elif action == "normalize_p":
                # A11: Convert string p-value to float
                container_path = iss.get("container_path", "")
                field_key = iss.get("field_key", "")
                normalized = iss.get("normalized_value")
                if container_path and field_key and normalized is not None:
                    if container_path == "equation_formula_reported":
                        container = edge.get("equation_formula_reported", {})
                    elif container_path == "literature_estimate":
                        container = edge.get("literature_estimate", {})
                    else:
                        container = {}
                    if container:
                        old_val = container.get(field_key)
                        container[field_key] = normalized
                        applied_fixes.append(
                            {
                                "edge_id": iss.get("edge_id", "?"),
                                "action": "normalized_p_value",
                                "field": iss.get("field", ""),
                                "old_value": old_val,
                                "new_value": normalized,
                            }
                        )

            elif action in ("clear_z", "clear_z_placeholders"):
                # A12/A13: Clear all Z fields across the edge
                rho = edge.get("epsilon", {}).get("rho", {})
                old_rho_z = rho.get("Z", [])
                rho["Z"] = []

                lit = edge.get("literature_estimate", {})
                old_adj = lit.get("adjustment_set", [])
                lit["adjustment_set"] = []

                efr = edge.get("equation_formula_reported", {})
                if isinstance(efr, dict):
                    old_efr_z = efr.get("Z", [])
                    efr["Z"] = []

                hm = edge.get("hpp_mapping", {})
                old_hm_z = hm.get("Z", [])
                if action == "clear_z_placeholders":
                    # Only remove placeholder entries, keep valid ones
                    placeholder_patterns = ("协变量", "...", "covariate_name", "变量")
                    hm["Z"] = [
                        z
                        for z in (hm.get("Z") or [])
                        if isinstance(z, dict)
                        and z.get("name")
                        and not any(
                            p in z.get("name", "") for p in placeholder_patterns
                        )
                        and z.get("name") != ""
                    ]
                else:
                    hm["Z"] = []

                applied_fixes.append(
                    {
                        "edge_id": iss.get("edge_id", "?"),
                        "action": f"cleared_z_fields_{action}",
                        "old_rho_z": old_rho_z,
                        "old_adjustment_set": old_adj,
                        "reason": iss.get("message", ""),
                    }
                )

            # Round 2: new auto-fix actions for evidence_first hard rules.
            elif action == "swap_ci":
                # A14: lower > upper, swap. We don't try to validate the
                # numbers further — that's a separate check.
                ci_field = iss.get("ci_field", "")
                if ci_field == "literature_estimate.ci":
                    ci = edge.get("literature_estimate", {}).get("ci")
                    if isinstance(ci, list) and len(ci) == 2:
                        edge["literature_estimate"]["ci"] = [ci[1], ci[0]]
                        applied_fixes.append({
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "swapped_ci",
                            "field": ci_field,
                        })
                elif ci_field == "equation_formula_reported.reported_ci":
                    ci = edge.get("equation_formula_reported", {}).get("reported_ci")
                    if isinstance(ci, list) and len(ci) == 2:
                        edge["equation_formula_reported"]["reported_ci"] = [ci[1], ci[0]]
                        applied_fixes.append({
                            "edge_id": iss.get("edge_id", "?"),
                            "action": "swapped_reported_ci",
                            "field": ci_field,
                        })

            elif action == "downgrade_grade":
                # A15: theta_hat present but ci null → grade='C'
                lit = edge.setdefault("literature_estimate", {})
                lit["grade"] = "C"
                applied_fixes.append({
                    "edge_id": iss.get("edge_id", "?"),
                    "action": "downgraded_grade",
                    "before": iss.get("before"),
                    "after": "C",
                })

            elif action == "drop_edge":
                # A16: mark for removal. We don't actually delete here
                # (the calling code owns the list); we tag with
                # _drop_reason so a downstream filter step can remove it.
                edge["_drop_reason"] = iss.get(
                    "drop_reason", "phase_a_drop_unspecified"
                )
                applied_fixes.append({
                    "edge_id": iss.get("edge_id", "?"),
                    "action": "marked_for_drop",
                    "drop_reason": edge["_drop_reason"],
                })

    # After per-edge processing, materialize any drops requested.
    drop_count = sum(1 for e in fixed_edges if e.get("_drop_reason"))
    if drop_count > 0:
        survivors: List[Dict] = []
        for e in fixed_edges:
            if e.get("_drop_reason"):
                applied_fixes.append({
                    "edge_id": e.get("edge_id", "?"),
                    "action": "edge_dropped",
                    "drop_reason": e.get("_drop_reason"),
                })
            else:
                survivors.append(e)
        fixed_edges = survivors

    return fixed_edges, applied_fixes


_PHASE_B_SYSTEM_PROMPT = """\
你是一名医学信息学审核员。你的任务是对照论文原文，逐字段验证证据卡（edge JSON）的准确性。

## 审核重点

你需要检查以下高频错误模式：

### 1. Y标签混淆
模型可能把数值填到了错误的Y变量下。例如：论文报告了WORD和Neale两种阅读测试的分数，
模型可能把WORD的分数填到了Neale的edge下。
**检查方法**: 找到论文中该数值的原始出处，确认它属于当前edge声称的Y变量。

### 2. 协变量语义错误
模型可能把matching变量、分层变量误标为regression covariates。
例如：论文用年龄/性别做分组匹配(matching)，但模型把它们填入Z（调整变量）。
**检查方法**: 阅读论文方法部分，区分 matching variables vs adjustment covariates vs stratification variables。

### 3. 样本量混淆
模型可能引用了筛选阶段(screening)而非最终分析样本的人数。
**检查方法**: 确认sample_size对应的是最终分析样本，而非初始招募人数。

### 4. 统计方法误判
模型可能错误识别统计方法（例如把ANOVA标记为linear regression）。
**检查方法**: 阅读论文方法部分，确认实际使用的统计模型。

## 审核输出格式

对每个edge，逐字段检查以下内容，输出JSON：

```json
{
  "edge_audits": [
    {
      "edge_id": "EV-xxxx#1",
      "issues": [
        {
          "field": "epsilon.rho.Z",
          "severity": "error",
          "finding": "论文使用3×2 ANOVA无协变量调整，Age和Sex是matching变量而非协变量",
          "current_value": ["Age", "Sex", "TDI"],
          "suggested_fix": [],
          "evidence_in_paper": "Page 3: 'A 3×2 (group × time) ANOVA was conducted'"
        }
      ],
      "verdict": "has_errors"
    }
  ]
}
```

**verdict**: "clean" (无问题) | "has_warnings" (小问题) | "has_errors" (需修复)

## 重要原则

- 只标注你有确切证据的问题，不要猜测
- 如果某个值在论文中找不到但也不能确定是错的，标为 warning 而非 error
- 每个 finding 必须引用论文中的具体位置（如 "Page 3" 或 "Table 2"）
- 如果论文未报告某个信息（如adjustment variables），suggested_fix 应为 null
"""


def build_phase_b_prompt(
    edges: List[Dict],
    pdf_text: str,
    phase_a_flags: List[Dict],
    max_edges_per_call: int = 5,
    max_text_chars: int = 25000,
    error_patterns_context: str = "",  # NEW: from GT error patterns
) -> str:
    """
    Build the Phase B LLM prompt.

    Includes:
      - Historical error patterns from GT cases (if available)
      - Phase A flagged issues (for focused checking)
      - Edge JSONs (stripped of internal metadata)
      - Paper text (truncated)
    """
    # Build error patterns section (from GT reference)
    error_patterns_section = ""
    if error_patterns_context:
        error_patterns_section = (
            "## 历史错误模式参考（从 GT 标注案例中提取）\n\n"
            "以下错误模式在过去的人工审核中高频出现，请重点关注：\n\n"
            f"{error_patterns_context}\n\n"
        )

    # Build Phase A summary for context
    flagged_summary = []
    for iss in phase_a_flags:
        if iss.get("severity") == "error":
            flagged_summary.append(
                f"  - [{iss['check']}] Edge {iss.get('edge_id','?')}: "
                f"{iss['message']}"
            )

    phase_a_section = ""
    if flagged_summary:
        phase_a_section = (
            "## Phase A 已检测到的问题（供参考，请在审核中一并验证）\n\n"
            + "\n".join(flagged_summary[:20])
            + "\n\n"
        )

    # Prepare edge JSONs
    edge_section_parts = []
    for i, edge in enumerate(edges[:max_edges_per_call]):
        # Strip internal keys
        clean_edge = {k: v for k, v in edge.items() if not k.startswith("_")}
        edge_json_str = json.dumps(clean_edge, ensure_ascii=False, indent=2)
        edge_section_parts.append(
            f"### Edge {i+1}: {edge.get('edge_id', '?')}\n\n"
            f"```json\n{edge_json_str}\n```\n"
        )

    edges_section = "\n".join(edge_section_parts)

    # Long-paper handling: instead of slicing the front of the OCR text
    # (which silently drops Tables sitting on later pages of papers like
    # 32895551), pull a Results/Tables-biased excerpt and then keyword-
    # refine using the X/Y/theta_hat of the edges currently being audited.
    if len(pdf_text) > max_text_chars:
        # Lazy import to avoid a cycle on module load.
        from .review import (
            _select_relevant_chunks,
            _spot_check_keywords,
            select_results_and_tables,
        )

        keywords: List[str] = []
        for edge in edges[:max_edges_per_call]:
            theta = edge.get("literature_estimate", {}).get("theta_hat") or 0.0
            keywords.extend(_spot_check_keywords(edge, theta))

        results_pool = select_results_and_tables(
            pdf_text, max_total_chars=max(max_text_chars + 8000, 32000)
        )
        truncated_text = _select_relevant_chunks(
            results_pool, keywords, max_total_chars=max_text_chars
        )
        truncated_text += (
            f"\n\n[... keyword-selected excerpt — "
            f"{len(truncated_text)} chars of {len(pdf_text)} total ...]"
        )
    else:
        truncated_text = pdf_text

    prompt = (
        f"{error_patterns_section}"
        f"{phase_a_section}"
        f"## 待审核的证据卡\n\n{edges_section}\n\n"
        f"## 论文原文\n\n{truncated_text}\n\n"
        f"请按照系统提示中的审核规则，对每个edge逐字段检查。输出JSON格式。"
    )

    return prompt


def parse_phase_b_response(response: Dict) -> List[Dict]:
    """
    Parse the LLM audit response into a list of issues.
    """
    all_issues = []
    audits = response.get("edge_audits", [])

    for audit in audits:
        edge_id = audit.get("edge_id", "?")
        verdict = audit.get("verdict", "unknown")
        issues = audit.get("issues", [])

        for iss in issues:
            all_issues.append(
                {
                    "edge_id": edge_id,
                    "check": f"llm_audit_{iss.get('field', 'unknown')}",
                    "severity": iss.get("severity", "warning"),
                    "field": iss.get("field", ""),
                    "message": iss.get("finding", ""),
                    "current_value": iss.get("current_value"),
                    "suggested_fix": iss.get("suggested_fix"),
                    "evidence": iss.get("evidence_in_paper", ""),
                    "source": "phase_b_llm",
                }
            )

    return all_issues


def _phase_c_autofix(
    edges: List[Dict],
    phase_b_issues: List[Dict],
    aggressive: bool = False,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Apply Phase B's `suggested_fix` for issues that pass a sanity gate.

    Two modes:
      - default (aggressive=False, "fill-only"): only apply the fix when
        the current value is "empty" — None / "" / [] / [None, None].
        Eliminates the risk of overwriting a correct value with the LLM's
        possibly-wrong suggestion. Recommended for production runs.
      - aggressive=True: also apply when current_value is non-empty
        (overwrites). Useful when you trust Phase B's LLM more than the
        existing edge state — but expect ~5–10% of fixes to flip correct
        values to wrong ones. Requires manual eyeballing of the diff.

    Sanity gates (all must hold regardless of mode):
      - severity == "error"  (warnings are too noisy)
      - suggested_fix is present and not "null" / "" / "TBD"
      - type-compatible with the field's expected type
      - magnitude check for numerics (within [0.01, 100×] of current)
      - prose / Chinese / "X or Y" alternatives are rejected as values

    Returns (autofixed_edges, applied_fixes_log).
    """

    def _resolve(path: str, edge: Dict) -> Tuple[Any, Any, str]:
        """
        Walk a dotted path (e.g. 'literature_estimate.n') down into
        edge and return (parent_dict_or_list, leaf_key, current_value).
        Returns (None, None, None) on any miss.
        """
        if not path:
            return None, None, None
        parts = path.split(".")
        node: Any = edge
        for p in parts[:-1]:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                return None, None, None
        leaf = parts[-1]
        if isinstance(node, dict) and leaf in node:
            return node, leaf, node[leaf]
        return None, None, None

    # Field paths that the schema says hold a number (or None). When old=None
    # we can't infer type from old alone; this list keeps prose suggestions
    # from getting stuffed into numeric slots.
    _NUMERIC_FIELDS: Set[str] = {
        "literature_estimate.theta_hat",
        "literature_estimate.p_value",
        "literature_estimate.n",
        "literature_estimate.ci_level",
        "equation_formula_reported.reported_effect_value",
        "equation_formula_reported.reported_p",
        "study_cohort.sample_size.value",
    }
    _LIST_FIELDS: Set[str] = {
        "literature_estimate.ci",
        "literature_estimate.adjustment_set",
        "epsilon.rho.Z",
        "equation_formula_reported.Z",
        "equation_formula_reported.reported_ci",
    }
    # Phrases that betray "this is a description, not a value". The LLM
    # sometimes writes prose into suggested_fix when it doesn't know the
    # answer ("Should extract OR value from Table 2"). We never apply those.
    _PROSE_TOKENS_EN = (
        "should ",
        "extract ",
        "should be",
        "would be",
        "could be",
        "needs ",
        "need to",
        "from table",
        "from figure",
        "see ",
        "refer to",
        "verify",
        "confirm ",
        "clarify ",
        "imputed ",
        "approximate ",
        "approximately",
        "not reported",
        "not provided",
        "if using",
        "if the",
        "if effect",
        "if applicable",
        "if adjusted",
        "if modeling",
        "if no",
        "remove this",
        "reclassify",
        "documented as",
        "either ",
        "rather than",
        "unclear",
        "ambiguous",
        "cannot ",
    )
    # Common Chinese imperative / suggestive markers. Even one of these
    # in suggested_fix means it's a comment to a human, not a value.
    _PROSE_TOKENS_ZH = (
        "应",
        "如",
        "或",
        "建议",
        "推荐",
        "不能",
        "必须",
        "而非",
        "如果",
        "如无法",
        "如使用",
        "请",
        "请填",
        "待定",
        "需要",
        "可能",
    )
    # Markers that the LLM gave alternatives instead of one value.
    _ALTERNATIVE_MARKERS = (
        " or ",
        " OR ",
        " and/or ",
        " 或 ",
        "/或",
        "或者",
    )

    def _looks_like_prose(s: str) -> bool:
        if not isinstance(s, str):
            return False
        s_strip = s.strip()
        if not s_strip:
            return False
        low = s_strip.lower()
        for tok in _PROSE_TOKENS_EN:
            if tok in low:
                return True
        for tok in _PROSE_TOKENS_ZH:
            if tok in s_strip:
                return True
        # "X or Y" / "X 或 Y" — LLM gave alternatives, not a value.
        # Only flag when the alternatives marker is *between* tokens
        # (avoid catching "color" → contains "or" as substring).
        for marker in _ALTERNATIVE_MARKERS:
            if marker in s_strip:
                return True
        # Long strings with several spaces and no obvious numeric structure
        # are almost always descriptive — values are short.
        if len(s_strip) > 60 and s_strip.count(" ") >= 5:
            return True
        # Sentence-ending punctuation outside of common medical units is
        # almost always prose (e.g. "...rather than 'reported'.").
        if s_strip.endswith((".", "。", "?", "？", "!", "！")):
            return True
        return False

    def _looks_like_number(s: str) -> bool:
        try:
            float(s.strip())
            return True
        except (ValueError, TypeError):
            return False

    def _types_compatible(old: Any, new: Any, path: str) -> bool:
        if old is None:
            # We don't know the schema type from old alone — consult the
            # numeric/list field maps before accepting strings.
            if new is None or isinstance(new, dict):
                return False
            if path in _NUMERIC_FIELDS:
                if isinstance(new, (int, float)) and not isinstance(new, bool):
                    return True
                # Allow numeric strings ("4.1") — caller will coerce.
                return isinstance(new, str) and _looks_like_number(new)
            if path in _LIST_FIELDS:
                return isinstance(new, list)
            # Unknown field path: be conservative — accept primitive scalars,
            # but never accept prose-like strings.
            if isinstance(new, str):
                return not _looks_like_prose(new)
            return isinstance(new, (int, float, list, bool))
        if isinstance(old, list):
            return isinstance(new, list)
        if isinstance(old, bool):
            return isinstance(new, bool)
        if isinstance(old, (int, float)):
            return isinstance(new, (int, float)) and not isinstance(new, bool)
        if isinstance(old, str):
            # Old was a string; reject prose-style replacements regardless
            # (they're suggestions about what to do, not values).
            return isinstance(new, str) and not _looks_like_prose(new)
        return type(old) is type(new)

    def _coerce_numeric_string(new: Any, path: str) -> Any:
        """If suggested fix is '4.1' (string) for a numeric field, return float(4.1)."""
        if path in _NUMERIC_FIELDS and isinstance(new, str) and _looks_like_number(new):
            try:
                f = float(new.strip())
                return int(f) if f.is_integer() and "." not in new.strip() else f
            except ValueError:
                return new
        return new

    def _passes_magnitude_check(old: Any, new: Any) -> bool:
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            return True
        if old == 0:
            return abs(new) < 1e6  # arbitrary cap
        ratio = abs(new / old) if old else 1.0
        # Reject changes more extreme than 100× — those are usually wild
        # LLM guesses rather than legitimate corrections.
        return 0.01 <= ratio <= 100.0

    edge_by_id: Dict[str, Dict] = {}
    for e in edges:
        eid = e.get("edge_id")
        if eid:
            edge_by_id[eid] = e

    applied: List[Dict] = []
    for iss in phase_b_issues:
        if iss.get("severity") != "error":
            continue
        eid = iss.get("edge_id")
        path = iss.get("field")
        suggested = iss.get("suggested_fix")
        if not eid or not path or suggested is None:
            continue
        if isinstance(suggested, str) and suggested.strip().lower() in (
            "",
            "null",
            "none",
            "n/a",
            "tbd",
        ):
            continue
        edge = edge_by_id.get(eid)
        if edge is None:
            continue
        parent, leaf, current = _resolve(path, edge)
        if parent is None or leaf is None:
            continue
        if not _types_compatible(current, suggested, path):
            continue
        # Coerce numeric strings ("4.1") into floats before magnitude check.
        suggested = _coerce_numeric_string(suggested, path)
        if not _passes_magnitude_check(current, suggested):
            continue

        # Fill-only safety gate: refuse to overwrite a non-empty current
        # value unless the caller explicitly opted into aggressive mode.
        # An "empty" current is None / "" / [] / [None, None] / [None, None, ...].
        def _is_empty(v: Any) -> bool:
            if v is None:
                return True
            if isinstance(v, str) and v.strip() == "":
                return True
            if isinstance(v, list):
                if len(v) == 0:
                    return True
                if all(x is None for x in v):
                    return True
            return False

        if not aggressive and not _is_empty(current):
            continue

        # All gates passed — apply.
        parent[leaf] = suggested
        applied.append(
            {
                "edge_id": eid,
                "field": path,
                "before": current,
                "after": suggested,
                "check": iss.get("check", "?"),
            }
        )

    return edges, applied


def run_step4_audit(
    edges: List[Dict],
    pdf_text: str,
    client=None,  # GLMClient instance, None to skip Phase B
    max_edges_per_llm_call: int = 5,
    error_patterns_path: str = None,  # NEW: path to error_patterns.json
    enable_phase_c_autofix: bool = False,  # NEW: opt-in Phase C
    phase_c_aggressive: bool = False,  # NEW: allow overwriting existing values
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Run full Step 4 audit.

    Returns:
        (audited_edges, audit_report)
    """
    import sys

    print(f"\n[Step 4] Auditing {len(edges)} edges ...", file=sys.stderr)

    # ── Load error patterns context (if available) ──
    error_patterns_context = ""
    if error_patterns_path:
        try:
            from .gt_loader import build_error_patterns_context, load_error_patterns

            patterns = load_error_patterns(error_patterns_path)
            if patterns:
                error_patterns_context = build_error_patterns_context(patterns)
                print(
                    f"[Step 4] Loaded error patterns: "
                    f"{patterns.get('total_patterns', 0)} patterns from "
                    f"{patterns.get('num_cases', 0)} GT cases",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[Step 4] Failed to load error patterns: {e}", file=sys.stderr)

    # ── Phase A ──
    print("[Step 4] Phase A: Deterministic checks ...", file=sys.stderr)
    _, phase_a_report = phase_a_audit(edges, pdf_text)

    phase_a_issues = phase_a_report["issues"]
    print(
        f"  Found {len(phase_a_issues)} issues " f"({phase_a_report['by_severity']})",
        file=sys.stderr,
    )

    # Apply auto-fixes
    fixed_edges, applied_fixes = apply_phase_a_fixes(edges, phase_a_issues)
    print(
        f"  Applied {len(applied_fixes)} automatic fixes",
        file=sys.stderr,
    )

    # ── Phase B (LLM) ──
    phase_b_issues: List[Dict] = []
    if client is not None:
        print("[Step 4] Phase B: LLM content audit ...", file=sys.stderr)
        for batch_start in range(0, len(fixed_edges), max_edges_per_llm_call):
            batch_end = min(batch_start + max_edges_per_llm_call, len(fixed_edges))
            batch = fixed_edges[batch_start:batch_end]
            batch_flags = [
                iss
                for iss in phase_a_issues
                if batch_start <= iss.get("edge_index", -1) < batch_end
            ]

            prompt = build_phase_b_prompt(
                batch,
                pdf_text,
                batch_flags,
                max_edges_per_call=max_edges_per_llm_call,
                error_patterns_context=error_patterns_context,  # NEW
            )

            try:
                result = client.call_json(
                    prompt,
                    system_prompt=_PHASE_B_SYSTEM_PROMPT,
                    max_tokens=16384,  # enough
                )
                batch_issues = parse_phase_b_response(result)
                phase_b_issues.extend(batch_issues)
                print(
                    f"  Batch {batch_start//max_edges_per_llm_call + 1}: "
                    f"{len(batch_issues)} issues found",
                    file=sys.stderr,
                )
            except Exception as e:
                print(
                    f"  Batch {batch_start//max_edges_per_llm_call + 1}: "
                    f"Phase B LLM call failed: {e}",
                    file=sys.stderr,
                )

    else:
        print("[Step 4] Phase B: Skipped (no LLM client)", file=sys.stderr)

    # ── Phase C: opt-in deterministic autofix using Phase B suggested_fix ──
    phase_c_applied: List[Dict] = []
    if enable_phase_c_autofix and phase_b_issues:
        mode_label = "aggressive (overwrite OK)" if phase_c_aggressive else "fill-only"
        print(
            f"[Step 4] Phase C: applying Phase B suggested_fix " f"({mode_label}) ...",
            file=sys.stderr,
        )
        fixed_edges, phase_c_applied = _phase_c_autofix(
            fixed_edges, phase_b_issues, aggressive=phase_c_aggressive
        )
        print(
            f"  Applied {len(phase_c_applied)} Phase C autofixes "
            f"(out of {sum(1 for i in phase_b_issues if i.get('severity')=='error')} "
            f"error-severity Phase B issues)",
            file=sys.stderr,
        )
    elif phase_b_issues:
        print(
            "[Step 4] Phase C: skipped (enable_phase_c_autofix=False)",
            file=sys.stderr,
        )

    # ── Compile report ──
    all_issues = phase_a_issues + phase_b_issues

    audit_report = {
        "step": 4,
        "total_edges": len(edges),
        "phase_a": phase_a_report,
        "phase_a_fixes_applied": applied_fixes,
        "phase_b_issues": phase_b_issues,
        "phase_c_fixes_applied": phase_c_applied,
        "total_issues": len(all_issues),
        "summary": {
            "phase_a_issues": len(phase_a_issues),
            "phase_a_fixes": len(applied_fixes),
            "phase_b_issues": len(phase_b_issues),
            "phase_c_fixes": len(phase_c_applied),
            "edges_with_errors": len(
                set(
                    iss["edge_id"]
                    for iss in all_issues
                    if iss.get("severity") == "error"
                )
            ),
        },
    }

    print(
        f"[Step 4] Complete: {audit_report['summary']}",
        file=sys.stderr,
    )

    return fixed_edges, audit_report
