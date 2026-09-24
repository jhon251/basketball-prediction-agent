# Phase 8-C1 — 三联赛独立校准闭环（2026-09-24）

数据：NBA 20,440 场（17 季）｜EuroLeague 1,867 场（6 季）｜ACB 1,989 场（6 季）。
协议：每联赛独立拟合（league-scoped，B5 机制）；训练窗 → 参数拟合 → 严格未来
测试窗评估 → `predict --params` → settle → aggregate。**测试窗从未参与拟合**；
模型选择只依据训练窗 validation logloss。全部数字为真实数据实测。

## 1. 参数拟合结果（逐联赛、逐窗口）

### NBA（cutoff 2025-07-01，测试窗 = 2025-26 常规赛 1,230 场）

| 窗口 | val logloss | HCA | scale | k | λ | 边界命中 |
|---|---|---|---|---|---|---|
| **1 季（2024-25）** | **0.6113** ← 最优 | 1.68 | 8.00 | 0.0 | **0.020** | scale 下界、k 下界 |
| 3 季 | 0.6330 | 2.11 | 11.50 | 20.0 | 0.020 | k 上界 |
| 5 季 | 0.6417 | 1.82 | 15.50 | 20.0 | 0.020 | k 上界 |

训练窗 λ 扫描（1 季窗）：λ=0.000→0.6828，**λ=0.020→0.6113**，λ=0.050→0.6402
（3/5 季窗同形：λ=0.02 均为内点最优）。**NBA：λ>0 有稳定收益。**

### EuroLeague（cutoff 2024-07-01，测试窗 = 2024-25 赛季 306 场）

| 窗口 | val logloss | HCA | scale | k | λ | 边界命中 |
|---|---|---|---|---|---|---|
| **1 季（2023-24）** | **0.6171** ← 最优 | 3.97 | 8.00 | 5.0 | 0.000 | scale 下界、λ 下界 |
| 3 季 | 0.6275 | 4.11 | 8.00 | 10.0 | 0.000 | scale 下界、λ 下界 |
| 5 季 | 0.6307 | 3.84 | 8.00 | 10.0 | 0.000 | scale 下界、λ 下界 |

λ 扫描全部窗口单调恶化（1 季窗：0→0.6171，0.02→0.6306，0.05→0.6670）。
**EuroLeague：λ>0 无收益，保留 frozen λ=0。**

### ACB（cutoff 2025-07-01，测试窗 = 2025-26 常规赛 306 场）

| 窗口 | val logloss | HCA | scale | k | λ | 边界命中 |
|---|---|---|---|---|---|---|
| **1 季（2024-25）** | **0.5695** ← 最优 | 3.13 | 8.00 | 0.0 | 0.000 | scale 下界、k 下界、λ 下界 |
| 3 季 | 0.5708 | 3.77 | 8.00 | 0.0 | 0.000 | 同上 |
| 5 季 | 0.5880 | 3.28 | 8.00 | 0.0 | 0.000 | 同上 |

λ 扫描恶化最剧烈（1 季窗：0→0.5695，0.02→0.6380，0.05→0.7758）。
**ACB：λ>0 无收益，保留 frozen λ=0。**

## 2. Frozen vs Fitted（测试窗实测；n=预测场数）

### NBA 1 季窗（n=1,230；粗体=显著变化）

| variant | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| frozen_elo | 0.6764 | 0.6266 | 0.2140 | 0.0673 | n/a | n/a |
| frozen_strength | 0.5724 | 0.6743 | 0.2408 | 0.0128 | 12.80 | 20.55 |
| frozen_simulation | 0.5602 | 0.6829 | 0.2448 | 0.0262 | 12.80 | 20.55 |
| fitted_elo（Elo 无校准分量，参照） | 0.6764 | 0.6266 | 0.2140 | 0.0673 | n/a | n/a |
| **fitted_strength** | **0.6862** | **0.6027** | **0.2066** | 0.0476 | **11.61** | **15.63** |
| **fitted_simulation** | **0.6748** | **0.6084** | **0.2102** | **0.0224** | 11.61 | 15.63 |

3 季/5 季 fitted 同样大幅改善（strength 0.6019 / 0.6123；sim 0.6042 / 0.6058）——
NBA 校准收益对窗口选择稳健。注：3 季窗 fitted_sim test logloss 0.6042 略优于
1 季窗的 0.6084，但**模型选择只按 validation logloss（1 季窗）**，测试窗数字
仅作描述记录，不据此改选。

### EuroLeague 1 季窗（n=301）

