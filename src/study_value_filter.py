"""
Step 1.6 — Study-Value Filter (evidence_first workflow only).

Purpose
-------
Step 1 is intentionally high-recall: it enumerates every X→Y relationship
the LLM can find. That gives high coverage but also tends to include
within-group changes, crude rates, redundant model specifications, AUC
curve points, sensitivity analyses, and other low-priority records that
shouldn't be treated as primary evidence by downstream consumers.

This module applies *paper-level* priority rules to decide which Step 1
edges are worth carrying forward into Step 2 (which is expensive — one
LLM call per kept edge). It is deliberately deterministic — no LLM call,
no batch-specific keywords. Rules below apply to any topic.

Filtering priority (highest first)
----------------------------------
1. Drop the entire paper if Step 0 classified it as a review/meta-analysis
   that doesn't itself report new statistics. Reviews usually contribute
   noise, and individual contributing studies should be extracted from
   the originals instead. Caller decides whether to act on this.
2. Within (X, Y, subgroup) groups, prefer:
     model_effect / between_group_effect      (rank 0, "primary")
     within_group_change / group_mean         (rank 1, "secondary")
     crude_rate                               (rank 2, "secondary")
     sensitivity / subgroup                   (rank 3, "tertiary")
     unknown                                  (rank 4, "last resort")
3. Within the same statistic_type bucket, prefer:
     priority="primary"   over  priority="secondary" / "exploratory"
     non-empty adjustment_set  over  empty
     larger n  over  smaller n
4. After per-group selection, drop edges that are:
     - duplicates by (X, Y, subgroup, statistic_type)
     - exploratory + has_numeric_estimate=false
5. Edges that no rule strongly excludes but raise a "needs_review" flag
   (e.g. statistic_type="unknown" without any effect numbers) are kept
   but marked.

Output (the third-tuple field):
    {
      "kept_edges":          [edge_index, ...],
      "dropped_edges":       [{edge_index, drop_reason, ...}, ...],
      "merge_or_duplicate_groups": [
          {"group_key": (X, Y, subgroup, st_type), "kept": idx, "dropped": [idx, ...]},
          ...
      ],
      "needs_review":        [edge_index, ...],
      "summary": {
          "step1_count": N, "step1_6_kept": K, "step1_6_dropped": D,
          "by_drop_reason": {...},
          "paper_classification": "associational" | ...
      }
    }
"""

from collections import defaultdict
from typing import Any, Dict, List, Tuple


# Lower rank = higher priority = more likely to be "real" evidence.
_STATISTIC_TYPE_RANK: Dict[str, int] = {
    "model_effect": 0,
    "between_group_effect": 0,
    "within_group_change": 1,
    "group_mean": 1,
    "crude_rate": 2,
    "sensitivity": 3,
    "subgroup": 3,
    "unknown": 4,
}

_PRIORITY_RANK: Dict[str, int] = {
    "primary": 0,
    "secondary": 1,
    "exploratory": 2,
}


