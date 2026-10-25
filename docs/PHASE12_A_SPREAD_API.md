# Phase 12 — 让分概率 + NBA 未来赛程 + 本地 API/前端

日期：2026-10-23 ｜ 性质：三个闭环（A 让分概率层、B NBA 赛程采集、C/D
matchup 解析 + HTTP API + 静态前端）。**冻结层（模型/特征/模拟参数）零改动**；
全部为新增消费层，881 个新/旧测试全绿（988 passed + 1 skipped，基线 907）。

## 12-F 置信度诚实化（2026-10-24 追加）

用户观察到"置信度一直是 84"——定位为构造性问题：`confidence_score` 是
罚分清单（100 − 15×HIGH − 8×MEDIUM − 4×LOW），只能取离散档位，且
`high_variance` 用全局阈值 15.0 在 NBA 上**每场必触发**（实测 25 场 std
分布 15.2–16.8），等于恒扣 8 分。三项修复：

1. **high_variance 阈值重校准（按联赛 p90）**：六联赛各测 25 场近期常规赛
   （冻结参数、400 sims、seed=7）的模拟分差 std 分布——

   | 联赛 | p50 | p90 → 新阈值 |
   |---|---|---|
   | NBA | 16.0 | 16.7 → 16.8 |
   | CBA | 15.3 | 15.8 → 15.9 |
   | NBL | 14.6 | 15.3 → 15.4 |
   | BSL | 14.0 | 14.7 → 14.8 |
   | ACB | 14.1 | 14.6 → 14.7 |
   | EUROLEAGUE | 13.9 | 14.3 → 14.4 |

   `RiskConfig.margin_std_threshold_by_league`（None 时用上表；未测联赛回退
   标量 15.0）。重校准后 8 场样本的置信度从 {92,92,76,84,84,84,84,76} 变为
   {100,100,92,92,92,92,84,84}——恢复区分度。冻结契约只钉接口不钉阈值，
   语义不变（检查表，非概率）。

2. **真实校准的把握（命中率分桶表）**：`evaluation/confidence_calibration.py`
   + `basketball confidence-calibrate` CLI。在冻结 walk-forward 窗口上把
   每场按所选一方概率（max(p,1−p)）分入 5 个百分点档位（0.50-0.55 …
   0.95-1.00），记录同档位实际命中数；三家家族分别建表，持久化
   `storage/calibration/confidence_calibration.json`（原子写）。API
   `predict` 响应新增 `calibration` 块（opt-in：文件不存在则 null，文件
   更新按 mtime 自动重载）：`{bucket, picked_side_is_home, n,
   empirical_hit_rate}`。

3. **前端改版**："把握"卡改为显示**该档位历史命中率**（如 62.0%，注明
   n 场样本与档位），检查分降为次要参考；新增独立"风险检查"卡（触发
   N 项 + 明细列表）。无校准文件时明确提示运行 `confidence-calibrate`。

校准窗口：2022-10-01 起全部六联赛（NBA 覆盖 2022-23 → 2025-26），共
8,933 条 walk-forward entries（NBA 4,920 / EL 1,274 / ACB 1,192 /
CBA 829 / BSL 434 / NBL 284；300 sims/场，seed=7）。代表性数字（各联赛
最大桶，Elo vs simulation）：

| 联赛 | Elo 最大桶 | 命中率 | Sim 最大桶 | 命中率 |
|---|---|---|---|---|
| NBA | 0.55-0.60 (n=593) | 54.8% | 0.50-0.55 (n=1,539) | 50.3% |
| EUROLEAGUE | 0.55-0.60 (n=209) | 56.9% | 0.50-0.55 (n=309) | 51.8% |
| CBA | 0.65-0.70 (n=128) | 71.9% | 0.50-0.55 (n=118) | 44.9% |
| NBL | 0.65-0.70 (n=49) | 67.3% | 0.50-0.55 (n=52) | 48.1% |

两个如实记录的结构性事实：Elo 在每个联赛都有最宽的概率区分度（10 个
桶全满）且同档位命中率更高；simulation 家族概率大量堆积在 0.50-0.55
（对比赛接近掷硬币），且其 0.60-0.65 档在 NBA 的实测命中率 58.4%
（n=1,002）低于标称——前端现在把这个差距直接暴露给用户，而不是用检查分
掩盖。

## 12-A 让分（spread）概率层

`agents/spread_probability.py`（纯函数，stdlib `statistics.NormalDist`）：

* 约定：`home_line` 是加在主队得分上的让分，主队赢盘 ⇔
  `margin + home_line > 0`；整数线有走盘（push），0.5 步长校验失败即抛错。
