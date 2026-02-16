"""
证据卡提取流水线

完整流程：
  Step 0: 文献分类 → interventional / causal / mechanistic / associational
  Step 1: 路径提取（穷举所有 Y + 亚组信息）
  Step 2: 全部路径合并为一张卡（含所有 effects）
  Step 3: HPP 平台字段映射

关键设计：
  - 同一篇论文同一核心对比 → 1 张卡
  - 亚组分层在 effects 中用不同 edge 表示，不拆卡
  - Step1 的多条 path 合并后传给 Step2
"""
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from llm_client import GLMClient
from ocr import init_extractor as init_ocr
from base import Classifier
from interventional import InterventionalExtractor
from causal import CausalExtractor
from mechanistic import MechanisticExtractor
from associational import AssociationalExtractor


EXTRACTOR_MAP = {
    "interventional": InterventionalExtractor,
    "causal": CausalExtractor,
    "mechanistic": MechanisticExtractor,
    "associational": AssociationalExtractor,
}


def _merge_paths_to_target(paths: List) -> str:
    """
    将 Step1 输出的多条 path 合并成一个完整的 target 描述。
    传给 Step2 作为 {target_path} 变量。

    策略：合并所有 Y 变量 + 所有亚组信息，去重后生成统一描述。
    """
    if not paths:
        return ""

    # 单条 path 直接用
    if len(paths) == 1:
        return json.dumps(paths[0], ensure_ascii=False)

    # 多条 path → 合并 Y 和 subgroups
    all_Y = []
    all_subgroups = []
    contrast = ""
    X = ""
    C = ""
    sources = []
    claims = []

    for p in paths:
        if isinstance(p, str):
            # 如果是字符串，直接拼接
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
        "source": "; ".join(dict.fromkeys(sources)),  # 去重保序
        "claim": " | ".join(claims),
    }
    return json.dumps(merged, ensure_ascii=False)


class EvidenceCardPipeline:
    """端到端的证据卡提取流水线"""

    def __init__(
        self,
        client: GLMClient,
        ocr_output_dir: str = "./ocr_cache",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
    ):
        self.client = client
        self.classifier = Classifier(client)

        init_ocr(
            ocr_output_dir=ocr_output_dir,
            dpi=ocr_dpi,
            validate_pages=ocr_validate_pages,
        )

    def run(
        self,
        pdf_path: str,
        force_type: Optional[str] = None,
        skip_hpp: bool = False,
        output_dir: Optional[str] = None,
    ) -> List[Dict]:
        """
        运行完整流水线。

        关键：同一论文输出 1 张卡（除非有多个完全独立的 X vs C 对比）。
        """
        pdf_name = Path(pdf_path).stem
        out_dir = Path(output_dir) if output_dir else None

        # ── Step 0: 分类 ──
        if force_type:
            evidence_type = force_type
            classification = {"primary_category": force_type, "forced": True}
            print(f"[Pipeline] 强制类型: {evidence_type}", file=sys.stderr)
        else:
            print(f"[Pipeline] Step 0: 文献分类...", file=sys.stderr)
            classification = self.classifier.classify(pdf_path)
            evidence_type = classification.get("primary_category", "associational")
            print(f"  类型: {evidence_type} (置信度: {classification.get('confidence', 'N/A')})", file=sys.stderr)

        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / f"{pdf_name}_classification.json", "w", encoding="utf-8") as f:
                json.dump(classification, f, ensure_ascii=False, indent=2)

        # ── 获取提取器 ──
        if evidence_type not in EXTRACTOR_MAP:
            print(f"  未知类型 {evidence_type}，回退到 associational", file=sys.stderr)
            evidence_type = "associational"

        extractor = EXTRACTOR_MAP[evidence_type](self.client)

        # ── Step 1: 提取路径 ──
        print(f"\n[Pipeline] Step 1: 提取路径...", file=sys.stderr)
        paths = extractor.extract_paths(pdf_path)
        if isinstance(paths, dict):
            paths = [paths]
        print(f"  发现 {len(paths)} 条路径", file=sys.stderr)
        for i, p in enumerate(paths):
            y_list = p.get("Y", []) if isinstance(p, dict) else []
            sg_list = p.get("subgroups", []) if isinstance(p, dict) else []
            print(f"  [{i+1}] Y={len(y_list)} 个变量, subgroups={len(sg_list)} 个", file=sys.stderr)

        if out_dir:
            with open(out_dir / f"{pdf_name}_paths.json", "w", encoding="utf-8") as f:
                json.dump(paths, f, ensure_ascii=False, indent=2)

        # ── Step 2: 合并路径 → 一张卡 ──
        merged_target = _merge_paths_to_target(paths)
        print(f"\n[Pipeline] Step 2: 构建证据卡（合并 {len(paths)} 条路径为 1 张卡）...", file=sys.stderr)

        try:
            cards = extractor.extract_evidence_card(pdf_path, merged_target)
            if isinstance(cards, dict):
                cards = [cards]
        except Exception as e:
            print(f"  ❌ 提取失败: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            return []

        n_effects = sum(len(c.get("effects", [])) for c in cards)
        print(f"  生成 {len(cards)} 张卡, 共 {n_effects} 条 effects", file=sys.stderr)

        # ── Step 3: HPP 映射 ──
        if not skip_hpp:
            for j, card in enumerate(cards):
                print(f"\n[Pipeline] Step 3: HPP 映射 [{j+1}/{len(cards)}]...", file=sys.stderr)
                try:
                    card = extractor.extract_hpp_mapping(card)
                except Exception as e:
                    print(f"  ⚠️ HPP 映射失败: {e}", file=sys.stderr)
                cards[j] = card

        # ── 保存 ──
        if out_dir and cards:
            output_file = out_dir / f"{pdf_name}_evidence_cards.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(cards, f, ensure_ascii=False, indent=2)
            print(f"\n✅ 已保存 {len(cards)} 张证据卡到: {output_file}", file=sys.stderr)

        return cards

    def run_single_step(
        self,
        pdf_path: str,
        step: str,
        evidence_type: Optional[str] = None,
        target_path: Optional[str] = None,
    ) -> any:
        """运行单个步骤"""
        if step == "classify":
            return self.classifier.classify(pdf_path)

        if not evidence_type:
            classification = self.classifier.classify(pdf_path)
            evidence_type = classification["primary_category"]

        extractor = EXTRACTOR_MAP.get(evidence_type, AssociationalExtractor)(self.client)

        if step == "paths":
            return extractor.extract_paths(pdf_path)
        elif step == "card":
            if not target_path:
                raise ValueError("Step 'card' 需要 target_path 参数")
            return extractor.extract_evidence_card(pdf_path, target_path)
        elif step == "hpp":
            if not target_path:
                raise ValueError("Step 'hpp' 需要证据卡 JSON 路径")
            with open(target_path, "r", encoding="utf-8") as f:
                card = json.load(f)
            return extractor.extract_hpp_mapping(card)
        else:
            raise ValueError(f"未知步骤: {step}")
