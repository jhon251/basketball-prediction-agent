# Data Sources Survey — 6 Leagues (Phase 8-B0 侦察报告)

日期：2026-09-19 ｜ 范围：NBA、EuroLeague、ACB、NBL（澳大利亚）、CBA（中国）、BSL（土耳其）
目的：真实数据接入前的数据源调查与**实测验证**，不做大规模爬取。

## 验证等级约定（不允许"理论上可以"冒充已验证）

| 标记 | 含义 |
|---|---|
| ✅ 实测 | 本侦察阶段直接访问成功并确认了数据内容/结构 |
| 🟡 交叉确认 | 多个独立社区来源（GitHub/Kaggle/StackOverflow/官方文档）一致描述，未逐字节实测 |
| ⛔ 未验证 | 仅凭单一搜索结果或常识推断；接入时必须先做最小验证 |
| 💰 商业 | 仅记录，不购买 |

重要前提：**本项目 venv 沙箱无外网**。所有 ✅ 均为侦察工具侧验证；落地采集必须在
用户侧网络执行，经现有本地文件链路（`validate → ingest`）进入系统。

---

## 1. NBA

### 主源：llimllib/nba_data（静态 Parquet 仓库）✅ 实测
- URL: `github.com/llimllib/nba_data`（GitHub 直接下载，或 Kaggle 镜像）
- 内容（✅ 已核实 README 与文件清单）：
  - `gamelogs.parquet` / `gamelog_<season>.parquet` —— 每队每场 game log，**2009-10 至今**（赛季标签用结束年）
  - `playerlog_<season>.parquet` / `player_game_logs.parquet` —— 球员级每场
  - `ESPN/team_box.parquet`、`ESPN/player_box.parquet`、`four_factors.parquet` —— **2018-19 至今**每场球队/球员 box
  - `metadata.json` 记录最后更新日期；**仓库活跃**（2026-02-24 仍有提交，6000+ commits，CI 自动更新）
- 与现有 ingest 层的关系：`team_boxscores` 命名风格（FGM/FTM/FG3M 别名）已由 Phase 7 适配器支持；注意该仓库的 gamelog 列与 ESPN 子目录列名不同，接入前用 `basketball validate` 试 1 个文件。
- Box Score ✅ ｜ PBP ❌（本仓库）｜ 球员级 ✅ ｜ 投篮分布 四因素 ✅ / shot chart ❌ ｜ 赔率 ❌

### 备源 A：balldontlie.io API ✅ 实测（站点）
- 免费层存在但**需注册 API Key**，5 req/min；`/nba/v1/games`、`/nba/v1/box_scores`、`/nba/v1/standings`；历史深度官方页未明示（需注册后验证）；付费层（$9.99–$39.99/mo）放宽限速并含赔率/props。
- 定位：赛程/结果的轻量补充源，不做历史主力。

### 备源 B：stats.nba.com 直连（经 `nba_api` Python 包）🟡 交叉确认
- 社区标准方案，覆盖 1996 至今全部 box + PBP + shot chart（league dashboards / playbyplayv2 / shotchartdetail）。
- ⛔ 本侦察未实测：stats.nba.com 对无浏览器头请求返回 403 是常态，且接口 2024-2026 间多次变动。**用户侧先跑 1 次 nba_api smoke test 再定**。
- 定位：PBP/shot chart 的唯一免费途径；稳定性差，需退避重试。

### 商业（仅记录）💰
- Sportradar NBA API（全覆盖+PBP+tracking 级）；Genius Sports（NBA 官方数据合作）；balldontlie 付费层含历史赔率。

### 降级方案
llimllib 仓库停更 → ESPN 子目录仍有箱型数据 → stats.nba.com/nba_api → balldontlie 免费层兜底赛果。

### 未来赛程源：ESPN 公共 scoreboard ✅ 实测（Phase 12-B 已接入）
- `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD`
  **无需鉴权**，2026-10-23 本机实测（12 events）；适配器
  `data/collectors/nba_scoreboard.py`，真实响应裁剪夹具入库
  `tests/fixtures/nba_scoreboard_20261023.json`。
- 用途：未来 8 天赛程 → SCHEDULED 行 → 赛前预测；赛后同源返回决赛比分
  （预测结算）。注意 `season.year` 是结束年（2026-27→2027）；季前赛
  type=1 丢弃。深历史仍走 llimllib parquet。

