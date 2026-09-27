# Phase 8-D — NBL（澳大利亚）首次真实数据接入（2026-09-27）

## 1. 数据源（含 B0 决策修订）

- **主源（本阶段起生效）**：NBL 官方 API —— `schedule.nbl.com.au/api/calendar/schedule?league=nbl&year={Y}`（赛季日历：主客/比分/轮次/team_code/状态）+ `/api/calendar/match?match={uuid}`（单场官方 `team_match_statistics`：逐节+合计行，与终场比分精确对账）。
- **B0 路径修订（如实记录）**：B0 优先推荐 FIBA LiveStats `data.json` 路径并已在 FIBA 赛事实测；本轮实测确认 NBL 的 LiveStats game id（competition code=`NBL`，如 `/u/NBL/642573/`）**没有从赛程到 id 的公开映射**（id 仅能被搜索引擎逐场发现），无法枚举整季。而 NBL 官方 API 公开提供每场完整官方 box score——按 ACB 先例改以**联盟官方 API 为主源**；LiveStats 路径记录为"单场可用、无法枚举"。
- 赛季日历 API 自身即权威赛程（主客/场地/轮次/比分），适配器实现**日历↔详情两源比分交叉核对**（同 EuroLeague Header/Boxscore 规则）。

## 2. 数据规模与质量（NBL26 = 2025-26 赛季，真实数据）

| 项 | 值 |
|---|---|
| 保留场次 | **180**（165 常规赛 + 15 季后赛/总决赛；48 场表演赛/Blitz/NBAxNBL/PG 由适配器丢弃并计数） |
| 球队 | 10（`NBL-` 前缀 id：NBL-MEL/NBL-TAS/…） |
| 赛季范围 | 2025-09-18 .. 2026-04-05（总决赛 2026-04-05） |
| 快照 | **`2e1fcac1f341`**（全库 24,476 场） |
| raw bundle | `C:\NBL_DATA\nbl_nbl25_raw.json`，SHA-256 `85bba7f0c33e9be4…`；完整单场原始 payload 另存 `C:\NBL_DATA\full\`（bundle 为裁剪版：仅保留球队级统计行，play_by_play/球员行在抓取时丢弃并注明） |
| 验证 | validate **PASS（0 errors / 0 warnings）**；联赛审计 **PASS**（10 PASS + 1 WARN） |

质量细节：

* 分值恒等式（2P/3P 合并后）与篮板恒等式 **0 违规**；0 重复；日历↔详情比分两源核对全过；全部时区 aware。
* 主客判定来自官方赛程（venue 主队），审计确认结构一致。
* **分钟漂移（真实缺陷，与 ACB 同类）**：5 队行偏离 200+25×OT 期望 ±1-4 分钟（如 `NBL25-0026/NBL-SEM=202`、`NBL25-0124/NBL-CNS=204`）——有界容差（±5 分钟）+ 原样保留 + `_meta.team_minutes_drifted`/审计 WARN 计数；超界仍硬报错。
* 20 行加时统计（加时场次的 minutes=225/250 正确入账）。

## 3. 独立 walk-forward 基线（全季 165 场常规赛，预测 138 场；冻结默认参数）

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5000 | 0.6931 | 0.2500 | 0.0000 | n/a | n/a |
| elo | 0.5870 | 0.6388 | 0.2266 | 0.1599 | n/a | n/a |
| strength | 0.6232 | 0.6020 | 0.2078 | 0.1105 | 11.78 | 14.71 |
| **simulation** | **0.6522** | **0.5991** | **0.2075** | 0.0820 | 11.78 | 14.71 |

random acc=0.5000：NBL26 主队恰半胜（真实）。单季小样本（138 场），按惯例不作强结论。

## 4. 独立校准（前半季拟合 → 后半季测试；未复制任何其它联赛参数）

- 拟合（训练窗 2025-09-18..2025-12-31，仅 NBL）：**HCA=1.77, scale=9.50, k=2.0, λ=0.000**（λ=0 内点最优：0→0.6266，0.02→0.6303，0.05→0.6470）——**NBL λ=0，与 EL/ACB 同侧**；参数组合与其它三联赛均不同（独立校准再次得到数据支持）。
- 测试（2026-01-01 起后半季 59 场）：fitted_strength logloss 0.5663 < frozen 0.5797（改善）；fitted_sim 0.5680 ≈ frozen 0.5672（持平）；fitted ECE 略升（0.1032→0.1115，记录）。
- 边界命中：仅 λ=0 下界（语义保留项）。无 C3 校准层需求。

## 5. 闭环（显式 --params）

`predict NBL25-0180（总决赛）--params storage/params/c2_nbl_2025-26_1s.json --save` → 主 71.2% → 真实 SYD 113-101 → **CORRECT**，brier 0.0829；settle→aggregate 正常入账。

## 6. 隔离与回归

- 特征管线按目标场次联赛硬隔离（B3 结构）：NBL 数据物理上不进入 NBA/EL/ACB 的 Elo/基线/校准；`window-backtest --league NBL` 独立评估。
- NBA/EL/ACB 数据、模型、参数、既有校准文件零改动（本轮仅新增 NBL 采集层/枚举/审计映射）。
- 全量测试 **558+ 通过**（新增 18 项 NBL 测试）；storage 零污染复验；git clean。

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe tools\fetch_nbl_bundle.py --year 2025 --out-dir C:\NBL_DATA --resume
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\NBL_DATA\nbl_nbl25_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\NBL_DATA\nbl_nbl25_raw.json --source nbl_api
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league NBL --expected-regular-map "2025-26=165"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league NBL --window-start 2025-09-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0
```
