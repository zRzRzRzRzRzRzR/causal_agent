# Review 阶段诊断 — 为什么 00 没事，51 出问题

## TL;DR

你同学说"问题在 review"，**只对一半**：

- review 步骤本身确实有几个独立的 bug，会在 **51 类论文上放大错误**，最显眼的是 **rerank 强行从 6 个不相关候选里挑一个并自信地标成 "close/exact"**。
- 但更根本的问题是 **review 没有当好"门卫"**：上游 (Step 1/2) 在 51 这种 HPP 词典覆盖不到的论文上漏出了占位符 / Pi 标签 / paper_title 不一致，review 只是把它们当 warning 记下，**没有拦截或回流修复**。
- 00 之所以"没事"，主要是 **HPP 词典是按 00 类论文（睡眠/BMI/T2D/生活方式）拟合的**，rerank 候选集刚好包含正确答案，掩盖了 review 的所有结构缺陷。

下面的证据都来自 `result_mm/00/*` 和 `result_mm/51/*` 的现有产物 + `src/review.py` 的代码。

---

## 1. 数据扫描：review 在 00 和 51 的"看法"

```
paper                                  edges  valid  sem  fill   consistency                  spot_check                       rerank
00/Healthy Lifestyle (UK)                 48    48    47  0.94   {warn:8, info:1}             {correct:5}                      65
00/Sleep Duration & Variability (BMI)      4     4     4  0.86   {}                           {correct:2}                       5
00/Clinical utility sleep T2D             15    15     6  0.90   {err:1, warn:9, info:1}      {correct:2, incorrect:3}         23
00/4- & 6-h TRF                           15    15    15  0.89   {info:1, warn:1}             {correct:5}                      26
00/Healthy Heroes shift workers           29    29    28  0.89   {warn:15}                    {unknown:1}                      44
51/12076551 (F&V RCT)                     13    13    10  0.93   {info:1}                     {correct:5}                      24
51/18054551 (sleep/GERD)                  11    11    11  0.88   {err:1, warn:6}              {correct:3, incorrect:2}         16
51/19111551 (gluten/celiac)                5     5     5  0.87   {info:1}                     {unknown:1}  ← 全部 theta=None    7
51/21903251 (malaria/bacteraemia)         28    28    27  0.89   {err:1, info:1, warn:3}      {correct:5}                      40
51/24637951 (COPD/MCI)                     6     6     6  0.94   {info:1}                     {correct:5}                       6
51/32895551 (pQTL phenome MR)             46→12  45    46  0.83   {err:2, warn:1}              {correct:1, not_found:4}         54
51/38365951 (HMHB anxiety RCT)             7     7     7  0.93   {err:1, info:1, warn:1}      {correct:5}                      10
```

注意几个尤其值得警觉的点：

- `51/19111551`：**5 条边全部 theta_hat=None**，spot_check 因此整篇 skip → review 等于零验证。
- `51/32895551`：spot_check 5 条里 **4 条 not_found**（PDF 截到 30 000 字符，长论文后半段全瞎）。
- `51/32895551`：**46 条进 review，最后只剩 12 条**，但 review 报告 `total_edges=46`。也就是说 review 是在还没瘦身前的脏边集合上算的指标。
- 多张表上 **valid_edges == total_edges**，pipeline 自我感觉良好 — 但你眼里的"边不准"它一条没拦下来。

---

## 2. 把 review 的输入掀开看 — 51 在到达 review 之前就已经歪了

### 2.1 占位符 (placeholder) 直接漏到最终 `edges.json`

在 `51/32895551/edges.json`（最终产物，不是中间产物）里就躺着这条：

```json
{
  "edge_id": "EV-2020-ZhengPhenome-wideMendelianrandomizationplasmaproteome#45",
  "paper_title": "论文完整标题",
  "rho": {"X": "暴露变量名称", "Y": "结局变量名称", "Z": [], "IV": null},
  "Pi": "cvd",
  "lit": {"theta_hat": null, "model": "logistic", ...},
  "hpp_mapping": {
    "X": {"name": "暴露变量名称", "dataset": "016-blood_tests",
          "field": "bt__ldl_cholesterol_float_value", "status": "exact"},
    "Y": {"name": "结局变量名称", "dataset": "021-medical_conditions",
          "field": "icd11_code", "status": "exact"}
  }
}
```

这是 **Step 2 fill_template 的中文骨架没有被填上**，被原样序列化成 JSON。
更糟的是 rerank 还跑过这条边，把"暴露变量名称"映射成 `bt__ldl_cholesterol_float_value` 并标 `status: "exact"`。

