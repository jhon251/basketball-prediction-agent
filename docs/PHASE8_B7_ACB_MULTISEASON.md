# Phase 8-B7 — ACB 多赛季真实数据补齐（2020-21 .. 2025-26）

数据来源与适配器沿用 B6：`https://api2.acb.com` + `tools/fetch_acb_bundle.py` + `basketball_agent/data/collectors/acb_api.py`。
本轮**只处理 ACB**：NBA / EuroLeague 的模型、参数、既有结果与历史数据未改动（§8 逐位回归证据）。

> 目标：把 ACB 从「单赛季」补齐到「≥5 个完整赛季」，并用同一套冻结默认参数做 1/3/全量 walk-forward 回测，
> 重点检验 B6 的解释——**「ACB 只有 1 季历史 → Elo 未收敛 → Elo 表现最差」**。

## 1. 赛季边界侦察（实测，先验证再采集）

对 `competitionId=1`（Liga Endesa）逐 edition 拉取赛季日历，得到每个赛季的形状：

| edition | 赛季 | 球队 | 比赛 | 常规赛 | 状态 | 是否纳入 |
|---|---|---|---|---|---|---|
| 84 | 2019-20 | 18 | 238 | 27 轮 | 全部 FINALIZED | ❌ **COVID 中止（27/34 轮，非完整赛季）** |
| 85 | 2020-21 | 19 | 360 | 38 轮 | 全部 FINALIZED | ✅ |
| 86 | 2021-22 | 18 | 327 | 34 轮 | 全部 FINALIZED | ✅ |
| 87 | 2022-23 | 18 | 325 | 34 轮 | 全部 FINALIZED | ✅ |
| 88 | 2023-24 | 18 | 326 | 34 轮 | 全部 FINALIZED | ✅ |
| 89 | 2024-25 | 18 | 325 | 34 轮 | 全部 FINALIZED | ✅ |
| 90 | 2025-26 | 18 | 327 | 34 轮 | 全部 FINALIZED | ✅（B6 已入库） |
| 91 | 2026-27 | 18 | 306 | 34 轮 | NOT_STARTED（未来赛季） | ❌ |

- **纳入 85–89 五个完整历史赛季**（要求 2 的"优先 5 个"），加 B6 的 90 共 **6 个赛季**。
- 2019-20 因 COVID 中止（27/34 轮）**不纳入**，理由与要求 2 的"完整赛季"定义一致；未来若需要可作为独立批次。
- 老赛季的 boxscore 结构已先行实测（2019-20 / 2021-22 / 2024-25 各抽 1 场）：`statsByPeriods` 长度 5（含 period 0
  全场合计）、`total` 20 个字段、每队 11–12 名球员 —— **结构与 2025-26 一致**，故同一适配器可直接复用。

## 2. 队名/队码跨赛季一致性（本轮真正的数据工程难点）

ACB 球队的官方三字母代码**随赞助商改名而变**（实测证据，2019-20 .. 2025-26）：

| clubId | 俱乐部 | 各赛季官方代码 |
|---|---|---|
| 3 | Saski Baskonia | KBA(19-20) → TDS(20-21) → BKN(21-22 起) |
| 4 | Surne Bilbao Basket | RBB(19-20,20-21) → SBB(21-22 起) |
| 5 | Dreamland Gran Canaria | HGC(19-20,20-21) → GCA(21-22) → DGC(22-23 起) |
| 8 | Joventut Badalona | JOV(19-20) → CJB(20-21,21-22) → JOV(22-23 起) |
| 10 | Baxi Manresa | BAX(19-20..24-25) → MAN(25-26) |
| 12 | UCAM Murcia | UCB(19-20) → UCM(20-21 起) |
| 17 | Fuenlabrada | MKF(19-20) → URF(20-21) → FUE(21-22,22-23) |
| 28 | La Laguna Tenerife | IBT(19-20) → LNT(20-21..22-23) → LLT(23-24 起) |
| 549 | San Pablo Burgos | SPB(19-20) → BUR(20-21,21-22,25-26) |
| 658 | Hiopos Lleida | HIO(24-25) → ILE(25-26) |

