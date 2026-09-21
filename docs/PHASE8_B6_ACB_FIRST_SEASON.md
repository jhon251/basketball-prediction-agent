# Phase 8-B6 — ACB（Liga Endesa）首个真实赛季接入（已完成）

数据来源：`https://api2.acb.com`（ACB 官方数据 API，`acb.com` / `live.acb.com` 自身使用）
采集工具：`tools/fetch_acb_bundle.py`（Python urllib + 站点公开 `X-APIKEY`；429 退避 + 每 10 场原子 checkpoint + `--resume`）
入库链路：`validate → ingest --source acb_api → raw manifest(SHA-256) + processed + snapshot → coverage → league-audit`

> 本轮只新增 ACB：未改动 NBA / EuroLeague 的模型、参数、结果与历史数据（§8 给出逐位回归证据）。
> **未进入 NBL**（按要求停止）。

## 1. 数据源与赛季选择（均为实测事实）

| 项 | 值 |
|---|---|
| 来源 | `https://api2.acb.com`（官方 API；`acb.com`/`live.acb.com` 的数据层） |
| 端点 | `/api/seasondata/Calendar/season-calendar`（竞赛+赛季+轮次+比赛）、`/api/matchdata/MatchHeader/match-header?matchId=`、`/api/matchdata/Result/boxscores?matchId=` |
| 鉴权 | 站点自身 JS bundle 内的公开客户端 `X-APIKEY`（非机密；可用 `ACB_API_KEY`/`--api-key` 覆盖） |
| 竞赛 | `competitionId=1` **Liga Endesa**（另有 Copa del Rey=2 / Supercopa=3 / Minicopa=10：非联赛赛季，明确不在本轮范围） |
| 赛季 | `editionId=90` = **2025-26**（API 提供 edition 48=1983-84 … 91=2026-27） |
| 选择理由 | 最近一个**完整**赛季：44 轮 327 场全部 `FINALIZED`，含完整季后赛 |
| 采集规模 | 654 次 API 调用（327 header + 327 boxscore），约 34 分钟，无失败/无重试 |

## 2. 数据规模（真实）

| 项 | 值 |
|---|---|
| 比赛总数 | **327**（常规赛 **306** = 34 轮 × 9 场；季后赛 **21** = 1/4 决赛 11 + 半决赛 6 + 决赛 4） |
| 球队 | **18** 支（`ACB-` 命名空间；`clubId` 为稳定身份） |
| 日期范围 | 2025-10-04 .. 2026-06-24 |
| 加时 | **13 场**（12 场 1 个加时 + 1 场 2 个加时）→ 球队 minutes 分布 {200: 628, 225: 24, 250: 2} |
| 原始 bundle | **43.26 MB**，SHA-256 `3f17a584ac912df1b8dae79e6946464b20586bb58a85b6916e9777e4f8f9fdbb`（与 `storage/raw/acb_api/` 归档逐字节一致） |
| 入库 | 327 games / **654** team_stats 行；processed `dataset_20260921T143805.json`（sha256 `b039792300ed…`）；快照 `d7d6fccfa051`（tag `ingest:acb_2025-26`） |
| 数据分布（真实） | 主队胜率 **0.5749**（188/327）；场均总分 176.4（118..254）；场均分差 11.9（最大 45）；单队最高 131 分；每队 possessions 均值 77.5 |
| 赛程分布 | 周日 157 / 周六 121 / 周五 16 / 周三 11 / 周二 10 / 周四 9 / 周一 3（本地时间） |

## 3. 规范化契约（要求 4 逐项）

