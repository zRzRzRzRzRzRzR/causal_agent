"""
review.py -- Post-extraction review, rerank, and quality assessment.

Step 3 runs AFTER Step 2 produces all filled edges. It operates on the
complete set of edges for one paper and performs:

  3a. HPP Mapping Rerank   -- LLM re-evaluates top HPP candidates per edge
  3b. Cross-Edge Consistency -- detect duplicates, contradictions, scale errors
  3b+. Fuzzy Duplicate Detection -- token-overlap-based near-duplicate detection
  3c. Content Spot-Check    -- LLM verifies key numeric values against paper
  3d. Quality Report        -- aggregate stats, per-edge scores, actionable flags

Changes from original:
  - generate_quality_report now includes per-edge semantic validation results
  - _generate_action_items flags semantic errors alongside format errors
"""

import json
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple

from .hpp_mapper import HPPMapper
from .llm_client import GLMClient
from .template_utils import compute_fill_rate, validate_filled_edge


def rerank_hpp_mapping(
    edge: Dict,
    mapper: HPPMapper,
    client: GLMClient,
    roles: Tuple[str, ...] = ("X", "Y"),
) -> Dict[str, Any]:
    """
    For each role (X, Y), ask the LLM to pick the best HPP field from
    the top-6 RAG candidates.  Updates edge['hpp_mapping'] in place.

    Returns a dict of changes: {role: {before, after, status, reason}}
    """
    changes: Dict[str, Any] = {}
    queries = _extract_role_queries(edge)
    hm = edge.get("hpp_mapping", {})

    for role in roles:
        query = queries.get(role)
        if not query:
            continue

        candidates = mapper.index.search(query, top_k=8)
        if not candidates:
            continue

        candidate_lines = []
        for i, c in enumerate(candidates[:6]):
            ds = c.dataset_id  # Keep original hyphen format
            candidate_lines.append(f"{i + 1}. {ds} / {c.field_name}")

        current = hm.get(role, {})
        if not isinstance(current, dict):
            current = {}
        current_ds = current.get("dataset", "N/A")
        current_field = current.get("field", "N/A")

        prompt = (
            f'Paper variable: "{query}" (role: {role})\n'
            f"Current mapping: {current_ds} / {current_field}\n\n"
            f"Candidate HPP fields from data dictionary:\n"
            + "\n".join(candidate_lines)
            + "\n\n"
            f"Reply in JSON:\n"
            f'{{"best": index(1-{len(candidates[:6])}), '
            f'"status": "exact|close|tentative|missing", '
            f'"reason": "brief reason"}}\n'
            f"If current mapping is already best, set best=0."
        )

        result = client.call_json(prompt, max_tokens=32678)
        best_idx = result.get("best", 0)
        reason = result.get("reason", "")
        new_status = result.get("status", current.get("status", "tentative"))

        if 0 < best_idx <= len(candidates[:6]):
            chosen = candidates[best_idx - 1]
            new_ds = chosen.dataset_id  # Keep original hyphen format
            new_field = chosen.field_name

            if new_ds != current_ds or new_field != current_field:
                changes[role] = {
                    "before": f"{current_ds}/{current_field}",
                    "after": f"{new_ds}/{new_field}",
                    "status": new_status,
                    "reason": reason,
                }
                hm[role] = {
                    "dataset": new_ds,
                    "field": new_field,
                    "status": new_status,
                }
        elif best_idx == 0 and new_status != current.get("status"):
            hm.setdefault(role, {})["status"] = new_status
            changes[role] = {
                "before_status": current.get("status"),
                "after_status": new_status,
                "reason": f"status updated: {reason}",
            }

    return changes


def _extract_role_queries(edge: Dict) -> Dict[str, str]:
    """Extract variable names for each role from a filled edge."""
    queries: Dict[str, str] = {}
    rho = edge.get("epsilon", {}).get("rho", {})
    iota = edge.get("epsilon", {}).get("iota", {}).get("core", {})
    o = edge.get("epsilon", {}).get("o", {})

    x_val = rho.get("X") or iota.get("name") or ""
    if x_val:
        queries["X"] = str(x_val)

    y_val = rho.get("Y") or o.get("name") or ""
    if y_val:
        queries["Y"] = str(y_val)

    return queries


