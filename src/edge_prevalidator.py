"""
edge_prevalidator.py -- Pre-validation of extracted edges BEFORE template filling.

Runs between Step 1 (edge enumeration) and Step 2 (template filling).
Catches formula/model/equation_type errors early so Step 2 only does
template filling without expensive retry loops.

Two validation phases:
  Phase A (hard_check): Verify reported numeric values exist in paper text.
  Phase B (soft_check): Deterministically derive equation_type, model, formula
                        from edge metadata and validate internal consistency.

If Phase B detects issues, it auto-corrects the edge metadata so Step 2
receives clean, pre-validated inputs.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

EFFECT_SCALE_TO_EQUATION_TYPE: Dict[str, str] = {
    "HR": "E2",
    "OR": "E1",
    "RR": "E1",
    "MD": "E1",
    "beta": "E1",
    "BETA": "E1",
    "SMD": "E1",
    "RD": "E1",
    "IRR": "E1",
}

EQUATION_TYPE_TO_MODEL: Dict[str, str] = {
    "E1_OR": "logistic",
    "E1_RR": "logistic",
    "E1_beta": "linear",
    "E1_BETA": "linear",
    "E1_MD": "linear",
    "E1_SMD": "linear",
    "E1_RD": "linear",
    "E1_IRR": "poisson",
    "E2_HR": "Cox",
    "E2_RR": "Cox",
}

EFFECT_SCALE_TO_MU: Dict[str, Dict[str, str]] = {
    "HR": {"family": "ratio", "type": "HR", "scale": "log"},
    "OR": {"family": "ratio", "type": "OR", "scale": "log"},
    "RR": {"family": "ratio", "type": "RR", "scale": "log"},
    "MD": {"family": "difference", "type": "MD", "scale": "identity"},
    "beta": {"family": "difference", "type": "BETA", "scale": "identity"},
    "BETA": {"family": "difference", "type": "BETA", "scale": "identity"},
    "SMD": {"family": "difference", "type": "SMD", "scale": "identity"},
    "RD": {"family": "difference", "type": "RD", "scale": "identity"},
    "IRR": {"family": "ratio", "type": "RR", "scale": "log"},
}

RATIO_SCALES = {"HR", "OR", "RR", "IRR"}

# Mediation keywords (force E4)
MEDIATION_KEYWORDS = {
    "mediation",
    "indirect effect",
    "direct effect",
    "acme",
    "ade",
    "mediator",
    "mediating",
    "path analysis",
    "structural equation",
}

# Interaction keywords (force E6)
INTERACTION_KEYWORDS = {
    "interaction",
    "modifier",
    "factorial",
    "joint effect",
    "effect modification",
    "multiplicative",
    "additive interaction",
}

# Longitudinal keywords (force E3)
LONGITUDINAL_KEYWORDS = {
    "mixed model",
    "linear mixed",
    "lmm",
    "gee",
    "repeated measure",
    "longitudinal",
    "random effect",
    "random intercept",
    "random slope",
}


def _normalize_number(val: Any) -> Optional[str]:
    """Convert a number to its string representation for text search."""
    if val is None:
        return None
    try:
        num = float(val)
    except (ValueError, TypeError):
        return None
    # Try multiple formats: 0.84, .84, 0.840
    candidates = []
    if num == int(num) and abs(num) < 10000:
        candidates.append(str(int(num)))
    candidates.append(f"{num:.2f}")
    candidates.append(f"{num:.3f}")
    if 0 < abs(num) < 1:
        candidates.append(f"{num:.2f}".lstrip("0"))
    return candidates[0] if candidates else None


def _number_appears_in_text(val: Any, text: str) -> bool:
    """Check if a numeric value appears somewhere in the paper text."""
    if val is None:
        return True  # null values don't need verification
    try:
        num = float(val)
    except (ValueError, TypeError):
        # Handle string p-values like "<0.001"
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


def hard_check_edge(edge: Dict, pdf_text: str) -> Dict[str, Any]:
    """
    Verify that reported numeric values from an edge actually appear
    in the paper text. Returns a validation result dict.

    Checks: estimate, CI bounds, p_value.
    """
    results = {
        "edge_index": edge.get("edge_index", 0),
        "checks": [],
        "passed": True,
        "missing_values": [],
    }

    estimate = edge.get("estimate")
    ci = edge.get("ci", [None, None])
    p_value = edge.get("p_value")

    # Check estimate
    if estimate is not None:
        found = _number_appears_in_text(estimate, pdf_text)
        results["checks"].append(
            {
                "field": "estimate",
                "value": estimate,
                "found_in_text": found,
            }
        )
        if not found:
            results["missing_values"].append(f"estimate={estimate}")

    # Check CI bounds
    if isinstance(ci, list) and len(ci) == 2:
        for i, bound in enumerate(ci):
            if bound is not None:
                label = "ci_lower" if i == 0 else "ci_upper"
                found = _number_appears_in_text(bound, pdf_text)
                results["checks"].append(
                    {
                        "field": label,
                        "value": bound,
                        "found_in_text": found,
                    }
                )
                if not found:
                    results["missing_values"].append(f"{label}={bound}")

    # Check p_value
    if p_value is not None:
        found = _number_appears_in_text(p_value, pdf_text)
        results["checks"].append(
            {
                "field": "p_value",
                "value": p_value,
                "found_in_text": found,
            }
        )
        if not found:
            results["missing_values"].append(f"p_value={p_value}")

    if estimate is not None and not _number_appears_in_text(estimate, pdf_text):
        results["passed"] = False

    return results


def _detect_special_equation_type(edge: Dict, evidence_type: str) -> Optional[str]:
    """
    Detect if edge requires a special equation_type (E3/E4/E6) based
    on keywords in the edge notes, X, Y, or subgroup fields.
    """
    searchable = " ".join(
        [
            str(edge.get("X", "")),
            str(edge.get("Y", "")),
            str(edge.get("notes", "")),
            str(edge.get("subgroup", "")),
        ]
    ).lower()

    # E4: mediation
    if any(kw in searchable for kw in MEDIATION_KEYWORDS):
        return "E4"

    # E6: interaction
    x_str = str(edge.get("X", "")).lower()
    if x_str.startswith("interaction:") or x_str.startswith("interaction "):
        return "E6"
    if any(kw in searchable for kw in INTERACTION_KEYWORDS):
        return "E6"

    # E3: longitudinal
    if any(kw in searchable for kw in LONGITUDINAL_KEYWORDS):
        return "E3"

    return None


def derive_equation_metadata(
    edge: Dict,
    evidence_type: str,
) -> Dict[str, Any]:
    """
    Deterministically derive equation_type, model, mu, and formula skeleton
    from edge metadata (effect_scale, outcome_type).

    Returns a dict with:
      - equation_type: E1-E6
      - model: statistical model name
      - mu: {family, type, scale}
      - formula_skeleton: basic formula template
      - id_strategy: from evidence_type
      - issues: list of derivation warnings
    """
    effect_scale = str(edge.get("effect_scale", "")).strip()
    outcome_type = str(edge.get("outcome_type", "")).strip()
    stat_method = str(edge.get("statistical_method", "")).strip().lower()
    issues = []

    # Step 0: If Step 1 provided statistical_method, use it as primary signal
    method_to_eq: Dict[str, Tuple[str, str]] = {
        "cox": ("E2", "Cox"),
        "logistic": ("E1", "logistic"),
        "linear": ("E1", "linear"),
        "poisson": ("E1", "poisson"),
        "lmm": ("E3", "LMM"),
        "gee": ("E3", "GEE"),
        "t-test": ("E1", "linear"),
        "ancova": ("E1", "ANCOVA"),
        "mr_ivw": ("E1", "IVW"),
        "km": ("E2", "KM"),
        "mediation": ("E4", "mediation"),
    }

    # Step 1: Check for special equation types (mediation/interaction/longitudinal)
    special_eq = _detect_special_equation_type(edge, evidence_type)

    # Step 2: Derive equation_type — priority: special > stat_method > effect_scale > outcome_type
    if special_eq:
        eq_type = special_eq
    elif stat_method in method_to_eq:
        eq_type = method_to_eq[stat_method][0]
    elif effect_scale in EFFECT_SCALE_TO_EQUATION_TYPE:
        eq_type = EFFECT_SCALE_TO_EQUATION_TYPE[effect_scale]
    elif outcome_type == "survival":
        eq_type = "E2"
        if not effect_scale:
            effect_scale = "HR"
    elif outcome_type == "binary":
        eq_type = "E1"
        if not effect_scale:
            effect_scale = "OR"
    elif outcome_type == "continuous":
        eq_type = "E1"
        if not effect_scale:
            effect_scale = "beta"
    else:
        eq_type = "E1"
        issues.append(
            {
                "check": "derive_eq_type_fallback",
                "severity": "warning",
                "message": (
                    f"Could not derive equation_type from statistical_method='{stat_method}', "
                    f"effect_scale='{effect_scale}', outcome_type='{outcome_type}'. Defaulting to E1."
                ),
            }
        )

    # Step 3: Derive model — priority: stat_method > equation_type + effect_scale
    if stat_method in method_to_eq:
        model = method_to_eq[stat_method][1]
    elif eq_type == "E2":
        model = "Cox"
    elif eq_type == "E3":
        model = "LMM"
    elif eq_type == "E4":
        model = "mediation"
    elif eq_type == "E6":
        model = "interaction_model"
    else:
        model_key = f"{eq_type}_{effect_scale}"
        if model_key in EQUATION_TYPE_TO_MODEL:
            model = EQUATION_TYPE_TO_MODEL[model_key]
        elif outcome_type == "binary":
            model = "logistic"
        elif outcome_type == "continuous":
            model = "linear"
        else:
            model = "linear"
            issues.append(
                {
                    "check": "derive_model_fallback",
                    "severity": "warning",
                    "message": (
                        f"Could not derive model for {eq_type}/{effect_scale}. "
                        f"Defaulting to 'linear'."
                    ),
                }
            )

    # Step 4: Derive mu
    if effect_scale in EFFECT_SCALE_TO_MU:
        mu = EFFECT_SCALE_TO_MU[effect_scale].copy()
    elif eq_type == "E2":
        mu = {"family": "ratio", "type": "HR", "scale": "log"}
    else:
        mu = {"family": "difference", "type": "BETA", "scale": "identity"}
        issues.append(
            {
                "check": "derive_mu_fallback",
                "severity": "warning",
                "message": (
                    f"Could not derive mu from effect_scale='{effect_scale}'. "
                    f"Defaulting to difference/BETA/identity."
                ),
            }
        )

    # Step 5: Derive id_strategy
    if evidence_type == "interventional":
        id_strategy = "rct"
    elif evidence_type == "causal":
        id_strategy = "observational"
    else:
        id_strategy = "observational"

    # Step 6: Build formula skeleton
    x_name = str(edge.get("X", "X")).replace(" ", "_")
    y_name = str(edge.get("Y", "Y")).replace(" ", "_")
    formula = _build_formula_skeleton(eq_type, model, x_name, y_name, effect_scale)

    return {
        "equation_type": eq_type,
        "model": model,
        "mu": mu,
        "formula_skeleton": formula,
        "id_strategy": id_strategy,
        "issues": issues,
    }


def _build_formula_skeleton(
    eq_type: str, model: str, x_name: str, y_name: str, effect_scale: str
) -> str:
    """Build a formula skeleton string based on equation type and model."""
    if eq_type == "E2":
        return f"lambda(t|do({x_name}),Z) = lambda_0(t) * exp(beta_{x_name} * {x_name} + gamma^T * Z)"
    elif eq_type == "E1" and model == "logistic":
        return f"logit(P({y_name}=1)) = alpha + beta * {x_name} + gamma^T * Z"
    elif eq_type == "E1" and model == "linear":
        return f"E[{y_name} | do({x_name}), Z] = alpha + beta * {x_name} + gamma^T * Z"
    elif eq_type == "E1" and model == "poisson":
        return f"log(E[{y_name}]) = alpha + beta * {x_name} + gamma^T * Z"
    elif eq_type == "E3":
        return f"{y_name}_it = (alpha + u_0i) + (beta_0 + u_1i)*t + beta_{x_name}*{x_name} + gamma^T*Z + epsilon_it"
    elif eq_type == "E4":
        return f"M = f_M({x_name}, Z_M, eps_M); {y_name} = f_Y({x_name}, M, Z_Y, eps_Y)"
    elif eq_type == "E6":
        return f"eta({x_name}, X2, Z) = alpha + beta_1*{x_name} + beta_2*X2 + beta_12*{x_name}*X2 + gamma^T*Z"
    else:
        return f"E[{y_name}] = alpha + beta * {x_name} + gamma^T * Z"


def soft_check_edge(
    edge: Dict,
    evidence_type: str,
) -> Dict[str, Any]:
    """
    Derive expected equation metadata from edge and validate consistency.
    Returns derived metadata + any inconsistency issues.

    This is a pure deterministic check -- no LLM needed.
    """
    derived = derive_equation_metadata(edge, evidence_type)

    # Cross-validate: if edge already has notes about the model
    notes = str(edge.get("notes", "")).lower()

    # Additional heuristics
    if "cox" in notes and derived["equation_type"] != "E2":
        derived["equation_type"] = "E2"
        derived["model"] = "Cox"
        derived["mu"] = {"family": "ratio", "type": "HR", "scale": "log"}
        derived["issues"].append(
            {
                "check": "notes_override_to_cox",
                "severity": "info",
                "message": "Edge notes mention 'Cox', overriding to E2/Cox.",
            }
        )

    if "logistic" in notes and derived["equation_type"] not in ("E4", "E6"):
        derived["model"] = "logistic"

    return derived


# ---------------------------------------------------------------------------
# Pre-compute theta_hat and CI on correct scale
# ---------------------------------------------------------------------------


def precompute_theta(edge: Dict, mu: Dict) -> Dict[str, Any]:
    """
    Pre-compute theta_hat and CI on the correct scale based on mu config.
    For ratio measures, converts to log scale.
    For difference measures, keeps as-is.

    Returns: {theta_hat, ci, reported_value, reported_ci}
    """
    estimate = edge.get("estimate")
    ci = edge.get("ci", [None, None])
    effect_scale = str(edge.get("effect_scale", "")).strip()
    result = {
        "theta_hat": None,
        "ci": [None, None],
        "reported_value": None,
        "reported_ci": None,
    }

    if estimate is None:
        return result

    try:
        est_float = float(estimate)
    except (ValueError, TypeError):
        return result

    is_ratio = effect_scale in RATIO_SCALES or mu.get("family") == "ratio"

    if is_ratio:
        result["reported_value"] = est_float
        if est_float > 0:
            result["theta_hat"] = round(math.log(est_float), 4)
        # Convert CI
        if isinstance(ci, list) and len(ci) == 2:
            result["reported_ci"] = ci[:]
            log_ci = [None, None]
            for i, bound in enumerate(ci):
                if bound is not None:
                    try:
                        b = float(bound)
                        if b > 0:
                            log_ci[i] = round(math.log(b), 4)
                    except (ValueError, TypeError):
                        pass
            result["ci"] = log_ci
    else:
        result["theta_hat"] = est_float
        if isinstance(ci, list) and len(ci) == 2:
            parsed_ci = [None, None]
            for i, bound in enumerate(ci):
                if bound is not None:
                    try:
                        parsed_ci[i] = float(bound)
                    except (ValueError, TypeError):
                        pass
            result["ci"] = parsed_ci

    return result


# ---------------------------------------------------------------------------
# Main entry point: prevalidate all edges
# ---------------------------------------------------------------------------


def prevalidate_edges(
    edges: List[Dict],
    pdf_text: str,
    evidence_type: str,
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Run Phase A (hard check) and Phase B (soft check) on all edges.
    Enriches each edge with pre-derived metadata for Step 2.

    Returns:
        (enriched_edges, validation_report)

    Each enriched edge gets a new key '_prevalidation' with:
      - equation_type, model, mu, formula_skeleton
      - theta_hat (on correct scale), ci (on correct scale)
      - reported_value, reported_ci (original scale for ratio measures)
      - hard_check results
      - soft_check issues
    """
    enriched = []
    report = {
        "total_edges": len(edges),
        "hard_check_passed": 0,
        "hard_check_failed": 0,
        "hard_check_missing_values": [],
        "soft_check_issues": [],
        "equation_type_distribution": {},
    }

    for edge in edges:
        idx = edge.get("edge_index", 0)

        # Phase A: Hard check
        hard_result = hard_check_edge(edge, pdf_text)
        if hard_result["passed"]:
            report["hard_check_passed"] += 1
        else:
            report["hard_check_failed"] += 1
            report["hard_check_missing_values"].append(
                {
                    "edge_index": idx,
                    "missing": hard_result["missing_values"],
                }
            )

        # Phase B: Soft check
        soft_result = soft_check_edge(edge, evidence_type)
        if soft_result["issues"]:
            for iss in soft_result["issues"]:
                iss["edge_index"] = idx
            report["soft_check_issues"].extend(soft_result["issues"])

        # Pre-compute theta on correct scale
        theta_result = precompute_theta(edge, soft_result["mu"])

        # Track equation_type distribution
        eq = soft_result["equation_type"]
        report["equation_type_distribution"][eq] = (
            report["equation_type_distribution"].get(eq, 0) + 1
        )

        # Enrich edge with pre-validation data
        edge["_prevalidation"] = {
            "equation_type": soft_result["equation_type"],
            "model": soft_result["model"],
            "mu": soft_result["mu"],
            "formula_skeleton": soft_result["formula_skeleton"],
            "id_strategy": soft_result["id_strategy"],
            "theta_hat": theta_result["theta_hat"],
            "ci": theta_result["ci"],
            "reported_value": theta_result["reported_value"],
            "reported_ci": theta_result["reported_ci"],
            "hard_check": hard_result,
            "soft_issues": soft_result["issues"],
            "adjustment_variables": edge.get("adjustment_variables", []),
        }

        enriched.append(edge)

    return enriched, report