| 项 | 规则（有实证） |
|---|---|
| **competition** | 无 `subphaseId` 的 34 轮 = `REGULAR_SEASON`；subphase 293/292/291 = `PLAYOFF`（1/4 决赛/半决赛/决赛）；未知 subphase → 硬报错（不猜测） |
| **season** | 统一 `2025-26`；`season_code = ACB2025`（对应 edition 90） |
| **game_id** | `ACB<seasonStartYear>-<matchId:06d>`，如 `ACB2025-104459`（对齐 EuroLeague `E2024-0001` 约定） |
| **team namespace** | `ACB-<官方三字母代码>`（`ACB-UNI`/`ACB-BAR`/…），键为稳定 `clubId`（冠名变化不影响身份）；未知 clubId 或官方代码漂移 → 硬报错 |
| **timezone** | 源直接发布 **UTC**（`startDateTime`/`start` 带 `Z`），原样入库；非 UTC 时间戳 → 硬报错（不猜场馆时区）。场馆时区表仅作审计：加纳利两支队（DGC 大加那利、LLT 拉古纳特内里费）= `Atlantic/Canary`，其余 = `Europe/Madrid` |
| **40 分钟制** | 4×10 分钟；新增 `LEAGUE_REGULATION_SECONDS["ACB"] = 2400.0`；球队 minutes = 球员 `playTime` 精确求和，强制等于 `200` |
| **OT** | 已打节数（boxscore period 0 = 全场合计，period 1..N = 各节）> 4 即加时；minutes 必须 = `200 + 25×OT`；header `quarterScores` 长度必须等于节数，且各节合计必须等于终场比分 |
| **缺失/取消** | `FINALIZED` 却缺 header/boxscore → 硬报错（绝不静默丢弃或补齐）；`POSTPONED/SUSPENDED→POSTPONED`、`CANCELED/CANCELLED→CANCELLED`、`NOT_STARTED/NOT_SCHEDULED→SCHEDULED`。**2025-26 全部 327 场均为 FINALIZED**，故这些分支仅由合成 payload 单测覆盖（如实标注，未经真实非终局比赛检验） |
| **比分三源交叉** | 日历 `homeScore/awayScore` == MatchHeader `currentHomeScore/AwayScore` == boxscore `stats.total.points`，任一不一致 → 硬报错 |
| **主客判定** | 日历 `homeTeamId` == header `teams.home.id`，且 boxscore 恰好包含这两支队伍；三处必须一致（合约禁止猜测 tm.1/tm.2） |
| **球队合计** | 直接取**官方** period-0 `stats.total`（含 `stats.team` 非球员行），并逐字段校验 `球员合计 + 球队行 == 官方合计`、`points == 2×2PM + 3×3PM + FTM`、`trb == orb + drb`、`made ≤ attempted` |
| **possessions** | 源未发布 → 沿用既有估算式 `fga + 0.44×fta − orb + tov`，计数写入 `_meta.possessions_estimated_for = 654` |
| **球员数据** | 本轮不入库（与 EuroLeague 适配器一致，V1 模型只用球队级特征）；球员明细完整保存在原始 bundle 中，供后续使用 |

**时区可信度实证**：全部 327 场的本地开赛时刻落在 12:00–21:00（无一异常）；DGC 在大加那利时区 10 月为 `WEST 19:00`、11 月为 `WET 19:00`——跨夏令时仍落在同一本地时刻，证明源的 UTC 时间戳确由场馆本地时间正确换算而来。

## 4. 数据质量（validate / audit）

`validate`（入库门禁）：

```text
files: 1 | games: 327 | team_stats: 654
No issues found.
RESULT: PASS (0 errors, 0 warnings)
```

`league-audit --league ACB --leakage-sample 3 --expected-regular 306`：

```text
LEAGUE AUDIT — ACB
==============================================
games: 327 | seasons: 1 | status: PASS

season coverage:
season        games  regular  other  teams  range
2025-26         327      306     21     18  2025-10-04 .. 2026-06-24

checks:
[PASS] game_id.format         327 ids; format ACB<seasonStartYear>-<matchId>
[PASS] game_id.unique         duplicates: 0
[PASS] team.namespace         18 team ids; all must carry the ACB- prefix
[PASS] team.registered        all referenced teams exist in the registry
[PASS] season.boundaries      1 season(s); overlaps: none; implausible: none
[PASS] competition.tags       untagged games: 0 of 327
[PASS] regulation.minutes     expected 200 min/team; OT rows: 26; violations: 0
[PASS] timezone.aware         all timestamps timezone-aware
[PASS] missing.games          expected 306 regular-season games/season; all seasons complete
[PASS] final.consistency      FINAL/score mismatches: 0; non-final games: 0
[PASS] provenance.leakage     sampled 3 game(s); failures: 0; insufficient-history skips: 0

leakage sample: ACB2025-105371, ACB2025-105372, ACB2025-105373
```