| variant | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| frozen_strength | 0.6312 | 0.6415 | 0.2250 | 0.0600 | 9.46 | 13.53 |
| **fitted_strength** | 0.6379 | **0.6306** | 0.2206 | **0.0267** | 9.43 | 13.38 |
| frozen_simulation | 0.6246 | **0.6304** | 0.2206 | 0.0640 | 9.46 | 13.53 |
| fitted_simulation | 0.6213 | 0.6366 | 0.2233 | 0.0511 | 9.43 | 13.38 |

fitted_strength 改善（-0.011 logloss，ECE 减半）；fitted_simulation 反而略差
（+0.006）→ EL 采用 fitted 仅对 strength 有依据。

### ACB 1 季窗（n=306）

| variant | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| frozen_strength | 0.6601 | 0.6145 | 0.2126 | 0.0590 | 10.13 | 14.29 |
| **fitted_strength** | 0.6634 | **0.6050** | 0.2091 | 0.0374 | 10.11 | 14.12 |
| frozen_simulation | 0.6601 | 0.6064 | 0.2095 | 0.0394 | 10.13 | 14.29 |
| fitted_simulation | 0.6569 | 0.6062 | 0.2100 | 0.0390 | 10.11 | 14.12 |

fitted_strength 改善（-0.0095）；fitted_simulation 持平。

## 3. 结论（按要求 2/7 的 λ 判定）

1. **λ>0 仅在 NBA 有稳定收益**（三窗一致选出 0.02，扫描内点最优，测试窗
   改善幅度最大）；**EL/ACB 的 λ=0 在所有窗口胜出且 λ>0 单调恶化** → 按纪律
   EL/ACB 保留 frozen λ=0，只有 NBA 采用 fitted（且仅经显式 `--params`）。
2. **三联赛校准结果完全不同**（NBA: λ=0.02/HCA 1.7-2.1/k 0-20；EL: HCA≈4,
   k 5-10, λ=0；ACB: HCA≈3-3.8, k=0, λ=0）——联赛独立校准的必要性得到数据
   证实，任何跨联赛共享参数都会互相伤害。
3. **scale=8.00 在全部 9 个窗口撞网格下界**（如实记录，未扩大搜索——这是
   下一阶段唯一的显式扩界候选，需独立提案）。
4. fitted 的主要收益之一是 **校准（ECE）改善**：EL strength 0.060→0.027、
   NBA sim 0.026→0.022（frozen strength 的 0.0128 是"几乎不冒险"的低风险
   校准，accuracy 仅 0.57，不是优点）。
5. NBA 校准让 simulation 首次在真实数据上整体超越 frozen Elo
   （fitted_sim 0.6084 vs frozen_elo 0.6266）；EL/ACB 上 Elo 仍最佳。

## 4. 闭环演示（训练窗 → 拟合 → 未来比赛 → settle → metrics）

| 联赛 | 比赛 | fitted 预测 | 真实结果 | settle |
|---|---|---|---|---|
| NBA | 2026 总决赛 G? SAS 90-94 NYK | 主 64.2%（--params nba_1s） | 客胜 | WRONG，brier 0.4119 |
| EuroLeague | 2025 Final4 决赛 MON 70-81 FEN | 主 65.2%（--params euroleague_1s） | 客胜 | WRONG，brier 0.4252 |
| ACB | 2026 决赛 BAR 84-108 VBC | 主 71.0%（--params acb_1s） | 客胜 | WRONG，brier 0.5041 |

三场决赛均为客队爆冷，三场全 WRONG——**单场决赛轶事不推翻 301-1,230 场窗口
上的拟合改善**（ 决赛=强弱最接近的球队对，本身是预测最难的分组）；
settle→aggregate 链路与 `--params` 的显式应用路径已验证工作。

## 5. 泄漏审计

- 拟合严格限制在 `[train_start, cutoff)`（`fit_v1_params_ex`，league-scoped）；
  单测证明 cutoff 之后的比赛不能改变任何拟合值。
- 测试窗评估使用与拟合相同的 walk-forward 特征纪律（每场只看自身开球前的
  历史）；测试窗 id 从不进入拟合。
- league-audit（每联赛抽样 5 场 as-of 特征计算）：NBA / EuroLeague / ACB
  全部 **PASS，0 失败**。
- 模型选择只使用训练窗 validation logloss；测试窗数字不用于选窗。

## 6. 使用方式（显式 opt-in，永不覆盖 frozen defaults）

```powershell
# 拟合 + frozen vs fitted 对比 + 参数落盘
basketball calibrate --league NBA --cutoff 2025-07-01T00:00:00+00:00 `
    --test-start 2025-07-01T00:00:00+00:00 --train-seasons 1 `
    --save-params storage\params\nba_1s.json
# 显式应用（报告会打印所用校准参数及其训练窗）
basketball predict <game_id> --params storage\params\nba_1s.json --save
basketball settle <game_id>
```

不传 `--params` 时行为与历史完全一致（frozen defaults）。