* 两个独立供给方：
  1. `cover_from_samples` — 直接统计冻结 Monte Carlo 的逐场 margin 样本
     （`MonteCarloResult.runs` 已保留，无需改模拟层）；
  2. `cover_from_elo_normal` — Elo 胜率经逆概率单位映射
     `mu = σ·Φ⁻¹(p)` + 连续性修正的整数分差正态，σ 由 as-of 之前的历史
     分差拟合（泄漏规则 §2 安全；<200 场退回冻结默认 11.5）。

### 家族决策（用数据定，不拍脑袋）

`evaluation/spread_calibration.py`，严格 walk-forward（as-of = 开球前一天，
冻结默认参数），2024-25 NBA 常规赛 12-25 之后 798 场、7,026 条线探测
（线网格锚定在 Elo 隐含期望分差 ±{0,1,3,6,10}，取最近 0.5 步长；整线走盘
不计；无真实盘口数据——全部赔率源 auth-blocked，见 DATA_REQUIREMENTS_ODDS）：

| 供给方 | log loss ↓ | Brier ↓ |
|---|---|---|
| **elo_normal** | **0.4446** | **0.1402** |
| simulation_samples | 0.4692 | 0.1506 |
| 常数 0.5 基线 | 0.6931 | 0.2500 |

σ = 13.91（17,888 场 ≤2024-06 常规赛分差拟合）。

**决策**：NBA 默认 `elo_normal`（`api/server.SPREAD_SOURCE_BY_LEAGUE`），
与 MULTI_SEASON_RESULTS 里 Elo 胜率全窗口最优一致；其它联赛默认
`simulation_samples`（Elo 历史薄）。API 同时返回两个口径 + 每请求可覆盖
（`spread_source`）。

**口径限制（如实记录）**：
* 线网格锚定 Elo 中心而非真实市场线——测的是分布形状校准 + 部署问题的
  近似；sim 口径在同一外部锚定的网格上评估，对其中心偏差不利（部署时
  赌徒面对的正是外部市场线，该近似偏保守）。
* 整体 home_cover_rate 0.613 ≠ 0.5：窗口内实际分差整体高于冻结 Elo 隐含
  中心（中心偏差），两个供给方同等承受；中心精度另由胜率 logloss 覆盖。

## 12-B NBA 未来赛程（ESPN 公共 scoreboard）

`data/collectors/nba_scoreboard.py`：
* 源：`site.api.espn.com/.../nba/scoreboard`（无需鉴权，本机实测可达；
  夹具 `tests/fixtures/nba_scoreboard_20261023.json` 为真实响应裁剪版）。
* 缩写映射：ESPN 30 队全部枚举核实，6 个差异 GS→GSW、NO→NOP、NY→NYK、
  SA→SAS、UTAH→UTA、WSH→WAS；未知缩写 fail-loud。
* 赛季标签：ESPN `season.year` 是**结束年**（2026-27→2027），标签取
  `year-1 / year%100`；季前赛（type 1）整行丢弃；季后赛（type 3）标
  PLAYOFF（默认训练历史仍常规赛 only）。
* 生命周期（`collection/nba_fixtures.py`，production loop 与 API 共用）：
  pre → SCHEDULED 行（`NBA{yy}-{seq}` 顺序号 + ledger 去重，与 BSL/NBL/CBA
  同规）；post → ledger 已知行经 `Repository.update_game_result` 落 FINAL
  比分（预测结算所需），未知行归 parquet 深历史路径跳过。
* 实测：一次 pass 入库 65 场未来 8 天比赛，零错误；随后
  `predict NBA26-0001` 全链正常。
* `Repository.update_game_result`（新增，附加方法）：仅允许非 FINAL →
  FINAL，已 FINAL 拒绝覆写（第二次不同比分=数据问题，绝不静默）。

## 12-C Matchup 解析

`api/matchup.py`：team_id/缩写精确 → NBA 别名表（含 ESPN 缩写）→
中文别名（30 队，`湖人/雄鹿/独行侠…`）→ 唯一名称子串；歧义/未命中带
候选列表 fail-loud。`find_scheduled` 严格主客定向（换主客=不同对阵，
故意不模糊匹配）。

## 12-D HTTP API + 静态前端

`api/server.py`（stdlib `http.server`，零新依赖；pydantic 请求模型）+

| 路由 | 说明 |
|---|---|
| `GET /api/health` | 存活 + 各联赛/状态计数 |
| `GET /api/teams` | 按联赛分组，NBA 带中文 |
| `GET /api/schedule?league=&days=&refresh_nba=1` | 未来赛程；`refresh_nba=1` 触发一次 ESPN pass |
| `POST /api/predict` | game_id 或 league+home+away；`home_line`、`spread_source`、`as_of`、`n_simulations`、`family`、`refresh_nba` |

