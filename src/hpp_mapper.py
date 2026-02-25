"""
hpp_mapper.py — Map paper variables to HPP (Human Phenotype Project) fields.

Given an edge's X, Y, Z variables, search the HPP data dictionary for
the best matching dataset+field pairs. Uses:
  1. Inverted token index over all fields
  2. Synonym expansion for medical/clinical terms
  3. Rule-based dataset forcing (e.g., disease outcomes → 021-medical_conditions)
  4. Context-aware ranking (direct hits weighted higher than synonyms)

Output format matches the new template:
  hpp_mapping.X = {dataset: "009_sleep", field: "total_sleep_time"}
  hpp_mapping.Y = {dataset: "002_anthropometrics", field: "bmi"}
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Synonym map — used for query expansion during field search
# ---------------------------------------------------------------------------

SYNONYM_MAP: Dict[str, Set[str]] = {
    # Anthropometrics
    "bmi": {
        "body",
        "mass",
        "index",
        "anthropometrics",
        "weight",
        "obesity",
        "overweight",
    },
    "weight": {"bmi", "obesity", "overweight", "body", "anthropometrics", "kg"},
    "height": {"stature", "anthropometrics", "cm"},
    "waist": {"circumference", "abdominal", "anthropometrics"},
    "hip": {"circumference", "anthropometrics"},
    "obesity": {"bmi", "overweight", "body", "mass", "weight", "adiposity"},
    # Lifestyle
    "smoking": {
        "tobacco",
        "smoke",
        "cigarette",
        "smoker",
        "nicotine",
        "current",
        "status",
    },
    "alcohol": {"drinking", "ethanol", "drink", "beer", "wine", "spirits", "frequency"},
    "diet": {
        "dietary",
        "food",
        "nutrition",
        "fruit",
        "vegetable",
        "meat",
        "grain",
        "fish",
        "intake",
    },
    "exercise": {
        "physical",
        "activity",
        "sport",
        "fitness",
        "walking",
        "vigorous",
        "moderate",
    },
    "physical": {"exercise", "activity", "sport", "fitness"},
    "activity": {
        "exercise",
        "physical",
        "sport",
        "walking",
        "moderate",
        "vigorous",
        "days",
    },
    "lifestyle": {
        "smoking",
        "alcohol",
        "diet",
        "exercise",
        "physical",
        "activity",
        "environment",
    },
    # Cardiovascular
    "hypertension": {"blood", "pressure", "systolic", "diastolic", "bp"},
    "blood": {"pressure", "hypertension", "tests", "serum", "hematology"},
    "heart": {
        "cardiac",
        "cardiovascular",
        "ischemic",
        "coronary",
        "arrhythmia",
        "failure",
    },
    "cardiac": {"heart", "cardiovascular", "ecg"},
    "ischemic": {"coronary", "heart", "angina", "ischemia"},
    "arrhythmia": {"atrial", "fibrillation", "rhythm", "ecg", "arrhythmias"},
    "stroke": {"cerebrovascular", "ischemic", "hemorrhagic"},
    "cerebrovascular": {"stroke", "brain", "vascular"},
    "thrombosis": {"embolism", "clot", "dvt", "venous"},
    "arteriosclerosis": {"atherosclerosis", "plaque", "vascular", "carotid"},
    # Metabolic
    "diabetes": {"glucose", "insulin", "hba1c", "glycated", "metabolic", "type"},
    "glucose": {"diabetes", "blood", "sugar", "glycemic", "cgm"},
    "gout": {"uric", "acid", "urate"},
    "liver": {"hepatic", "hepato", "fatty", "cirrhosis", "ultrasound"},
    "cholesterol": {"ldl", "hdl", "lipid", "lipidomics", "triglyceride"},
    # Renal
    "kidney": {"renal", "nephro", "creatinine", "gfr", "failure"},
    "renal": {"kidney", "nephro", "failure"},
    # Respiratory
    "asthma": {"respiratory", "bronchial", "wheeze", "lung", "pulmonary"},
    "pulmonary": {"lung", "respiratory", "asthma"},
    # Oncology
    "cancer": {"tumor", "carcinoma", "adenocarcinoma", "malignant", "neoplasm"},
    "colorectal": {"colon", "rectum", "bowel"},
    "breast": {"mammary"},
    "endometrial": {"uterine", "uterus"},
    "ovarian": {"ovary"},
    "esophageal": {"esophagus", "oesophageal"},
    "pancreatic": {"pancreas"},
    # Musculoskeletal
    "osteoarthritis": {"arthritis", "joint", "bone", "musculoskeletal"},
    # Psychological
    "mood": {"depression", "anxiety", "mental", "psychological", "depressive"},
    "depression": {"mood", "depressive", "mental", "psychological"},
    "anxiety": {"mood", "anxious", "mental", "psychological"},
    # Sleep
    "sleep": {
        "insomnia",
        "apnea",
        "circadian",
        "rest",
        "duration",
        "bedtime",
        "nap",
        "wake",
        "total",
    },
    "insomnia": {"sleep", "circadian", "wake"},
    # Infection
    "infection": {"bacterial", "viral", "pathogen", "sepsis", "infections"},
    # Demographics
    "age": {"years", "born", "birth"},
    "sex": {"gender", "male", "female"},
    "ethnicity": {"race", "ethnic", "country", "birth", "origin"},
    "deprivation": {"socioeconomic", "townsend", "income", "poverty"},
    "socioeconomic": {"deprivation", "townsend", "income", "education"},
    # Outcomes
    "mortality": {"death", "survival", "died", "fatal", "cause"},
    "death": {"mortality", "survival", "fatal"},
    "incidence": {"onset", "diagnosis", "incident"},
    # Blood tests
    "crp": {"inflammation", "reactive", "protein", "inflammatory"},
    "ldl": {"cholesterol", "lipid", "lipoprotein"},
    "hdl": {"cholesterol", "lipid", "lipoprotein"},
    "hba1c": {"glycated", "hemoglobin", "diabetes", "glucose"},
    # Medications
    "medication": {"drug", "treatment", "prescription", "statin", "medications"},
    "statin": {"medication", "cholesterol", "lipid"},
}


# ---------------------------------------------------------------------------
# Field search index
# ---------------------------------------------------------------------------


@dataclass
class FieldCandidate:
    dataset_id: str
    field_name: str
    score: float
    matched_tokens: Set[str] = field(default_factory=set)


class HPPFieldIndex:
    """Inverted token index over HPP data dictionary fields."""

    def __init__(self, raw_dict: Dict[str, Any]):
        self.raw_dict = raw_dict
        self.inverted_index: Dict[str, List[Tuple[str, str]]] = {}
        self.field_registry: Dict[str, Dict] = {}
        self._build_index()

    def _build_index(self):
        for dataset_id, info in self.raw_dict.items():
            fields = info.get("tabular_field_name", [])
            dataset_tokens = self._tokenize(dataset_id)
            for field_name in fields:
                key = f"{dataset_id}::{field_name}"
                self.field_registry[key] = {
                    "dataset_id": dataset_id,
                    "field_name": field_name,
                }
                tokens = self._tokenize(field_name) | dataset_tokens
                for token in tokens:
                    self.inverted_index.setdefault(token, []).append(
                        (dataset_id, field_name)
                    )

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """Tokenize a string into lowercase tokens, stripping numeric prefixes."""
        text = re.sub(r"^\d+[-_]", "", text)
        parts = re.split(r"[_\-/\s.()]+", text.lower())
        return {p for p in parts if len(p) > 1}  # Allow 2-char tokens (e.g. "bp", "hr")

    @staticmethod
    def _expand_synonyms(tokens: Set[str]) -> Set[str]:
        expanded = set(tokens)
        for token in list(tokens):
            synonyms = SYNONYM_MAP.get(token, set())
            expanded |= synonyms
        return expanded

    def search(self, query: str, top_k: int = 30) -> List[FieldCandidate]:
        """Search for HPP fields matching the query string."""
        query_tokens = self._tokenize(query)
        expanded_tokens = self._expand_synonyms(query_tokens)

        hit_count: Dict[str, int] = {}
        hit_tokens: Dict[str, Set[str]] = {}

        for token in expanded_tokens:
            for dataset_id, field_name in self.inverted_index.get(token, []):
                key = f"{dataset_id}::{field_name}"
                hit_count[key] = hit_count.get(key, 0) + 1
                hit_tokens.setdefault(key, set()).add(token)

        candidates = []
        for key, count in hit_count.items():
            info = self.field_registry[key]
            direct_hits = hit_tokens[key] & query_tokens
            synonym_hits = hit_tokens[key] - query_tokens
            # Score: direct matches count 2x, synonym matches count 1x
            score = (len(direct_hits) * 2 + len(synonym_hits)) / max(
                len(expanded_tokens), 1
            )
            candidates.append(
                FieldCandidate(
                    dataset_id=info["dataset_id"],
                    field_name=info["field_name"],
                    score=score,
                    matched_tokens=hit_tokens[key],
                )
            )

        candidates.sort(key=lambda c: -c.score)
        return candidates[:top_k]


# ---------------------------------------------------------------------------
# HPP Mapper — main class
# ---------------------------------------------------------------------------


class HPPMapper:
    """Map edge variables to HPP dataset+field pairs."""

    # Keywords that indicate a disease/condition outcome
    _DISEASE_KEYWORDS = {
        "diabetes",
        "hypertension",
        "cancer",
        "heart",
        "failure",
        "stroke",
        "arrhythmia",
        "arrhythmias",
        "asthma",
        "liver",
        "kidney",
        "renal",
        "gout",
        "osteoarthritis",
        "arthritis",
        "sleep",
        "mood",
        "depression",
        "anxiety",
        "infection",
        "infections",
        "thrombosis",
        "embolism",
        "cerebrovascular",
        "arteriosclerosis",
        "atherosclerosis",
        "disease",
        "disorder",
        "mortality",
        "death",
        "incidence",
        "pulmonary",
        "colorectal",
        "breast",
        "pancreatic",
        "esophageal",
        "ovarian",
        "endometrial",
        "obesity",
        "copd",
    }

    # Rules for force-including datasets based on variable content
    _FORCE_INCLUDE_RULES = {
        "021-medical_conditions": lambda queries: any(
            HPPMapper._has_disease_keyword(q)
            for role, q in queries.items()
            if role.startswith("Y") or role == "Y"
        ),
        "058-health_and_medical_history": lambda queries: any(
            HPPMapper._has_disease_keyword(q)
            for role, q in queries.items()
            if role.startswith("Y") or role == "Y"
        ),
        "055-lifestyle_and_environment": lambda queries: any(
            any(
                kw in q.lower()
                for kw in [
                    "lifestyle",
                    "smoking",
                    "alcohol",
                    "diet",
                    "exercise",
                    "physical activity",
                    "tobacco",
                    "drinking",
                    "healthy",
                ]
            )
            for q in queries.values()
        ),
        "000-population": lambda _: True,
        "002-anthropometrics": lambda queries: any(
            any(
                kw in q.lower()
                for kw in [
                    "bmi",
                    "weight",
                    "obesity",
                    "overweight",
                    "body mass",
                    "anthropo",
                    "height",
                    "waist",
                    "hip",
                ]
            )
            for q in queries.values()
        ),
        "009-sleep": lambda queries: any(
            any(
                kw in q.lower()
                for kw in [
                    "sleep",
                    "insomnia",
                    "apnea",
                    "circadian",
                    "bedtime",
                    "wake",
                    "rest",
                    "nap",
                    "duration",
                ]
            )
            for q in queries.values()
        ),
        "016-blood_tests": lambda queries: any(
            any(
                kw in q.lower()
                for kw in [
                    "cholesterol",
                    "ldl",
                    "hdl",
                    "glucose",
                    "hba1c",
                    "crp",
                    "creatinine",
                    "hemoglobin",
                    "blood test",
                    "serum",
                    "triglyceride",
                    "uric acid",
                ]
            )
            for q in queries.values()
        ),
        "007-blood_pressure": lambda queries: any(
            any(
                kw in q.lower()
                for kw in [
                    "blood pressure",
                    "systolic",
                    "diastolic",
                    "hypertension",
                    "bp",
                ]
            )
            for q in queries.values()
        ),
    }

    @staticmethod
    def _has_disease_keyword(text: str) -> bool:
        tokens = set(re.split(r"[_\-/\s.()]+", text.lower()))
        return bool(tokens & HPPMapper._DISEASE_KEYWORDS)

    def __init__(self, raw_dict: Dict[str, Any]):
        self.raw_dict = raw_dict
        self.index = HPPFieldIndex(raw_dict)

    def get_context_for_edge(
        self,
        edge: Dict,
        max_datasets: int = 10,
        max_fields_per_dataset: int = 25,
    ) -> str:
        """
        Build a context string for the LLM prompt showing:
          1. Relevant HPP datasets and their fields
          2. Per-role mapping suggestions (X, Y, Z.*)
          3. All available dataset IDs
        """
        queries = self._extract_mapping_queries(edge)
        relevant_datasets: Dict[str, float] = {}
        role_suggestions: Dict[str, List[FieldCandidate]] = {}

        for role, query in queries.items():
            candidates = self.index.search(query, top_k=15)
            role_suggestions[role] = candidates[:5]
            for c in candidates[:10]:
                if c.dataset_id not in relevant_datasets:
                    relevant_datasets[c.dataset_id] = c.score
                else:
                    relevant_datasets[c.dataset_id] = max(
                        relevant_datasets[c.dataset_id], c.score
                    )

        # Force-include datasets based on rules
        for ds_id, rule_fn in self._FORCE_INCLUDE_RULES.items():
            if ds_id in self.raw_dict and rule_fn(queries):
                if ds_id not in relevant_datasets:
                    relevant_datasets[ds_id] = 0.01

        sorted_datasets = sorted(relevant_datasets.items(), key=lambda x: -x[1])[
            :max_datasets
        ]

        parts = []
        parts.append("#### 检索到的相关 HPP 数据集\n")

        for ds_id, _ in sorted_datasets:
            fields = self.raw_dict.get(ds_id, {}).get("tabular_field_name", [])
            shown = fields[:max_fields_per_dataset]
            # Normalize dataset ID for display (use hyphen format: 055-lifestyle_and_environment)
            display_id = ds_id  # Already in hyphen format from raw_dict keys
            parts.append(f"\n**`{display_id}`** ({len(fields)} fields)")
            parts.append("```")
            parts.append(", ".join(shown))
            if len(fields) > max_fields_per_dataset:
                parts.append(f"... +{len(fields) - max_fields_per_dataset} more")
            parts.append("```")

        parts.append("\n#### 映射建议\n")
        for role, candidates in role_suggestions.items():
            if not candidates:
                parts.append(f"- **{role}**: 无匹配 → status=missing")
                continue
            suggestions = []
            for c in candidates[:3]:
                ds_display = c.dataset_id  # Keep original hyphen format
                suggestions.append(
                    f"`{ds_display}` → `{c.field_name}` (score={c.score:.2f})"
                )
            parts.append(f"- **{role}**: {' | '.join(suggestions)}")

        parts.append("\n#### 所有可用数据集 ID\n")
        parts.append(", ".join(f"`{k}`" for k in sorted(self.raw_dict.keys())))

        return "\n".join(parts)

    def _extract_mapping_queries(self, edge: Dict) -> Dict[str, str]:
        """Extract search queries from edge for each variable role."""
        queries = {}
        for key in ["X", "Y"]:
            val = edge.get(key, "")
            if val:
                queries[key] = str(val)

        z = edge.get("C") or edge.get("Z") or edge.get("covariates")
        if isinstance(z, list):
            for item in z[:8]:
                name = str(item) if isinstance(item, str) else item.get("name", "")
                if name:
                    queries[f"Z.{name}"] = name
        elif isinstance(z, str) and z:
            queries["Z"] = z

        subgroup = edge.get("subgroup", "")
        if subgroup and subgroup not in ("总体人群", "overall", ""):
            queries["subgroup"] = str(subgroup)

        return queries


# ---------------------------------------------------------------------------
# Module-level cache and convenience function
# ---------------------------------------------------------------------------

_mapper_cache: Dict[str, HPPMapper] = {}


def get_hpp_context(edge: Dict, dict_path: str, max_datasets: int = 10) -> str:
    """Get HPP field context for an edge, using a cached mapper."""
    if dict_path not in _mapper_cache:
        with open(dict_path, "r", encoding="utf-8") as f:
            raw_dict = json.load(f)
        _mapper_cache[dict_path] = HPPMapper(raw_dict)
    mapper = _mapper_cache[dict_path]
    return mapper.get_context_for_edge(edge, max_datasets=max_datasets)