`review.py` 完全没有"占位符识别"这一关 —
没有任何字符串规则去匹配 `论文完整标题 / 暴露变量名称 / 结局变量名称 / E1/E2/E3/E4/E5/E6 / TBD / <…>` 这些常见 LLM 留白。
00 里也漏过 1 条（`Sleep Duration & Variability` 论文 3 条里有 1 条），只是数量少没被注意到。

### 2.2 同一篇论文里的 paper_title 互相不一致

```
51/21903251  4 个不同 paper_title 变体（连字符/逗号/空格不同）
51/38365951  3 个变体
51/32895551  2 个（其中一个是上面那个占位符）
00/*         全部 1 个
```

这是 Step 2 的 LLM 在每条边各自生成 title 时未作归一化。
review 里 `check_cross_edge_consistency` **能检出**这条，但只发一个 `metadata_inconsistency` warning，**不会做合并/规整**：

```python
if len(titles) > 1:
    issues.append({"type": "metadata_inconsistency", "severity": "error", ...})
```

后果：你下游再用 `paper_title` 做聚合/索引时，同一篇论文会被当成 4 篇。

### 2.3 Pi（人群标签）不一致 — 而且全是错的

`51/18054551` 是 GERD 患者 vs 健康对照的睡眠剥夺试验。它的边里 Pi ∈ `{"cvd", "adult_general"}` —
**两个标签都跟 GERD 没关系**。`check_population_consistency` 只看到"两个不一致"，发了 error 就完事，
不会去判断"两个里哪个对，或两个都不对"。

类似地 `51/32895551`（pQTL phenome-wide MR）的 Pi 也在 `{cvd, adult_general}` 之间漂移。

这些 Pi 的可选值大概率是按 00 类论文（睡眠/T2D/CVD/UK Biobank 普通成人）写死的，
prompt 没教 LLM 在 GERD/celiac/malaria/COPD/MR 这些场景该怎么贴标签 —
review 也没有"Pi must come from valid set, otherwise reject"的硬规则。

### 2.4 edge_id 在同一篇里互相不一致

`51/32895551` 出现了 `EV-2020-Zheng#1`、`EV-2020-ZHENG#2`、`EV-2020-Zheng-NatGen#3`、`EV-2020-ZhengPhenome-wide…#45` —
**4 种 prefix，全是同一篇论文**。
00 papers 大都只有 1–2 种。
review 没有任何 edge_id 一致性检查。

### 2.5 underscore vs space 的同源 X/Y 没被判作重复

在 `51/12076551`，13 条边里有些 X 写成 `Fruit_and_vegetable_intake_intervention_vs_control_group`、有些写成 `Fruit and vegetable intake intervention vs control group` — 完全同一个变量。
`check_cross_edge_consistency` 用的是：

```python
x = str(rho.get("X", "")).lower().strip()
sub = str(...).lower().strip()
edge_sigs.append((i, (x, y, sub)))
```

`lower().strip()` 不归一化下划线/空格 — 严格重复检测会漏。
（fuzzy 那一支 `detect_fuzzy_duplicates_step3` 用了 token overlap，能补上一部分，但仅当相似度 ≥0.70 才报。）

---

## 3. review.py 的硬假设 — 哪些是为 00 调出来的

逐条看 `src/review.py` + `pipeline.py` 里的 step3：

### 3.1 `rerank_hpp_mapping`（最严重）

把"被告 LLM 在 6 个 RAG 候选里二选一"的 prompt 抽出来：

```
Paper variable: "{query}" (role: X/Y)
Current mapping: {current_ds} / {current_field}
Candidate HPP fields from data dictionary:
1. ...
...
6. ...
Reply in JSON: {"best": index(1-6), "status": "exact|close|tentative|missing", "reason": ""}
If current mapping is already best, set best=0.
```

代码里的执行逻辑：

```python
if 0 < best_idx <= len(candidates[:6]):
    chosen = candidates[best_idx - 1]
    if new_ds != current_ds or new_field != current_field:
        hm[role] = {"dataset": new_ds, "field": new_field, "status": new_status}
```

问题列表：

1. **没有"全军覆没"出口**：当 6 个候选都不对时，prompt 其实允许 `status="missing"`，但代码里如果 `best>0`，**仍会用错误候选覆盖原映射**，再附上 `status="close"` 之类的标签。
   实测见 `51/12076551` 那条把 `Plasma lutein change` 改成 `female_current_pregnancy_breastfeeding_months` 并标"close"，理由是"lutein 常在孕妇人群里研究"。
