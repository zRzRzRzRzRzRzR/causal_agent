"""
证据卡验证器

功能：
1. JSON Schema 校验（字段完整性）
2. 数值一致性检查（effects 与 estimand_equation 对齐）
3. 可复现性检查（核心参数是否非 null）
"""
import json
from typing import Dict, List, Any, Tuple


# 所有类型共享的必需顶层字段
COMMON_REQUIRED_FIELDS = [
    "schema_version", "evidence_id", "paper", "provenance",
    "design", "population", "variables",
]

# 各类型特有的必需字段
TYPE_SPECIFIC_FIELDS = {
    "interventional": ["arms", "effects", "estimand_equation", "inference"],
    "mechanistic": ["mediation_equations", "identification"],
    "causal": ["effects", "identification", "inference"],
    "associational": ["effects", "inference"],
}

# paper 子字段
PAPER_REQUIRED = ["title", "journal", "year"]


class EvidenceCardValidator:
    """证据卡验证器"""

    def __init__(self, evidence_type: str = "interventional"):
        self.evidence_type = evidence_type
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self, card: Dict) -> Tuple[bool, List[str], List[str]]:
        """
        验证单张证据卡

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
        required = COMMON_REQUIRED_FIELDS + TYPE_SPECIFIC_FIELDS.get(self.evidence_type, [])
        for field in required:
            if field not in card:
                self.errors.append(f"缺少必需字段: {field}")
            elif card[field] is None:
                self.warnings.append(f"字段为 null: {field}")

    def _check_paper(self, paper: Dict):
        for field in PAPER_REQUIRED:
            if not paper.get(field):
                self.errors.append(f"paper.{field} 缺失或为空")
        if paper.get("year") and not isinstance(paper["year"], int):
            self.errors.append(f"paper.year 应为整数，当前: {paper['year']}")
        if not paper.get("doi") and not paper.get("pmid"):
            self.warnings.append("paper 缺少 doi 和 pmid，建议至少提供一个")

    def _check_provenance(self, prov: Dict):
        if not prov.get("figure_table"):
            self.warnings.append("provenance.figure_table 为空，缺少数据来源追溯")
        if not prov.get("pages"):
            self.warnings.append("provenance.pages 为空")

    def _check_design(self, design: Dict):
        if not design.get("type"):
            self.errors.append("design.type 缺失")
        if not design.get("n_total"):
            self.warnings.append("design.n_total 缺失")

    def _check_variables(self, variables: Dict):
        nodes = variables.get("nodes", [])
        if not nodes:
            self.errors.append("variables.nodes 为空")
        roles = variables.get("roles", {})
        if not roles.get("X") and not roles.get("Y"):
            self.errors.append("variables.roles 缺少 X 或 Y")

    def _check_interventional(self, card: Dict):
        arms = card.get("arms", [])
        if len(arms) < 2:
            self.warnings.append(f"arms 数量不足 2，当前: {len(arms)}")

        effects = card.get("effects", [])
        if not effects:
            self.errors.append("effects 为空，缺少效应估计")
        for i, eff in enumerate(effects):
            if eff.get("estimate") is None:
                self.warnings.append(f"effects[{i}].estimate 为 null")
            if not eff.get("p_value"):
                self.warnings.append(f"effects[{i}].p_value 缺失")

    def _check_mechanistic(self, card: Dict):
        mediation = card.get("mediation_equations", [])
        if not mediation:
            self.errors.append("mediation_equations 为空")
        for i, med in enumerate(mediation):
            if not med.get("path"):
                self.errors.append(f"mediation_equations[{i}].path 缺失")
            for key in ["total_effect", "indirect_effect", "proportion_mediated"]:
                if not med.get(key) or med[key].get("estimate") is None:
                    self.warnings.append(f"mediation_equations[{i}].{key}.estimate 为 null")

    def _check_effects_consistency(self, card: Dict):
        """检查 effects 和 estimand_equation 的一致性"""
        effects = card.get("effects", [])
        equations = card.get("estimand_equation", {}).get("equations", [])

        effect_ids = {e.get("edge_id") for e in effects}
        equation_refs = {eq.get("source_edge") for eq in equations}

        # 每个 equation 应该引用一个已有的 effect
        unmatched = equation_refs - effect_ids - {None, ""}
        if unmatched:
            self.warnings.append(f"estimand_equation 引用了不存在的 edge_id: {unmatched}")

    def format_report(self) -> str:
        """格式化验证报告"""
        lines = []
        if self.errors:
            lines.append(f"❌ {len(self.errors)} 个错误:")
            for e in self.errors:
                lines.append(f"  - {e}")
        if self.warnings:
            lines.append(f"⚠️  {len(self.warnings)} 个警告:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if not self.errors and not self.warnings:
            lines.append("✅ 验证通过，无错误无警告")
        return "\n".join(lines)


def validate_evidence_cards(
    cards: List[Dict],
    evidence_type: str = "interventional",
) -> Dict:
    """批量验证证据卡"""
    validator = EvidenceCardValidator(evidence_type)
    results = []
    all_valid = True

    for i, card in enumerate(cards):
        is_valid, errors, warnings = validator.validate(card)
        if not is_valid:
            all_valid = False
        results.append({
            "index": i,
            "evidence_id": card.get("evidence_id", "N/A"),
            "is_valid": is_valid,
            "errors": errors,
            "warnings": warnings,
        })

    return {
        "all_valid": all_valid,
        "total": len(cards),
        "results": results,
    }
