# Multi-League Unified Data Contract（Phase 8-B0 设计稿）

目标：让 NBA、EuroLeague、ACB、NBL、CBA、BSL 六个联赛通过**同一套比赛级数据契约**
进入现有管线（validators → DataIngestor → SQLite → snapshot → features → models）。
本文件是设计契约；实现（enum 扩展 + adapter 代码）属后续阶段。

## 0. 与现有 NBA schema 的关系

现有 `Game`（game_id/league/season/game_date/home_team_id/away_team_id/
home_score/away_score/status）与 `TeamGameStats`（15 项统计 + minutes +
possessions）**保持不变**。本契约在此之上定义统一输入行（CSV/JSON 的"每场一行或每队一行"格式），
由各联赛 adapter 归一化后写入同样的库。需要的代码级扩展仅两处（均为加法）：

1. `League` 枚举增加 `ACB = "ACB"`、`NBL = "NBL"`、`BSL = "BSL"`
   （`EUROLEAGUE`、`CBA` 已存在；`GameStatus` 已含 POSTPONED/CANCELLED，无需改）。
2. `Game` 增加三个可选元数据字段：`source`、`source_game_id`、`competition`
   （旧数据留空，向后兼容；见 §3 溯源字段）。

## 1. 统一字段清单与级别

级别定义：
- **REQUIRED** — 缺失即校验 FAIL，不进入库（现有校验门禁语义不变）；
- **CONDITIONAL** — 条件性必填（如 FINAL 比赛的 box score）；
- **OPTIONAL** — 缺失可接受，值为 NULL；
- **DERIVED** — 不要求来源提供，由确定性公式从其他字段推导；
- **NOT_AVAILABLE** — 本阶段免费渠道不可得，字段保留位置但恒空，**不允许伪造**。

### 1.1 比赛身份字段

| 字段 | 级别 | 说明 |
|---|---|---|
| `league` | REQUIRED | 枚举：NBA / EUROLEAGUE / ACB / NBL / CBA / BSL |
| `competition` | REQUIRED | 同联赛内的分区：如 NBA=regular/playoffs/in-season；EuroLeague=regular/playoffs；ACB=liga/copa/supercopa；NBL=regular/playoffs；CBA=regular/playoffs；BSL=regular/playoffs/cup。**用于把训练窗口限制在同一 competition** |
| `season` | REQUIRED | 统一格式 `{起始年}-{结束年两位}`，如 `2024-25`（见 §4 赛季命名） |
| `game_id` | REQUIRED | **入库主键，由 adapter 生成**：`{LEAGUE}:{season}:{source_game_id}`，如 `EURO:2024-25:E2024-1`、`NBA:2024-25:0022400123`、`NBL:2024-25:2305989`。保证六联赛全局唯一（见 §6 game_id 冲突） |
| `source_game_id` | REQUIRED | 来源系统原生 ID（回溯与去重的锚点） |
| `source` | REQUIRED | 来源标识：`nba_data` / `euroleague_live` / `acb_official` / `livestats` / `realgm` / `cbaleague` / `bref` … |
| `fetched_at` | REQUIRED | ISO-8601 UTC，采集时刻 |
| `game_date` | REQUIRED | ISO-8601 **UTC**（见 §4 时区）；开球时刻未知时允许日期 00:00Z，但必须标记 `data_quality.tz_approx` |
| `status` | REQUIRED | 复用现有 GameStatus：SCHEDULED / LIVE / FINAL / POSTPONED / CANCELLED |

### 1.2 对阵与结果

| 字段 | 级别 |
|---|---|
| `home_team`（来源原名） | REQUIRED |
| `away_team`（来源原名） | REQUIRED |
| `home_team_id` / `away_team_id` | REQUIRED（经 `normalize_team` 得到的规范 ID） |
| `home_score` / `away_score` | CONDITIONAL：FINAL 时必填且为整数 ≥0；SCHEDULED/POSTPONED 时必须为空 |
| `period_scores`（逐节） | OPTIONAL（EuroLeague ByQuarter、LiveStats 可得；缺失不影响入库） |
| `overtime`（是否加时/加时数） | DERIVED（由逐节推断）或 OPTIONAL |

### 1.3 球队 box score（每队一份；FINAL 必须双方齐备才能通过现有校验）

| 字段 | 级别 | 备注 |
|---|---|---|
| `FG, FGA, FG3, FG3A, FT, FTA` | CONDITIONAL（FINAL REQUIRED） | 与现有 23 列最小集一致 |
| `ORB, DRB, TRB` | CONDITIONAL（FINAL REQUIRED） | 校验器强制 ORB+DRB==TRB |
| `AST, TOV, STL, BLK, PF` | OPTIONAL | 缺失记 NULL；TOV 缺失会降低 possessions 估算质量（记 data_quality） |
| `possessions` | **DERIVED** | 缺失时按 `fga + 0.44*fta - orb + tov` 估算并标记 ESTIMATED（现有行为不变）；若 TOV 缺失则该公式不可用，possessions 记 NOT_AVAILABLE 并由更高层降级（该场比赛不参与 pace 类特征） |
| `minutes` | DERIVED | 按联赛规则固定：NBA=240，FIBA 系=200（见 §4 比赛时长） |

