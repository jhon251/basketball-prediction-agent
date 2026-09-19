# LeagueAdapter Contract（Phase 8-B0 接口设计稿）

六个联赛（NBA、EuroLeague、ACB、NBL、CBA、BSL）通过**同一套标准接口**进入系统。
本文件定义接口、流水线阶段、失败语义与每个 adapter 的最小实现清单。
本阶段只交付契约文档，不实现代码；实现顺序见 DATA_SOURCES.md「接入顺序」。

## 1. 流水线（所有联赛同一条管线）

```text
fetch(season, competition)
  → parse(raw_record)               # 来源原生 → 中间表示 ParsedGame
  → normalize_team(raw_name)        # 来源队名 → 规范 TeamIdentity
  → normalize_game(parsed)          # 中间表示 → UnifiedGame（MULTI_LEAGUE_SCHEMA §1.1/1.2）
  → normalize_boxscore(parsed)      # → UnifiedBoxScore（§1.3；缺则 None + data_quality 记录）
  → validate(unified_batch)         # 复用现有校验电池，FAIL 不落盘
  → [现有] DataIngestor.ingest(raw)  # 同一套入库门禁（parse→validate→load，全有或全无）
  → [现有] RawDataStore / ProcessedDataStore / SnapshotManager  # 溯源链完全复用
```

关键不变量：**adapter 只做"翻译"，不做"放宽"**。校验器、入库门禁、快照、
as_of 防泄漏、train-only 拟合对六个联赛一视同仁。

## 2. 接口定义

```python
from typing import Protocol, Iterable
from basketball_agent.data.schemas.base import League

@dataclass(frozen=True)
class SeasonSpec:
    season: str          # 统一格式 "2024-25"
    competition: str     # "regular" / "playoffs" / "cup" / 来源原生等价物

@dataclass(frozen=True)
class TeamIdentity:
    team_id: str         # 规范缩写（联赛内唯一，如 "MIA"、"BAS"、"MEL"）
    name: str
    alias_used: str | None   # 命中的别名（审计用）

@dataclass
class UnifiedGame:       # = MULTI_LEAGUE_SCHEMA §1 全字段
    league: League
    competition: str
    season: str
    game_id: str                 # "{LEAGUE}:{season}:{source_game_id}"
    source_game_id: str
    source: str
    fetched_at: datetime
    game_date: datetime          # UTC
    home_team: TeamIdentity
    away_team: TeamIdentity
    home_score: int | None
    away_score: int | None
    status: GameStatus
    box: UnifiedBoxScore | None  # None 仅允许 status != FINAL
    period_scores: ... | None
    overtime: bool | None
    data_quality: frozenset[str]

@dataclass
class UnifiedBoxScore:   # 双方各一份，字段见 MULTI_LEAGUE_SCHEMA §1.3
    home: TeamBoxLine | None
    away: TeamBoxLine | None

class LeagueAdapter(Protocol):
    league: League                     # 类属性，如 League.ACB
    source_name: str                   # "acb_official" 等，写进 provenance

    def fetch(self, spec: SeasonSpec, **filters) -> Iterable[RawRecord]:
        """枚举来源数据。要求：
        - 可断点续传（yield 逐条；调用方记录进度）；
        - 网络失败抛 FetchError，调用方决定退避重试；
        - 不做静默截断：枚举到的比赛数若低于该 competition 已知场次的
          合理下限（如 EuroLeague 常规赛 306），必须由调用方按
          report/coverage 的 missing-games 逻辑显式暴露，不得自行补齐。"""

    def parse(self, raw: RawRecord) -> ParsedGame:
        """来源原生 JSON/HTML 行 → ParsedGame（原始字段 + 原始队名 + 原始分节）。
        解析失败抛 ParseError（现有 ParseError 语义），绝不出 NaN/默认值填充。"""

    def normalize_team(self, raw_name: str) -> TeamIdentity:
        """别名表命中 → 规范 ID；未知 → 抛 UnknownTeamError（等价于现有
        nba_csv 的 unknown-team FAIL 行为，绝不猜测、绝不静默跳过）。"""

    def normalize_game(self, parsed: ParsedGame) -> UnifiedGame:
        """字段映射 + game_id 生成 + 时区转换 + 赛季格式统一。
        主客判断不确定（如 LiveStats 的 tm.1/tm.2）时必须用来源的
        venue/官方对阵信息核对，核不出 → 抛 NormalizeError。"""

    def normalize_boxscore(self, parsed: ParsedGame) -> UnifiedBoxScore | None:
        """box 行映射 + minutes 按联赛规制 DERIVED（240/200）+ possessions
        派生或估算标记。FINAL 缺关键列 → 返回带缺失标记的行，由 validate
        决定 FAIL（与现有 23 列最小集语义一致）。"""

    def validate(self, games: list[UnifiedGame]) -> ValidationReport:
        """复用现有 checks 电池（duplicate/score-vs-box/bounds/temporal/
        referential...），另加两项联赛级检查：
        - competition 一致性（同一 batch 不得混 competition）；
        - minutes 合规（NBA=240、FIBA 系=200，误差 0）。
        返回现有 ValidationReport，ERROR 语义不变。"""
```