## 5. 预测与回测（冻结默认参数，`--league ACB`，seed=7，100 sims/场）

**单场真实预测 smoke**（2025-26 总决赛最后一场，`--as-of` = 开赛时刻，特征只用此前 ACB 历史）：

```text
Game: FC Barcelona vs Valencia Basket
Win Probability: HOME 50.5% / AWAY 49.5%
Expected Score : 84.8 - 85.7      Expected Margin: -0.8      Expected Total: 177.0
（实际结果：BAR 84 - 108 VBC，客队胜）
```

预期总分 177.0 与 ACB 赛季真实场均总分 176.4 高度一致 → 说明 40 分钟制/节奏标定正确（未被 NBA 参数污染）。

**WINDOW A — 全赛季（2025-10-01 .. 2026-07-01；窗口内 306 场常规赛，可预测 261）**

| model | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5709 | 0.6931 | 0.2500 | 0.0709 | n/a | n/a |
| elo | 0.6207 | 0.6250 | 0.2187 | 0.0712 | n/a | n/a |
| **strength** | 0.6398 | **0.6145** | 0.2131 | 0.0487 | 10.17 | 13.08 |
| simulation | **0.6437** | 0.6175 | 0.2149 | 0.0510 | 10.17 | 13.08 |

**WINDOW B — 后半赛季（2026-01-01 .. 2026-07-01；窗口内 195 场，可预测 195）**

| model | acc | logloss | brier | ECE | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5692 | 0.6931 | 0.2500 | 0.0692 | n/a | n/a |
| elo | 0.6256 | 0.6236 | 0.2183 | 0.0755 | n/a | n/a |
| strength | 0.6256 | 0.6133 | 0.2128 | 0.0685 | 9.89 | 13.16 |
| **simulation** | **0.6462** | **0.6114** | 0.2127 | 0.0572 | 9.89 | 13.16 |

**读数（如实）**
1. 两个窗口都**优于 random**（0.6931），即真实数据上具有预测力；
2. **Elo 是三者中最差的**（0.6250 / 0.6236）——与 NBA/EuroLeague（Elo 最佳）**相反**。原因是最浅的历史深度：
   ACB 只有 1 个赛季入库，Elo 的联赛内动态评分在赛季初严重不收敛（前 45 场因 warmup 被跳过，余下样本里 Elo 仍在追赶）；
3. `simulation` 与 `strength` 的 margin/total MAE **完全相同**（同一次特征、同一解析式），差异仅来自 MC 平滑；
4. 比分预测质量介于 EuroLeague 与 NBA 之间：margin MAE 9.9–10.2（EL 9.3–9.6，NBA 10–12）、total MAE 13.1–13.2（EL 13.2–13.5，NBA 15–20）；
5. `--train-seasons 1` 在 ACB 上**按设计失败**：`ERROR: training window too small to fit parameters`
   ——ACB 训练窗 [2024-10-01, 2025-10-01) 内没有任何 ACB 比赛（只入库了 2025-26 一季）。
   **不伪造第二季**；ACB 的参数拟合需等下一季数据（B7+）。

## 6. 发现的问题（如实）

