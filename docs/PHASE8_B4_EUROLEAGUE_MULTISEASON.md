# Phase 8-B4 — EuroLeague 多赛季真实回测基础（已完成）

数据来源：`live.euroleague.net/api`（Header + Boxscore，无需 Key）
采集：`tools/fetch_euroleague_bundle.py`（Python urllib；429 退避 + 每 10 场原子 checkpoint + `--resume`）
入库链路：`validate → ingest --source euroleague_live → processed + raw manifest(SHA-256) + snapshot`

## 1. 实际获得的赛季与比赛数量（6 季，全部真实）

| seasonCode | 赛季 | 场次 | 常规赛 | 其他 | 日期范围 | 快照 |
|---|---|---|---|---|---|---|
| E2019 | 2019-20 | 252 | 252 | 0 | 2019-10-03 .. 2020-03-06 | `61734258258c` |
| E2020 | 2020-21 | 327 | 305 | 22 | 2020-10-01 .. 2021-05-30 | `11ffb91199db` |
| E2021 | 2021-22 | 299 | 278 | 21 | 2021-09-30 .. 2022-05-21 | `df05070ddc23` |
| E2022 | 2022-23 | 328 | 306 | 22 | 2022-10-06 .. 2023-05-21 | `2355e5f75b93` |
| E2023 | 2023-24 | 331 | 306 | 25 | 2023-10-05 .. 2024-05-26 | `bbc85e0d905f` |
| E2024 | 2024-25 | 330 | 306 | 24 | 2024-10-03 .. 2025-05-25 | `ba262abb470a` |
| **合计** | | **1867** | 1753 | 114 | | |

**赛季缩短的历史原因（源事实，非我方数据缺口）**
- 2019-20：2020-03 因 COVID 中止（252 场，无季后赛）。
- 2020-21：305 场常规赛（1 场取消）。
- 2021-22：278 场常规赛（俄罗斯俱乐部 2022-02 被移除，赛季结构变更）。

## 2. 数据质量审计（6 季，`basketball league-audit --league EUROLEAGUE`）

```text
games: 1867 | seasons: 6 | status: PASS
[PASS] game_id.format        E<yyyy>-<gameCode>，全部合规
[PASS] game_id.unique        0 重复
[PASS] team.namespace        23 支球队全部 EL- 前缀（含 E2019-E2021 的 CSK/DYR/UNK/KHI）
[PASS] team.registered       全部在注册表
[PASS] season.boundaries     6 季，无重叠、无异常跨度
[PASS] competition.tags      0 未标记（REGULAR_SEASON / PLAY_IN / PLAYOFF）
[WARN] regulation.minutes    期望 200；加时行 192；10 处源侧聚合瑕疵（195/201/240，原样保留不修补）
[PASS] timezone.aware        全部 tz-aware（主场本地 tip-off → UTC）
[WARN] missing.games         无赛程参照（如实标注，未伪造）
[PASS] final.consistency     0 不一致
[PASS] provenance.leakage    抽样 3 场 as-of 特征计算，0 泄漏、0 跨联赛历史
```

**源缺陷处理（不修补、不绕过门禁）**
- `E2020-0127`（Efes vs Milan）：源 `totr.Minutes = 179`（同场 Efes 报 200），低于任何合法节次合计 → 记入
  `euroleague_live.KNOWN_SOURCE_DEFECTS` **显式排除**（该季入库 327 场而非 328），并在 `_meta["source_excluded"]` 留痕。
- 其余 10 处（195/201/240 等）在入库门禁 `[180,400]` 内 → **原样入库**；审计按"源侧瑕疵 WARN + 逐条列出"报告，
  既不静默接受、也不当作不可能值 FAIL。

**别名表扩展（按源端官方代码，有证据）**：`PAM→VAL`(Valencia)、`CSK`(CSKA)、`DYR`(Zenit)、`UNK`(UNICS)、`KHI`(Khimki)，时区均为 Europe/Moscow 或 Europe/Madrid。

## 3. 三窗口 Walk-Forward 回测（冻结默认参数，`--league EUROLEAGUE`，seed=7，100 sims/场）

**WINDOW 1 — 1 season（2024-25，306 场，可预测 301）**

| model | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.6113 | 0.6931 | 0.2500 | 0.1113 | n/a | n/a |
| **elo** | **0.6545** | **0.6240** | 0.2184 | 0.0596 | n/a | n/a |
| strength | 0.6312 | 0.6415 | 0.2250 | 0.0600 | 9.46 | 13.53 |
| simulation | 0.6246 | 0.6304 | 0.2206 | 0.0640 | 9.46 | 13.53 |

**WINDOW 2 — 3 seasons（2022-23..2024-25，918 场，可预测 904）**

