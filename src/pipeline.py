"""
Evidence Edge Extraction Pipeline (Template-Fill Architecture)

Design:
  Step 0: Classify paper type (interventional / causal / mechanistic / associational)
  Step 1: Enumerate all X→Y edges from the paper
  Step 2: For each edge, fill the HPP unified template via LLM

Key principles:
  - One edge = one X→Y statistical relationship = one LLM call
  - LLM fills a pre-defined template (no free-form generation)
  - No try/except; all operations assumed to succeed on the happy path
"""

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .llm_client import GLMClient

# Directories
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SRC_DIR.parent
_PROMPTS_DIR = _PROJECT_DIR / "prompts"
_TEMPLATES_DIR = _PROJECT_DIR / "templates"


def _load_prompt(name: str) -> str:
    """Load a prompt markdown file from prompts/ directory."""
    path = _PROMPTS_DIR / f"{name}.md"
    assert path.exists(), f"Prompt file not found: {path}"
    return path.read_text(encoding="utf-8")


def _load_template() -> Dict:
    """Load the HPP unified template JSON."""
    path = _TEMPLATES_DIR / "hpp_mapping_template.json"
    assert path.exists(), f"Template not found: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_template_for_prompt(template: Dict) -> Dict:
    """Remove internal metadata keys (starting with _) from template for cleaner prompt."""
    cleaned = {}
    for k, v in template.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            cleaned[k] = _clean_template_for_prompt(v)
        else:
            cleaned[k] = v
    return cleaned


def step0_classify(client: GLMClient, pdf_text: str) -> Dict[str, Any]:
    """Classify the paper into one of four evidence types."""
    prompt_template = _load_prompt("step0_classify")
    full_prompt = (
        f"{prompt_template}\n\n---\n\n**论文全文如下：**\n\n{pdf_text[:500000]}"
    )
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
    """Extract all X→Y edges from the paper."""
    prompt_template = _load_prompt("step1_edges")
    # Inject evidence_type into the prompt
    prompt_template = prompt_template.replace("{evidence_type}", evidence_type)
    full_prompt = (
        f"{prompt_template}\n\n---\n\n**论文全文如下：**\n\n{pdf_text[:500000]}"
    )

    result = client.call_json(full_prompt, max_tokens=32768)

    edges = result.get("edges", [])
    paper_info = result.get("paper_info", {})
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
    template: Dict,
    pdf_name: str,
) -> Dict:
    """Fill the HPP template for a single edge."""
    prompt_template = _load_prompt("step2_fill_template")

    # Build template JSON string (cleaned of internal _ keys)
    clean_tmpl = _clean_template_for_prompt(template)
    template_json_str = json.dumps(clean_tmpl, indent=2, ensure_ascii=False)

    # Substitute variables into prompt
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
        "{template_json}": template_json_str,
    }
    for placeholder, value in replacements.items():
        prompt_template = prompt_template.replace(placeholder, value)

    full_prompt = (
        f"{prompt_template}\n\n---\n\n**论文全文如下：**\n\n{pdf_text[:180000]}"
    )

    result = client.call_json(full_prompt, max_tokens=32768)

    # Post-process: inject provenance if not set
    if "provenance" not in result or not result["provenance"].get("pdf_name"):
        result["provenance"] = {
            "pdf_name": pdf_name,
            "page": None,
            "table_or_figure": edge.get("source", None),
            "extractor": "llm",
        }

    return result


def _postprocess_edge(edge_json: Dict) -> Dict:
    """Apply deterministic rules that don't need LLM."""

    # --- Infer equation_type from hints if missing ---
    hints = edge_json.get("equation_inference_hints", {})
    eq_type = edge_json.get("equation_type")

    if not eq_type:
        if hints.get("has_joint_intervention"):
            eq_type = "E6"
        elif hints.get("has_counterfactual_query"):
            eq_type = "E5"
        elif hints.get("has_mediator"):
            eq_type = "E4"
        elif hints.get("has_survival_outcome"):
            eq_type = "E2"
        elif hints.get("has_longitudinal_timepoints"):
            eq_type = "E3"
        else:
            eq_type = "E1"
        edge_json["equation_type"] = eq_type

    # --- Ensure modeling_directives consistency ---
    md = edge_json.get("modeling_directives", {})
    eq_num = eq_type.replace("E", "e")  # "E1" -> "e1"
    for key in ["e1", "e2", "e3", "e4", "e5", "e6"]:
        if key in md and isinstance(md[key], dict):
            md[key]["enabled"] = (key == eq_num) or md[key].get("enabled", False)

    # --- Ensure schema_version ---
    if "schema_version" not in edge_json:
        edge_json["schema_version"] = "1.1"
    if "equation_version" not in edge_json:
        edge_json["equation_version"] = "1.0"

    return edge_json


class EdgeExtractionPipeline:
    """
    End-to-end pipeline: PDF → list of HPP-template-conformant edge JSONs.

    Usage:
        pipeline = EdgeExtractionPipeline(client, ocr_func)
        edges = pipeline.run("paper.pdf", output_dir="./output")
    """

    def __init__(
        self,
        client: GLMClient,
        ocr_text_func: Callable[[str], str],
        ocr_init_func: Optional[Callable] = None,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
    ):
        self.client = client
        self.ocr_text_func = ocr_text_func
        self.template = _load_template()

        # Initialize OCR module if init function provided
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
        """
        Run the full pipeline on a PDF.

        Returns a list of edge JSONs conforming to hpp_mapping_template.
        """
        pdf_name = Path(pdf_path).stem
        out_dir = Path(output_dir) if output_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[Pipeline] Processing: {pdf_name}", file=sys.stderr)
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

        if out_dir:
            _save_json(
                out_dir / f"{pdf_name}_step0_classification.json", classification
            )

        print("\n[Step 1] Enumerating edges ...", file=sys.stderr)
        step1_result = step1_enumerate_edges(self.client, pdf_text, evidence_type)
        edges_list = step1_result.get("edges", [])
        paper_info = step1_result.get("paper_info", {})

        if out_dir:
            _save_json(out_dir / f"{pdf_name}_step1_edges.json", step1_result)

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
                template=self.template,
                pdf_name=pdf_name,
            )

            filled = _postprocess_edge(filled)
            all_filled_edges.append(filled)

            eid = filled.get("edge_id", f"#{idx}")
            eq = filled.get("equation_type", "?")
            print(f"         Done: {eid} (equation_type={eq})", file=sys.stderr)

        if out_dir:
            output_file = out_dir / f"{pdf_name}_edges.json"
            _save_json(output_file, all_filled_edges)
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
        """Run only one step of the pipeline."""
        pdf_text = self._get_pdf_text(pdf_path)

        if step == "classify":
            return step0_classify(self.client, pdf_text)

        if step == "edges":
            if not evidence_type:
                classification = step0_classify(self.client, pdf_text)
                evidence_type = classification["primary_category"]
            return step1_enumerate_edges(self.client, pdf_text, evidence_type)

        raise ValueError(f"Unknown step: {step}. Valid: classify, edges")


def _save_json(path: Path, data: Any) -> None:
    """Write data as pretty-printed JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> Saved: {path}", file=sys.stderr)