def _norm_str(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return " ".join(s.lower().replace("_", " ").split()).strip()


def _group_key(edge: Dict) -> Tuple[str, str, str, str]:
    """
    Group edges that are 'about the same thing' so we can pick the best
    representative. Uses normalized X/Y/subgroup + statistic_type bucket.
    """
    x = _norm_str(edge.get("X", ""))
    y = _norm_str(edge.get("Y", ""))
    sub = _norm_str(edge.get("subgroup", ""))
    st = str(edge.get("statistic_type", "") or "unknown").lower()
    return (x, y, sub, st)


def _edge_score(edge: Dict) -> Tuple[int, ...]:
    """
    Tuple sort key, lower = better. Order matters:
      1. statistic_type rank (model_effect first)
      2. priority rank (primary first)
      3. negative #covariates (more adjusted first → negative for ascending sort)
      4. negative n (larger sample first)
      5. has_numeric_estimate (with-numbers first)
    """
    st = str(edge.get("statistic_type", "") or "unknown").lower()
    st_rank = _STATISTIC_TYPE_RANK.get(st, 4)

    prio = str(edge.get("priority", "") or "secondary").lower()
    p_rank = _PRIORITY_RANK.get(prio, 1)

    adj = edge.get("adjustment_variables") or []
    n_adj = len(adj) if isinstance(adj, list) else 0

    n = edge.get("n")
    if isinstance(n, str):
        try:
            n = int("".join(c for c in n if c.isdigit()) or "0")
        except ValueError:
            n = 0
    if not isinstance(n, (int, float)):
        n = 0

    has_num = 0 if edge.get("has_numeric_estimate") is False else 1

    # Lower score = "better". Use negative for fields where larger-is-better.
    return (st_rank, p_rank, -n_adj, -int(n), -has_num)


def filter_edges_by_study_value(
    edges: List[Dict],
    paper_classification: str = "",
) -> Tuple[List[Dict], List[Dict], Dict[str, Any]]:
    """
    Apply paper-level study-value filtering. See module docstring.

    Returns:
        (kept_edges, dropped_edges, report)

        kept_edges and dropped_edges are NEW lists (the function does not
        mutate the input). Each dropped edge gets a `_drop_reason` key.
    """
    if not edges:
        return [], [], {
            "kept_edges": [], "dropped_edges": [], "merge_or_duplicate_groups": [],
            "needs_review": [],
            "summary": {
                "step1_count": 0, "step1_6_kept": 0, "step1_6_dropped": 0,
                "by_drop_reason": {}, "paper_classification": paper_classification,
            },
        }

    # ── Pass 1: tag each edge with index, group, score
    indexed: List[Tuple[int, Dict, Tuple, Tuple]] = []
    for i, e in enumerate(edges):
        indexed.append((i, e, _group_key(e), _edge_score(e)))

    # ── Pass 2: per-group winner selection
    groups: Dict[Tuple, List[Tuple[int, Dict, Tuple]]] = defaultdict(list)
    for i, e, gk, score in indexed:
        groups[gk].append((i, e, score))

    kept_indices: set = set()
    merged_groups: List[Dict[str, Any]] = []
    for gk, members in groups.items():
        if len(members) == 1:
            kept_indices.add(members[0][0])
            continue
        # Sort by score ascending (best first)
        members.sort(key=lambda m: m[2])
        winner_idx = members[0][0]
        kept_indices.add(winner_idx)
        merged_groups.append({
            "group_key": list(gk),
            "kept": winner_idx,
            "dropped": [m[0] for m in members[1:]],
            "reason": "duplicate_or_redundant_in_group",
        })

    # ── Pass 3: blanket exclusions on the survivors
    needs_review_idx: List[int] = []
    drop_records: List[Dict[str, Any]] = []
    final_keep: List[int] = []

    for i, e, gk, score in indexed:
        if i not in kept_indices:
            # already dropped via grouping
            continue

        # Drop exploratory edges with no numeric estimate.
        st = str(e.get("statistic_type", "") or "unknown").lower()
        prio = str(e.get("priority", "") or "secondary").lower()
        has_num = e.get("has_numeric_estimate") is not False

        if prio == "exploratory" and not has_num:
            drop_records.append({
                "edge_index": i,
                "drop_reason": "exploratory_without_numeric_estimate",
                "statistic_type": st,
                "priority": prio,
            })
            continue

        # Reviews don't usually carry primary evidence (the underlying
        # studies do). Keep but flag for human review.
        if paper_classification.lower() in (
            "review", "meta-analysis", "meta_analysis", "systematic_review"
        ) and st == "unknown":
            needs_review_idx.append(i)

        # statistic_type=unknown without numbers → flag, don't drop.
        if st == "unknown" and not has_num:
            needs_review_idx.append(i)

        final_keep.append(i)

    # ── Pass 4: build outputs
    kept_edges: List[Dict] = []
    dropped_edges: List[Dict] = []

    grouped_drops = {
        d["edge_index"]: d for d in drop_records
    }
    for grp in merged_groups:
        for idx in grp["dropped"]:
            grouped_drops.setdefault(idx, {
                "edge_index": idx,
                "drop_reason": grp["reason"],
                "kept_winner": grp["kept"],
            })

    for i, e in enumerate(edges):
        if i in final_keep:
            kept_edges.append(e)
        else:
            d = dict(e)  # shallow copy
            drop_info = grouped_drops.get(i, {"drop_reason": "unspecified"})
            d["_drop_reason"] = drop_info.get("drop_reason")
            if drop_info.get("kept_winner") is not None:
                d["_dropped_for_winner_index"] = drop_info["kept_winner"]
            dropped_edges.append(d)

    by_reason: Dict[str, int] = defaultdict(int)
    for d in dropped_edges:
        by_reason[d.get("_drop_reason", "unspecified")] += 1

    report = {
        "kept_edges": [i for i in final_keep],
        "dropped_edges": [
            {
                "edge_index": i,
                "drop_reason": d.get("_drop_reason"),
                "kept_winner": d.get("_dropped_for_winner_index"),
                "X": d.get("X", ""),
                "Y": d.get("Y", ""),
                "statistic_type": d.get("statistic_type"),
            }
            for i, d in enumerate(edges)
            if i not in final_keep
        ],
        "merge_or_duplicate_groups": merged_groups,
        "needs_review": needs_review_idx,
        "summary": {
            "step1_count": len(edges),
            "step1_6_kept": len(kept_edges),
            "step1_6_dropped": len(dropped_edges),
            "by_drop_reason": dict(by_reason),
            "paper_classification": paper_classification,
        },
    }

    return kept_edges, dropped_edges, report