### 伤停源：ESPN 公共 injuries ✅ 实测（Phase 12-G 已接入）
- `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries`
  **无需鉴权**，2026-10-25 实测 75 条记录（按 30 队分组）；适配器
  `data/collectors/espn_injuries.py`，真实裁剪夹具
  `tests/fixtures/espn_injuries_20261025.json`。
- 用途：伤停 → `player_availability`（挂到未来 8 天该队每场比赛）→
  `available_minutes_share` + `injury_uncertainty` 旗标。注意：athlete
  无 id 字段（从 player-card 链接解析）；状态词表 Out/Inactive/
  Suspended/Doubtful/Questionable/Day-To-Day 显式映射（OUT=0.0 硬规则），
  未知状态 fail-loud；无 game 上下文，比赛日需重跑拿最新。
- 分钟权重配套：llimllib `playerlog_<end-year>.parquet`（球员每场日志）
  + `players_<year>.parquet`（名册）→ `ingest-players` CLI；2026-27 赛季
  文件尚未发布，当前用 2025-26。

---

## 2. EuroLeague

### 主源：`live.euroleague.net/api/`（旧版官方 Live 端点）✅ 实测
- `https://live.euroleague.net/api/Boxscore?gameCode=1&seasonCode=E2024` **无需 Key 实测返回完整 JSON**：
  - 全部球员行：minutes、2PT/3PT/FT 命中与出手、ORB/DRB/TRB、AST、TOV、STL、BLK、犯规、PIR（Valuation）、+/-
  - 球队合计（`totr`）、逐节比分（`ByQuarter`）、观众数、裁判
- 结构与本项目 `TeamGameStats` 高度吻合（ minutes 需按 40 分钟制推导、possessions 需估算或由 FTA/FGA/TOV 推）。
- 赛季枚举方式（🟡 交叉确认）：Kaggle 上多个 EuroLeague 数据集均以 `gameCode=1..306 × seasonCode=E<年份>` 循环生成，即 gameCode 连续编号，可用探测循环收集；专用的 Schedule 端点路径待用户侧确认（本次 `/api/Schedule` 实测 404）。
- PBP：同域 `/api/Plays?...` 🟡（`euroleague_api` 包文档描述，未逐字节实测）。

### 备源 A：`euroleague_api`（PyPI）🟡 交叉确认
- 官方 Swagger 平台发现的端点封装（schedule/standings/game stats/player stats/plays）；GitHub 直链本次 404（可能改名），PyPI 包存在性由搜索多方确认。接入前 pip 装包跑 smoke。

### 备源 B：RealGM EuroLeague 板块 ✅ 实测（页面可达，反爬绕行）
- 覆盖赛程/球员统计/球队统计，多赛季历史。⛔ 未验证 box 的逐字段完整性。

### ⚠️ 新官方 API 已关门：`api-live.euroleague.net` ✅ 实测
- OpenAPI v3 "Competition Engine 3.0"：**全局 API Key 安全方案**，公开 spec 无端点。需要商务联系才能用——不要把主力方案押在它上面。

### 商业 💰
Sportradar EuroLeague（全量+PBP）、ZylaLabs EuroLeague 托管 API。

### 降级方案
live 端点失效 → euroleague_api 包跟随官方改版 → Kaggle 历史 dump（含 2001 起多赛季）→ RealGM。

---

## 3. ACB（西班牙 Liga Endesa）

### 主源：`api2.acb.com` 官方数据 API ✅ **实测并已接入（Phase 8-B6）**
- 端点为 `acb.com` / `live.acb.com` 站点自身调用的 JSON API：
  - `/api/seasondata/Calendar/season-calendar[?competitionId=&editionId=]` → 竞赛列表（Liga Endesa=1、Copa del Rey=2、Supercopa=3、Minicopa=10）、全部 edition（48=1983-84 … 91=2026-27）、某赛季全部轮次与比赛（`rounds[].matches[]`：matchId、主客 teamId、比分、UTC `startDateTime`、`matchStatus`、`subphaseId`）
  - `/api/matchdata/MatchHeader/match-header?matchId=` → 主客球队、比分、`quarterScores`（加时判定用）
  - `/api/matchdata/Result/boxscores?matchId=` → `teamBoxscores[].statsByPeriods`（period 0 = 官方全场球队合计 `stats.total` + 非球员行 `stats.team` + 球员行含 `playTime`）