### 1.4 数据质量与溯源

| 字段 | 级别 | 取值 |
|---|---|---|
| `data_quality` | REQUIRED（枚举集合） | `OK` / `ESTIMATED_POSSESSIONS` / `MISSING_TEAM_STATS_OPTIONAL`（AST/TOV 等缺）/ `TZ_APPROX` / `SOURCE_UNVERIFIED`（如 RealGM 抓取页）/ `MERGED_FROM_MULTIPLE_SOURCES` |
| `source` / `source_game_id` / `fetched_at` | REQUIRED | 见 §1.1 |

### 1.5 明确的 NOT_AVAILABLE（本阶段）

| 字段 | NBA | EuroLeague | ACB | NBL | CBA | BSL |
|---|---|---|---|---|---|---|
| `odds_*`（历史赔率） | 🟡 Kaggle/SBR 待验证 | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE |
| `pbp`（体系化逐回合） | 🟡 via nba_api | 🟡 /api/Plays 待实测 | 🟡 近年 | ✅ 可重建 | NOT_AVAILABLE | 🟡 部分 |
| `shot_coords`（投篮坐标） | 🟡 | NOT_AVAILABLE | NOT_AVAILABLE | ✅ | NOT_AVAILABLE | 🟡 |

赔率缺失 ⇒ spec §26 的 market baseline 在六联赛中继续**如实标注 unavailable**。

## 2. 各来源字段映射要点

| 来源 | 原生字段 → 统一字段 |
|---|---|
| llimllib/nba_data (ESPN team_box) | FGM→FG、FTM→FT、FG3M→FG3、队名列→home_team/away_team（别名表归一）；**无 TOV 时 possessions 不可估** → 检查列字典后定 |
| EuroLeague `live/api/Boxscore` | `totr.sFieldGoalsMade`→FG 等；PIR(Valuation) 保留为 source 特有列（不入 schema）；逐节 `ByQuarter`→period_scores；**minutes 由球员行汇总或按 200 推导** |
| FIBA LiveStats `data.json` | `tm.1` = 主队 / `tm.2` = 客队（**必须用 venue/官方对阵信息核对主客**）；`tot_s*`→队统计；`pl.*`→球员级（可选入库）；`sFieldGoalsMade...` 同上 |
| RealGM | HTML 表解析 → 每场得分 + 球员场均；box 逐字段完整性低 → 标 `SOURCE_UNVERIFIED`，多场抽样对账后才可升级 |
| cbaleague.com | AES 解密后 JSON → 字段映射待逆向确认 |
| Basketball-Reference 国际 | HTML 表 → 球队级统计，列名与 NBA 表基本同构 |

## 3. 溯源与去重（同一比赛多来源）

- **主键即去重锚**：`(league, season, source_game_id)` 唯一；同一来源重复 → 校验 FAIL（现有 duplicate-game 检查）。
- **跨来源合并**：同一真实比赛经两个来源时，`(league, season, competition, game_date 的日期部分, home_team_id, away_team_id)` 相同视为同一比赛：
  - 取**质量更高的来源**为准（官方 > LiveStats > RealGM/BRef）；
  - 次来源仅用于补缺（如官方缺 TOV、RealGM 有）→ 合并行标 `MERGED_FROM_MULTIPLE_SOURCES`；
  - 分数对不上时 **FAIL 并人工裁决**，不静默取一边。
- 现有 `merge_raw_datasets` 的跨文件重复 FAIL 语义保持；跨来源合并是 adapter 层显式步骤，不放进同一个 ingest 批次。

## 4. 联赛差异专项处理

### 4.1 比赛时长（48 vs 40 分钟）
| | NBA | EuroLeague/ACB/BSL/CBA | NBL |
|---|---|---|---|
| 规制 | 4×12 | 4×10 | 4×10 |
| 队分钟 | 240 | 200 | 200 |
| 加时 | +5 | +5 | +5 |

- `TeamGameStats.minutes`（球队总分钟）由 adapter 按上表 DERIVED，**不得硬编码 240**（现有 nba_csv 对 NBA 硬编码 240 保持不变，那是 NBA 专用路径）。
- 对模型的影响：PPP/pace 均为每回合或每分钟率，48/40 天然被率化吸收；**唯一必须守住的是" possessions = per-team 回合数"的 NBA 口径约定**（Phase 5 已修正），FIBA 系同样按每队回合计。
- 仿真层 `REGULATION_SECONDS=2880` 是 NBA 专用；FIBA 联赛接入仿真时需参数化（2400 秒）——**本阶段不改代码，只记录**。

