"""
Evidence Card Extraction Pipeline

Complete workflow:
  Step 0: Document classification -> interventional / causal / mechanistic / associational
  Step 1: Path extraction (enumerate all Y + subgroup information)
  Step 2: Merge all paths into one card (containing all effects)
  Step 3: HPP platform field mapping

Key design:
  - Same paper, same core comparison -> 1 card
  - Subgroup stratification uses different edges in effects, does not split cards
  - Multiple paths from Step 1 are merged and passed to Step 2
"""
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .llm_client import GLMClient
from .base import BaseExtractor, Classifier
from .interventional import InterventionalExtractor
from .causal import CausalExtractor
from .mechanistic import MechanisticExtractor
from .associational import AssociationalExtractor


EXTRACTOR_MAP = {
    "interventional": InterventionalExtractor,
    "causal": CausalExtractor,
    "mechanistic": MechanisticExtractor,
    "associational": AssociationalExtractor,
}


def _merge_paths_to_target(paths: List) -> str:
    """
    Merge multiple paths from Step 1 into a complete target description.
    Passed to Step 2 as {target_path} variable.

    Strategy: Merge all Y variables + all subgroup information, deduplicate and generate unified description.
    """
    if not paths:
        return ""

    # Single path, use directly
    if len(paths) == 1:
        return json.dumps(paths[0], ensure_ascii=False)

    # Multiple paths -> merge Y and subgroups
    all_Y = []
    all_subgroups = []
    contrast = ""
    X = ""
    C = ""
    sources = []
    claims = []

    for p in paths:
        if isinstance(p, str):
            claims.append(p)
            continue

        if not contrast and p.get("contrast"):
            contrast = p["contrast"]
        if not X and p.get("X"):
            X = p["X"]
        if not C and p.get("C"):
            C = p["C"]

        for y in p.get("Y", []):
            if y not in all_Y:
                all_Y.append(y)

        for sg in p.get("subgroups", []):
            if sg not in all_subgroups:
                all_subgroups.append(sg)

        if p.get("source"):
            sources.append(p["source"])
        if p.get("claim"):
            claims.append(p["claim"])

    merged = {
        "contrast": contrast,
        "X": X,
        "C": C,
        "Y": all_Y,
        "subgroups": all_subgroups,
        "source": "; ".join(dict.fromkeys(sources)),
        "claim": " | ".join(claims),
    }
    return json.dumps(merged, ensure_ascii=False)


class EvidenceCardPipeline:
    """End-to-end evidence card extraction pipeline"""

    def __init__(
        self,
        client: GLMClient,
        ocr_text_func=None,
        ocr_init_func=None,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
    ):
        self.client = client
        self.classifier = Classifier(client)

        # Initialize OCR and inject into BaseExtractor
        if ocr_init_func is not None:
            ocr_init_func(
                ocr_output_dir=ocr_output_dir,
                client=client,
                dpi=ocr_dpi,
                validate_pages=ocr_validate_pages,
            )
        if ocr_text_func is not None:
            BaseExtractor.set_ocr_func(ocr_text_func)

    def run(
        self,
        pdf_path: str,
        force_type: Optional[str] = None,
        skip_hpp: bool = False,
        output_dir: Optional[str] = None,
    ) -> List[Dict]:
        """
        Run the complete pipeline.

        Key: Output 1 card per paper (unless there are multiple completely independent X vs C comparisons).
        """
        pdf_name = Path(pdf_path).stem
        out_dir = Path(output_dir) if output_dir else None

        # Step 0: Classification
        if force_type:
            evidence_type = force_type
            classification = {"primary_category": force_type, "forced": True}
            print("[Pipeline] Forced type: {evidence_type}", file=sys.stderr)
        else:
            print("[Pipeline] Step 0: Document classification...", file=sys.stderr)
            classification = self.classifier.classify(pdf_path)
            evidence_type = classification.get("primary_category", "associational")
            print(
                f"  Type: {evidence_type} (confidence: {classification.get('confidence', 'N/A')})",
                file=sys.stderr,
            )

        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(
                out_dir / f"{pdf_name}_classification.json", "w", encoding="utf-8"
            ) as f:
                json.dump(classification, f, ensure_ascii=False, indent=2)

        # Get extractor
        if evidence_type not in EXTRACTOR_MAP:
            print(
                f"  Unknown type {evidence_type}, falling back to associational",
                file=sys.stderr,
            )
            evidence_type = "associational"

        extractor = EXTRACTOR_MAP[evidence_type](self.client)

        # Step 1: Extract paths
        print("[Pipeline] Step 1: Extracting paths...", file=sys.stderr)
        paths = extractor.extract_paths(pdf_path)
        if isinstance(paths, dict):
            paths = [paths]
        print(f"  Found {len(paths)} paths", file=sys.stderr)
        for i, p in enumerate(paths):
            y_list = p.get("Y", []) if isinstance(p, dict) else []
            sg_list = p.get("subgroups", []) if isinstance(p, dict) else []
            print(
                f"  [{i+1}] Y={len(y_list)} variables, subgroups={len(sg_list)} items",
                file=sys.stderr,
            )

        if out_dir:
            with open(out_dir / f"{pdf_name}_paths.json", "w", encoding="utf-8") as f:
                json.dump(paths, f, ensure_ascii=False, indent=2)

        # Step 2: Merge paths into one card
        merged_target = _merge_paths_to_target(paths)
        print(
            f"\n[Pipeline] Step 2: Building evidence cards (merging {len(paths)} paths into 1 card)...",
            file=sys.stderr,
        )

        cards = extractor.extract_evidence_card(pdf_path, merged_target)
        if isinstance(cards, dict):
            cards = [cards]

        n_effects = sum(len(c.get("effects", [])) for c in cards)
        print(
            f"  Generated {len(cards)} cards with {n_effects} effects", file=sys.stderr
        )

        # Step 3: HPP mapping
        if not skip_hpp:
            for j, card in enumerate(cards):
                print(
                    f"\n[Pipeline] Step 3: HPP mapping [{j+1}/{len(cards)}]...",
                    file=sys.stderr,
                )
                card = extractor.extract_hpp_mapping(card)
                cards[j] = card

        # Save results
        if out_dir and cards:
            output_file = out_dir / f"{pdf_name}_evidence_cards.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(cards, f, ensure_ascii=False, indent=2)
            print(
                f"\n[Pipeline] Saved {len(cards)} evidence cards to: {output_file}",
                file=sys.stderr,
            )

        return cards

    def run_single_step(
        self,
        pdf_path: str,
        step: str,
        evidence_type: Optional[str] = None,
        target_path: Optional[str] = None,
    ) -> any:
        """Run a single step"""
        if step == "classify":
            return self.classifier.classify(pdf_path)

        if not evidence_type:
            classification = self.classifier.classify(pdf_path)
            evidence_type = classification["primary_category"]

        extractor = EXTRACTOR_MAP.get(evidence_type, AssociationalExtractor)(
            self.client
        )

        if step == "paths":
            return extractor.extract_paths(pdf_path)
        elif step == "card":
            if not target_path:
                raise ValueError("Step 'card' requires target_path parameter")
            return extractor.extract_evidence_card(pdf_path, target_path)
        elif step == "hpp":
            if not target_path:
                raise ValueError("Step 'hpp' requires evidence card JSON path")
            with open(target_path, "r", encoding="utf-8") as f:
                card = json.load(f)
            return extractor.extract_hpp_mapping(card)
        else:
            raise ValueError(f"Unknown step: {step}")
