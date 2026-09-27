# Phase 8-F — BSL（土耳其 Basketbol Süper Ligi）首次真实数据接入（2026-09-27）

## 1. 数据源（B0 路径实测与组合）

- **B0 主源 FIBA LiveStats：数据端实测可用，但不可枚举**。competition code=TBF（TBF 与 Genius 合作属实）；旧地址 `fibalivestats.com/u/TBF/{id}/` 301 至 Genius 托管 `fibalivestats.dcd.shared.geniussports.com`，其中 `data/{gameId}/data.json` 公开返回完整官方数据（含球队级合计、逐节比分、PBP/投篮坐标等）；但 LiveStats 体系**没有任何公开的赛程→gameId 映射**（`/u/TBF/` 索引 403、webcast 页 404、othermatches 仅同日跨项目混列无赛事标签）——与 NBL 8-D 同结论：**单场可用、无法枚举**。
- **枚举源：TBF 官方 API**（`www.tbf.org.tr` Nuxt SPA 后端）：`League/get-leagues-and-seasons-by-prefix?prefix=bsl` 给出 BSL 全部赛季（1966-67 起）；`Match/get-all-matches-for-filter?ActivityId=..&TeamId=&Page=1&PageSize=-1&WeekFilter=*` 返回整季每场（主客 TBF 队 id/名称/比分、week、场馆、加时旗、**genuisId**）——赛程与 LiveStats box 为**两个独立源**，适配器做两源比分交叉核对。API 参数形状（`PageSize:-1`、`WeekFilter:"*"`）从前端 Nuxt chunk 逆向取得。
- **网络实测（本机环境，如实）**：www.tbf.org.tr 与本机 OpenSSL **TLS 1.3 协商失败**（exit 35），强制 `--tlsv1.2 --tls-max 1.2` 即通；页面路径受 Cloudflare 挑战但 `/api/*` JSON 直答；api.tbf.org.tr 仅承载 SignalR hub。geniussports 主机普通 TLS 可达。Turkish 官方三站（bsl.org.tr/tbl.org.tr/tbf.org.tr）默认直连全部连接层失败；proballers 403、eurobasket 000、realgm 403（8-E 域名级取证）、flashscore 可达但无 box——均未采用。
- 竞赛映射：`N. Hafta`→REGULAR_SEASON；`Playoff Çeyrek Final / Yarı Final / Final`→PLAYOFF；未知标签硬报错。

## 2. 数据规模与质量（BSL 2025-26，TBF + LiveStats 双源，真实数据）

| 项 | 值 |
|---|---|
| 赛季范围 | **2025-09-26 .. 2026-06-19**（揭幕战→总决赛） |
| 场次 | **261 入库** = 239 常规赛（30 轮×16 队=240 减 1 场记录性排除）+ 22 季后赛（四分之一决赛 9 / 半决赛 9 / 总决赛 4） |
| 球队 | 16（`BSL-` 前缀 TBF 数字 id，如 BSL-246936 Anadolu Efes） |
| 快照 | **`8ac434a8ddd3`**（全库 25,190 场） |
| raw bundle | `C:\BSL_DATA\bsl_2526_raw.json`，SHA-256 `f36ffa039d2a4885bc3f36c9b13d280c990877357e7db90244fda70f33ad04e5`；manifest 源标签 `bsl_livestats` |
| 验证 | validate **PASS（0 errors / 0 warnings）**；联赛审计 **PASS（11/11，以 `--expected-regular-map "2025-26=239"` 记录排除场）** |

质量细节（全部实测）：

* **两源交叉核对**：262 场 TBF 赛程比分 vs LiveStats box 逐场核对，261 场一致。
* 恒等式：fg（=2P+3P 合并）与 2P/3P 细分自洽；得分恒等式（2×2P+3×3P+FT）、篮板恒等式（orb+drb=trb）**522 行 0 违规**；重复 0；时区全部 aware（土耳其 UTC+3 永久制，保留 +03:00）。
* 加时 14 场：分钟数推得 OT 数与 TBF `hasUzatma` 旗 100% 一致；球队分钟 200+25×OT（28 行加时统计）**0 漂移**。
* **真实源缺陷（1 场，显式排除）**：matchId 303916（2025-10-04 Türk Telekom vs Bahçeşehir）LiveStats 流定格于第四节 09:37（75-74、152 分钟）且上游永不补全（2026-09-27 重取逐字节相同）；TBF 有终场 98-102 但无 box——按 ACB 先例在适配器 `KNOWN_SOURCE_DEFECTS` **显式排除并计数**，不伪造数据；该条目有条件生效（上游若补全则自动恢复入库，测试覆盖）。
* 记分员写法：`tot_sMinutes="199:60"`（=200:00 整）按 MM:SS 总秒数归一；同一球队的 LiveStats 3 字母码跨场漂移（Bursaspor 4 种码）——身份锚定 TBF id，码漂移记录于 `_meta.ls_codes_per_team`。
* 额外资产（仅记录，未扩 schema，遵指令 12）：LiveStats 逐球员行、PBP、投篮坐标（x/y）、lead/timeline 数据存在且已在抓取时丢弃（bundle 内 `_asset_flags` 记录存在性）。

