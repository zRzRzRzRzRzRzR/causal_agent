import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def load_error_patterns(patterns_path: str) -> Optional[Dict]:
    if not os.path.exists(patterns_path):
        return None
    with open(patterns_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_error_patterns_context(patterns: Dict, max_examples_per_cat: int = 2) -> str:
    """
    Build a compact text block from error_patterns.json for injection
    into Step 4 Phase B prompt.

    Output format:
        ## 历史错误模式分布 (从 N 篇 GT 论文中提取)
        - covariate_hallucination: 12次 (28.6%)
        - numeric_hallucination: 8次 (19.0%)
        ...

        ## 典型错误示例
        ### covariate_hallucination
        - Case X, Edge EV-xxx#1, field Z: "TDI 不存在于本论文..."
        ...
    """
    lines = []
    total = patterns.get("total_patterns", 0)
    n_cases = patterns.get("num_cases", 0)

    lines.append(
        f"## 历史错误模式分布（从 {n_cases} 篇 GT 论文中提取，共 {total} 个问题）\n"
    )

    dist = patterns.get("category_distribution", {})
    for cat, count in dist.items():
        pct = count / max(total, 1) * 100
        lines.append(f"- **{cat}**: {count}次 ({pct:.1f}%)")

    lines.append("")

    top_examples = patterns.get("top_examples", {})
    if top_examples:
        lines.append("## 典型错误示例\n")
        for cat, examples in top_examples.items():
            lines.append(f"### {cat}")
            for ex in examples[:max_examples_per_cat]:
                lines.append(
                    f"- Case {ex.get('case', '?')}, "
                    f"Edge {ex.get('edge_id', '?')}, "
                    f"field `{ex.get('field', '?')}`: "
                    f"\"{ex.get('text', '')[:120]}\""
                )
            lines.append("")

    return "\n".join(lines)


_FEWSHOT_KEEP_FIELDS = {
    "edge_id",
    "equation_type",
    "equation_formula",
    "equation_formula_reported",
    "epsilon",
    "literature_estimate",
    "hpp_mapping",
}

# Fields within epsilon to keep
_EPSILON_KEEP = {"Pi", "iota", "o", "mu", "alpha", "rho"}

# Fields within literature_estimate to keep
_LIT_KEEP = {
    "theta_hat",
    "ci",
    "p_value",
    "n",
    "design",
    "model",
    "adjustment_set",
    "equation_type",
    "equation_formula",
    "reason",
}


def _truncate_edge_for_fewshot(edge: Dict) -> Dict:
    """
    Truncate a GT edge to only the fields useful for few-shot demonstration.
    Strips study_cohort, paper_title, paper_abstract to save tokens.
    """
    truncated = {}
    for k in _FEWSHOT_KEEP_FIELDS:
        if k in edge:
            truncated[k] = edge[k]

    # Trim epsilon to essential sub-fields
    if "epsilon" in truncated:
        eps = truncated["epsilon"]
        truncated["epsilon"] = {k: v for k, v in eps.items() if k in _EPSILON_KEEP}

    # Trim literature_estimate
    if "literature_estimate" in truncated:
        lit = truncated["literature_estimate"]
        truncated["literature_estimate"] = {
            k: v for k, v in lit.items() if k in _LIT_KEEP
        }

    return truncated


def load_gt_cases(
    reference_dir: str,
    max_cases: int = 3,
) -> List[Tuple[str, List[Dict]]]:
    """
    Load GT edge files from reference directory.

    Returns list of (case_id, edges) tuples.
    Only loads *_edges_verified.json files.
    """
    ref_path = Path(reference_dir)
    if not ref_path.exists():
        return []

    cases = []
    for case_dir in sorted(ref_path.iterdir()):
        if not case_dir.is_dir():
            continue
        for f in sorted(case_dir.iterdir()):
            if f.name.endswith("_edges_verified.json") or f.name.endswith(
                "_edges_verified.jsonc"
            ):
                try:
                    raw = f.read_text(encoding="utf-8")
                    clean = re.sub(r"//[^\n]*", "", raw)
                    clean = re.sub(r",\s*([}\]])", r"\1", clean)
                    data = json.loads(clean)
                    if isinstance(data, list):
                        edges = data
                    else:
                        edges = [data]
                    cases.append((case_dir.name, edges))
                    if len(cases) >= max_cases:
                        return cases
                except Exception as e:
                    print(
                        f"[GT] Failed to load {f}: {e}",
                        file=sys.stderr,
                    )
    return cases


def build_fewshot_context(
    gt_cases: List[Tuple[str, List[Dict]]],
    max_edges: int = 2,
    equation_type_filter: Optional[str] = None,
) -> str:
    if not gt_cases:
        return ""

    all_edges = []
    for case_id, edges in gt_cases:
        for e in edges:
            all_edges.append((case_id, e))

    if not all_edges:
        return ""

    selected = []
    seen_eq_types = set()

    if equation_type_filter:
        for case_id, e in all_edges:
            if e.get("equation_type") == equation_type_filter:
                selected.append((case_id, e))
                seen_eq_types.add(equation_type_filter)
                if len(selected) >= max_edges:
                    break

    if len(selected) < max_edges:
        for case_id, e in all_edges:
            eq = e.get("equation_type", "")
            if eq not in seen_eq_types and len(selected) < max_edges:
                selected.append((case_id, e))
                seen_eq_types.add(eq)

    if len(selected) < max_edges:
        for case_id, e in all_edges:
            if (case_id, e) not in selected and len(selected) < max_edges:
                selected.append((case_id, e))

    lines = [
        "## GT 参考示例（人工验证过的正确输出）\n",
        "以下是经过人工验证的正确 edge 填充示例，请参考其 `parameters`、`reason` 的格式和详细程度：\n",
    ]

    for i, (case_id, edge) in enumerate(selected[:max_edges]):
        truncated = _truncate_edge_for_fewshot(edge)
        edge_json = json.dumps(truncated, ensure_ascii=False, indent=2)
        lines.append(f"### 示例 {i+1}: {edge.get('edge_id', '?')} (case: {case_id})\n")
        lines.append(f"```json\n{edge_json}\n```\n")

    return "\n".join(lines)


def get_reference_contexts(
    reference_dir: str,
    error_patterns_path: Optional[str] = None,
    equation_type_filter: Optional[str] = None,
) -> Dict[str, str]:
    result = {
        "fewshot_context": "",
        "error_patterns_context": "",
    }

    if os.path.isdir(reference_dir):
        gt_cases = load_gt_cases(reference_dir, max_cases=3)
        if gt_cases:
            result["fewshot_context"] = build_fewshot_context(
                gt_cases,
                max_edges=2,
                equation_type_filter=equation_type_filter,
            )
            n_cases = len(gt_cases)
            n_edges = sum(len(edges) for _, edges in gt_cases)
            print(
                f"[GT] Loaded {n_edges} GT edges from {n_cases} cases",
                file=sys.stderr,
            )

    if error_patterns_path and os.path.exists(error_patterns_path):
        patterns = load_error_patterns(error_patterns_path)
        if patterns:
            result["error_patterns_context"] = build_error_patterns_context(patterns)
            print(
                f"[GT] Loaded error patterns: {patterns.get('total_patterns', 0)} "
                f"patterns from {patterns.get('num_cases', 0)} cases",
                file=sys.stderr,
            )

    return result
