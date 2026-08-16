# Phase 7 — Real NBA Data: Source Status & Requirements

## SOURCE_STATUS

```text
SOURCE_STATUS: UNAVAILABLE
```

Verified on this machine (probes of all candidate endpoints failed; the
sandbox has no external network access):

| Source | Status |
|---|---|
| raw.githubusercontent.com (FiveThirtyEight nba-elo CSV 等) | UNREACHABLE |
| api.github.com | UNREACHABLE |
| stats.nba.com | UNREACHABLE |
| api.balldontlie.io | UNREACHABLE |
| PyPI (`nba-api` 等包) | UNREACHABLE |
| 本机下载/文档/工作区中的 NBA 数据文件 | NOT FOUND |

Market baseline (历史赔率): **unavailable** — 本阶段不伪造市场数据。

## 需要你提供的数据（任选其一）

### 方式 A：CSV 文件（推荐，最简单）

把 CSV 放到本机任意路径，然后：

```powershell
$env:BASKETBALL_DB_PATH = "C:\lanqiu\storage\basketball.db"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest <你的csv路径> --source nba_games
```

支持两种布局（列名大小写/常见别名自动识别）：

**布局 1 — 每行一场比赛**（推荐，最少必填 5 列）：

```csv
game_date,home_team,away_team,home_score,away_score,home_fg,home_fga,home_fg3,home_fg3a,home_ft,home_fta,home_orb,home_drb,home_trb,home_ast,home_tov,home_stl,home_blk,home_pf,away_fg,away_fga,away_fg3,away_fg3a,away_ft,away_fta,away_orb,away_drb,away_trb,away_ast,away_tov,away_stl,away_blk,away_pf
2024-10-25,LAL,BOS,112,105,40,85,12,30,20,24,9,33,42,25,12,8,5,19,38,88,13,36,16,20,10,30,40,22,14,7,4,21
```

* 必填：`game_date`、`home_team`、`away_team`、`home_score`、`away_score`
* 球队名支持标准缩写与历史别名（SEA→OKC、NJN→BKN、NOH→NOP、VAN→MEM、CHH→CHA 等）
* 统计列可部分缺失；`possessions` 缺失时按 `fga + 0.44*fta - orb + tov` 估计并在清单中注明

**布局 2 — 每队一行**（两行组成一场）：

```csv
date,team,is_home,score,fg,fga,fg3,fg3a,ft,fta,orb,drb,trb,ast,tov,stl,blk,pf
2024-11-01,LAL,1,110,39,84,11,29,21,25,10,32,42,24,13,7,5,18
2024-11-01,BOS,0,103,37,86,14,35,15,19,9,30,39,21,15,6,4,20
```

### 方式 B：API 访问

如果能为本机开放某个 NBA 数据 API（如 balldontlie / nba_api / 自建代理），
告诉我可用的 base URL 与鉴权方式，我将实现对应的 collector。

### 方式 C：SQLite / JSON / parquet

提供文件 + 字段说明即可，我会写适配器（Schema 同上）。

## 数据到位后会自动执行

```text
ingest（原始文件 verbatim 归档 + SHA-256 清单 + 快照）
  → Reality Validation（未知球队名直接 FAIL，绝不静默映射）
  → NBA DATA COVERAGE REPORT
  → basketball experiments --train-cutoff <ISO> --test-start <ISO>
     EXP-NBA-001  V1 冻结参数首轮评估
     EXP-NBA-002  衰减 λ 扫描（仅训练窗口）
     EXP-NBA-003  Ensemble 训练/测试隔离对比
     EXP-NBA-004  Simulation vs Baselines
  → Error Analysis（强队/弱队/主客/接近比分/大比分/高总分/低总分/分赛季）
  → Phase 7 Final Report（§21 模板，含六个结论问题）
```

## 审计链（已就绪，数据到位即生效）

```text
Prediction → Data Snapshot (snapshot_id + SHA-256)
          → Feature Provenance (使用的每场比赛/每条伤停记录)
          → Model Version → Simulation Config (n_simulations)
          → Random Seed
```

`Data Snapshot: n/a` 只会在没有任何快照时出现；执行一次 `ingest` 后，
`basketball predict` 会自动绑定最新快照 ID。
