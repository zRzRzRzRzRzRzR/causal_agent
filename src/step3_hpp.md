# Step 3: HPP 平台字段映射

你需要将证据卡中的 X, Y, Z, M 变量映射到 HPP (Human Phenotype Project) 平台的具体数据集和字段。

## 输入
上一阶段输出的证据卡 JSON。

## HPP 数据集参考
| 编号 | 数据集 | 典型字段举例 |
|------|--------|-------------|
| 000-population | 人口统计 | age, sex, ethnicity |
| 001-events | 事件/结局 | event_type, event_date |
| 002-anthropometrics | 人体测量 | height, weight, bmi |
| 003-blood_pressure | 血压 | systolic_bp, diastolic_bp |
| 004-body_composition | 体成分 | body_fat_pct, lean_mass |
| 005-diet_logging | 饮食记录 | local_timestamp, calories, meal_type |
| 009-sleep | 睡眠 | sleep_duration, bedtime |
| 014-human_genetics | 基因组 | gencove_vcf, variants_qc_parquet |
| 016-blood_tests | 血液检测 | hdl_cholesterol, glucose, hba1c |
| 017-cgm | 连续血糖监测 | cgm_mean, cgm_auc, cgm_cv |
| 020-health_and_medical_history | 健康病史 | diagnosis, medication |
| 021-medical_conditions | 医学状况 | icd11_code, condition_name |
| 023-lifestyle_and_environment | 生活方式 | physical_activity, smoking |

具体字段名请参考: https://knowledgebase.pheno.ai/datasets.html

## status 规则
- **exact**: 定义、单位、测量方式与时间锚点均一致 → ✅ 可验证
- **close**: 概念一致但单位/量表/设备略有差异 → ✅ 可验证（notes 中说明差异）
- **derived**: 需由多个 HPP 字段计算/聚合 → ✅ 可验证（notes 中给出公式）
- **missing**: HPP 中无此变量 → ❌ 无法验证

## 输出格式
更新证据卡中的 hpp_mapping 字段，严格遵循以下结构：

```json
{
  "hpp_mapping": {
    "X": {
      "name": "暴露变量名（与variables.roles.X一致）",
      "dataset": "HPP数据集名",
      "field": "具体字段名",
      "status": "exact | close | derived | missing",
      "notes": "映射说明、差异、计算方式等"
    },
    "Y": [
      {
        "name": "结局变量名",
        "dataset": "...",
        "field": "...",
        "status": "...",
        "notes": "..."
      }
    ],
    "Z": [
      {
        "name": "协变量名",
        "dataset": "...",
        "field": "...",
        "status": "...",
        "notes": "..."
      }
    ],
    "M": []
  }
}
```

## 验证必要条件
hpp_mapping.X.status 和 hpp_mapping.Y[].status 必须为 exact/close/derived 才能进入 EL-GSE 验证流程。如果某变量在 HPP 中确实不存在，填 missing 并在 notes 中说明。
