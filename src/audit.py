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
    A2: Check that key numeric values (theta_hat, CI, reported values)
    appear in the paper text.
    """
    issues = []
    lit = edge.get("literature_estimate", {})
    efr = edge.get("equation_formula_reported", {})

    # Check reported_effect_value
    rev = efr.get("reported_effect_value")
    if rev is not None and not _number_appears_in_text(rev, pdf_text):
        issues.append(
            {
                "check": "numeric_hallucination",
                "severity": "error",
                "field": "equation_formula_reported.reported_effect_value",
                "value": rev,
                "message": f"reported_effect_value={rev} not found in paper text.",
                "action": "flag_for_review",
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
                        "action": "flag_for_review",
                    }
                )

    mu = edge.get("epsilon", {}).get("mu", {}).get("core", {})
    theta = lit.get("theta_hat")
    if mu.get("scale") != "log" and theta is not None:
        if not _number_appears_in_text(theta, pdf_text):
            issues.append(
                {
                    "check": "numeric_hallucination",
                    "severity": "warning",
                    "field": "literature_estimate.theta_hat",
                    "value": theta,
                    "message": (
                        f"theta_hat={theta} not found in paper text "
                        f"(difference scale)."
                    ),
                    "action": "flag_for_review",
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
                        "severity": "warning",
                        "field": f"literature_estimate.ci[{i}]",
                        "value": bound,
                        "message": f"CI {label}={bound} not found in paper text.",
                        "action": "flag_for_review",
                    }
                )

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

        for iss in edge_issues:
            iss["edge_id"] = edge_id
            iss["edge_index"] = i

        all_issues.extend(edge_issues)

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
    Apply automatic fixes for Phase A issues with action='remove'.

    Currently handles:
      - covariate_hallucination: remove variable from Z and adjustment_set
      - extra_field: remove forbidden fields

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
            if iss.get("action") != "remove":
                continue

            check = iss["check"]

            if check == "covariate_hallucination":
                var = iss["variable"]
                # Remove from rho.Z
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
                # Remove from adjustment_set
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
                # Also remove from hpp_mapping.Z
                hm_z = edge.get("hpp_mapping", {}).get("Z", [])
                if isinstance(hm_z, list):
                    edge["hpp_mapping"]["Z"] = [
                        z
                        for z in hm_z
                        if not (isinstance(z, dict) and z.get("name") == var)
                    ]

            elif check == "extra_field":
                field_path = iss["field"]
                # Parse field path like "literature_estimate.subgroup"
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
                    # hpp_mapping role extra keys
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

        # Process in batches
        for batch_start in range(0, len(fixed_edges), max_edges_per_llm_call):
            batch_end = min(batch_start + max_edges_per_llm_call, len(fixed_edges))
            batch = fixed_edges[batch_start:batch_end]

            # Get Phase A flags for this batch
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

            try:
                result = client.call_json(
                    prompt,
                    system_prompt=_PHASE_B_SYSTEM_PROMPT,
                    max_tokens=8192,
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
                    f"LLM call failed: {e}",
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