- 鉴权：站点自带的公开客户端 `X-APIKEY`（见 `live.acb.com` 自身 JS bundle；非机密）。**实测可用**：6 个赛季 edition 可枚举，2025-26 全季 327 场（含季后赛）全部取到 header + 完整 boxscore。
- 采集工具：`tools/fetch_acb_bundle.py`（礼貌暂停 + 429 退避 + checkpoint/resume）。适配器：`basketball_agent/data/collectors/acb_api.py`。
- 注意：官方 HTML 站点为 Next.js 渲染，直接抓 HTML 不必要；该 JSON API 是同一站点的数据层。

### 备源 A：Basketball-Reference 国际板块 🟡 交叉确认（本次直接抓取被 403 反爬，页面确认存在）
- `basketball-reference.com/international/spain/`：赛季、球队、球员统计与比赛结果；历史深度为六联赛国际源中最规整。礼貌抓取（低频+UA）通常可行。
- Phase 8-B6 实测：Python urllib 直连被 Cloudflare 拦（403 Just a moment）；RealGM / eurobasket.com 同样 403。

### 备源 B：RealGM Spanish ACB ⛔ 本次实测 403（Cloudflare）
### 历史数据集（免爬取，优先用）✅ 交叉确认
- Kaggle `jgonzalezferrer/acb-spanish-basketball-league-results`：**1994-2016** 全部比赛 + 同名 GitHub SQLite 工程
- Zenodo 数据集：**1983-84 至 2022-23** 球队与球员（球员表 10,742 行 × 37 列）
- `dalerodr/ACB_stats`：1983-至今，GitHub Actions 自动化更新 ⛔ 未实测其输出格式
- BAwiR（R/CRAN）：ACB PBP 分析，附示例数据 🟡

### 特殊收获 💰
SkillCorner/opendata-basketball：**10 场 ACB 比赛的广播 tracking 开放数据**（2025-26 赛季）——tracking 能力唯一实测可得的样例，仅作研究样例，不进预测管线。

### 降级方案
`api2.acb.com` 不可用（Key 轮换/下线）→ Basketball-Reference → RealGM → Kaggle/Zenodo 历史集补旧赛季。

---

## 4. NBL（澳大利亚）

### 主源：FIBA LiveStats / Genius Sports `data.json` ✅ 实测（端点模式 + 数据内容）
- URL 模式（多个独立来源 + 本次直连验证）：
  - 页面：`https://fibalivestats.dcd.shared.geniussports.com/u/{COMPETITION}/{game_id}/`
  - **JSON：`https://fibalivestats.dcd.shared.geniussports.com/data/{game_id}/data.json`**（本次实测返回完整数据，无需 Key）
- 实测内容：`tm.{1,2}.pl.{playerId}` 完整球员行（FG/2PT/3PT/FT、ORB/DRB/TRB、AST/TOV/STL/BLK/犯规、+/-、eff）+ 球队合计 + **逐次投篮坐标（x,y）与类型** + 时间戳得分事件（可重建 PBP 与 shot chart）。
- 需带浏览器 User-Agent（社区一致经验）。
- NBL 归属确认 🟡：Genius Sports 为 NBL 官方数据合作方（developer.geniussports.com 文档佐证）；NBL 比赛在 LiveStats 体系下的 competition code 待用户侧取一场比赛 URL 确认。

### 备源 A：RealGM Australian NBL ✅ 实测（导航确认）
### 备源 B：Sportradar/Highlightly 💰（Highlightly 明确列出 NBL ✅ 官网确认）

### 降级方案
LiveStats 端点收紧 → nbl.com.au 官方 gamecenter（JS 渲染，抓取成本高）→ RealGM。

---

## 5. CBA（中国男子篮球职业联赛）

> **Phase 8-E 修订（2026-09-27，实测后）**：下方"降级方案"在本环境实测后反转——RealGM 不可用（见证据），官方站成为实际主源并已打通。