## 3. 独立 walk-forward 基线（全季 239 场常规赛，预测 198 场；冻结默认参数）

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5859 | 0.6931 | 0.2500 | 0.0859 | n/a | n/a |
| elo | 0.6818 | 0.5949 | 0.2034 | 0.1208 | n/a | n/a |
| strength | 0.7424 | 0.5762 | 0.1944 | 0.1102 | 9.46 | 12.95 |
| **simulation** | **0.7424** | **0.5537** | **0.1852** | **0.0703** | 9.46 | 12.95 |

simulation 在 acc/logloss/brier/ECE 全部最优。单季小样本（198 场），按惯例不作长期结论。

## 4. 独立校准（前半季拟合 → 后半季测试；未复制任何其它联赛参数）

- 拟合（训练窗 2025-09-26..2026-01-13，仅 BSL 常规赛）：**HCA=3.47, scale=4.50, k=5.0, λ=0.020**；边界命中：无（scale=4.50 为网格 4.0..18.0 内点）；train val_logloss 0.5355。
- λ sweep（训练窗内）：0→0.5361，**0.02→0.5355（内点最优）**，0.05→0.5512。
- 测试（2026-01-13 起后半季 120 场常规赛）：

| variant | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| frozen_elo | 0.7167 | 0.5911 | 0.2011 | 0.1053 | n/a | n/a |
| frozen_strength | 0.7333 | 0.5831 | 0.1975 | 0.0947 | 9.39 | 12.52 |
| frozen_simulation | 0.7250 | **0.5652** | 0.1901 | 0.0709 | 9.39 | 12.52 |
| fitted_elo | 0.7167 | 0.5911 | 0.2011 | 0.1053 | n/a | n/a |
| fitted_strength | 0.6917 | 0.6144 | 0.2003 | 0.1127 | 9.75 | 12.86 |
| fitted_simulation | 0.7000 | 0.5953 | 0.2027 | **0.0548** | 9.75 | 12.86 |

- **诚实负结果**：fitted 在后半季测试 logloss 上劣于 frozen（strength +0.0313、simulation +0.0301）——按 C2 预登记采纳规则（测试窗 logloss 必须优于 frozen），**fitted 不构成 BSL 候选，frozen defaults 仍是 BSL 基准**；拟合参数仅存档（`storage/params/phase8f_bsl_2025-26_1s.json`，显式 opt-in）。train→test 的泛化退化与单季 119 场训练窗样本量一致，属预期风险而非流程缺陷。
- 反向信号如实记录：fitted_simulation 的 ECE 改善（0.0709→0.0548）但 logloss 恶化——与 C2 定义的 C3 候选模式（logloss 改善而 ECE 恶化）方向相反，不构成 C3 信号。
- HCA=3.47（vs NBA 1.7-2.1 / NBL 1.77 / CBA 4.16 / EL≈4）：独立拟合值，未复制任何联赛。

## 5. 闭环（显式 --params）

`predict BSL25-0261（总决赛，as-of 赛前）--params storage/params/phase8f_bsl_2025-26_1s.json --save` → 主队（Beşiktaş）68.9% → 真实 75-77 客队（Fenerbahçe）胜 → **WRONG**（brier 0.4749）——真实错判案例，验证的是 predict→settle→aggregate 全链路（id=7 入账；风险旗系统触发 LOW extreme_shooting_dependence 亦演示正常）。赛后时刻预测被现实校验拒绝（FAIL-as-designed，8-E 已验证同一机制）。

## 6. 隔离与回归

- 特征管线按目标场次联赛硬隔离（B3 结构）：BSL 数据不进入 NBA/EL/ACB/NBL/CBA 的 Elo/基线/校准；`window-backtest --league BSL` / `calibrate --league BSL` 独立评估；跨联赛拟合被结构性禁止。
- 前五联赛数据、模型、参数、既有校准文件零改动（本轮仅新增 BSL 采集层、`League.BSL` 枚举、regulation/审计映射与 CLI 选项）。
- 全量测试 **623 通过 + 1 既有环境分支跳过**（新增 23 项 BSL 测试，金样为真实 2025-26 场次：揭幕战、加时场、缺陷场、总决赛）。
- storage 零污染（raw/processed/manifest 仅 `bsl_livestats` 正名工件）；git clean。
- NBA 长窗 k=40 维持 C2 记录状态，未触碰（遵指令 10）。

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe tools\fetch_bsl_bundle.py --season 2025-2026 --out-dir C:\BSL_DATA --resume
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\BSL_DATA\bsl_2526_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\BSL_DATA\bsl_2526_raw.json --source bsl_livestats --snapshot-tag "ingest:bsl_livestats"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league BSL --expected-regular-map "2025-26=239"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league BSL --window-start 2025-09-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0
.\.venv\Scripts\python.exe -m basketball_agent.cli.main calibrate --league BSL --train-start 2025-09-01T00:00:00+00:00 --cutoff 2026-01-13T00:00:00+00:00 --test-start 2026-01-13T00:00:00+00:00 --test-end 2026-07-01T00:00:00+00:00 --save-params storage/params/phase8f_bsl_2025-26_1s.json
```