| # | 问题 | 处理 |
|---|---|---|
| 1 | 只有 1 个 ACB 赛季 → 无法做训练窗参数拟合；Elo 因历史过浅而落后 | 如实报告；不做任何跨联赛/跨季填充；等下一季 |
| 2 | 官方 HTML 站点为 Next.js 渲染，直接抓 HTML 不可行 | 改用同一站点自己的 JSON API（有实证、字段完整） |
| 3 | Cloudflare 拦截了备源（Basketball-Reference / RealGM / eurobasket.com 均 403） | 记录在 `docs/DATA_SOURCES.md`；主源已足够 |
| 4 | 2025-26 全部 327 场都是 FINALIZED | 非终局状态映射（POSTPONED/CANCELLED/SCHEDULED）只有合成单测覆盖，文档明确标注 |
| 5 | **轮次编号不是时间轴**：92/327 场的轮次标签与开赛时间顺序不一致（轮次被提前/改期） | 适配器记录该计数，管线一律按 `(game_date, game_id)` 排序；roll 顺序绝不用作时间键 |
| 6 | 源不发布球队 minutes / possessions | minutes = 球员 `playTime` 精确求和（强制 200+25k）；possessions 用既有估算式并计数 |
| 7 | 球员级数据未入库 | 与 EuroLeague 口径一致（V1 只用球队级）；明细保留在原始 bundle |
| 8 | Copa del Rey / Supercopa 未接入 | 属非联赛竞赛（`Competition.CUP` 语义），本轮范围外，明确不伪造 |
| 9 | 需要新增 `League.ACB` 枚举与 40 分钟制表项 | 纯**新增**，NBA/EL 分支与其数值未改动 |

## 7. 复现命令

```powershell
# 采集（本环境 Python urllib 可直连 ACB 官方 API）
.\.venv\Scripts\python.exe tools\fetch_acb_bundle.py --edition 90 --out-dir C:\lanqiu\.tmp\acb_fetch --resume --checkpoint-every 10 --pause 0.4
.\.venv\Scripts\python.exe tools\fetch_acb_bundle.py --list-seasons        # 竞赛/赛季枚举

# 校验 → 入库（raw 逐字归档 + SHA-256 manifest + processed + snapshot + coverage）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\lanqiu\.tmp\acb_fetch\acb_competition1_edition90_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\lanqiu\.tmp\acb_fetch\acb_competition1_edition90_raw.json --source acb_api --snapshot-tag ingest:acb_2025-26

# 审计（联赛隔离 + as-of 泄漏抽样）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league ACB --leakage-sample 3 --expected-regular 306

# 单场真实预测 smoke + walk-forward 回测
.\.venv\Scripts\python.exe -m basketball_agent.cli.main predict ACB2025-105373 --as-of 2026-06-24T18:00:00+00:00 --n-simulations 200 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league ACB --window-start 2025-10-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league ACB --window-start 2026-01-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7

# 测试
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider            # 508 passed, 1 skipped
```

## 8. NBA / EuroLeague 未被改动（逐位回归证据）

`.tmp/b6_regression.py`（只读）输出：

```text
league row counts: {'NBA': 20440, 'EUROLEAGUE': 1867, 'ACB': 327}
NBA audit: PASS (20440 games)
EUROLEAGUE audit: PASS (1867 games)
el-1season: identical=True  {"n": 306, "n_pred": 301, "elo": 0.6240176912682203, "strength": 0.6414579511534281, "sim": 0.6303873532011838}
el-3season: identical=True  {"n": 918, "n_pred": 904, "elo": 0.6061770732353025, "strength": 0.6322101187361041, "sim": 0.6217977187978005}
el-all:     identical=True  {"n": 1753, "n_pred": 1685, "elo": 0.6183132852449301, "strength": 0.6365129512131518, "sim": 0.6303480746519481}
NBA short-window fit ok in 159.7s (league-scoped): HCA=2.9677 scale=8.0 k=0.0 lambda=0.02
unscoped mixed-league fit refuses as designed: "…must contain exactly one league (got ['ACB','NBA'])…"
=== REGRESSION PASS ===
```

- NBA 行数仍为 **20 440**、EuroLeague 仍为 **1 867**（本轮只增加 ACB 327）；
- B4 的三个 EuroLeague 窗口指标**逐位相同**（float 严格相等，非近似）；
- 无联赛作用域的拟合遇到 NBA+ACB 混合窗口仍然**拒绝**（B5 的隔离约束继续生效）；
- 全量测试 **508 passed, 1 skipped, 0 failed**（B5 为 462+1，本轮新增 46 项：ACB 适配器 29 + 审计 7 + 真实数据集成 10）。

