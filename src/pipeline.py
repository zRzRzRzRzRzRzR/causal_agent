"""
pipeline.py — Edge extraction pipeline aligned with new hpp_mapping_template.

Three-step pipeline:
  Step 0: Classify paper type (interventional/causal/mechanistic/associational)
  Step 1: Enumerate all X→Y statistical edges
  Step 2: Fill each edge into the HPP template

Step 2 uses a template-first approach:
  1. Load template (hpp_mapping_template.json with // comments as hints)
  2. Pre-fill deterministic fields (edge_id, literature_estimate partials)
  3. Send template + // comments as inline hints to LLM
  4. Merge LLM output into skeleton (skeleton is structural authority, but
     extra keys like mapping_notes, composite_components are accepted)
  5. Auto-fix, validate, compute fill rate
"""

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .hpp_mapper import get_hpp_context
from .llm_client import GLMClient
from .template_utils import (
    build_filled_edge,
    load_template,
    prepare_template_for_prompt,
    prepare_template_with_comments,
)
from .utils import save_json

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SRC_DIR.parent
_PROMPTS_DIR = _PROJECT_DIR / "prompts"
_TEMPLATES_DIR = _PROJECT_DIR / "templates"
_DEFAULT_HPP_DICT = _TEMPLATES_DIR / "pheno_ai_data_dictionaries_simplified.json"
_DEFAULT_TEMPLATE = _TEMPLATES_DIR / "hpp_mapping_template.json"