### 4.2 赛季命名
- 六联赛赛季均跨公历年（NBL 10月→2月，其余 10月→6月）。
- 统一 `season = {起始年}-{结束年两位}`：NBA `2024-25`、EuroLeague `2024-25`（source `E2024`）、ACB `2024-25`、NBL `2024-25`（source 常写作 "2024-25" 或单年）、CBA `2024-25`、BSL `2024-25`。
- adapter 把来源原生标签映射为统一格式；`_season_for()` 的月份推断规则（≥7 月归下一赛季）对全部六联赛成立，但 **adapter 必须优先用来源原生标签**，推断只作 fallback。

### 4.3 球队改名 / 缩写 / 历史别名
- 每联赛一张别名表（结构同 `nba_teams.py`）：`{ACB,NBL,CBA,BSL}_TEAMS` + 别名 → 规范 ID。
- 规范 ID 命名空间按联赛隔离：`NBA:LAL` 与欧洲球队无冲突风险，但统一采用 `{league}:{abbr}` 内部表示防撞名（如 ACB 的 "BAS" 与任何联赛无冲突，但 CBA 与中国 CBA 联赛缩写同形——**NBA 的 CBA 联赛枚举值是 Continental Basketball Association 历史遗留，注意区分**）。
- 未知球队名 → 现有语义：**FAIL 报出，不猜测**（`nba_csv._team_id` 同款）。
- 搬迁/改名映射进别名表（如 ACB: Caja Laboral→Baskonia、TAU；CBA: 多次冠名商变更，用城市主体名作规范 ID，赞助商名进别名）。

### 4.4 时区
- `game_date` 统一存 UTC；来源给本地时间的按联赛默认时区转换：NBA=America/New_York（客队时区不追）、EuroLeague/ACB/BSL=Europe 系（按来源）、NBL=Australia/Melbourne 等、CBA=Asia/Shanghai。
- 只给日期不给时刻的来源：存 `00:00Z` + `data_quality.TZ_APPROX`。**as_of 纪律不变**：预测 as_of ≤ game_date；TZ_APPROX 的比赛在"赛前数小时"级别才有歧义，日粒度回测不受影响。

### 4.5 常规赛 / 季后赛 / 杯赛
- `competition` 字段区分；杯赛（ACB Copa del Rey、Supercopa；EuroLeague 无单场杯；BSL Cup；CBA 无）单独成 competition，回测默认只用 regular+playoffs，杯赛可入历史但不进默认训练窗。
- EuroLeague 与 EuroCup 是**两个 competition**（不同参赛队集合），不得混在 EUROLEAGUE league 名下默认合并。

### 4.6 取消 / 延期 / 加时
- POSTPONED/CANCELLED：`status` 正确标记，score 必须为空；这些比赛**不进任何特征/回测**（现有 `GameStatus.FINAL` 过滤天然保证）。
- 加时：box score 是含加时的合计，无需拆分；`overtime` 可选字段仅记录。 possessions 估算公式天然包含加时回合，无需修正。

### 4.7 数据缺失
- 缺 OPTIONAL 统计 → NULL + `MISSING_TEAM_STATS_OPTIONAL`；模型层现有 None-fallback（收缩到联赛均值）已兼容。
- 缺整场 box（比分-only 来源）→ **拒绝入库**（现有 FINAL 必须可对账的设计不变）。
- 缺 possessions 且缺 TOV → 该场不参与 pace 特征，其余特征照常。

## 5. 独立性约束（硬规则，Phase 8-B0 起生效）

1. **禁止跨联赛混合训练**：每联赛独立 ingest → 独立 DB（或独立 league 命名空间）→ 独立 calibration 窗口。
2. **禁止跨联赛共享 Elo**：Elo ratings 的键空间按联赛隔离。
3. **禁止共享联赛基线**：`LeagueFeatureSet`（league averages）只在同联赛历史上计算——现有代码按传入 history 计算，天然满足；adapter 层必须保证 feature 计算的 repo 查询带 league 过滤（现有 `games.league` 列可支持，**待 8-B 实现时验证 list_games 过滤**）。
4. 48/40 分钟差异只允许通过率化特征（PPP、per-100）进入模型，绝不允许绝对分数特征跨联赛迁移。
5. 参数拟合（HCA、logistic scale、shrinkage k、λ）逐联赛独立执行并独立冻结（复用 `param_tuning.py`，repo 限定单联赛）。

## 6. game_id 冲突预防（细则）

- 格式 `{LEAGUE}:{season}:{source_game_id}`，source_game_id 保留来源原样（数字或字符串）。
- 来源未提供原生 ID（如部分 CSV）→ source_game_id = adapter 生成 `{date}_{home}_{away}` 并标 `data_quality: DERIVED_ID`；同日同对阵二番战由来源提供轮次后缀，否则 FAIL。
- 侦察已验证的 ID 形态：EuroLeague `gameCode`（1..306 连续整数，需拼 seasonCode 才唯一——**这就是 game_id 必须带 season 的实证**）；LiveStats 数字 game_id（如 2305989，跨 competition 可能复用，同样必须拼 competition/season）。
