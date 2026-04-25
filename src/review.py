import json
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple

from .hpp_mapper import HPPMapper
from .llm_client import GLMClient
from .template_utils import compute_fill_rate, validate_filled_edge

# ---------------------------------------------------------------------------
# Placeholder & normalization helpers
# ---------------------------------------------------------------------------

# Strings that almost always indicate an unfilled template slot.
# Hit on any of these → the edge is poisoned and must be flagged.
PLACEHOLDER_TOKENS: Tuple[str, ...] = (
    # Chinese template skeleton placeholders (templates/hpp_mapping_template.json)
    "论文完整标题",
    "暴露变量名称",
    "结局变量名称",
    "变量名称",
    "协变量名称",
    "对照组名称",
    "请填",
    "待填",
    "占位",
    "示例值",
    # English placeholders sometimes left by LLMs
    "TBD",
    "TODO",
    "PLACEHOLDER",
    "<example>",
    "<EXAMPLE>",
    "{{",
    "}}",
    # Self-describing markers we plant in the template
    "<<FILL_ME",
    "FILL_ME:",
    # equation_type "全选" leakage
    "E1/E2",
    "E2/E3",
    "E3/E4",
    "E4/E5",
    "E5/E6",
)


def _looks_like_placeholder_string(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    if not s:
        return False
    for tok in PLACEHOLDER_TOKENS:
        if tok in s:
            return True
    # Pure Chinese-noun placeholder pattern: ends with a generic role word.
    if len(s) <= 12 and any(s.endswith(suf) for suf in ("名称", "占位符", "变量")):
        return True
    return False


def has_placeholder(value: Any) -> bool:
    """Recursively scan an edge / dict / list / scalar for placeholder strings."""
    if value is None:
        return False
    if isinstance(value, str):
        return _looks_like_placeholder_string(value)
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(has_placeholder(v) for v in value)
    return False


def collect_placeholder_locations(edge: Dict) -> List[str]:
    """Return a list of dotted-path strings pointing at placeholder fields in edge."""
    hits: List[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if _looks_like_placeholder_string(node):
                hits.append(f"{path}={node!r}")
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")

    _walk(edge, "")
    return hits


_DASH_CHARS = ("–", "—", "‑", "−", "‐")


def _normalize_for_match(s: str) -> str:
    """Normalize a variable string for comparison.

    - lower-case
    - fold Unicode dashes to ASCII '-'
    - collapse whitespace and underscores into a single space
    - strip simple quoting noise
    """
    if not s:
        return ""
    s = str(s).lower().strip()
    for d in _DASH_CHARS:
        s = s.replace(d, "-")
    s = s.replace("'", "").replace('"', "")
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Pi (population label) reconciliation — whitelist-free
# ---------------------------------------------------------------------------
#
# Earlier this module shipped a hardcoded VALID_PI_VALUES set and a
# specificity table tied to specific disease names. That scaled fine for the
# half-dozen batches we'd seen (sleep / TRF / GERD / COPD / pQTL etc.) but
# becomes a liability past ~20 batches: every new domain (COVID-ICU,
# rare-disease pediatrics, transplant, etc.) breaks reconciliation when its
# labels fall off the whitelist.
#
# The new policy:
#   - No whitelist. Whatever Pi the LLM emits is a candidate.
#   - Prefer the *plural majority* label (raw count).
#   - Break ties by treating "adult_general" / "other" / "" as generic
#     (rank 0) and everything else as specific (rank 1). This still
#     reproduces the GERD / COPD examples without naming them.
#   - If the paper has ≥3 distinct labels, log a warning so a human can
#     eyeball it — the LLM is probably confused about who the population is.

_GENERIC_PI_LABELS: Set[str] = {"adult_general", "other", "general", ""}


def _pi_is_generic(label: str) -> bool:
    return (
        not isinstance(label, str)
        or label.strip().lower() in _GENERIC_PI_LABELS
    )


def reconcile_pi(edges: List[Dict]) -> List[Dict]:
    """
    Collapse all Pi values from one paper to a single canonical label.

    Algorithm (whitelist-free):
      1. Count Pi values across edges.
      2. Sort by (-count, -specificity_bit, alpha) — specificity_bit is 1
         for any non-generic label, 0 for adult_general / other / "".
      3. Write the winner back to every edge.

    Warnings emitted:
      - `pi_reconciled`         : 2+ distinct labels, collapsed to the winner.
      - `pi_high_disagreement`  : ≥3 distinct labels — possible LLM confusion,
                                  worth a human glance.
    """
    issues: List[Dict] = []
    if not edges:
        return issues

    pi_seen: List[str] = []
    for e in edges:
        pi = e.get("epsilon", {}).get("Pi", "")
        if isinstance(pi, str) and pi.strip():
            pi_seen.append(pi.strip())

    if not pi_seen:
        return issues

    counts = Counter(pi_seen)
    canonical = sorted(
        counts.items(),
        key=lambda kv: (
            -kv[1],
            0 if _pi_is_generic(kv[0]) else -1,
            kv[0],
        ),
    )[0][0]

    distinct_count = len(counts)
    if distinct_count > 1:
        issues.append(
            {
                "type": "pi_reconciled",
                "severity": "warning",
                "message": (
                    f"Pi values {dict(counts)} reconciled to {canonical!r} "
                    f"(by count, generic-vs-specific tiebreak); "
                    f"written back to all {len(edges)} edge(s)."
                ),
            }
        )
    if distinct_count >= 3:
        issues.append(
            {
                "type": "pi_high_disagreement",
                "severity": "warning",
                "message": (
                    f"{distinct_count} distinct Pi labels in one paper — "
                    f"LLM may be confused about the population. "
                    f"Manual review recommended."
                ),
            }
        )

    for e in edges:
        eps = e.setdefault("epsilon", {})
        eps["Pi"] = canonical

    return issues


_STATUS_RANK = {"missing": 0, "tentative": 1, "close": 2, "exact": 3}


def rerank_hpp_mapping(
    edge: Dict,
    mapper: HPPMapper,
    client: GLMClient,
    roles: Tuple[str, ...] = ("X", "Y"),
) -> Dict[str, Any]:
    """
    For each role (X, Y), ask the LLM to pick the best HPP field from
    the top-6 RAG candidates. Updates edge['hpp_mapping'] in place.

    Behavior changes vs. the original implementation:
    - Skip rerank entirely when X / Y is a placeholder string.
    - Prompt explicitly allows "all candidates wrong → status='missing'".
    - When the LLM returns status='missing' with best>0, the candidate
      is NOT applied — only the status is downgraded.
    - When the LLM tries to demote an 'exact' mapping to a worse status,
      the change is rejected unless the candidate itself is being kept.
    """
    changes: Dict[str, Any] = {}
    queries = _extract_role_queries(edge)
    hm = edge.get("hpp_mapping", {})

    # Hard skip: if the edge itself is poisoned by template placeholders,
    # rerank can only confabulate. Refuse to touch it.
    if has_placeholder(edge):
        return {
            "skipped": True,
            "reason": "edge contains placeholder strings; rerank refused",
        }

    for role in roles:
        query = queries.get(role)
        if not query:
            continue

        if _looks_like_placeholder_string(query):
            changes[role] = {
                "skipped": True,
                "reason": f"role query is a placeholder ({query!r})",
            }
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
        old_status = current.get("status", "tentative")

        prompt = (
            f'Paper variable: "{query}" (role: {role})\n'
            f"Current mapping: {current_ds} / {current_field}"
            f" (status: {old_status})\n\n"
            f"Candidate HPP fields from data dictionary:\n"
            + "\n".join(candidate_lines)
            + "\n\n"
            "DECISION RULES — read carefully:\n"
            "- status='exact'    → candidate measures the SAME concept,"
            " same unit, same scale.\n"
            "- status='close'    → candidate measures the same concept"
            " but differs in unit / definition slightly.\n"
            "- status='tentative'→ candidate captures a partial or related"
            " aspect (composite).\n"
            "- status='missing'  → NONE of the 6 candidates is a"
            " reasonable match for the paper variable.\n"
            "- DO NOT pick the 'least bad' candidate. If all 6 are wrong"
            " concepts, set best=0 and status='missing'.\n"
            "- If the current mapping is already best, set best=0"
            " (status may still update).\n\n"
            "Reply in JSON:\n"
            f'{{"best": 0 or 1-{len(candidates[:6])}, '
            f'"status": "exact|close|tentative|missing", '
            f'"reason": "brief reason"}}'
        )

        try:
            result = client.call_json(prompt, max_tokens=32678)
        except Exception as e:
            print(f"    [Rerank] LLM call failed for {role}: {e}")
            continue

        best_idx = result.get("best", 0)
        reason = result.get("reason", "")
        new_status = result.get("status", old_status)

        if new_status not in _STATUS_RANK:
            new_status = old_status

        # Branch 1: LLM said "all candidates are wrong" — keep mapping, downgrade status.
        if new_status == "missing":
            hm.setdefault(role, {})["status"] = "missing"
            changes[role] = {
                "before_status": old_status,
                "after_status": "missing",
                "kept_existing": True,
                "reason": f"all candidates rejected: {reason}",
            }
            continue

        # Branch 2: candidate selected.
        if 0 < best_idx <= len(candidates[:6]):
            chosen = candidates[best_idx - 1]
            new_ds = chosen.dataset_id
            new_field = chosen.field_name

            same_target = new_ds == current_ds and new_field == current_field

            if not same_target:
                # Refuse to demote a confidently 'exact' mapping by swapping
                # in a different candidate at lower confidence. The LLM tends
                # to confabulate "close" matches when the real answer is missing.
                if _STATUS_RANK.get(old_status, 1) > _STATUS_RANK.get(new_status, 1):
                    changes[role] = {
                        "kept_existing": True,
                        "before": f"{current_ds}/{current_field}",
                        "rejected_after": f"{new_ds}/{new_field}",
                        "before_status": old_status,
                        "rejected_status": new_status,
                        "reason": f"refused downgrade: {reason}",
                    }
                    continue

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
            elif new_status != old_status:
                # Same target, just status update. Still don't allow demotion
                # without evidence — the LLM saying "exact→close" with no field
                # change is usually self-doubt, not new information.
                if _STATUS_RANK.get(old_status, 1) > _STATUS_RANK.get(new_status, 1):
                    changes[role] = {
                        "kept_existing": True,
                        "before_status": old_status,
                        "rejected_status": new_status,
                        "reason": f"refused status downgrade: {reason}",
                    }
                else:
                    hm.setdefault(role, {})["status"] = new_status
                    changes[role] = {
                        "before_status": old_status,
                        "after_status": new_status,
                        "reason": f"status updated: {reason}",
                    }
        elif best_idx == 0 and new_status != old_status:
            # Branch 3: best=0, only status changes. Same demotion guard.
            if _STATUS_RANK.get(old_status, 1) > _STATUS_RANK.get(new_status, 1):
                changes[role] = {
                    "kept_existing": True,
                    "before_status": old_status,
                    "rejected_status": new_status,
                    "reason": f"refused status downgrade: {reason}",
                }
            else:
                hm.setdefault(role, {})["status"] = new_status
                changes[role] = {
                    "before_status": old_status,
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


def filter_edges_by_priority(
    edges: List[Dict],
    keep: Tuple[str, ...] = ("primary", "secondary"),
    warn_drop_fraction: float = 0.30,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Filter edges based on the 'priority' field set during Step 1.
    Returns (kept_edges, removed_edges).

    Behavior:
    - If no edges have a priority field (backward compat), all are kept.
    - If filtering would remove ALL edges, keep everything (safety net).
    - If more than ``warn_drop_fraction`` (default 30%) would be dropped,
      print a warning but still apply the filter. Callers who want
      conservative behavior at scale should pre-filter priorities in
      Step 1 instead of relying on a post-hoc magic threshold.
    """
    # Check if any edges have priority field
    has_priority = any(e.get("priority") for e in edges)
    if not has_priority:
        return edges, []

    kept = []
    removed = []
    for e in edges:
        prio = str(e.get("priority", "primary")).lower().strip()
        if prio in keep:
            kept.append(e)
        else:
            removed.append(e)

    # Safety: if filtering would remove ALL edges, keep everything
    if not kept:
        return edges, []

    # Warn when dropping a large fraction — may indicate LLM mis-tagging.
    # At scale, warn rather than revert — the filter is meant to be real.
    import sys as _sys

    total = len(edges)
    if total > 0 and len(removed) / total > warn_drop_fraction:
        print(
            f"  [priority filter] WARNING: dropping {len(removed)}/{total} "
            f"({len(removed)/total:.0%}) edges as non-primary/secondary — "
            f"check Step 1 priority tagging if this looks wrong.",
            file=_sys.stderr,
        )

    return kept, removed


def check_population_consistency(edges: List[Dict]) -> List[Dict]:
    """
    All edges from the same paper should have the same Pi (population).

    With reconcile_pi running upstream this normally returns an empty list;
    it remains here as a backstop for callers that bypass step3_review or
    skip the reconcile step.
    """
    issues: List[Dict] = []
    pi_values = set()
    for e in edges:
        pi = e.get("epsilon", {}).get("Pi", "")
        if pi:
            pi_values.add(pi)

    if len(pi_values) > 1:
        issues.append(
            {
                "type": "population_inconsistency",
                "severity": "error",
                "message": (
                    f"Edges have inconsistent Pi values: {pi_values}. "
                    f"All edges from the same paper should share one population label."
                ),
            }
        )

    return issues


def canonicalize_paper_titles(edges: List[Dict]) -> List[Dict]:
    """
    Collapse paper_title variants (whitespace / dash / colon noise) onto the
    longest non-placeholder variant. Mutates edges in place. Returns issue
    dicts for ANY remaining inconsistency that is not pure formatting noise.
    """
    issues: List[Dict] = []
    titles = [e.get("paper_title", "") for e in edges]
    real = [t for t in titles if t and not _looks_like_placeholder_string(t)]
    if not real:
        if any(t and _looks_like_placeholder_string(t) for t in titles):
            issues.append(
                {
                    "type": "metadata_inconsistency",
                    "severity": "error",
                    "message": "All paper_title values are template placeholders.",
                }
            )
        return issues

    groups: Dict[str, List[str]] = {}
    for t in real:
        groups.setdefault(_normalize_for_match(t), []).append(t)

    canonical = max(real, key=len)

    for e in edges:
        t = e.get("paper_title", "")
        if not t:
            continue
        if _looks_like_placeholder_string(t) or _normalize_for_match(t) in groups:
            e["paper_title"] = canonical

    if len(groups) > 1:
        issues.append(
            {
                "type": "metadata_inconsistency",
                "severity": "warning",
                "message": (
                    f"{len(groups)} paper_title variants merged into "
                    f"canonical form (longest variant kept)."
                ),
            }
        )

    return issues


def canonicalize_edge_ids(edges: List[Dict]) -> List[Dict]:
    """
    Force a single EV-{year}-{author}#{n} prefix across all edges of a paper.
    Picks the most-common (year, author) tuple. Mutates edges in place.
    """
    issues: List[Dict] = []
    if not edges:
        return issues

    pat = re.compile(r"^EV-(\d{4}|YYYY)-([A-Za-z][A-Za-z0-9\-]*)")
    parsed: List[Tuple[str, str]] = []
    for e in edges:
        eid = e.get("edge_id", "")
        m = pat.match(eid)
        if m:
            parsed.append((m.group(1), m.group(2)))

    if not parsed:
        return issues

    # Author tokens like "ZHENG", "Zheng-NatGen", "ZhengPhenome-wide" should
    # collapse to the shortest pure-alpha prefix to maximize agreement.
    def _author_root(a: str) -> str:
        a = re.split(r"[-_]", a, maxsplit=1)[0]
        # Trim any non-alpha tail glued on (e.g. "ZhengPhenome" → "Zheng")
        m = re.match(r"^[A-Za-z]+", a)
        return (m.group(0) if m else a).capitalize()

    rooted = [(y, _author_root(a)) for y, a in parsed]
    counter: Counter = Counter(rooted)
    canonical_year, canonical_author = counter.most_common(1)[0][0]

    distinct_prefixes = len({(y, a) for y, a in parsed})
    if distinct_prefixes > 1:
        issues.append(
            {
                "type": "edge_id_inconsistency",
                "severity": "warning",
                "message": (
                    f"{distinct_prefixes} distinct edge_id prefixes detected; "
                    f"unified to EV-{canonical_year}-{canonical_author}#N."
                ),
            }
        )

    for i, e in enumerate(edges):
        new_eid = f"EV-{canonical_year}-{canonical_author}#{i + 1}"
        e["edge_id"] = new_eid

    return issues


def detect_placeholder_edges(edges: List[Dict]) -> List[Dict]:
    """
    Scan filled edges for placeholder strings leaked from the Step 2 template
    (e.g., '论文完整标题', '暴露变量名称'). Returns one issue per affected edge.
    """
    issues: List[Dict] = []
    for i, e in enumerate(edges):
        hits = collect_placeholder_locations(e)
        if hits:
            issues.append(
                {
                    "type": "placeholder_leak",
                    "severity": "error",
                    "message": (
                        f"Edge #{i+1} (id={e.get('edge_id','?')}) carries "
                        f"{len(hits)} unfilled template field(s): {hits[:5]}"
                    ),
                    "edge_indices": [i],
                }
            )
    return issues


def check_cross_edge_consistency(edges: List[Dict]) -> List[Dict]:
    """
    Check for issues across the full set of edges from one paper.
    Returns a list of issue dicts.
    """
    issues: List[Dict] = []
    if not edges:
        return issues

    # -- Population consistency --
    issues.extend(check_population_consistency(edges))

    # -- Exact duplicate detection --
    # Use _normalize_for_match so that underscores vs spaces vs Unicode
    # dashes don't fragment the signature. Without this, the LLM emitting
    # "Sleep_deprivation_..." on some edges and "Sleep deprivation ..." on
    # others (common in the 51-batch papers) would silently bypass dedup.
    edge_sigs: List[Tuple[int, Tuple[str, str, str]]] = []
    for i, e in enumerate(edges):
        rho = e.get("epsilon", {}).get("rho", {})
        x = _normalize_for_match(rho.get("X", ""))
        y = _normalize_for_match(rho.get("Y", ""))
        sub = _normalize_for_match(
            e.get("literature_estimate", {}).get("subgroup", "") or ""
        )
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
    # Use normalized comparison so that ":" vs "," vs "_" vs space variants
    # of the same title don't all fire as separate inconsistencies. After
    # canonicalize_paper_titles has run upstream this should usually be 1.
    title_groups: Dict[str, List[str]] = {}
    for e in edges:
        t = e.get("paper_title", "")
        if not t:
            continue
        title_groups.setdefault(_normalize_for_match(t), []).append(t)
    if len(title_groups) > 1:
        issues.append(
            {
                "type": "metadata_inconsistency",
                "severity": "error",
                "message": (
                    f"{len(title_groups)} distinct paper_titles after "
                    f"normalization: {[v[0] for v in title_groups.values()]}"
                ),
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


_PAGE_MARK_RE = re.compile(r"<!--\s*Page\s+(\d+)\s*-->", re.IGNORECASE)


def split_pages(pdf_text: str) -> List[Tuple[int, str]]:
    """
    Split an OCR `combined.md` into [(page_no, text), ...] using the
    `<!-- Page N -->` markers planted by src/ocr.py. If no markers are
    present (e.g. an old cache or a non-OCR text), return [(1, pdf_text)].
    """
    if not pdf_text:
        return []
    marks = list(_PAGE_MARK_RE.finditer(pdf_text))
    if not marks:
        return [(1, pdf_text)]

    pages: List[Tuple[int, str]] = []
    for i, m in enumerate(marks):
        page_no = int(m.group(1))
        body_start = m.end()
        body_end = marks[i + 1].start() if i + 1 < len(marks) else len(pdf_text)
        body = pdf_text[body_start:body_end].strip()
        if body:
            pages.append((page_no, body))
    return pages


# IMRAD-anchor approach to pulling Results-region pages.
#
# Earlier this module enumerated section-name keywords (Results, Findings,
# Outcomes, Discovery, Replication, …). At the corpus we'd looked at that
# was fine, but the keyword list grows without bound across 100 batches —
# every new domain (genomics, vitamin D reviews, COVID supplements, etc.)
# uses its own header conventions. Instead of chasing keywords we now
# read the paper's *structure*:
#
#   - Almost every paper has stable opening anchors:
#       Methods / Patients / Materials / Subjects / Study design / etc.
#   - Almost every paper has stable closing anchors:
#       Discussion / Conclusion / Limitations / References / etc.
#   - Whatever section sits between the LAST Methods-anchor page and the
#     FIRST Discussion-anchor page is "Results-region" by construction —
#     we don't need to know what header it carries.
#
# Combined with the existing Table/Figure pickup pass, this generalizes to
# review papers (which never had "Results" but always have tables),
# supplements (often header-less), and unusual domains (genomics with
# Discovery / Replication sections).

_METHODS_ANCHOR = re.compile(
    r"^\s*#{1,4}\s*"
    r"("
    r"methods?|materials? and methods?|methodology|"
    r"patients?(?: and methods?)?|subjects?(?: and methods?)?|"
    r"study design|study population|study participants?|"
    r"experimental procedures?|data and methods?|"
    r"design and setting|design and methods?|"
    r"online methods?|star\s*[★*]?\s*methods?|stars?\s*methods?"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

_DISCUSSION_ANCHOR = re.compile(
    r"^\s*#{1,4}\s*"
    r"("
    r"discussion|conclusions?|comment(?:ary)?|"
    r"limitations?|references?|"
    r"acknowled?gments?|funding|competing interests?|"
    r"data availability|author contributions?|"
    r"peer review information|publisher.?s note|"
    r"supplementary information|supplemental information"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

_TABLE_HEADER = re.compile(r"(?:^|\n)\s*(?:#{0,4}\s*)?Table\s+\d+[^\n]*", re.IGNORECASE)
_TABLE_CONTENT = re.compile(r"<table[\s>]", re.IGNORECASE)
_FIGURE_LEGEND = re.compile(
    r"(?:^|\n)\s*(?:#{0,4}\s*)?(?:Fig(?:ure)?|Extended Data Fig)\.?\s*\d+",
    re.IGNORECASE,
)


def select_results_and_tables(
    pdf_text: str,
    max_total_chars: int = 28000,
) -> str:
    """
    Pull pages likely to contain numeric findings, using IMRAD anchors.

      Phase 1 — Results region by structure (no header keywords):
        * find the LAST page whose body matches a Methods-style anchor
        * find the FIRST subsequent page matching a Discussion-style anchor
        * pages in [last_methods, first_discussion) are the Results region
        * if either anchor is missing we degrade gracefully:
            - only Discussion found  → keep [0, first_discussion)
            - only Methods found     → keep [last_methods, end)
            - neither found          → leave it to Phase 2

      Phase 2 — Table/Figure pickup paper-wide:
        * any page with a "Table N" header, a raw <table> tag, or a
          figure legend; include i+1 to catch 2-column wrap-around.

    Returns at most max_total_chars of `<!-- Page N -->\\n…` blocks in
    reading order. Never returns an empty string for non-empty input —
    final fallback is `pdf_text[:max_total_chars]`.
    """
    pages = split_pages(pdf_text)
    if not pages:
        return ""

    # Phase 1: anchor-based Results region.
    #
    # Two layouts to handle:
    #   (a) Traditional IMRAD — Methods → Results → Discussion in order.
    #       Results region = [first_methods, first_discussion).
    #   (b) Nature/Cell style — Results → Discussion → Methods (STAR
    #       methods at the back). Methods late, before References.
    #       Results region = [start, first_discussion).
    #
    # Distinguishing the two: compare positions of first Methods anchor
    # and first Discussion anchor. If Methods comes BEFORE Discussion →
    # traditional. If Methods comes AFTER → Nature-style. If Methods is
    # missing → degrade to [start, first_discussion).
    first_methods_idx = -1
    for i, (_, body) in enumerate(pages):
        if _METHODS_ANCHOR.search(body):
            first_methods_idx = i
            break

    first_discussion_idx = len(pages)
    for i, (_, body) in enumerate(pages):
        if _DISCUSSION_ANCHOR.search(body):
            first_discussion_idx = i
            break

    keep_idx: Set[int] = set()
    if (
        first_methods_idx >= 0
        and first_discussion_idx < len(pages)
        and first_methods_idx < first_discussion_idx
    ):
        # Traditional layout: take Methods through (just before) Discussion.
        for i in range(first_methods_idx, first_discussion_idx):
            keep_idx.add(i)
    elif first_discussion_idx < len(pages):
        # Either Nature-style (Methods after Discussion) or Methods absent.
        # Either way, the Results we care about lives BEFORE Discussion.
        for i in range(0, first_discussion_idx):
            keep_idx.add(i)
    elif first_methods_idx >= 0:
        # Methods anchor but no Discussion — supplement, take from Methods on.
        for i in range(first_methods_idx, len(pages)):
            keep_idx.add(i)
    # else: neither anchor; Phase 2 + final fallback handle it.

    # Phase 2: Table / Figure pickup paper-wide.
    for i, (_, body) in enumerate(pages):
        if (
            _TABLE_HEADER.search(body)
            or _TABLE_CONTENT.search(body)
            or _FIGURE_LEGEND.search(body)
        ):
            keep_idx.add(i)
            keep_idx.add(i + 1)

    keep_idx = {i for i in keep_idx if 0 <= i < len(pages)}

    if not keep_idx:
        # No anchors, no tables, no figures — front of paper.
        return pdf_text[:max_total_chars]

    parts: List[str] = []
    used = 0
    for i in sorted(keep_idx):
        page_no, body = pages[i]
        chunk = f"<!-- Page {page_no} -->\n{body}"
        if used + len(chunk) > max_total_chars and parts:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts) if parts else pdf_text[:max_total_chars]


def _select_relevant_chunks(
    pdf_text: str,
    keywords: List[str],
    chunk_chars: int = 4000,
    max_total_chars: int = 28000,
) -> str:
    """
    Score sliding chunks of pdf_text by keyword count and return the
    top-scoring concatenation (up to max_total_chars).

    This replaces the naive pdf_text[:30000] slice, which silently lost
    the results section of long papers (e.g. 51/32895551 phenome-wide MR,
    where 4/5 spot_checks came back 'not_found' because the relevant
    tables sit past the 30 000 char mark).
    """
    if not pdf_text:
        return ""
    if len(pdf_text) <= max_total_chars:
        return pdf_text

    # Prefer page-aligned chunking when OCR page markers are present —
    # tables don't sit cleanly on 4000-char windows, and LLMs do better
    # with intact page bodies than with sliding-window slices.
    pages = split_pages(pdf_text)
    chunks: List[Tuple[int, int, str]] = []
    if len(pages) >= 2:
        cursor = 0
        for page_no, body in pages:
            piece = f"<!-- Page {page_no} -->\n{body}"
            chunks.append((cursor, cursor + len(piece), piece))
            cursor += len(piece) + 2
    else:
        # Build chunks with 25% overlap so a result split across boundaries
        # is still likely to land inside one chunk.
        step = max(chunk_chars * 3 // 4, 1)
        for start in range(0, len(pdf_text), step):
            piece = pdf_text[start : start + chunk_chars]
            if not piece.strip():
                continue
            chunks.append((start, start + len(piece), piece))

    norm_keywords = [k.lower() for k in keywords if k]

    def _score(piece: str) -> int:
        low = piece.lower()
        return sum(low.count(k) for k in norm_keywords)

    scored = sorted(
        ((idx, _score(p), p) for idx, (_, _, p) in enumerate(chunks)),
        key=lambda x: (-x[1], x[0]),
    )

    selected: List[Tuple[int, str]] = []
    used = 0
    for idx, sc, piece in scored:
        if sc <= 0:
            break
        if used + len(piece) > max_total_chars:
            continue
        selected.append((idx, piece))
        used += len(piece)
        if used >= max_total_chars:
            break

    if not selected:
        return pdf_text[:max_total_chars]

    selected.sort(key=lambda x: x[0])  # restore reading order
    return "\n\n".join(p for _, p in selected)


def _spot_check_keywords(edge: Dict, theta_val: float) -> List[str]:
    """Build a set of strings the relevant paper passage probably contains."""
    rho = edge.get("epsilon", {}).get("rho", {})
    out: List[str] = []
    for v in (rho.get("X"), rho.get("Y")):
        if not v:
            continue
        s = str(v)
        # Strip parenthetical noise so token matches more readily.
        s_clean = re.sub(r"\([^)]*\)", " ", s)
        for tok in re.split(r"[\s/_,;]+", s_clean):
            tok = tok.strip(":,.;'\"")
            if len(tok) >= 4 and not tok.isdigit():
                out.append(tok)
    if isinstance(theta_val, (int, float)):
        out.append(f"{theta_val:.2f}")
        out.append(f"{theta_val:.3f}")
    return out


def spot_check_values(
    edges: List[Dict],
    pdf_text: str,
    client: GLMClient,
    sample_size: int = 5,
) -> List[Dict]:
    """
    Ask LLM to verify a sample of extracted numeric values against the paper.

    Sampling strategy (changed from "first 5 with theta_hat"):
    - Spread across distinct (X, Y) pairs to avoid wasting all 5 slots
      on duplicates from the same Table row.
    - For each sampled edge, retrieve the most keyword-relevant ~28 KB of
      paper text instead of slicing the front of the PDF.
    """
    checkable: List[Tuple[int, Dict, float]] = []
    seen_pairs: Set[Tuple[str, str]] = set()
    for i, e in enumerate(edges):
        lit = e.get("literature_estimate", {})
        theta = lit.get("theta_hat")
        if theta is None or not isinstance(theta, (int, float)):
            continue
        rho = e.get("epsilon", {}).get("rho", {})
        pair = (
            _normalize_for_match(rho.get("X", "")),
            _normalize_for_match(rho.get("Y", "")),
        )
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        checkable.append((i, e, theta))

    to_check = checkable[:sample_size]
    if not to_check:
        return [{"status": "skipped", "reason": "No reported ratios to check"}]

    check_items: List[str] = []
    keywords: List[str] = []
    for idx, (i, e, theta_val) in enumerate(to_check):
        rho = e.get("epsilon", {}).get("rho", {})
        mu_type = e.get("epsilon", {}).get("mu", {}).get("core", {}).get("type", "")
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
        keywords.extend(_spot_check_keywords(e, theta_val))

    paper_excerpt = _select_relevant_chunks(pdf_text, keywords)

    prompt = (
        "Verify each extracted result against the paper content below.\n"
        "For each item reply: correct / incorrect (give correct value) / not_found\n\n"
        + "".join(check_items)
        + "\nReply in JSON:\n"
        '{"checks": [{"item": index, "verdict": "correct/incorrect/not_found", '
        '"correct_value": null_or_correct_value, "note": ""}]}\n\n'
        f"--- Paper content (keyword-selected excerpt; "
        f"{len(paper_excerpt)} chars of {len(pdf_text)} total) ---\n"
        f"{paper_excerpt}"
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
    nf_count = 0
    sc_total = 0
    for c in spot_checks:
        v = c.get("verdict")
        if v in ("correct", "incorrect", "not_found"):
            sc_total += 1
        if v == "incorrect":
            actions.append(
                f"[SPOT_CHECK] Failed: {c.get('edge_id', '?')} "
                f"correct_value={c.get('correct_value')}"
            )
        elif v == "not_found":
            nf_count += 1
    if sc_total > 0 and nf_count >= max(2, sc_total // 2):
        actions.append(
            f"[SPOT_CHECK_LOW_COVERAGE] {nf_count}/{sc_total} spot-checks "
            f"returned not_found — the relevant numbers may live outside the "
            f"keyword-selected excerpt; manually verify these edges."
        )

    if not actions:
        actions.append(
            "[OK] All edges passed format, semantic, and consistency checks."
        )

    return actions