def check_cross_edge_consistency(edges: List[Dict]) -> List[Dict]:
    """
    Check for issues across the full set of edges from one paper.
    Returns a list of issue dicts.
    """
    issues: List[Dict] = []
    if not edges:
        return issues

    # -- Exact duplicate detection (original) --
    edge_sigs: List[Tuple[int, Tuple[str, str, str]]] = []
    for i, e in enumerate(edges):
        rho = e.get("epsilon", {}).get("rho", {})
        x = str(rho.get("X", "")).lower().strip()
        y = str(rho.get("Y", "")).lower().strip()
        sub = str(e.get("literature_estimate", {}).get("subgroup", "")).lower().strip()
        edge_sigs.append((i, (x, y, sub)))

    sig_counter = Counter(sig for _, sig in edge_sigs)
    for sig, count in sig_counter.items():
        if count > 1:
            dup_idx = [i for i, s in edge_sigs if s == sig]
            issues.append(
                {
                    "type": "duplicate_edge",
                    "severity": "warning",
                    "message": (
                        f"Possible duplicate: X='{sig[0]}', Y='{sig[1]}' "
                        f"appears {count} times"
                    ),
                    "edge_indices": dup_idx,
                }
            )

    # -- Metadata consistency --
    titles: Set[str] = set()
    for e in edges:
        t = e.get("paper_title", "")
        if t:
            titles.add(t)
    if len(titles) > 1:
        issues.append(
            {
                "type": "metadata_inconsistency",
                "severity": "error",
                "message": f"Multiple paper_titles: {titles}",
            }
        )

    # -- Model <-> equation_type consistency across edges --
    model_eq: Dict[str, Set[str]] = defaultdict(set)
    for e in edges:
        m = e.get("literature_estimate", {}).get("model", "")
        eq = e.get("equation_type", "")
        if m and eq:
            model_eq[m].add(eq)
    for model, eqs in model_eq.items():
        if len(eqs) > 1:
            issues.append(
                {
                    "type": "equation_type_inconsistency",
                    "severity": "warning",
                    "message": (
                        f"Model '{model}' maps to multiple " f"equation_types: {eqs}"
                    ),
                }
            )

    # -- Adjustment set variation --
    adj_sets: List[frozenset] = []
    for e in edges:
        adj = e.get("literature_estimate", {}).get("adjustment_set", [])
        adj_sets.append(frozenset(str(a).lower() for a in adj))
    unique_adj = set(adj_sets)
    if len(unique_adj) > 1 and len(edges) > 2:
        issues.append(
            {
                "type": "adjustment_set_variation",
                "severity": "info",
                "message": (
                    f"{len(unique_adj)} different adjustment sets "
                    f"across {len(edges)} edges."
                ),
            }
        )

    # -- Theta scale and sign checks --
    for i, e in enumerate(edges):
        mu = e.get("epsilon", {}).get("mu", {}).get("core", {})
        lit = e.get("literature_estimate", {})
        theta = lit.get("theta_hat")

        if mu.get("family") != "ratio" or mu.get("scale") != "log":
            continue
        if theta is None or not isinstance(theta, (int, float)):
            continue

        # |theta| > 3 on log scale is suspicious
        if abs(theta) > 3:
            issues.append(
                {
                    "type": "theta_scale_suspect",
                    "severity": "error",
                    "message": (
                        f"Edge #{i + 1}: theta_hat={theta} on log scale "
                        f"too large. May have forgotten log transform."
                    ),
                    "edge_indices": [i],
                }
            )

    return issues