处理方式（**有证据、不猜测**）：仍以稳定的 `clubId` 为身份，新增
`TEAM_CODE_ALIASES`（per-club 的历史代码白名单）→ 全部映射到该俱乐部的**规范代码**，
使一个俱乐部跨赛季只有**一个** `ACB-` id（多赛季 Elo/strength 连续性依赖这一点）。
未被记录且不等于规范代码的写法仍是硬报错；实际用到的别名会写入
`_meta.team_code_aliases_used` 留痕。
同时补入升降级球队（evidence-based）：`MOV`(6) Estudiantes、`GBC`(7)、`BET`(15) Real Betis、
`FUE`(17) Fuenlabrada、`MOB`(57) Obradoiro、`ZPA`(624) Palencia、`COR`(657) Leyva Coruña。

## 3. 数据规模与质量

实际入库（2026-09-23 会话完成采集与入库）：

| edition | 赛季 | 入库场次 | 常规赛 | 季后赛 | 加时 | 快照 |
|---|---|---|---|---|---|---|
| 85 | 2020-21 | 360 | 342 | 18 | 15 | `a7b0e0a0481d` |
| 86 | 2021-22 | 326 | 305 | 21 | 14 | `cf73ba5f1255` |
| 87 | 2022-23 | 325 | 306 | 19 | 17 | `63a62f632eb6` |
| 88 | 2023-24 | 326 | 306 | 20 | 12 | `6b6f0754fc45` |
| 89 | 2024-25 | 325 | 306 | 19 | 18 | `e75c3dc5c8b5` |
| 90 | 2025-26 | 327 | 306 | 21 | —（B6 已入库） | B6 快照 |

合计 **1,989 场**（日历 1,990 − 1 场文档化排除，见 §6）。全部 validate PASS
（0 errors / 0 warnings）。质量审计（`league-audit --league ACB`）：
**PASS**，10 项 PASS + 1 项 WARN（regulation.minutes：2 个 2020-21 球队行的
playTime 漂移 ±1 分钟，原样保留未修复，见 §6）；provenance/泄漏抽样 3 场 0 失败；
25 个 `ACB-` 球队 id 全部通过命名空间检查；赞助商别名实际命中 76 队次留痕于
`_meta.team_code_aliases_used`。

## 4. 1/3/全量赛季 walk-forward 回测（冻结默认参数，seed=7，100 sims/场，`--league ACB`）

窗口定义（ACB 赛季 9 月开打，故窗口起点取 9-01 以覆盖整季）：

| 窗口 | 评估范围 | 场次（预测） | random | **elo** | strength | simulation |
|---|---|---|---|---|---|---|
| 1-season（拟合窗 2024-25） | 2025-26 常规赛 | 306 | 0.6931 | **0.5972** | 0.6145 | 0.6064 |
| 3-season | 2023-24 .. 2025-26 | 898（预测 885） | 0.6931 | **0.5807** | 0.5987 | 0.5877 |
| all | 2020-21 .. 2025-26 | 1,871（预测 1,791） | 0.6931 | **0.5832** | 0.5977 | 0.5900 |

1-season 窗的拟合（仅报告未应用）：HCA=3.13、scale=8.0、k=0、λ=0，val_logloss 0.5695。
全部窗口 Elo 最佳——与 NBA、EuroLeague 的多季结论一致。

## 5. Elo 假设检验：同一窗口、历史深度 1 季 → 6 季

B6 曾观察到「ACB 单季窗口上 Elo 表现最差」，并假设原因是历史深度不足。
本轮以 6 季历史直接检验：

* 1 季评估窗（历史仅 1 季时）Elo logloss 0.5972，已是该窗最佳，但仍弱于
  3 季窗（0.5807）与全量窗（0.5832）；
* 结论：**B6 的现象是历史深度伪影，不是 ACB 联赛特性**——给予多季历史后
  Elo 在 ACB 与在其他联赛同样占优；深度从 1→3 季的增益（≈0.016 logloss）
  大于 3→6 季（≈持平），提示 3 季左右已接近当前模型形态下的收益上限。
* 与 B2 NBA 结论一致：frozen-default simulation 在长窗口仍不敌 Elo
  （ACB 全量窗 0.5900 vs 0.5832）。各联赛独立校准（不改参数）是下一阶段靶点。

## 6. 发现的问题（如实）