2. **从 8 个里只看 top-6，再"二选一"**：top-7/8 会被丢；如果正确字段在 RAG 排名 9+，rerank 永远看不到。
3. **置信度膨胀**：reason 里说"no field captures it"，status 还是写 "close"。00 因为词典覆盖好，这种自我矛盾很少出现；51 几乎条条都触发。
4. **没有保护 status 升级方向**：rerank 可以把已经 `exact` 的 hpp_mapping 改成 `close` / `missing`，反过来也可以。没有"只允许向更稳的方向移动"或"差异要 ≥ 阈值才能改"。
5. **无审计**：rerank changes 全部应用，没有"changes ≥ N 触发人工/二次审"的阈值。

### 3.2 `check_cross_edge_consistency`

- duplicate detection 用 `(x.lower().strip(), y.lower().strip(), subgroup)` — **不归一化下划线/空格 / 全半角 / Unicode dashes**（51 的 dashes 是 `‑`、`–`，跟 ASCII `-` 不等价）。
- 多 paper_title / 多 Pi 只 raise 不修复。
- `theta_scale_suspect`（|theta|>3 on log scale）这条规则是按"00 类 OR/HR 一般在 0.3–3"的经验定的。51 里有些研究（pQTL 的 logOR、巨大 effect size）实际可能 |theta|>3 而正确 — 会误报；反之 OCR 把 0.026 抄成 0.26 之类的小幅错没人管。

### 3.3 `spot_check_values`

```python
if theta is not None and isinstance(theta, (int, float)):
    checkable.append(...)
to_check = checkable[:sample_size]   # 取前 5 个有 theta 的
prompt = "...\n--- Paper content ---\n{pdf_text[:30000]}"
```

- **只看 PDF 前 30 000 字符**：`51/32895551` 这种长篇 MR 论文核心结果在后半，导致 spot_check 大量 `not_found`。
- **只采有 theta_hat 的边**：`51/19111551` 5/5 都 None → 整篇 skip，零核查；其它 51 论文里大约一半边没 theta，本身就削减了能采样的池子。
- **只采前 5 个**：26、46 条边的论文（`51/21903251`、`51/32895551`）等于查一个零头。
- **`not_found` 不进 action_items**：当前只把 `verdict=="incorrect"` 写进 action items，`not_found` 默默吞掉，让 summary 看着干净。

### 3.4 `filter_edges_by_priority`

Step 1 prompt 给每条边打了 `priority`（实测 step1_edges.json 都有 primary/secondary/exploratory），
但 Step 2 的 `_final_schema_enforcement` 把这个字段删了 — 所以最终 `edges.json` 都没有 priority，
review 里 `has_priority = any(e.get("priority") for e in edges)` 永远是 False，过滤等于 no-op。
（这本来该在 Step 2 之前就过滤掉 exploratory 的 — 现在它们一路活到 review，并占用 spot_check 的 5 个槽位。）

### 3.5 `_generate_action_items`

只把 `not_found` 静默化、把 `metadata_inconsistency` 的 4 个 title 当 warn 而不是 fix。
`semantic_errors` 会进 action_items，但是 review 阶段不再 retry — Step 2 已结束，错误就这么挂着。

---

## 4. 为什么 00 没事 — 跟 review 关系不大，是词典 + 数据形态匹配

| 维度 | 00 papers | 51 papers |
|---|---|---|
| 主题 | 睡眠/BMI/TRF/UK 生活方式 | F&V/GERD/celiac/malaria/COPD-MCI/pQTL MR/产后焦虑 |
| HPP 词典覆盖 | ✅ 高（睡眠时长、apnea、ICD11、生活方式问卷都直接命中） | ❌ 低（plasma 类胡萝卜素、esophageal sensitivity、pQTL、celiac titer 几乎没字段） |
| Step 2 prompt 占位符泄漏 | 偶发（1 篇 1 条） | 频发（21903251 / 32895551 / 38365951） |
| paper_title 一致性 | 全部 1 个 | 多达 4 个变体 |
| Pi 标签合理性 | 落在合法集合里 | 误用（GERD 标 cvd） |
| theta_hat 缺失率 | 0–73% | 0–100% (`19111551` 5/5 全空) |
| PDF 长度 | 8–20 页常规 | 部分 ≥ 30 页（pQTL phenome），spot_check 30k 截断会丢核心 |

00 真正"赢"的地方就是：
- HPP 词典里大概率有正确字段 → rerank 哪怕 prompt 再随便也能拍中；
- Pi 等枚举值都在 prompt 教过的范围 → 不出现 GERD 这种没见过的人群；
- 论文短，前 30k 字符基本覆盖结果表 → spot_check 看得完。