def spot_check_values(
    edges: List[Dict],
    pdf_text: str,
    client: GLMClient,
    sample_size: int = 5,
) -> List[Dict]:
    """
    Ask LLM to verify a sample of extracted numeric values against the paper.
    """
    checkable: List[Tuple[int, Dict, float]] = []
    for i, e in enumerate(edges):
        lit = e.get("literature_estimate", {})
        # Use theta_hat for spot checking since reported_HR etc are no longer stored
        theta = lit.get("theta_hat")
        if theta is not None and isinstance(theta, (int, float)):
            checkable.append((i, e, theta))

    to_check = checkable[:sample_size]
    if not to_check:
        return [{"status": "skipped", "reason": "No reported ratios to check"}]

    check_items: List[str] = []
    for idx, (i, e, theta_val) in enumerate(to_check):
        rho = e.get("epsilon", {}).get("rho", {})
        lit = e.get("literature_estimate", {})
        mu_type = e.get("epsilon", {}).get("mu", {}).get("core", {}).get("type", "")
        # Convert theta back to ratio scale for human-readable check
        import math as _math

        if mu_type.startswith("log") and theta_val is not None:
            try:
                display_val = round(_math.exp(theta_val), 2)
                effect_label = mu_type.replace("log", "")
            except (OverflowError, ValueError):
                display_val = theta_val
                effect_label = mu_type
        else:
            display_val = theta_val
            effect_label = mu_type

        check_items.append(
            f"{idx + 1}. {rho.get('X', '?')} -> {rho.get('Y', '?')}\n"
            f"   Extracted: {effect_label}={display_val}, theta_hat(log)={theta_val}\n"
        )

    prompt = (
        "Verify each extracted result against the paper content below.\n"
        "For each item reply: correct / incorrect (give correct value) / not_found\n\n"
        + "".join(check_items)
        + "\nReply in JSON:\n"
        '{"checks": [{"item": index, "verdict": "correct/incorrect/not_found", '
        '"correct_value": null_or_correct_value, "note": ""}]}\n\n'
        f"--- Paper content ---\n{pdf_text[:30000]}"
    )

    try:
        result = client.call_json(prompt, max_tokens=2048)
    except Exception:
        # Retry with explicit JSON instruction
        try:
            raw = client.call(
                "Reply ONLY with valid JSON.\n\n" + prompt,
                system_prompt="Output valid JSON only.",
                max_tokens=2048,
            )
            import re as _re

            # Try to extract JSON from response
            raw = raw.strip()
            if raw.startswith("```"):
                match = _re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, _re.DOTALL)
                if match:
                    raw = match.group(1)
            result = json.loads(raw)
        except Exception:
            return [{"status": "error", "reason": "LLM returned invalid JSON"}]

    checks = result.get("checks", [])
    for check in checks:
        item_idx = check.get("item", 0) - 1
        if 0 <= item_idx < len(to_check):
            check["edge_index"] = to_check[item_idx][0]
            check["edge_id"] = to_check[item_idx][1].get("edge_id", "?")
    return checks


def generate_quality_report(
    edges: List[Dict],
    consistency_issues: List[Dict],
    spot_checks: List[Dict],
    rerank_changes: List[Dict],
) -> Dict:
    """
    Aggregate all Step 3 results into a quality report.
    Now also includes per-edge semantic validation results
    from the _validation metadata attached during Step 2.
    """
    edge_reports: List[Dict] = []
    total_valid = 0
    total_fill = 0.0
    total_semantic_pass = 0
    all_issues: List[str] = []

    for i, e in enumerate(edges):
        is_valid, issues = validate_filled_edge(e)
        fill_rate = compute_fill_rate(e)
        total_fill += fill_rate
        if is_valid:
            total_valid += 1

        hm = e.get("hpp_mapping", {})
        mapping_statuses: Dict[str, str] = {}
        for role in ("X", "Y", "M", "X2"):
            m = hm.get(role)
            if m and isinstance(m, dict):
                mapping_statuses[role] = m.get("status", "unknown")

        rho = e.get("epsilon", {}).get("rho", {})

        # Extract semantic validation results from _validation metadata
        validation_meta = e.get("_validation", {})
        semantic_issues = validation_meta.get("semantic_issues", [])
        is_sem_valid = validation_meta.get("is_semantically_valid", True)
        retries_used = validation_meta.get("retries_used", 0)
        if is_sem_valid:
            total_semantic_pass += 1

        semantic_error_checks = [
            iss["check"] for iss in semantic_issues if iss.get("severity") == "error"
        ]
        semantic_warning_checks = [
            iss["check"] for iss in semantic_issues if iss.get("severity") == "warning"
        ]

        edge_reports.append(
            {
                "edge_index": i + 1,
                "edge_id": e.get("edge_id", "?"),
                "X": rho.get("X", "?"),
                "Y": rho.get("Y", "?"),
                "equation_type": e.get("equation_type", "?"),
                "is_valid": is_valid,
                "is_semantically_valid": is_sem_valid,
                "fill_rate": round(fill_rate, 3),
                "issues": issues,
                "semantic_errors": semantic_error_checks,
                "semantic_warnings": semantic_warning_checks,
                "retries_used": retries_used,
                "mapping_statuses": mapping_statuses,
            }
        )
        all_issues.extend(issues)

    error_count = sum(1 for x in all_issues if not x.startswith("WARNING"))
    warning_count = sum(1 for x in all_issues if x.startswith("WARNING"))
    consistency_by_sev = Counter(
        x.get("severity", "unknown") for x in consistency_issues
    )
    spot_verdicts = Counter(c.get("verdict", "unknown") for c in spot_checks)
    rerank_count = sum(len(r) for r in rerank_changes)

    report: Dict[str, Any] = {
        "summary": {
            "total_edges": len(edges),
            "valid_edges": total_valid,
            "semantically_valid_edges": total_semantic_pass,
            "avg_fill_rate": round(total_fill / max(len(edges), 1), 3),
            "validation_errors": error_count,
            "validation_warnings": warning_count,
            "consistency_issues": dict(consistency_by_sev),
            "spot_check_verdicts": dict(spot_verdicts),
            "rerank_changes": rerank_count,
        },
        "edges": edge_reports,
        "consistency_issues": consistency_issues,
        "spot_checks": spot_checks,
        "rerank_changes": rerank_changes,
        "action_items": _generate_action_items(
            edge_reports, consistency_issues, spot_checks
        ),
    }
    return report