1. **playTime 求和漂移（2020-21 实测）**：该季逐球员 playTime 行合计可偏离
   赛制期望（实测 +45s / +135s 两例），2025-26 则精确到秒。处理：±300s 有界
   容差 + 最近分钟入库 + `_meta.team_minutes_drifted` 计数（本季 2 队行）+
   审计 WARN——原样保留、绝不静默修复；超界（>300s）仍硬报错。
2. **源缺陷 1 场（显式排除，B4 先例）**：`ACB2021-102902`（2021-22）ACB-BUR
   11 名球员 playTime 合计 13181s（219.7min），而 header 节比分确认该场 4 节
   （200min）；对手行精确。单行源头多计，修复即猜测 → 全场显式排除并入
   `_meta.source_excluded`（该季入库 326 而非 327，常规赛 305 而非 306）。
3. **尾随空加时槽伪影（edition 87 实测）**：1 加时比赛的 boxscore 会多发一行
   全零 `quarter=6`（0 分、全员 00:00），按节行数推导加时数会错成 2 加时。
   根因修复：尾随全零行（0 分且 0 秒）识别为空槽并忽略（真实节行不可能
   双零——0 分节球员仍在场计时）；金样例测试钉住。
4. **抓取限速**：api2.acb.com 在并发抓取 ~25 分钟后拒绝连接（B7 早期实测）；
   `--resume` + 单进程 + 退避是可行姿势，85/86/88 的首批 bundle 均有缺口并
   已通过 resume 补齐（完整性由 `calendar_skipped` 硬检查保证）。
5. edition 89（2024-25）在 playTime 容差修复**之前**入库（其数据本身精确无
   漂移，数值不受影响）；其 processed `_meta` 无 `team_minutes_drifted` 字段
   属已知外观差异。

## 7. 复现命令

```powershell
# 赛季枚举 + 逐季采集（本轮：85 2020-21, 86 2021-22, 87 2022-23, 88 2023-24, 89 2024-25）
.\.venv\Scripts\python.exe tools\fetch_acb_bundle.py --list-seasons
.\.venv\Scripts\python.exe tools\fetch_acb_bundle.py --edition 85 --out-dir C:\lanqiu\.tmp\acb_fetch --resume --checkpoint-every 10 --pause 0.6
# … 86 / 87 / 88 / 89 同理

# 逐季：validate → ingest（raw 逐字归档 + SHA-256 manifest + processed + snapshot）→ league-audit
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\lanqiu\.tmp\acb_fetch\acb_competition1_edition85_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\lanqiu\.tmp\acb_fetch\acb_competition1_edition85_raw.json --source acb_api --snapshot-tag ingest:acb_2020-21
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league ACB --leakage-sample 3 --expected-regular-map "2020-21=342,2021-22=306,2022-23=306,2023-24=306,2024-25=306,2025-26=306"

# 三窗口回测（1 / 3 / 全量赛季）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league ACB --window-start 2025-09-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league ACB --window-start 2023-09-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league ACB --window-start 2020-09-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
```

## 8. NBA / EuroLeague 未被改动（逐位回归证据）

- 全部 421+ 项测试通过（含 NBA / EuroLeague 既有回归与金样例测试）；
- NBA 20,440 场与 EuroLeague 6 季数据未被本轮改动（本轮只新增 ACB 行）；
- 特征管线按目标场次联赛硬隔离（B3 结构），ACB 数据物理上无法进入
  NBA/EuroLeague 的 Elo/基线/校准；
- 模型公式与默认参数零改动（本轮只动了 ACB 采集器、审计 CLI 与测试）。

## 9. 验收对照（本轮）

- [x] ≥5 个完整 ACB 赛季入库（6 季，1,989 场，2019-20 COVID 缺季如实排除在外）
- [x] 每赛季 validate PASS + snapshot + SHA-256 raw 归档
- [x] 联赛审计 PASS（per-season 赛程参照 + 泄漏抽样）
- [x] 1/3/全量 walk-forward 三窗口真实结果入档（§4）
- [x] B6 Elo 假设完成检验（§5）
- [x] NBA / EuroLeague 未受影响（§8；全部既有测试通过）
- [x] 已知问题如实记录（§6），无静默修复
