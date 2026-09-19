# Phase 7 — Real NBA Data: Source Status & Requirements

## SOURCE_STATUS

```text
SOURCE_STATUS: AVAILABLE (NBA only)
FIRST_REAL_DATA_INGESTED: 2026-09-19
```

Phase 8-B1 已接入第一份真实数据：`llimllib/nba_data` 的 2025-26 赛季球队
gamelog（`gamelog_2026.parquet`，1,322 场，30 队，2025-10-21 → 2026-06-13，
含季后赛/附加赛/杯赛决赛；季前赛与全明星排除）。快照 `90d58d1a28fd`，
SHA-256 `f992e6659dee93cff9964471e30e0d16929530360f5f135d41cfb00ee921d2bf`。
全季 17 个赛季的 `gamelogs.parquet`（41,038 行）已下载至
`C:\NBA_DATA\llimllib\`，因参数拟合框架当前按全量训练窗 O(历史×网格) 运行，
多赛季回测的规模优化完成后再入库。

## 支持的输入格式（ingest 层，核心模型零改动）

| 格式 | 支持 | 说明 |
|---|---|---|
| CSV（每行一场 / 每队一行） | ✅ 原生 | `basketball ingest <file.csv>` |
| **Parquet（本地）** | ✅ 适配器 | `basketball ingest <file.parquet>`；读取需要 duckdb 或 pandas+pyarrow（当前 3.13 venv 已含 pandas+pyarrow 25.0.1，可直接读；duckdb 未装，装了会优先使用）。无效/损坏的 Parquet 会以干净的 FAIL 报错，不会裸抛异常 |
| 多文件（多个 CSV/Parquet 一起） | ✅ | `basketball ingest a.csv b.csv c.parquet` — 合并后单次校验、单快照；跨文件重复 game_id 会被校验器 FAIL |
| SQLite / DuckDB 外部库 | ❌ 无直接适配器 | 请先用各自工具导出为 CSV（`duckdb :memory: -c "COPY (SELECT * FROM 'x.db'...) TO 'out.csv'"` 或 sqlite3 `.mode csv`），再走 CSV 路径 |
| 字段名差异 | ✅ 别名归一化 | 大小写不敏感；支持 `home_/away_/visitor_` 前缀与 `_home/_visitor` 后缀、`fgm/ftm/fg3m/3pm` 命中口径、`home_team_id/visitor_team_id`；球队历史别名 SEA→OKC、NJN→BKN 等；未知缩写直接 FAIL |

## 最小可用字段（以能通过全部校验为准）

**仅 5 列的比分文件会校验 FAIL**（FINAL 比赛必须有可对账的 box score，这是设计如此，不是缺陷）。
能通过校验的最小集合 = 5 个基础字段 + 双方投篮/篮板 9 项（共 23 列）：

```text
game_date, home_team, away_team, home_score, away_score,
home_FG, home_FGA, home_FG3, home_FG3A, home_FT, home_FTA, home_ORB, home_DRB, home_TRB,
away_FG, away_FGA, away_FG3, away_FG3A, away_FT, away_FTA, away_ORB, away_DRB, away_TRB
```

（AST/TOV/STL/BLK/PF/possessions 可选；possessions 缺失时按
`fga + 0.44*fta - orb + tov` 估计并记录。你指定的 33 列全集为最佳。）

## 验证文件是否合格（PowerShell）

```powershell
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate <文件路径...>
```

输出 `RESULT: PASS`（0 错误）/ `RESULT: FAIL`（含逐条错误）；不会写数据库。

## Parquet 两条路径

1. **直接 ingest**（在装有 duckdb/pyarrow 的环境，如你的 nbadb uv 环境）：
   ```powershell
   python -m basketball_agent.cli.main ingest C:\...\team_boxscores.parquet --source nba_data
   ```
2. **离线转换后再 ingest**（任意环境，转换命令需在装有 duckdb/pyarrow 的环境执行）：
   ```powershell
   python -m basketball_agent.cli.main convert C:\...\team_boxscores.parquet --out games.csv
   .\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest games.csv
   ```
   转换产物是标准 33 列 CSV，且经同一解析/校验路径（写回测试保证无损往返）。

## 本机已有文件分类（如实）

| 路径 | 内容 | 可否作为比赛数据 |
|---|---|---|
| `C:\basketball-data-index\csv\nba\column_mapping.json` | 字段名→snake_case 映射表 | ❌ 元数据 |
| `C:\basketball-data-index\csv\nba\data_dictionary.csv` | 各目标文件（Games.csv / team_boxscores.csv / game_odds.csv …）的列字典 | ❌ 元数据（但指明了应下载的**目标文件**：`team_boxscores.csv`、`games_traditional.csv` 等） |
| `C:\NBA_DATA\*` | 空（nbadb 下载失败，无残留数据） | ❌ |
| `C:\lanqiu\storage\*` | `validation_materials/bad.csv`（故意损坏的校验门禁演示文件，已移出 raw/ 扫描范围，不会被 data-status 误报为可用数据源） | ❌ |

## Phase 8-B 执行链（数据到位后按序执行）

前提：文件已放到任意可读路径（建议 `C:\NBA_DATA\`）。以下 `<file>` 为数据文件
（CSV 或 Parquet），`<cutoff>/<start>` 为 ISO-8601 UTC 时间戳。

```powershell
# 0) 环境准备（每个新终端执行一次；或改用 `basketball` 入口脚本）
$env:BASKETBALL_DB_PATH = "C:\lanqiu\storage\basketball.db"