predict 响应：胜负概率（主家族 + elo/strength/simulation 三家对比）、
预期比分/分差/总分、分差分布 P5–P95、让分双口径（含默认口径标注与 σ）、
风险把握（confidence + 旗标）、解释层（prediction/risk/limitations）、
快照/版本、模拟次数。UI（`api/static/index.html`，中文、零构建）：
联赛/主客选择（NBA 中文）、让分输入、赛程点选填入、胜负双条、让分卡、
把握卡、家族对比表、口径与限制折叠；页脚明示"置信度≠对市场的优势"。

CLI：`basketball serve --host --port --n-simulations`。

实测（真实 DB、真实 HTTP）：
* `雄鹿 vs 公牛，主队 -3.5`：主胜 60.8%（sim）/Elo 65.6%，让分主赢盘
  sim 52.5% vs elo 56.2%，把握 92/100；
* NBL（Illawarra vs Brisbane，明日）：默认 simulation_samples 口径生效；
* 未知 game_id/无未来对阵 → 明确 404 JSON；非 UTF-8 请求体 → 400。

## 12-G 伤停层点亮（2026-10-25 追加）

12-F 记录的最大盲区是伤停：`player_availability` 表 0 行、players/
player_game_stats 也是 0 行——`available_minutes_share` 结构性返回 1.0，
`injury_uncertainty` 旗标永远不可能触发。本节用两个真实数据源把它接活：

1. **分钟权重（llimllib `playerlog_2026.parquet` + `players_2026.parquet`，
   2025-26 赛季）**：`data/collectors/nba_playerlog.py` + `collection/
   player_data.py`。一次入库 582 名球员 + 28,701 行球员统计（0 失败；
   2,618 行比赛不在库——季前赛等未入库场次，计数跳过；6,086 行 DNP 无分
   钟记录，计数跳过）。细节：parquet 的 gameId 是 int（前导零丢失），
   zfill(10) 恢复成库内规范形式；minutes "MM:SS" 转浮点（含上游
   "30:60" 舍入残留的进位归一）；未知球队缩写 fail-loud。CLI：
   `basketball ingest-players <playerlog.parquet> --roster <players.parquet>`。

2. **伤停报告（ESPN 公共 injuries 端点，无需鉴权）**：`data/collectors/
   espn_injuries.py`。75 条真实记录 → 按未来 8 天赛程展开成 216 行
   availability（每条伤停 × 该队未来每场比赛），20 个无法解析的名字
   （2026 新秀不在上赛季日志里）如实计数跳过。硬规则（DATA_REQUIREMENTS）：
   OUT=0.0 钉死；Day-To-Day→PROBABLE 0.75、Doubtful→0.25、
   Questionable→0.5，未知状态 fail-loud；source_time=抓取时间 < 开球
   （结构性满足泄漏规则）；名字按全名精确不区分大小写连接 players 表，
   歧义即弃。幂等：主键 (player_id, game_id, source_time)，重复抓取按
   mtime 语义叠加，known_at 永远取最新报告。CLI：`basketball
   nba-injuries`；production loop 的 NBA pass 与 API refresh 路径均已
   接线（失败隔离，不阻塞预测）。

**实测（2026-10-25，NBA26-0020 GSW @ PHX）**：库里 PROBABLE、巴特勒/
波杰姆斯基/穆迪 OUT → 客队可出场份额 0.772、主队 0.917，
`injury_uncertainty`（MEDIUM）首次在真实数据上触发；HTTP 响应新增
`availability` 块（份额 + 每条伤停记录），前端新增"伤停"卡。

**如实边界**：① 概率本身不变——冻结设计是"风险层绝不改动概率"，
伤停只影响旗标/解释/份额（规格书 §20）；要让它进概率需要新的训练
目标，属下一阶段决策。② 分钟权重来自 2025-26 赛季（llimllib 尚未发布
2026-27 文件），对新秀和夏天转会的球员覆盖不全——解析不到即跳过并
计数，不猜测。③ ESPN 伤停没有 game 上下文，同一伤停挂在窗口内该队
每场未来比赛上，比赛日当天应重跑 `nba-injuries` 拿最新状态。

## 局限与后续

* 让分校准基于模型锚定线网格，真实盘口校准待赔率数据解锁
  （DATA_REQUIREMENTS_ODDS）；
* ESPN 只覆盖未来 8 天窗口（`n_days` 可调），赛季完整日程未拉全量；
* NBA scheduled 行的深度历史回填仍走 parquet 手动刷新；
* API 单机单用户定位（ThreadingHTTPServer，无鉴权——仅绑 127.0.0.1）。

