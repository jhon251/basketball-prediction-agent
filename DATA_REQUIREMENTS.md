# Phase 7 — Real NBA Data: Source Status & Requirements

## SOURCE_STATUS

```text
SOURCE_STATUS: UNAVAILABLE
BLOCKED_WAITING_FOR_REAL_NBA_DATA
```

沙箱无外网（raw.githubusercontent.com / api.balldontlie.io / stats.nba.com /
PyPI 全部 UNREACHABLE，nbadb 的 Kaggle 下载在用户侧同样 ConnectTimeout）。
Market baseline（历史赔率）: **unavailable** — 不伪造。

## 支持的输入格式（ingest 层，核心模型零改动）

| 格式 | 支持 | 说明 |
|---|---|---|
| CSV（每行一场 / 每队一行） | ✅ 原生 | `basketball ingest <file.csv>` |
| **Parquet（本地）** | ✅ 适配器 | `basketball ingest <file.parquet>`；读取需要 duckdb 或 pandas+pyarrow（你的 uv/nbadb 环境已具备；本 3.13 venv 没有，会自动给出明确指引） |
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
| `C:\lanqiu\storage\*` | 本项目自有库/清单（fixture 残留） | ❌ |

## 下一步你只需准备一个文件

```text
llimllib/nba_data 的 team_boxscores.parquet（或 basketball-data-index 同源的 team_boxscores.csv / games_traditional.csv）
```

到位后执行（顺序即 §21 计划）：

```powershell
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate <文件>
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest <文件> --source nba_data
.\.venv\Scripts\python.exe -m basketball_agent.cli.main coverage
.\.venv\Scripts\python.exe -m basketball_agent.cli.main report --train-cutoff <ISO> --test-start <ISO>
```

约束保持不变：测试集不调参、未来数据进 feature 即 FAIL、不预设
Simulation/Ensemble 优于 baseline、不如即如实报告。
