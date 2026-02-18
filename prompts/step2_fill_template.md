# Step 2: Fill the HPP Template for a Single Edge

You are a medical informatics researcher. You are given:
1. An **annotated JSON template** where every field has a `_hint` sibling explaining what to fill
2. A paper's full text
3. One extracted edge (X → Y relationship) with summary statistics
4. Retrieved HPP dataset fields for variable mapping

Your task: **fill every `"..."` placeholder in the template** with actual values from the paper.

---

## Edge to Fill

```
Edge #{edge_index}: {X} → {Y}
Control/Reference: {C}
Subgroup: {subgroup}
Outcome type: {outcome_type}
Effect scale: {effect_scale}
Estimate: {estimate}
CI: {ci}
P-value: {p_value}
Source: {source}
```

## Paper Info

```
First author: {first_author}
Year: {year}
DOI: {doi}
Evidence type: {evidence_type}
```

---

## How to Fill the Template

### Core Rules
- **Only use information from the paper**. Fill `null` for uncertain fields.
- **Do NOT invent** any data not mentioned in the paper.
- **All variable names**: lowercase + underscores (e.g., `dinner_timing` not `dinner timing`).
- **Read each `_hint` field** — it tells you exactly what value to put in the adjacent field.
- **Do NOT include `_hint` or `_meta` keys** in your output — only fill the real fields.

### Naming Consistency (CRITICAL)
The same variable must have the **exact same name** everywhere:
- `epsilon.rho.X` = `epsilon.iota.core.name` → same string
- `epsilon.rho.Y` = `epsilon.o.name` → same string

### HPP Field Mapping

Below are **retrieved HPP datasets and fields** for this edge. Use them to fill `hpp_mapping`:

{hpp_context}

**Status values**: `exact` | `close` | `derived` | `tentative` | `missing`
- When status = `missing`: set `dataset` = `"N/A"`, `field` = `"N/A"`
- Dataset and field names must come from the retrieval results above

---

## Annotated Template

Each field has a `_hint` sibling that explains what to fill. Read the hints, then output the JSON with only the real fields filled in (no `_hint` or `_meta` keys).

```json
{template_json}
```

---

## Output Requirements

Output **one complete JSON object** (not an array). It must:
1. Contain **all non-hint keys** from the template — do not add or remove keys
2. Use lowercase_with_underscores for all variable names
3. Have consistent naming: `rho.X` = `iota.core.name`, `rho.Y` = `o.name`
4. Use `"N/A"` for dataset/field when hpp_mapping status = `missing`
5. `theta_hat` must be a **number** (not string), or `null`
6. **Do NOT output** any `_hint` or `_meta` keys — only real data fields
