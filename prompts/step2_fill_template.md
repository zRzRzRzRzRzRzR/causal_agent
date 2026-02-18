# Step 2: Fill the HPP Template for a Single Edge

You are a medical informatics researcher. You are given:
1. A **JSON template** (with inline `//` comments explaining each field)
2. A paper's full text
3. One extracted edge (X → Y relationship) with summary statistics
4. Retrieved HPP dataset fields for variable mapping

Your task: **fill every placeholder in the template** with actual values from the paper.

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
- **All variable names**: use underscores for spaces (e.g., `sleep_duration` not `sleep duration`).
- **Read each `//` comment in the template** — it tells you exactly what value to put in the adjacent field.
- The output should be **valid JSON** with no `//` comments.

### Field-Specific Guidance

#### edge_id
Format: `EV_{year}_{AuthorStudy}#{edge_number}`, e.g. `EV_2023_RassyUKBiobank#1`

#### paper_title & paper_abstract
- `paper_title`: full paper title, underscores for spaces
- `paper_abstract`: brief abstract (1-3 sentences summarizing design, N, main finding), underscores for spaces

#### equation_type (E1–E6)
Choose based on the statistical method:
- **E1**: logistic, linear, Poisson, ANCOVA, t-test, MR/IVW — static models
- **E2**: Cox proportional hazards, survival models, KM comparisons
- **E3**: LMM, GEE, repeated measures — longitudinal models
- **E4**: mediation analysis, path analysis (requires mediator M)
- **E5**: individual treatment effects (ITE, CATE)
- **E6**: joint intervention with interaction terms (requires second treatment X2)

#### equation_formula
Write the specific model formula, e.g.:
- `"λ(t|do(X=x),Z) = λ₀(t) · exp(β_X · X + γ_age · Age + γ_sex · Sex)"`
- `"logit(P(Y=1)) = α + β*X + γ*Age + δ*Sex"`
- `"E[Y | BMI_group] = α + β · BMI_group"` (for t-test)

#### epsilon.Pi (population tag)
Common values: `"adult_general"`, `"cvd"`, `"diabetes"`, `"oncology"`, `"pediatric"`

#### epsilon.mu.core
- For HR/OR/RR: `family="ratio"`, `type="HR"/"OR"/"RR"`, `scale="log"`
  - `theta_hat` must be on **log scale**: theta_hat = ln(HR)
  - `ci` must also be on log scale: ci = [ln(CI_lower), ln(CI_upper)]
  - Also report original values as `reported_HR` and `reported_CI_HR`
- For MD/BETA/SMD: `family="difference"`, `type="MD"/"BETA"/"SMD"`, `scale="identity"`
  - `theta_hat` is the raw difference

#### epsilon.alpha
- `id_strategy`: `"rct"` / `"observational"` / `"MR"` / `"IV"` / `"pooled_estimates"`
- `assumptions`: list from `["exchangeability", "positivity", "consistency", "proportional_hazards", "no_publication_bias", "sequential_ignorability"]`
- `status`: `"identified"` / `"partially_identified"` / `"not_identified"`

#### epsilon.rho
- `X`: exposure/treatment variable name (must relate to `iota.core.name`)
- `Y`: outcome variable name (must equal `o.name`)
- `Z`: list of covariates/adjustors from the paper
- `IV`: instrument variable (only for MR studies, else `null`)

#### literature_estimate
- `theta_hat`: effect estimate as a **number** (not string), on the scale matching `mu.core.scale`
- `ci`: [lower, upper] on same scale, or `null` if not reported
- `p_value`: number or string like `"<0.001"`, or `null`
- `n`: sample size (integer)
- `design`: `"RCT"` / `"cohort"` / `"cross-sectional"` / `"case-control"` / `"meta-analysis"` / `"MR"`
- `grade`: `"A"` (high-quality RCT), `"B"` (moderate), `"C"` (low/no adjustment)
- `model`: statistical model name matching equation_type
- `adjustment_set`: list of adjustors (should match `rho.Z`)
- **Extra fields allowed**: `reported_HR`, `reported_CI_HR`, `group_means`, `notes`, etc.

#### hpp_mapping
Map each variable to an HPP dataset+field:
- Use the **retrieved HPP datasets** below to find the best match
- Dataset IDs use underscore format: `"009_sleep"`, `"002_anthropometrics"`
- Only include `M` for E4, only include `X2` for E6
- **Extra fields allowed**: `mapping_notes`, `composite_components`, `BMI_covariate`, etc.

### HPP Field Mapping

Below are **retrieved HPP datasets and fields** for this edge. Use them to fill `hpp_mapping`:

{hpp_context}

---

## Template (with // comments as hints)

Read each `//` comment carefully — they explain what each field means and what values are valid.
Your output must be a **clean JSON** with all fields filled and **no // comments**.

```
{template_json}
```

---

## Output Requirements

Output **one complete JSON object** (not an array). It must:
1. Match the template structure — all top-level keys present
2. Use `null` for unknown fields (not `"..."` or empty string)
3. `theta_hat` must be a **number** or `null`
4. For ratio measures (HR/OR/RR): theta_hat and ci on **log scale**; also include `reported_HR`/`reported_CI_HR` with original values
5. Variable naming: `rho.Y` must equal `o.name`
6. Dataset IDs in hpp_mapping use **underscore** format (e.g., `009_sleep`)
7. You MAY add extra descriptive fields (mapping_notes, composite_components, notes, group_means, etc.)
8. Do NOT include any `//` comments or `_comment` keys in output