def _generate_action_items(
    edge_reports: List[Dict],
    consistency_issues: List[Dict],
    spot_checks: List[Dict],
) -> List[str]:
    actions: List[str] = []

    # Format validation failures
    invalid = [e for e in edge_reports if not e["is_valid"]]
    if invalid:
        ids = [e["edge_id"] for e in invalid]
        actions.append(
            f"[FORMAT_ERROR] {len(invalid)} edge(s) failed validation: {ids}."
        )

    # Semantic validation failures
    sem_invalid = [e for e in edge_reports if not e.get("is_semantically_valid", True)]
    if sem_invalid:
        for e in sem_invalid:
            errs = e.get("semantic_errors", [])
            actions.append(
                f"[SEMANTIC_ERROR] Edge {e['edge_id']}: "
                f"{len(errs)} unresolved semantic error(s) after retry: {errs}"
            )

    # Low fill rate
    low_fill = [e for e in edge_reports if e["fill_rate"] < 0.6]
    if low_fill:
        ids = [e["edge_id"] for e in low_fill]
        actions.append(f"[LOW_FILL] {len(low_fill)} edge(s) fill rate <60%: {ids}.")

    # Missing HPP mappings
    missing_maps = [
        e
        for e in edge_reports
        if any(s == "missing" for s in e["mapping_statuses"].values())
    ]
    if missing_maps:
        ids = [e["edge_id"] for e in missing_maps]
        actions.append(
            f"[MISSING_MAP] {len(missing_maps)} edge(s) missing HPP mappings: {ids}."
        )

    # Consistency errors
    for err in consistency_issues:
        if err.get("severity") == "error":
            actions.append(f"[CONSISTENCY] {err['type']}: {err['message']}")

    # Fuzzy duplicates
    fuzzy_dups = [
        err for err in consistency_issues if err.get("type") == "fuzzy_duplicate_edge"
    ]
    if fuzzy_dups:
        actions.append(
            f"[DUPLICATE] {len(fuzzy_dups)} fuzzy duplicate pair(s) detected. "
            f"Review and remove redundant edges."
        )

    # Spot check failures
    for c in spot_checks:
        if c.get("verdict") == "incorrect":
            actions.append(
                f"[SPOT_CHECK] Failed: {c.get('edge_id', '?')} "
                f"correct_value={c.get('correct_value')}"
            )

    if not actions:
        actions.append(
            "[OK] All edges passed format, semantic, and consistency checks."
        )

    return actions