review 的 5 个子环节缺陷在 00 上几乎全部被掩盖。51 一上来就把这些缺陷暴露了。

---

## 5. 结论 + 建议（按性价比排序）

> **同学说"review 出问题"是对的方向，但只占问题的 ~40%**。剩下 60% 是 Step 2 prompt 泄漏 + HPP 词典覆盖差，
> review 因为没有当好门卫所以放行了。

### 5.1 立刻能加在 review 里、收益最大的 3 个补丁

**A. 占位符黑名单（5 行代码，能挡掉最离谱的边）**

在 `check_cross_edge_consistency` 起始处加一段：

```python
PLACEHOLDER_TOKENS = ["论文完整标题", "暴露变量名称", "结局变量名称",
                      "变量名称", "E1/E2/E3", "E1/E2/E3/E4/E5/E6",
                      "TBD", "<", "{{", "请填", "待填"]

def _has_placeholder(edge):
    flat = json.dumps(edge, ensure_ascii=False)
    return any(tok in flat for tok in PLACEHOLDER_TOKENS)
```

所有命中边直接 `severity=error` 并在最终保存前剔除（不要进 rerank、也不要进 spot_check 样本）。
单这一条就能干掉 `51/32895551` 的那条假"LDL"边。

**B. rerank 增加"全军覆没"出口 + 不允许向劣等方向覆盖**

改写 `rerank_hpp_mapping` 的 prompt：

```
... 
Reply JSON: {"best": 1-6 or 0, "status": "exact|close|tentative|missing", ...}
RULES:
- If NONE of the 6 candidates is semantically the same concept,
  reply best=0 and status="missing". Do NOT pick the least bad one.
- Only set status="exact" if the candidate is the SAME measurement (unit + concept).
- Only set status="close" if it covers ≥80% of the concept.
```

Python 端追加：

```python
status_rank = {"missing":0, "tentative":1, "close":2, "exact":3}
if status_rank[new_status] < status_rank[current.get("status","tentative")]:
    # rerank 试图把 mapping 降级 — 通常是 LLM 自相矛盾，丢弃改动，只更新 status
    hm[role]["status"] = new_status
    continue
```

这一步能止住 51 里 `lutein → female_pregnancy_months` 那种荒谬替换。

**C. spot_check 改用整篇 PDF + 优先采无 theta_hat 边**

- 把 `pdf_text[:30000]` 改成 chunk-by-chunk（每 chunk 12k）轮询，verdict 投票，或者先用 BM25 把 X/Y 关键词附近的段落取出来塞 prompt。
- 采样策略改成"必须覆盖所有不同 X 的边、theta=None 边优先采（人工还能看）、edges 多的论文 sample_size=min(10, total)"。
- `not_found` ≥ 50% 时把整篇标 `[SPOT_CHECK_LOW_COVERAGE]`，让你一眼能看到。

### 5.2 中期（顺手做了能省事很多）

1. **paper_title / Pi / equation_type 在 review 里做 canonicalization 而不是只报 warning**：
   - title 用最长公共子串 + 字符规整，写回每条边；
   - Pi 配合一个 `valid_pi` 白名单 + LLM 二次裁定 (one call per paper, not per edge)；
   - `equation_type` 含 `/` 直接判 placeholder。
2. **edge_id 统一化**：在 step3 入口处按 `(first_author, year)` 重建 prefix。
3. **priority 字段保留到最终**：把 `_final_schema_enforcement` 里删 priority 的那行去掉，让 `filter_edges_by_priority` 真的能干活；或者干脆在 Step 1 之后立刻按 priority 过滤。

### 5.3 长期 — 这些不是 review 能修的

- HPP 词典对 51 类论文（pQTL、celiac titer、esophageal sensitivity、carotenoids 等）覆盖不足，rerank 再优化也变不出字段。需要在词典里补条目、或在 hpp_mapping 里允许 `status=missing` 不再强行补全。
- Step 2 fill_template 的中文骨架要么改成英文、要么在每个字段加 `must_be_filled` schema 校验，杜绝占位符外泄。
- 长论文 OCR 截断要在 Step 0/1 阶段就处理，不要把责任甩给 review 的 spot_check。

---

## 一句话答你同学

> review 步骤的确有真 bug（rerank 强行二选一、spot_check 30k 截断、占位符不识别），
> 但更准确的描述是 **review 在 51 类论文上失去了"门卫"作用**，把 Step 1/2 漏出来的脏边全部放行。
> 在 51 上恢复准确率，先按上面 5.1 的 A/B/C 三条补丁打 — 单这三条估计能让 51 的"明显错边"少 60–80%。
