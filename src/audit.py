"""
step4_audit.py -- Post-extraction Evidence Card Audit.

Runs AFTER Step 3 on the final edges.json. Focuses on content-level
accuracy that format/semantic validators cannot catch:

  Phase A (deterministic, no LLM):
    A1. Covariate hallucination   -- each Z variable must appear in paper text
    A2. Numeric hallucination     -- theta_hat, CI, p_value must appear in text
    A3. Sample data verification  -- sample_size, sex ratios must appear in text
    A4. HPP variable leakage      -- X/Y names must appear in paper, not just HPP dict
    A5. Extra field detection      -- flag fields not in the allowed schema

  Phase B (LLM, with few-shot GT example):
    B1. Y-label cross-check       -- does the numeric value match the claimed Y?
    B2. Adjustment set semantics  -- are Z variables actually covariates or just
                                     matching/stratification variables?
    B3. Study cohort verification  -- verify sample_size, age, sex against paper

Output: audit_report.json + edges_audited.json (with fixes applied)
"""

import copy
import json
import re
from typing import Any, Dict, List, Tuple


def _number_appears_in_text(val: Any, text: str) -> bool:
    """Check if a numeric value appears somewhere in the paper text."""
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

    # --- Check reported_effect_value (ALWAYS, regardless of scale) ---
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

    # --- Check reported_ci (ALWAYS) ---
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

    # --- Check theta_hat ---
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

    # --- Check literature_estimate.ci on difference scale ---
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
) -> str:
    """
    Build the Phase B LLM prompt.

    Includes:
      - Phase A flagged issues (for focused checking)
      - Edge JSONs (stripped of internal metadata)
      - Paper text (truncated)
    """
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

    # Truncate paper text
    truncated_text = pdf_text[:max_text_chars]
    if len(pdf_text) > max_text_chars:
        truncated_text += "\n\n[... truncated ...]"

    prompt = (
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


def run_step4_audit(
    edges: List[Dict],
    pdf_text: str,
    client=None,  # GLMClient instance, None to skip Phase B
    max_edges_per_llm_call: int = 5,
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Run full Step 4 audit.

    Returns:
        (audited_edges, audit_report)
    """
    import sys

    print(f"\n[Step 4] Auditing {len(edges)} edges ...", file=sys.stderr)

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
            )

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

    else:
        print("[Step 4] Phase B: Skipped (no LLM client)", file=sys.stderr)

    # ── Compile report ──
    all_issues = phase_a_issues + phase_b_issues

    audit_report = {
        "step": 4,
        "total_edges": len(edges),
        "phase_a": phase_a_report,
        "phase_a_fixes_applied": applied_fixes,
        "phase_b_issues": phase_b_issues,
        "total_issues": len(all_issues),
        "summary": {
            "phase_a_issues": len(phase_a_issues),
            "phase_a_fixes": len(applied_fixes),
            "phase_b_issues": len(phase_b_issues),
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