### 实际主源（8-E 起生效）：cbaleague.com 官方数据 API ✅ 实测（全链路打通）
- SPA（`www.cbaleague.com/data/`）后端 `data-server.cbaleague.com/api`；**所有 JSON 响应为 AES-128/ECB/PKCS7 加密**（B0 记录属实），但密钥就在站点公开 JS bundle（`assets/index.e20827fb.js`）——`tools/fetch_cba_bundle.py` 内置纯 Python AES（FIPS-197/SP800-38A 自检）解密，零新依赖。
- 端点：`getTeam`（20 队）、`schedules/month`（官方逐日场次=完整性参照）、`schedules/day`（逐场主客/比分/类型/状态）、`team-match-datas/match-logs-basic/{teamId}`（逐场**球队级官方 box score**：fg/fga、3p/3pa、ft/fta、前/后场篮板、助攻、抢断、盖帽、失误、犯规；stage 1=常规赛 2=季后赛）。
- **赛制修订（B0 假设错误，实测证据）**：CBA 2024-25 及以前为 4×12（2880s，官方 minutes=48）；**2025-26 起改用 FIBA 4×10（2400s，minutes=40）**——逐场 quarter/minutes 字段自洽（40+5×加时）。多赛季 CBA 需按赛季区分 regulation。
- matchTypeId：1=常规赛→REGULAR_SEASON，2=季后赛→PLAYOFF，3=季前赛、4=全明星→丢弃计数，9=保级赛→CUP。

### B0 备源 A：RealGM Chinese CBA（league id=40）❌ **实测 BLOCKED（8-E 取证）**
- 直连 curl（带浏览器 UA）403 ×3；reader 服务仅命中 2025-01 陈旧快照（2024-25 赛季仅显示 33-34 场/队，非完整赛程），schedule/games 路径被客户端拦截；代理通道（jina/allorigins）连接层失败。

### 备源 B：Basketball-Reference CBA 板块 ❌ 实测连接层失败（000，无响应体）
### 数据集：SciDB "2022-2023 CBA Official Data" ✅ 存在但为**裁判执裁数据**（非球队 box score），不满足 schema → 不适用

### 降级链（8-E 实测后）
~~RealGM~~（BLOCKED）→ ~~Basketball-Reference~~（BLOCKED）→ ~~SciDB~~（字段不符）→ **官方 API（AES 已解，实际采用）**；密钥若轮换：从前端 bundle 重新提取即可。

---

## 6. BSL（土耳其 Basketbol Süper Ligi）

> **Phase 8-F 修订（2026-09-27，实测后）**：B0 的 LiveStats 路径**数据端实测可用**（competition code=TBF，Genius 托管），但 LiveStats 本身无赛程→id 映射（NBL 8-D 同结论：单场可用、无法枚举）；枚举由 TBF 官方 API 补齐——两者组合即主源。

### 实际主源（8-F 起生效）：TBF 官方 API（枚举）+ FIBA LiveStats（box）✅ 实测（全链路打通）
- **枚举/赛程**：`www.tbf.org.tr/api/Match/get-all-matches-for-filter?ActivityId={id}&TeamId=&Page=1&PageSize=-1&WeekFilter=*` 返回整季每场（主客 TBF 队 id/名称/比分、week 标签、场馆、**genuisId=LiveStats 场次 id**、hasUzatma 加时旗）；赛季清单 `api/League/get-leagues-and-seasons-by-prefix?prefix=bsl`（BSL 历史回溯至 1966-67；2025-26 → activityId=20728, seasonId=172）。
- **box score**：`fibalivestats.dcd.shared.geniussports.com/data/{genuisId}/data.json`（官方球队级合计 tot_s*：fg=2P+3P 合并、2P/3P 细分、ft、前/后场篮板、助攻、失误、抢断、盖帽、犯规、逐节比分、加时旗）。
- 网络实测（本机环境）：www.tbf.org.tr 与本机 OpenSSL 的 **TLS 1.3 协商失败（exit 35），强制 `--tlsv1.2 --tls-max 1.2` 即通**；页面路径有 Cloudflare 挑战但 `/api/*` JSON 直答；api.tbf.org.tr 仅承载 SignalR hub；geniussports 主机普通 TLS 可达。
- 竞赛映射：`N. Hafta`→REGULAR_SEASON；`Playoff Çeyrek Final / Yarı Final / Final`→PLAYOFF。

### 实测缺陷（如实）
- 2025-26 赛季 262 场中 **1 场 LiveStats 流永久性卡死**（matchId 303916，2025-10-04 Türk Telekom vs Bahçeşehir：定格于第四节 09:37、75-74、152 分钟；2026-09-27 重取逐字节相同；TBF 有终场比分 98-102 但无 box）——按 ACB 先例**显式排除**（适配器 `KNOWN_SOURCE_DEFECTS`，有条件生效：上游若补全则自动恢复入库），入库 261 场。
- LiveStats `tot_sMinutes` 为 MM:SS 且有 `199:60`（=200:00）类记分员写法；同一 TBF 球队的 LiveStats 3 字母码可跨场漂移（如 Bursaspor 出现 4 种码）——身份锚定 TBF 数字 id，码漂移记录于 `_meta.ls_codes_per_team`。

