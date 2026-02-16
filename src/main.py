#!/usr/bin/env python3
"""
医学文献证据卡提取工具 - 命令行入口

用法：
  # 完整流水线（自动分类 → 提取路径 → 证据卡 → HPP映射）
  python main.py full paper.pdf --output ./output

  # 仅分类
  python main.py classify paper.pdf

  # 仅提取路径（指定类型）
  python main.py paths paper.pdf --type interventional

  # 仅提取证据卡（指定路径）
  python main.py card paper.pdf --type interventional --target "Late dinner vs Early dinner → Glucose AUC"

  # 完整流水线（强制类型，跳过HPP映射）
  python main.py full paper.pdf --type interventional --skip-hpp --output ./output
"""
import argparse
import json
import sys

from llm_client import GLMClient
from pipeline import EvidenceCardPipeline
from config import DEFAULT_MODEL


def main():
    parser = argparse.ArgumentParser(
        description="医学文献证据卡提取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "step",
        choices=["full", "classify", "paths", "card", "hpp"],
        help="执行步骤: full(完整流水线), classify(分类), paths(提取路径), card(证据卡), hpp(HPP映射)",
    )
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="GLM 模型名称")
    parser.add_argument("--api-key", help="智谱AI API Key")
    parser.add_argument("--base-url", help="API Base URL")
    parser.add_argument(
        "--type",
        choices=["interventional", "causal", "mechanistic", "associational"],
        help="强制指定文献类型（跳过自动分类）",
    )
    parser.add_argument("--target", help="目标路径（用于 card/hpp 步骤）")
    parser.add_argument("--output", "-o", help="输出目录")
    parser.add_argument("--skip-hpp", action="store_true", help="跳过 HPP 映射")
    parser.add_argument("--ocr-dir", default="./cache_ocr", help="OCR 缓存目录 (默认 ./ocr_cache)")
    parser.add_argument("--dpi", type=int, default=200, help="PDF 转图片 DPI (默认 200)")
    parser.add_argument("--no-validate-pages", action="store_true", help="跳过 OCR 正文页验证")
    parser.add_argument("--force-ocr", action="store_true", help="强制重跑 OCR（忽略缓存）")
    parser.add_argument("--pretty", action="store_true", default=True, help="美化 JSON 输出")

    args = parser.parse_args()

    # 初始化客户端
    client = GLMClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    pipeline = EvidenceCardPipeline(
        client,
        ocr_output_dir=args.ocr_dir,
        ocr_dpi=args.dpi,
        ocr_validate_pages=not args.no_validate_pages,
    )

    try:
        if args.step == "full":
            result = pipeline.run(
                pdf_path=args.pdf,
                force_type=args.type,
                skip_hpp=args.skip_hpp,
                output_dir=args.output or ".",
            )
        else:
            result = pipeline.run_single_step(
                pdf_path=args.pdf,
                step=args.step,
                evidence_type=args.type,
                target_path=args.target,
            )

        # 输出到 stdout
        indent = 2 if args.pretty else None
        print(json.dumps(result, ensure_ascii=False, indent=indent))

    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()