# 1) 校验（不写库；必须 RESULT: PASS 才继续）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\NBA_DATA\<file>

# 2) 入库（自动完成：raw 留档 + SHA-256 manifest + 处理后数据集 + 数据快照）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest C:\NBA_DATA\<file> --source nba_data

# 3) 覆盖率与数据状态
.\.venv\Scripts\python.exe -m basketball_agent.cli.main coverage
.\.venv\Scripts\python.exe -m basketball_agent.cli.main data-status

# 4) 训练/测试窗实验（参数只在训练窗拟合）+ §21 报告
.\.venv\Scripts\python.exe -m basketball_agent.cli.main experiments `
    --train-cutoff <cutoff> --test-start <start>
.\.venv\Scripts\python.exe -m basketball_agent.cli.main report `
    --train-cutoff <cutoff> --test-start <start>

# 5) 单场赛前预测（--save 持久化以便结算；as_of 必须早于开球时间）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main predict <game_id> `
    --as-of <as_of> --n-simulations 10000 --seed 42 --save

# 6) 比赛终场后结算（game_id 必须已是 FINAL 且有比分）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main settle <game_id>
```

注意（Phase 8-A 已确认的限制，届时如实处理）：

* 当前 CSV/Parquet 采集器把所有比赛标为 **FINAL**（`nba_csv.py` 硬编码），
  且 FINAL 必须有 box score——**未来的 SCHEDULED 比赛无法经此路径入库**。
  Phase 8-B 若要预测未来赛程，需先扩展采集器支持 SCHEDULED 行（已记录，未实现）。
* 球队别名表覆盖 30 队现行缩写及常见历史别名；**Basketball-Reference 的
  `CHO`（夏洛特）不在表内**，真实数据到位时若命中会直接 FAIL 报出，届时扩充
  `nba_teams.py` 即可（已记录，未实现）。

## 球员级 availability/injury 数据（最小字段契约）

现状说明：team-only 的 CSV/Parquet（如 team_boxscores）不产生任何球员记录，
`FeaturePipeline._player_minutes` 无输入 → `available_minutes_share` 恒为 1.0
（`features/player/availability.py`：无加权球员时按定义返回 1.0）→ RiskAgent
的 `injury_uncertainty` 旗在真实数据上永远不触发。**这是数据缺失下的诚实默认
值，不是伪造**；但意味着接入球员数据前，伤病风险完全不进模型。

存储层（SQLite `players` / `player_game_stats` / `player_availability` 三张表）
和特征层已经就绪，缺的只是采集器。真实球员数据接入的最小契约：

1. **players（注册表，一次性）**：`player_id`（跨赛季稳定）、`name`、
   `current_team_id`（须能映射到规范球队 ID）。
2. **player_game_stats（每球员每场，用于算 minutes 权重）**：`game_id`、
   `player_id`、`team_id`、`minutes`（必填）；其余列（points/fga/fg3a/fta/
   rebounds/assists/turnovers/steals/blocks/fouls）按 schema 可选但建议提供。
   权重 = 最近 5 场已知比赛的场均 minutes。
3. **player_availability（每球员每场，赛前发布）**：`player_id`、`game_id`、
   `status`（AVAILABLE/PROBABLE/QUESTIONABLE/DOUBTFUL/OUT）、
   `availability_probability`（0–1）、`source`、`source_time`。

硬约束（校验器强制，不可绕过）：

* `source_time <= 开球时间`——赛后才知道的伤停信息进特征即 FAIL（防泄漏）；
* `availability_probability ∈ [0,1]`，OUT 必须 0.0、AVAILABLE 必须 1.0；
* 引用的 `player_id`/`game_id` 必须已存在（外键）；
* **不允许伪造伤病数据**：没有真实 injury report 就不要编造概率——宁可让
  share 保持 1.0 并在报告中如实标注"伤病特征未激活"。

## 下一步你只需准备一个文件

```text
llimllib/nba_data 的 team_boxscores.parquet（或 basketball-data-index 同源的 team_boxscores.csv / games_traditional.csv）
```

到位后按上面的 **Phase 8-B 执行链** 逐条执行。

约束保持不变：测试集不调参、未来数据进 feature 即 FAIL、不预设
Simulation/Ensemble 优于 baseline、不如即如实报告。