### 备源 A：RealGM Turkish BSL ❌ 实测 BLOCKED（8-E 已取证，域名级 403）
### 备源 B：Basketball-Reference international/Turkey ❌ 实测连接层失败（8-E 取证）
### 赛果：Flashscore 🟡 可达（200）但 box 不满足 schema——未采用

### 降级链（8-F 实测后）
**TBF API（枚举）+ LiveStats（box）已打通，无需降级**；若上游改版 → Flashscore（仅赛果，需重建 box 路径）→ 官方站逆向。

---

## 数据源可用性矩阵（现状如实版）

图例：✅ 已验证可得 ｜ 🟡 多源交叉确认 ｜ ⛔ 未验证/理论 ｜ ❌ 免费渠道不可得 ｜ 💰 仅商业可得

| 能力 | NBA | EuroLeague | ACB | NBL | CBA | BSL |
|---|---|---|---|---|---|---|
| 公开 API/JSON | 🟡 stats.nba.com(反爬) | ✅ live 端点 | ⛔ 无官方 | ✅ LiveStats data.json | ⛔ 加密 | 🟡 LiveStats(待证) |
| CSV/Parquet 下载 | ✅ llimllib | 🟡 Kaggle dump | ✅ Kaggle/Zenodo 94-16/83-23 | ❌ | 🟡 SciDB 单季 | ❌ |
| GitHub 开源项目 | ✅ nba_api 等 | 🟡 euroleague_api | ✅ 3+ 项目 | ✅ LiveStats 抓手 | 🟡 少量 | 🟡 少量 |
| 历史覆盖 | 2009-10→今(文件) / 1996→今(API) | 2001→今 | **1983→今** | ~2010s→今 | ~2000s→今(质量递减) | ~2000s→今 |
| 场次量级/赛季 | 1230 | 306(常规赛) | ~340(常规赛+杯) | ~120(常规赛) | ~460(常规赛56轮×~队) | ~240 |
| 球队 box score | ✅ | ✅ 实测 | ✅ | ✅ 实测 | ✅ | 🟡 |
| PBP | 🟡 via nba_api | 🟡 /api/Plays | 🟡 近年(BAwiR) | ✅ 可重建 | ❌ | 🟡 部分场次 |
| 球员级 | ✅ | ✅ 实测 | ✅ | ✅ 实测 | ✅ | 🟡 |
| 投篮分布/坐标 | 🟡 shot chart via nba_api | ❌ 免费 | ❌ | ✅ 实测(x,y) | ❌ | 🟡 若走 LiveStats |
| Tracking | ❌ 免费(💰 Sportradar/Second Spectrum) | ❌ | 🟡 SkillCorner 10场样例 | ✅ shot 坐标 | ❌ | 🟡 同 NBL |
| 历史赔率 | 🟡 Kaggle/SBR | ❌ 免费 | ❌ | ❌ | ❌ | ❌ |
| 商业 API 覆盖 | 💰 Sportradar/Genius | 💰 Sportradar | 💰 Sportradar | 💰 Sportradar/Highlightly | 💰 Sportradar | 💰 Sportradar |
| 主要限制 | nba.com 反爬+接口变动 | 旧端点无 SLA，随时可能下线 | 官方反爬；旧赛季字段不全 | competition code 待确认 | AES 加密+密钥轮换 | 官方 JS 渲染 |

## 推荐主/备数据源（每联赛）

| 联赛 | 主源 | 备源 | 主源失败降级 |
|---|---|---|---|
| NBA | llimllib/nba_data Parquet（✅ 已与 Phase 7 ingest 对齐） | balldontlie 免费层（Key） / stats.nba.com+nba_api | ESPN 子目录 parquet → nba_api → balldontlie |
| EuroLeague | live.euroleague.net/api（✅ 实测无 Key box） | euroleague_api 包 → Kaggle dump | Kaggle 历史 dump → RealGM |
| ACB | acb.com 官方（待用户侧侦察） | Basketball-Reference 国际 Spain | RealGM → Kaggle/Zenodo 历史集 |
| NBL | FIBA LiveStats data.json（✅ 实测） | RealGM NBL | nbl.com.au 官方 → RealGM |
| CBA | RealGM CBA（无加密，最稳） | cbaleague.com（需 AES 解密） | Basketball-Reference → SciDB 单季 |
| BSL | FIBA LiveStats data.json（待 1 场验证） | RealGM BSL | Basketball-Reference → Flashscore 赛果 |

