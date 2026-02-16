"""
证据卡提取流水线

完整流程：
  Step 0: 文献分类 → interventional / causal / mechanistic / associational
  Step 1: 路径/对比提取
  Step 2: 以路径为单位构建证据卡
  Step 3: HPP 平台字段映射
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


class EvidenceCardPipeline:
    """端到端的证据卡提取流水线"""

    def __init__(
        self,
        client: GLMClient,
        ocr_output_dir: str = "./cache_ocr",
        ocr_dpi: int = 200,
        ocr_validate_pages: bool = True,
    ):
        self.client = client
        self.classifier = Classifier(client)

        # 初始化 OCR 模块单例，后续所有 get_pdf_text() 调用都会使用此配置
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
        skip_classify: bool = False,
        output_dir: Optional[str] = None,
    ) -> List[Dict]:
        """
        运行完整流水线

        Args:
            pdf_path: PDF 文件路径
            force_type: 强制指定类型，跳过分类
            skip_hpp: 是否跳过 HPP 映射
            skip_classify: 是否跳过分类（需要 force_type）
            output_dir: 输出目录，None 则不保存文件

        Returns:
            证据卡列表
        """
        pdf_name = Path(pdf_path).stem

        # Step 0: 分类
        if force_type:
            evidence_type = force_type
            classification = {"primary_category": force_type, "forced": True}
            print(f"[Pipeline] 强制类型: {evidence_type}", file=sys.stderr)
        else:
            print(f"[Pipeline] Step 0: 文献分类...", file=sys.stderr)
            classification = self.classifier.classify(pdf_path)
            evidence_type = classification.get("primary_category", "associational")
            print(f"  类型: {evidence_type} (置信度: {classification.get('confidence', 'N/A')})", file=sys.stderr)
            print(f"  信号: {classification.get('category_signals', [])}", file=sys.stderr)

        # 保存分类结果
        if output_dir:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / f"{pdf_name}_classification.json", "w", encoding="utf-8") as f:
                json.dump(classification, f, ensure_ascii=False, indent=2)

        # 获取对应的提取器
        if evidence_type not in EXTRACTOR_MAP:
            print(f"  未知类型 {evidence_type}，回退到 associational", file=sys.stderr)
            evidence_type = "associational"

        extractor_cls = EXTRACTOR_MAP[evidence_type]
        extractor = extractor_cls(self.client)

        # Step 1: 提取路径
        print(f"\n[Pipeline] Step 1: 提取路径...", file=sys.stderr)
        paths = extractor.extract_paths(pdf_path)
        if isinstance(paths, dict):
            paths = [paths]
        print(f"  发现 {len(paths)} 条路径", file=sys.stderr)

        if output_dir:
            with open(out_dir / f"{pdf_name}_paths.json", "w", encoding="utf-8") as f:
                json.dump(paths, f, ensure_ascii=False, indent=2)

        # Step 2 & 3: 逐条提取证据卡
        all_cards = []
        for i, path in enumerate(paths):
            path_str = path if isinstance(path, str) else json.dumps(path, ensure_ascii=False)
            print(f"\n[Pipeline] Step 2: 证据卡 [{i+1}/{len(paths)}]: {path_str[:100]}", file=sys.stderr)

            try:
                cards = extractor.extract_evidence_card(pdf_path, path_str)
                if isinstance(cards, dict):
                    cards = [cards]
            except Exception as e:
                print(f"  ❌ 提取失败: {e}", file=sys.stderr)
                continue

            # Step 3: HPP 映射
            if not skip_hpp:
                for j, card in enumerate(cards):
                    print(f"  Step 3: HPP 映射 [{j+1}/{len(cards)}]...", file=sys.stderr)
                    try:
                        card = extractor.extract_hpp_mapping(card)
                    except Exception as e:
                        print(f"  ⚠️ HPP 映射失败: {e}", file=sys.stderr)
                    cards[j] = card

            all_cards.extend(cards)

        # 保存最终结果
        if output_dir and all_cards:
            output_file = out_dir / f"{pdf_name}_evidence_cards.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_cards, f, ensure_ascii=False, indent=2)
            print(f"\n✅ 已保存 {len(all_cards)} 张证据卡到: {output_file}", file=sys.stderr)

        return all_cards

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

        extractor_cls = EXTRACTOR_MAP.get(evidence_type, AssociationalExtractor)
        extractor = extractor_cls(self.client)

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