## 3. 每联赛 adapter 的最小实现清单

| 联赛 | fetch | parse 难点 | normalize_team | 预计复杂度 |
|---|---|---|---|---|
| NBA | 文件下载（llimllib parquet / CSV） | gamelog vs ESPN 两种列集；FGM→FG 别名（已有） | 复用 `nba_teams.py` | **最低**（Phase 7 已完成 80%） |
| EuroLeague | `gameCode` 1..N × `seasonCode=E{yy}` 循环探测 | `totr` 嵌套结构；minutes 推导 | 新表：18 队 + 历史搬迁（Baskonia/TAU 等） | 低 |
| NBL | game_id 枚举 → data.json | `tm.1/tm.2` 主客核对；UA 头 | 新表：10 队 + 搬迁（Bullets 返队等） | 低 |
| BSL | 同 NBL（待 1 场实证 competition code） | 同 NBL | 新表：16 队（升降级注意：**BSL 有降级，球队集合逐年变**） | 低-中 |
| ACB | 阶段一：Kaggle/Zenodo 历史文件；阶段二：acb.com/BR 抓取 | HTML/旧数据字段缺（老赛季无 3P） | 新表：约 20+ 历史队，**别名最多**（赞助商冠名） | 中 |
| CBA | 阶段一：RealGM；阶段二：官方 AES 解密 | 解密管道；字段逆向 | 新表：20 队，**冠名商别名大量**（按城市主体规范） | 中-高 |

## 4. 失败语义（与现有纪律一致）

| 情况 | 行为 |
|---|---|
| 来源可达但比赛缺失 | 不伪造；coverage 报告的 missing-games 按已知赛程参照暴露（无赛程参照时如实写 unavailable，现有语义） |
| 未知队名/别名 | UnknownTeamError → 该批 FAIL（不跳过） |
| box score 与比分对不上 | 现有 `consistency.game_score_vs_team_stats` FAIL |
| 主客不确定 | NormalizeError FAIL（LiveStats tm.1≠主队的情况真实存在） |
| 同比赛多来源分值不一致 | FAIL 人工裁决（MULTI_LEAGUE_SCHEMA §3） |
| 网络失败 | FetchError + 指数退避；resume 从上次进度，不重写已有记录 |
| 来源结构性改版 | adapter 版本号 +1，写入 `source_version`（RawDataStore 已支持） |

## 5. 实现顺序与验收门槛（每个 adapter）

1. **NBA**：真实 parquet → validate PASS → ingest → coverage → 一次 `report` 真实输出。
2. **EuroLeague**：≥1 个赛季全量 box → 通过全部校验 → 40 分钟制 minutes/possessions 抽查 5 场对账。
3. **NBL**：≥1 个赛季 → data_quality 标记抽查 → 主客核对抽查 10 场。
4. **ACB**：历史集先入（1983-2016 Kaggle/Zenodo）→ 官方/BR 抓取另立批次。
5. **BSL**：competition code 实证后才写 fetch。
6. **CBA**：RealGM 批次单独走，全部标 `SOURCE_UNVERIFIED` 直到对账抽样通过。

每个 adapter 的单测最低要求：1 个真实样例文件的 golden test + 主客核对样例 +
未知队名 FAIL 样例 + minutes 规制断言。

## 6. 禁止事项（写进 adapter 代码审查清单）

- 不放宽/绕过现有校验器（ERROR≠WARNING 语义）。
- 不猜测队名映射、不猜测比分、不填充默认统计值。
- 不做跨联赛 Elo/参数共享（MULTI_LEAGUE_SCHEMA §5）。
- 不在 adapter 层做任何"预测相关"计算——adapter 是纯数据翻译层。
- 不绕过 RawDataStore/manifest 直接写库——溯源链是硬要求。