## 12-H 其余联赛推进（2026-10-25 追加）

目标：把 Phase 12 的能力按"数据可达性"扩展到其余五联赛。结论先行：
让分/校准/中文别名全部六联赛可用；未来赛程 ACB/CBA 新接入；EuroLeague
赛程与五联赛伤停**确认不可行**（如实记录，不硬造）。

### 1) 中文别名（NBL/EUROLEAGUE/ACB/BSL，CBA 库内本就是中文）

`api/matchup.py` 的别名表扩展为 `CHINESE_ALIASES_BY_LEAGUE`（NBA 36 /
NBL 20 / EL 28 / ACB 27 / BSL 22 条）。解析规则不变；非 NBA 联赛的
表内 id 必须存在于该联赛 roster（防手写表笔误静默错解析），NBA 的
30 队固定代码表保持权威直通。全部表 id 已对真实库校验。

### 2) 让分家族默认口径——五联赛实测定案

`evaluate_spread_calibration` 扩跑五联赛（同 NBA 协议：冻结 walk-forward、
线网格锚定 Elo 隐含中心）：

| 联赛 | 场次 | 线探测 | elo_normal | sim_samples | 结论 |
|---|---|---|---|---|---|
| EUROLEAGUE 24-25 | 305 | 2,672 | **0.4269** | 0.4385 | elo |
| ACB 25-26 | 306 | 2,690 | **0.3577** | 0.3779 | elo |
| NBL 25-26 | 163 | 1,425 | 0.5025 | **0.4972** | sim（噪声级差异） |
| CBA 25-26 | 420 | 3,704 | **0.3150** | 0.3331 | elo |
| BSL 25-26 | 237 | 2,081 | **0.3824** | 0.3941 | elo |

`SPREAD_SOURCE_BY_LEAGUE` 更新：五联赛默认 `elo_normal`，NBL 保持
`simulation_samples`。实测 σ：EL 12.75 / ACB 13.92 / NBL 11.50 /
CBA 16.33 / BSL 13.65。

### 3) 未来赛程新接入：ACB + CBA

* **ACB（api2.acb.com 赛季日历，2 次调用）**：`sources.acb_schedule_rows`
  306 场（36 完赛 + 260 未赛 + 1 进行中丢弃 + 9 未定时丢弃）。两个源
  坑如实记录：① 未来场次比分初始化为 0——状态判定必须用 `matchStatus`
  （FINALIZED/NOT_STARTED），不能从比分推断；② **2026-27 起 ACB 把日历
  里的 team id 换成赛季级新 id（4463+）**，稳定的 `clubId` 只在
  MatchHeader 里——`acb_edition_team_map` 用已完赛场次 Header 建桥
  （18/18 全部命中既有 clubId，缓存 24h `acb_team_map_91.json`）；
  尚未打过球的俱乐部当轮报错、下场球后自动可解（不猜）。game_id 用
  `ACB{yyyy}-{源id}`（与深刷新同规范，避免双 ID）；实测 260 行入库
  + 中文预测链全通（拉科鲁尼亚@穆尔西亚，elo_normal 让分口径生效）。
* **CBA（data-server 日/月赛程，AES 解密）**：`cba_schedule_rows` 升级为
  真行输出（当前+下月窗口）。两个源坑：① 状态语义与 NBL 相反（4=完赛、
  1=未赛），在源头归一化成 upsert 约定；② 未赛场次 matchTime 恒为
  00:00 占位——kickoff 记为当日上界 23:59 UTC（保守边界，不猜时刻，
  保证同日赛前预测可用）。实测 47 行入库，预测链通（辽宁 vs 新疆）。

### 4) 确认不可行（如实记录）

* **EuroLeague 未来赛程**：无已验证的日历端点（Calendar 探测返回非
  JSON）；仓库既有做法是盲枚举比赛码逐场拉 Header（~300 请求/赛季），
  适合赛季刷新、不适合每轮循环。待官方日历端点被验证后再接。
* **五联赛伤停**：EuroLeague/ACB/NBL/CBA/BSL 无公开结构化伤停报告
  （伤停数据基本是 NBA 特有生态）。`injury_uncertainty` 在这些联赛
  结构性保持"无数据=不出旗标"，前端伤停卡如实显示"未接入"。

### 生产语义

ACB/CBA fixtures 已接入 production loop（`_acb_fixtures_pass` +
通用 `_upsert_scheduled_pass`），每轮自动发现未来赛程 + 决赛落分。
实测后 DB SCHEDULED 分布：NBA 72 / NBL 145 / BSL 175 / ACB 260 /
CBA 47——**六联赛里五个有可预测的未来赛程**（EuroLeague 除外）。
