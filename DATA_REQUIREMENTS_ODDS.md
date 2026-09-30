# DATA_REQUIREMENTS_ODDS.md

## SOURCE_STATUS: UNAVAILABLE

## 需要的 CSV 格式（canonical header）

```csv
game_date,away_team,home_team,close_moneyline_home,close_moneyline_away,close_spread,close_spread_odds,close_total,close_total_odds,bookmaker,season
2024-11-20,BOS,LAL,-110,110,-3.5,1.90,220.5,1.85,pinnacle,2024-25
```

## 列说明

| 列 | 必填 | 说明 |
|---|---|---|
| game_date | ✅ | 比赛日期 YYYY-MM-DD |
| away_team | ✅ | 客队 code（与 games 表 away_team_id 匹配，大小写不敏感） |
| home_team | ✅ | 主队 code |
| close_moneyline_home | ✅ | 主队美式收盘赔率（如 -150, +130） |
| close_moneyline_away | ✅ | 客队美式收盘赔率 |
| close_spread | 可选 | 让分盘口（如 -3.5） |
| close_spread_odds | 可选 | 让分盘欧赔 |
| close_total | 可选 | 大小分盘口 |
| close_total_odds | 可选 | 大小分欧赔 |
| bookmaker | 可选 | 博彩公司名 |
| season | 可选 | 赛季标签 |

## 匹配规则

- home_team/away_team 与 games 表 team_id 大小写不敏感精确匹配
- game_date ±1 天容差
- 不匹配 → UnknownTeamError（不猜不映射）

## 使用方法

```powershell
# 基本用法
basketball odds ingest --file odds.csv

# 指定列名映射（CSV 列名 ≠ canonical 时）
basketball odds ingest --file odds.csv --column-map "{\"date\": \"game_date\"}"

# 查看覆盖率
basketball odds coverage
```

## 双边 moneyline 必填说明

去水（de-vig）需要主客两边的收盘赔率才能移除抽水。只有单边赔率的行
将被跳过并计数（skipped_no_pair），不会被猜测补全。