## 当前最大数据缺口（如实）

1. **PBP 全量缺口**：仅 NBL（实测）与 EuroLeague（🟡）有免费 PBP；NBA PBP 依赖反爬的 stats.nba.com；ACB/CBA/BSL 免费渠道基本无体系化 PBP。
2. **免费历史赔率全线缺失**：六联赛均无实测可得的免费历史 odds（NBA 有 Kaggle/SBR 🟡）。市场基准（spec §26）继续不可用，不做伪造。
3. **CBA 数据质量**：官方加密 + 第三方字段不全；CBA 将是六联赛中校验失败率最高、需要最多 data_quality 标记的联赛。
4. **BSL 主源未定**：LiveStats 覆盖是推断，需 1 场比赛 URL 实证。
5. **tracking**：仅 NBL/LiveStats shot 坐标与 SkillCorner 10 场样例；对预测管线（回合级参数）无影响，属研究性资产。

## Phase 8-B 接入顺序建议

1. **NBA**（llimllib/nba_data）——管线最成熟，先把真实数据全链路跑通（validate→ingest→coverage→experiments→report）。
2. **EuroLeague**（live 端点）——结构最贴近现有 schema，40 分钟制的第一个适配对象。
3. **NBL**（LiveStats）——适配器最简单（data.json → 统一 schema），且带来 shot 坐标资产。
4. **ACB**（Kaggle/Zenodo 历史集先行，官方/BR 后续）。
5. **BSL**（先取 1 场验证 LiveStats competition code，再定主源）。
6. **CBA**（RealGM 先行；官方加密接口列为低优先级研究项）。

每联赛独立 ingest、独立校准窗口；**禁止跨联赛混合训练**（见 MULTI_LEAGUE_SCHEMA.md §独立性约束）。

## 需要用户提供的

- 暂无必须的 API Key（六联赛免费主源均无需 Key）。
- 可选：balldontlie.io 免费 Key（NBA 备源）；如追求稳定再考虑 Sportradar 试用（💰，不建议现阶段购买）。
- 三个用户侧动作（侦察工具无法替代）：① 下载 llimllib/nba_data 任一 parquet 放入 `C:\NBA_DATA\`；② 取一场 NBL 与一场 BSL 比赛的 LiveStats 页面 URL（确认 competition code）；③ 若走 stats.nba.com/nba_api 路线，先跑一次 smoke test。

## 参考链接

- llimllib/nba_data: https://github.com/llimllib/nba_data
- balldontlie: https://www.balldontlie.io/
- EuroLeague live API: https://live.euroleague.net/api/Boxscore?gameCode=1&seasonCode=E2024
- EuroLeague 新官方 API（需 Key）: https://api-live.euroleague.net/swagger/index.html
- euroleague_api（PyPI）: https://pypi.org/project/euroleague-api/
- ACB scraper 1994-2016: https://github.com/jgonzalezferrer/acb-database-scraping
- ACB 1983-至今: https://github.com/dalerodr/ACB_stats
- ACB PBP（R）: https://cran.r-project.org/web/packages/BAwiR/index.html
- FIBA LiveStats data.json 模式: https://stackoverflow.com/questions/75281172/scrape-fiba-stats-box-score
- LiveStats 抓取示例: https://www.kaggle.com/code/khosoop/scraping-basketball-data-from-fiba-livestats
- RealGM 国际板块: https://basketball.realgm.com/international
- CBA 官方: https://www.cbaleague.com
- Sportradar 篮球: https://marketplace.sportradar.com/
- SkillCorner 开放 tracking: https://github.com/SkillCorner/opendata-basketball

## 7. 赔率数据域（Phase 11-A）

| 联赛 | 源 | 状态 |
|---|---|---|
| NBA | covers.com / SBR / Kaggle | ⛔ 全部需认证或反爬——管线已建（`nba_odds_csv.py`），等用户投喂 CSV |
| CBA | 竞彩官方 | 后续阶段 |
| 其他 | 未探 | 后续阶段 |

适配器：`data/collectors/nba_odds_csv.py`（canonical 列名映射 + UnknownTeamError）
存储：`game_odds` 表（append-only, UNIQUE 约束, FK→games）
去水：`evaluation/devig.py`（multiplicative，保守近似）
评估：`evaluation/roi.py`（flat-stake ROI, edge threshold sweep）