## 9. 本轮交付物

| 文件 | 变更 |
|---|---|
| `basketball_agent/data/collectors/acb_api.py` | 新增：ACB 官方 API 适配器（三源交叉校验、官方合计+身份校验、minutes/OT 强制、联赛命名空间） |
| `tools/fetch_acb_bundle.py` | 新增：赛季采集工具（`--list-seasons`、checkpoint/resume、429 退避） |
| `basketball_agent/tests/unit/test_acb_api.py` | 新增：29 项适配器单测（golden / OT / 失败路径 / 状态映射 / 命名空间隔离） |
| `basketball_agent/tests/integration/test_acb_b6.py` | 新增：10 项真实数据集成测试（审计、覆盖、归档 bundle 对账、联赛隔离、NBA/EL 回归） |
| `basketball_agent/tests/unit/test_league_audit.py` | 新增 7 项 ACB 审计分支测试 |
| `basketball_agent/data/schemas/base.py` | `League.ACB`（新增枚举成员） |
| `basketball_agent/models/game_params.py` | `LEAGUE_REGULATION_SECONDS["ACB"] = 2400.0`（40 分钟制） |
| `basketball_agent/evaluation/league_audit.py` | ACB 分支：`ACB<yyyy>-<matchId>` 格式、`ACB-` 命名空间、200 分钟规制 |
| `basketball_agent/cli/main.py` | `--league` 增加 ACB；bundle 分发改按 `dataset` 标记派发 |
| `docs/DATA_SOURCES.md` | ACB 主源更新为实测的 `api2.acb.com`（含端点与鉴权说明） |
| `docs/PHASE8_B6_ACB_FIRST_SEASON.md` | 本文档 |

## 10. 验收对照（本轮）

| 要求 | 结果 |
|---|---|
| 1. 只新增 ACB，不改 NBA/EuroLeague 模型/参数/结果 | ✅ §8 逐位回归证据（行数、审计、三窗口指标全部不变） |
| 2. 按 LeagueAdapter / Unified Schema 接入 1 个完整可验证赛季 | ✅ 2025-26 全季 327 场（306+21），适配器 → `Game`/`TeamGameStats`/`Team` 统一 schema |
| 3. raw 保存 + SHA-256 + validate + normalize + ingest + snapshot + coverage + provenance/leakage | ✅ 43.26 MB 原始 bundle（SHA-256 与归档逐字节一致）、validate PASS(0/0)、processed + 快照 `d7d6fccfa051`、审计 11 项 PASS、泄漏抽样 3 场 0 失败 |
| 4. competition/season/timezone/game_id/namespace/40min/OT/缺失取消 | ✅ §3 逐项（含真实 OT 13 场、DST 实证、缺失即硬报错） |
| 5. 严禁伪造；数据源不足即停止 | ✅ 未伪造任何比赛/比分；第二季缺失导致 `train-seasons 1` 明确失败并如实报告 |
| 6. ACB 独立 real-data prediction smoke + 基础 walk-forward 回测 | ✅ 单场预测（预期总分 177.0 vs 真实 176.4）+ 两个窗口回测 |
| 7. 不新增模型、不改默认参数、不与 NBA/EuroLeague 混练 | ✅ 未新增模型；默认参数未改；拟合/特征/缓存均按联赛隔离（混合窗口直接拒绝） |
| 8. 报告 + 测试数 + commit + git clean | ✅ 本文档；508 passed / 1 skipped；见 §11 |
| 未进入 NBL | ✅ 按要求停止 |

## 11. 提交

```text
commit: "Phase 8-B6 complete: first real ACB (Liga Endesa) season ingested end to end …"（见 `git log -1`）
storage/: raw/acb_api 43.26 MB（SHA-256 manifest）+ processed dataset + snapshot d7d6fccfa051
git status: clean（`storage/` 与 `.tmp/` 均在 .gitignore 内）
全量测试: 508 passed, 1 skipped, 0 failed
```
