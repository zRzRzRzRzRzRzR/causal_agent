"""
Evidence Card Validator

Functionality:
1. JSON Schema validation (field completeness)
2. Numerical consistency checks (effects aligned with estimand_equation)
3. Reproducibility checks (core parameters are non-null)
"""

from typing import Dict, List, Tuple

# Required top-level fields shared by all types
COMMON_REQUIRED_FIELDS = [
    "schema_version",
    "evidence_id",
    "paper",
    "provenance",
    "design",
    "population",
    "variables",
]

# Required fields specific to each type
TYPE_SPECIFIC_FIELDS = {
    "interventional": ["arms", "effects", "estimand_equation", "inference"],
    "mechanistic": ["mediation_equations", "identification"],
    "causal": ["effects", "identification", "inference"],
    "associational": ["effects", "inference"],
}

# paper subfields
PAPER_REQUIRED = ["title", "journal", "year"]


class EvidenceCardValidator:
    """Evidence card validator"""

    def __init__(self, evidence_type: str = "interventional"):
        self.evidence_type = evidence_type
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self, card: Dict) -> Tuple[bool, List[str], List[str]]:
        """
        Validate a single evidence card

        Returns:
            (is_valid, errors, warnings)
        """
        self.errors = []
        self.warnings = []

        self._check_top_level_fields(card)
        self._check_paper(card.get("paper", {}))
        self._check_provenance(card.get("provenance", {}))
        self._check_design(card.get("design", {}))
        self._check_variables(card.get("variables", {}))

        if self.evidence_type == "interventional":
            self._check_interventional(card)
        elif self.evidence_type == "mechanistic":
            self._check_mechanistic(card)

        self._check_effects_consistency(card)

        is_valid = len(self.errors) == 0
        return is_valid, self.errors, self.warnings

    def _check_top_level_fields(self, card: Dict):
        required = COMMON_REQUIRED_FIELDS + TYPE_SPECIFIC_FIELDS.get(
            self.evidence_type, []
        )
        for field in required:
            if field not in card:
                self.errors.append(f"Missing required field: {field}")
            elif card[field] is None:
                self.warnings.append(f"Field is null: {field}")

    def _check_paper(self, paper: Dict):
        for field in PAPER_REQUIRED:
            if not paper.get(field):
                self.errors.append(f"paper.{field} is missing or empty")
        if paper.get("year") and not isinstance(paper["year"], int):
            self.errors.append(
                f"paper.year should be an integer, current: {paper['year']}"
            )
        if not paper.get("doi") and not paper.get("pmid"):
            self.warnings.append(
                "paper is missing doi and pmid, recommend providing at least one"
            )

    def _check_provenance(self, prov: Dict):
        if not prov.get("figure_table"):
            self.warnings.append(
                "provenance.figure_table is empty, missing data source tracking"
            )
        if not prov.get("pages"):
            self.warnings.append("provenance.pages is empty")

    def _check_design(self, design: Dict):
        if not design.get("type"):
            self.errors.append("design.type is missing")
        if not design.get("n_total"):
            self.warnings.append("design.n_total is missing")

    def _check_variables(self, variables: Dict):
        nodes = variables.get("nodes", [])
        if not nodes:
            self.errors.append("variables.nodes is empty")
        roles = variables.get("roles", {})
        if not roles.get("X") and not roles.get("Y"):
            self.errors.append("variables.roles is missing X or Y")

    def _check_interventional(self, card: Dict):
        arms = card.get("arms", [])
        if len(arms) < 2:
            self.warnings.append(f"arms count is less than 2, current: {len(arms)}")

        effects = card.get("effects", [])
        if not effects:
            self.errors.append("effects is empty, missing effect estimates")
        for i, eff in enumerate(effects):
            if eff.get("estimate") is None:
                self.warnings.append(f"effects[{i}].estimate is null")
            if not eff.get("p_value"):
                self.warnings.append(f"effects[{i}].p_value is missing")

    def _check_mechanistic(self, card: Dict):
        mediation = card.get("mediation_equations", [])
        if not mediation:
            self.errors.append("mediation_equations is empty")
        for i, med in enumerate(mediation):
            if not med.get("path"):
                self.errors.append(f"mediation_equations[{i}].path is missing")
            for key in ["total_effect", "indirect_effect", "proportion_mediated"]:
                if not med.get(key) or med[key].get("estimate") is None:
                    self.warnings.append(
                        f"mediation_equations[{i}].{key}.estimate is null"
                    )

    def _check_effects_consistency(self, card: Dict):
        """Check consistency between effects and estimand_equation"""
        effects = card.get("effects", [])
        equations = card.get("estimand_equation", {}).get("equations", [])

        effect_ids = {e.get("edge_id") for e in effects}
        equation_refs = {eq.get("source_edge") for eq in equations}

        # Each equation should reference an existing effect
        unmatched = equation_refs - effect_ids - {None, ""}
        if unmatched:
            self.warnings.append(
                f"estimand_equation references non-existent edge_id: {unmatched}"
            )

    def format_report(self) -> str:
        """Format validation report"""
        lines = []
        if self.errors:
            lines.append(f"❌ {len(self.errors)} errors:")
            for e in self.errors:
                lines.append(f"  - {e}")
        if self.warnings:
            lines.append(f"⚠️  {len(self.warnings)} warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if not self.errors and not self.warnings:
            lines.append("✅ Validation passed, no errors or warnings")
        return "\n".join(lines)


def validate_evidence_cards(
    cards: List[Dict],
    evidence_type: str = "interventional",
) -> Dict:
    """Batch validate evidence cards"""
    validator = EvidenceCardValidator(evidence_type)
    results = []
    all_valid = True

    for i, card in enumerate(cards):
        is_valid, errors, warnings = validator.validate(card)
        if not is_valid:
            all_valid = False
        results.append(
            {
                "index": i,
                "evidence_id": card.get("evidence_id", "N/A"),
                "is_valid": is_valid,
                "errors": errors,
                "warnings": warnings,
            }
        )

    return {
        "all_valid": all_valid,
        "total": len(cards),
        "results": results,
    }
