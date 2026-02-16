# Step 3: HPP 平台字段映射

将证据卡中的 X, Y, Z, M 变量映射到 HPP (Human Phenotype Project) 平台的数据集和字段。

## 输入
上一阶段输出的证据卡 JSON。

## ★ 核心原则：诚实映射
- **严禁编造 HPP 中不存在的字段名**
- HPP 是一个特定的健康队列数据库，不是通用医学数据库
- HPP 有 CGM 数据但**没有** OGTT 数据（没有 glucose_ogtt_30min 这类字段）
- HPP 有空腹血糖（016-blood_tests）但**没有**胰岛素测量
- 如果论文变量在 HPP 中无法精确对应，必须如实标为 close/tentative/missing

## HPP 已知数据集及实际字段

| 数据集 | 已知字段 | 备注 |
|--------|---------|------|
| 000-population | age, sex, ethnicity | 人口统计 |
| 001-events | event_type, event_date | 事件 |
| 002-anthropometrics | height, weight, bmi, waist_circumference | 人体测量 |
| 003-blood_pressure | systolic_bp, diastolic_bp | 血压 |
| 004-body_composition | body_fat_pct, lean_mass | 体成分 |
| 005-diet_logging | local_timestamp, calories, meal_type | 饮食 |
| 009-sleep | sleep_duration, bedtime, wake_time | 睡眠 |
| 014-human_genetics | gencove_vcf, variants_qc_parquet | 基因组 |
| 016-blood_tests | glucose (空腹), hba1c, hdl, ldl, triglycerides | 血液 |
| 017-cgm | cgm_mean, cgm_auc, cgm_cv, cgm_mage | CGM |
| 020-health_and_medical_history | diagnosis, medication | 病史 |
| 021-medical_conditions | icd11_code, condition_name | 诊断 |
| 023-lifestyle_and_environment | physical_activity, smoking | 生活方式 |

详细字段请参考: https://knowledgebase.pheno.ai/datasets.html

## status 规则
- **exact**: 定义、单位、测量方式完全一致（如 空腹血糖 → 016-blood_tests.glucose）
- **close**: 概念相关但测量方式不同（如 OGTT glucose AUC → 017-cgm.cgm_auc，notes 说明 CGM 非 OGTT）
- **derived**: 需计算才能得到（如 dinner timing → 005-diet_logging.local_timestamp + 009-sleep.bedtime）
- **tentative**: 仅概念相近，实际不可替代（如 OGTT insulin → HPP 无胰岛素数据）
- **missing**: HPP 中完全无此类数据

**重要判断规则**：
- 论文用 OGTT 测血糖，HPP 只有 CGM → status = **close**（不是 derived）
- 论文用胰岛素测量，HPP 无胰岛素 → status = **tentative** 或 **missing**
- 论文用基因分型，HPP 有 VCF 文件需提取 → status = **derived**（notes 说明需从 VCF 提取）

## 输出格式
更新证据卡中的 hpp_mapping 字段：

```json
{
  "hpp_mapping": {
    "X": {
      "name": "变量名", "dataset": "数据集", "field": "字段名",
      "status": "exact | close | derived | tentative | missing",
      "notes": "映射说明、差异、计算方式"
    },
    "C": {
      "name": "对照变量名", "dataset": "...", "field": "...",
      "status": "...", "notes": "..."
    },
    "Y": [
      {"name": "结局1", "dataset": "...", "field": "...", "status": "...", "notes": "..."}
    ],
    "Z": [
      {"name": "协变量", "dataset": "...", "field": "...", "status": "...", "notes": "..."}
    ],
    "M": []
  }
}
```

## 验证前提
- hpp_mapping.X.status 和 hpp_mapping.Y[].status 必须为 exact/close/derived 才能进入 EL-GSE 验证
- tentative/missing 意味着该变量无法被 HPP 验证——这是诚实的结论，不要为了"好看"而虚标 derived