def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts directory."""
    path = _PROMPTS_DIR / f"{name}.md"
    assert path.exists(), f"Prompt file not found: {path}"
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "latin1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def step0_classify(client: GLMClient, pdf_text: str) -> Dict[str, Any]:
    """Classify paper into: interventional / causal / mechanistic / associational."""
    prompt_template = _load_prompt("step0_classify")
    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text[:500000]}"
    result = client.call_json(full_prompt)

    print(
        f"[Step 0] Classification: {result.get('primary_category')} "
        f"(confidence: {result.get('confidence', 'N/A')})",
        file=sys.stderr,
    )
    return result


def step1_enumerate_edges(
    client: GLMClient, pdf_text: str, evidence_type: str
) -> Dict[str, Any]:
    """Extract all X→Y statistical edges from the paper."""
    prompt_template = _load_prompt("step1_edges")
    prompt_template = prompt_template.replace("{evidence_type}", evidence_type)
    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text[:500000]}"

    result = client.call_json(full_prompt, max_tokens=32768)

    edges = result.get("edges", [])
    paper_info = result.get("paper_info", {})

    # Filter out baseline balance check rows (Table 1 demographic comparisons)
    _BASELINE_DEMO_KEYWORDS = {
        "age",
        "sex",
        "gender",
        "bmi",
        "weight",
        "height",
        "ethnicity",
        "race",
        "waist",
        "% female",
        "% male",
        "systolic",
        "diastolic",
        "years",
    }

    def _is_baseline_check(edge: Dict) -> bool:
        if edge.get("significant", True):
            return False
        source = str(edge.get("source", "")).lower()
        if "table 1" not in source and "supplementary" not in source:
            return False
        y = str(edge.get("Y", "")).lower()
        return any(kw in y for kw in _BASELINE_DEMO_KEYWORDS)

    filtered = [e for e in edges if not _is_baseline_check(e)]
    n_filtered = len(edges) - len(filtered)
    if n_filtered:
        print(
            f"[Step 1] Filtered {n_filtered} baseline balance check edges",
            file=sys.stderr,
        )
    result["edges"] = filtered
    edges = filtered

    print(
        f"[Step 1] Found {len(edges)} edges from "
        f"{paper_info.get('first_author', '?')} {paper_info.get('year', '?')}",
        file=sys.stderr,
    )
    for i, e in enumerate(edges):
        sig = "✓" if e.get("significant") else "✗"
        print(
            f"  [{i+1}] {sig} {e.get('X', '?')[:40]} → {e.get('Y', '?')[:40]}"
            f"  ({e.get('source', '')})",
            file=sys.stderr,
        )
    return result


def step2_fill_one_edge(
    client: GLMClient,
    pdf_text: str,
    edge: Dict,
    paper_info: Dict,
    evidence_type: str,
    annotated_template: Dict,
    pdf_name: str,
    template_path: Optional[str] = None,
    hpp_dict_path: Optional[str] = None,
) -> Dict:
    prompt_template = _load_prompt("step2_fill_template")
    template_json = prepare_template_for_prompt(annotated_template)

    if template_path:
        template_with_hints = prepare_template_with_comments(template_path)
    else:
        template_with_hints = template_json

    replacements = {
        "{edge_index}": str(edge.get("edge_index", 1)),
        "{X}": str(edge.get("X", "")),
        "{C}": str(edge.get("C", "")),
        "{Y}": str(edge.get("Y", "")),
        "{subgroup}": str(edge.get("subgroup", "总体人群")),
        "{outcome_type}": str(edge.get("outcome_type", "")),
        "{effect_scale}": str(edge.get("effect_scale", "")),
        "{estimate}": str(edge.get("estimate", "null")),
        "{ci}": str(edge.get("ci", [None, None])),
        "{p_value}": str(edge.get("p_value", "null")),
        "{source}": str(edge.get("source", "")),
        "{first_author}": str(paper_info.get("first_author", "")),
        "{year}": str(paper_info.get("year", "")),
        "{doi}": str(paper_info.get("doi", "")),
        "{evidence_type}": evidence_type,
        "{template_json}": template_with_hints,
    }

    if hpp_dict_path:
        hpp_context = get_hpp_context(edge, dict_path=hpp_dict_path)
        print(
            f"         [HPP] Retrieved ~{len(hpp_context)//4} tokens of field context",
            file=sys.stderr,
        )
    else:
        hpp_context = (
            "(HPP data dictionary not configured. "
            "Fill hpp_mapping based on available information.)"
        )
    replacements["{hpp_context}"] = hpp_context

    # Apply replacements
    for placeholder, value in replacements.items():
        prompt_template = prompt_template.replace(placeholder, value)

    full_prompt = f"{prompt_template}\n\n---\n\n**Paper**\n\n{pdf_text[:500000]}"
    llm_output = client.call_json(full_prompt, max_tokens=32768)
    filled, is_valid, issues, fill_rate = build_filled_edge(
        annotated_template=annotated_template,
        llm_output=llm_output,
        edge=edge,
        paper_info=paper_info,
        evidence_type=evidence_type,
        pdf_name=pdf_name,
    )

    return filled


class EdgeExtractionPipeline:
    """
    Three-step pipeline:
      Step 0: Classify paper type
      Step 1: Enumerate all statistical edges
      Step 2: Fill each edge into the HPP template
    """

    def __init__(
        self,
        client: GLMClient,
        ocr_text_func: Callable[[str], str],
        ocr_init_func: Optional[Callable] = None,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
        hpp_dict_path: Optional[str] = None,
        template_path: Optional[str] = None,
    ):
        self.client = client
        self.ocr_text_func = ocr_text_func
        self.template_path = template_path or str(_DEFAULT_TEMPLATE)
        self.annotated_template = load_template(self.template_path)
        print(f"[Pipeline] Template: {self.template_path}", file=sys.stderr)

        if hpp_dict_path:
            self.hpp_dict_path = hpp_dict_path
        elif _DEFAULT_HPP_DICT.exists():
            self.hpp_dict_path = str(_DEFAULT_HPP_DICT)
        else:
            self.hpp_dict_path = None

        print(f"[Pipeline] HPP dict: {self.hpp_dict_path}", file=sys.stderr)

        if ocr_init_func is not None:
            ocr_init_func(
                ocr_output_dir=ocr_output_dir,
                client=client,
                dpi=ocr_dpi,
                validate_pages=ocr_validate_pages,
            )

    def _get_pdf_text(self, pdf_path: str) -> str:
        return self.ocr_text_func(pdf_path)

    def run(
        self,
        pdf_path: str,
        force_type: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> List[Dict]:
        pdf_name = Path(pdf_path).stem
        base_dir = Path(output_dir) if output_dir else None

        pdf_dir = None
        if base_dir:
            pdf_dir = base_dir / pdf_name
            pdf_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[Pipeline] Processing: {pdf_name}", file=sys.stderr)
        if pdf_dir:
            print(f"[Pipeline] Output folder: {pdf_dir}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        pdf_text = self._get_pdf_text(pdf_path)

        if force_type:
            evidence_type = force_type
            classification = {"primary_category": force_type, "forced": True}
            print(f"[Step 0] Forced type: {evidence_type}", file=sys.stderr)
        else:
            print("\n[Step 0] Classifying paper ...", file=sys.stderr)
            classification = step0_classify(self.client, pdf_text)
            evidence_type = classification.get("primary_category", "associational")

        save_json(pdf_dir / "step0_classification.json", classification)

        print("\n[Step 1] Enumerating edges ...", file=sys.stderr)
        step1_result = step1_enumerate_edges(self.client, pdf_text, evidence_type)
        edges_list = step1_result.get("edges", [])
        paper_info = step1_result.get("paper_info", {})
        save_json(pdf_dir / "step1_edges.json", step1_result)

        print(
            f"\n[Step 2] Filling templates for {len(edges_list)} edges ...",
            file=sys.stderr,
        )
        all_filled_edges: List[Dict] = []

        for i, edge in enumerate(edges_list):
            idx = edge.get("edge_index", i + 1)
            y_short = str(edge.get("Y", ""))[:50]
            print(
                f"\n  [{idx}/{len(edges_list)}] Filling: → {y_short} ...",
                file=sys.stderr,
            )

            filled = step2_fill_one_edge(
                client=self.client,
                pdf_text=pdf_text,
                edge=edge,
                paper_info=paper_info,
                evidence_type=evidence_type,
                annotated_template=self.annotated_template,
                pdf_name=pdf_name,
                template_path=self.template_path,
                hpp_dict_path=self.hpp_dict_path,
            )

            all_filled_edges.append(filled)

            eid = filled.get("edge_id", f"#{idx}")
            eq = filled.get("equation_type", "?")
            print(f"         Done: {eid} (equation_type={eq})", file=sys.stderr)

        output_file = pdf_dir / "edges.json"
        save_json(output_file, all_filled_edges)
        print(
            f"\n[Pipeline] Saved {len(all_filled_edges)} edges to: {output_file}",
            file=sys.stderr,
        )

        print(f"\n{'='*60}", file=sys.stderr)
        print(
            f"[Pipeline] Complete: {len(all_filled_edges)} edges extracted",
            file=sys.stderr,
        )
        print(f"{'='*60}\n", file=sys.stderr)

        return all_filled_edges

    def run_single_step(
        self,
        pdf_path: str,
        step: str,
        evidence_type: Optional[str] = None,
    ) -> Any:
        """Run a single pipeline step (classify or edges)."""
        pdf_text = self._get_pdf_text(pdf_path)

        if step == "classify":
            return step0_classify(self.client, pdf_text)

        if step == "edges":
            if not evidence_type:
                classification = step0_classify(self.client, pdf_text)
                evidence_type = classification["primary_category"]
            return step1_enumerate_edges(self.client, pdf_text, evidence_type)

        raise ValueError(f"Unknown step: {step}. Valid: classify, edges")
