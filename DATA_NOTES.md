# NBA Data Notes — Stat Definition Differences (spec §6)

真实数据源的统计口径差异必须记录并统一。已知常见差异（接入时逐项核对，并在
`ingest --stats-notes` 中记录数据源特有口径）：

1. **Possessions / Pace 口径**
   - 官方（stats.nba.com）不直接提供 possessions；常见公式
     `FGA + 0.44*FTA - ORB + TOV`（Basketball-Reference 口径）是**每队**口径。
   - 本系统 `TeamGameStats.possessions` 语义 = 该队当场合计回合数（≈99/场/队）；
     "pace"（每队 48 分钟回合数）由 `(poss_home + poss_away)/2` 得到。
   - CSV 缺失 possessions 时自动按上述公式估计，processed manifest 的
     `possessions_estimated_for` 记录受影响行数。

2. **FG 包含 3P**
   - 标准 box score 中 FGM 包含 3PM（`fg >= fg3`、`fga >= fg3a`）。
   - 若数据源把 2P 与 3P 分开记录，采集时必须先合并，否则一致性校验 FAIL。

3. **Team rebounds**
   - 部分数据源把"团队篮板"单独计入 TRB；本系统要求
     `ORB + DRB == TRB`（个人/团队篮板归属差异需在采集时统一，默认不保留团队篮板行）。

4. **加时统计**
   - 加时比赛的球员 minutes > 48 是合法的（校验器按 WARNING 处理，>60 才 ERROR）；
     球队 minutes 含加时为 265（校验范围 [180, 400]）。

5. **弃权/取消/延期比赛**
   - 只摄入 `FINAL` 且双方有比分的比赛；延期/取消比赛不要伪造 0-0。

6. **球队改名与搬迁**
   - 统一到 franchise 稳定 ID：SEA→OKC、NJN→BKN、NOH/NOK→NOP、
     VAN→MEM、CHH→CHA、WSH→WAS、PHO→PHX。
   - 未知缩写直接 FAIL（`IngestDataError`），绝不静默映射。

7. **比赛时间时区**
   - 一律转换为 UTC 存储；`game_date` 使用该场比赛的开球时间（无时间时按 UTC 00:00）。

8. **Market / 赔率数据**
   - 本阶段不可用（`Market baseline unavailable`），绝不伪造。
