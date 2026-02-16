# 证据卡 2月16版本

## 完整流水线：分类 → 提取路径 → 证据卡 → HPP映射

python main.py full ../paper.pdf --output ../output

## 强制指定类型，跳过HPP映射

python main.py full ../paper.pdf --type interventional --skip-hpp -o ../output

## 单步运行

```shell
python main.py classify ../paper.pdf                          # Step 0: 分类
python main.py paths ../paper.pdf --type interventional        # Step 1: 提路径
python main.py card ../paper.pdf --type interventional \       # Step 2: 证据卡
  --target "Late dinner vs Early dinner -> Glucose AUC"
python main.py hpp ../paper.pdf --type interventional \        # Step 3: HPP映射
  --target ../output/paper_evidence_cards.json
```

## 3. 流水线执行流程

```
paper.pdf
  │
  ├─ Step 0: Classifier 读PDF → GLM判断类型 → interventional/causal/mechanistic/associational
  │
  ├─ Step 1: Extractor.extract_paths() → 提取 X vs C → 所有Y + 亚组
  │
  ├─ Step 2: 合并paths → Extractor.extract_evidence_card() → 完整证据卡JSON
  │
  └─ Step 3: Extractor.extract_hpp_mapping() → 映射到HPP平台字段
  
输出: output/paper_evidence_cards.json
```