| model | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.6327 | 0.6931 | 0.2500 | 0.1327 | n/a | n/a |
| **elo** | **0.6825** | **0.6062** | 0.2098 | 0.0507 | n/a | n/a |
| strength | 0.6670 | 0.6322 | 0.2205 | 0.0860 | 9.30 | 13.25 |
| simulation | 0.6538 | 0.6218 | 0.2164 | 0.0622 | 9.30 | 13.25 |

**WINDOW 3 — all available（2019-20..2024-25，1753 场，可预测 1685）**

| model | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.6184 | 0.6931 | 0.2500 | 0.1184 | n/a | n/a |
| **elo** | **0.6617** | **0.6183** | 0.2150 | 0.0228 | n/a | n/a |
| strength | 0.6528 | 0.6365 | 0.2226 | 0.0720 | 9.56 | 13.45 |
| simulation | 0.6386 | 0.6303 | 0.2202 | 0.0541 | 9.56 | 13.45 |

## 4. 关键发现

1. **Elo 在全部三个窗口都是最佳预测器**（0.6240 / 0.6062 / 0.6183），与 NBA（B2）结论一致。
2. **上一轮 B4 的"EL 中 Elo 更差"结论被推翻，原因是历史深度而非联赛特性**：只有 E2024 一季时
   elo 0.6381 > sim 0.6320；补入 E2019-E2023 后同一赛季窗口变为 elo 0.6240 < sim 0.6304（Elo 反超）。
   → 结论：**Elo 稳定优于 strength/simulation 的前提是联赛历史足够深**；单季样本会给出相反且不可靠的排序。
3. **EuroLeague 主场胜率显著更高**：random baseline 的 accuracy = 真实主队胜率 ≈ 0.61-0.63（NBA 为 0.55-0.57）。
4. **比分预测比 NBA 更准**：margin MAE 9.3-9.6 / total MAE 13.2-13.5（NBA 为 10.0-12.0 / 15.2-20.3），
   与 40 分钟制、更低回合数（≈72/队）一致。
5. **simulation ≈ strength 再次成立**（margin/total MAE 完全相同）；sim 的 logloss 略优来自 MC 平滑，非新增信息。
6. **ECE 随历史加深改善**（Elo 0.0596 → 0.0228），说明校准依赖样本量而非调参。
7. **未根据结果调整任何参数**（任务 #8）；三窗口均为冻结默认参数。

## 5. 未完成 / 已知问题

| 项 | 状态 |
|---|---|
| `--train-seasons ≥1` 训练窗参数拟合 | ✅ 已在 **Phase 8-B5** 解决（29 遍特征遍历 → 3 遍；真实 331 场训练窗 421.7 s → 71.7 s，参数逐位不变）。B2 约定"拟合参数单独报告、不应用于默认模型"，故上表结论与 B5 前后一致 → 见 `docs/PHASE8_B5_FITTING_EFFICIENCY.md` |
| 市场基线 | 不可用（无免费历史赔率，不伪造） |
| 缺失比赛参照 | 无官方赛程源（审计如实 WARN） |
| 其他联赛（ACB/NBL/CBA/BSL） | 未触及（按指示） |
| 2019-20/2020-21/2021-22 赛季场次不足 306 | 源历史事实（COVID / 俄俱乐部移除），非数据缺口 |

## 6. 复现命令

```powershell
# 采集（用户侧联网；已完成的赛季可用 --resume 跳过）
python tools/fetch_euroleague_bundle.py --seasons E2019 E2020 E2021 E2022 E2023 `
    --out-dir C:\lanqiu\.tmp\el_fetch --resume --pause 0.5 --checkpoint-every 10

# 逐季校验 + 入库（各自生成 raw manifest + SHA-256 + processed + snapshot）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate <bundle.json>
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   <bundle.json> --source euroleague_live --snapshot-tag ingest:E20xx

# 审计 + 三窗口
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league EUROLEAGUE --leakage-sample 3
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league EUROLEAGUE --window-start 2024-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league EUROLEAGUE --window-start 2022-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league EUROLEAGUE --window-start 2019-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
```

## 7. 验收对照（本轮）

- NBA 原有测试全部通过 → ✅（448 passed / 1 skipped / 0 failed）
- EuroLeague 多赛季真实数据可追溯 → ✅（6 季，raw manifest + SHA-256 + processed + 快照 + `EL-` 命名空间）
- 1/3/all 窗口均可运行 → ✅（真实 306 / 918 / 1753 场，非退化）
- leakage/provenance PASS → ✅（审计抽样 0 失败；无跨联赛历史）
- 不伪造数据 → ✅（1 场源缺陷显式排除而非修补；10 处源瑕疵原样保留并逐条报告）
- 未调参 / 未新增模型 / 未接其他联赛 → ✅
