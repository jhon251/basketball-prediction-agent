# Phase 10-E.1 — Production Collector Hardening（2026-09-28）

**性质**：修复 10-E 报告中明确暴露的两个生产缺口（增量 top-up、跨季队伍
映射）。冻结层（Prediction Core / 9-A~9-E / 10-A~10-D 契约与数据）零修改。

## 1. source_game_id → 内部 game_id 稳定映射（缺口 #2）

新增 `collection/ledger.py`：`SourceGameLedger`——逐联赛 append-only JSONL
（`storage/collector/`），映射源稳定 id（TBF matchId / CBA scheduleId /
NBL uuid / EL gameCode）→ 内部 game_id。

- 首记生效：同 source_id 重复映射到不同 game_id → `refusing re-map` 硬错
  （内部 id 不可变）；同映射重复 → `duplicate` 幂等；
- `max_game_seq(league, prefix)`：给 top-up 提供续号起点；
- 六联赛各自独立文件（隔离）；NBA 的 game_id 由日期+对阵自然派生（稳定），
  台账在其手动刷新时同样登记。

配套适配器增强（**默认行为不变**，全部带测试）：
- `bsl_livestats` / `cba_api` / `nbl_api` 的 parse 增加
  `game_seq_start=0`（top-up 续号）与 `_meta.source_ids`（逐场源 id，
  与 games 顺序一致）。冻结路径（不传参）逐位不变。

## 2. 已入库赛季安全增量 top-up（缺口 #1）

`refresh_season` 重写为双路径：

| 场景 | 行为 |
|---|---|
| 赛季 0 场（首季入驻） | 全量：fetcher → 适配器（默认 id 序）→ validate → ingest → snapshot |
| 赛季已有场次（top-up） | fetcher（resume）→ parse（`game_seq_start`=台账最大续号）→ **按台账过滤掉已入库场次** → **再按核心已有 game_id 二次去重** → validate → ingest 新场次 → 台账登记新映射 → snapshot（仅当有新场次） |

- 双重去重：台账 source id 过滤 + 核心 game_id 去重——已入库比赛**永不重复
  入库、永不重编号**（测试钉住）；
- 比分修正：台账已映射场次的源比分与核心不一致时 **report-only**
  （`RefreshResult.errors` 记录 "N score correction(s) detected
  (report-only)"），不自动改历史（与 10-D settlement 不可变语义一致）；
- 全部已入库 → `no_new_games` 状态。

## 3. BSL 2026-27 跨季球队映射（要求 4：先验证后映射，不猜测）

证据 = 两季官方 TBF 队名（取自入库 schedule 行）。11 对俱乐部身份明确：

| 2026-27 TBF id | 2025-26 canonical id | 俱乐部 | 依据 |
|---|---|---|---|
| 265990 | 246936 | Anadolu Efes | 队名逐字一致 |
| 265991 | 246960 | Bahçeşehir Koleji | 同 |
| 265992 | 246961 | Beşiktaş | 赞助商去名（Gain），联赛内唯一该俱乐部 |
| 265995 | 246972 | Tofaş | 同 |
| 266025 | 246965 | Fenerbahçe | 赞助商变更（Beko→Tarfin），唯一 Fenerbahçe |
| 266026 | 246966 | Galatasaray | 赞助商增名（MCT Technic），唯一 Galatasaray |
| 266027 | 246967 | Karşıyaka | 同 |
| 266029 | 246973 | Trabzonspor | 升班马，唯一 Trabzonspor |
| 266030 | 246974 | Türk Telekom | 同 |
| 266048 | 246971 | Aliağa Petkim | 赞助商拼写变体（Petkimspor→Petkim Spor） |
| 266176 | 246962 | Bursaspor | 赞助商去名（Yörsan） |

**不映射（不确定/新俱乐部）**：Pizzabulls Bordo Bandırma、Çayirova
Belediyesi、Yukatel Denizli Basket（Merkezefendi 与 Denizli 的关系无法仅
凭名称确证）——保持原 id，待更多证据后人工扩展。

适配器新增 `CLUB_ID_CONTINUITY_2026_27`；`resolve_team(id, "2026-27")` 映射
到 2025-26 canonical id，使 2026-27 新数据与既有两季 Elo/strength 历史链接。
**存量 7 场已入库比赛的身份已按新映射校正**（迁移审计：逐场 before→after
记录于执行输出；未映射 4 队保持原 id）。

## 4. 测试

新增/更新（合计 +20）：`test_source_ledger.py`（5：record/lookup/remap
拒绝/幂等/隔离/max_seq）、`test_collection.py` 扩展（15，含：二轮
no_change、比分修正检测为 updated、Scheduled→Final 转换指纹、单联赛失败
隔离、零场异常、manual_refresh 豁免、已入库赛季 top-up 去重（mock
fetcher）、未知赛季/unsupported 清晰失败）、`test_decision_log.py` 扩展
（BSL continuity 4 项：2024-25 映射、单季队不映射、2026-27 bundle 行经
台账 join、TBF box 路径）。

## 5. 真实增量 refresh smoke（2026-09-28）

`collector run --leagues BSL`（BSL 2026-27 已入库 7 场）→ refresh gate：
fetcher（resume，7 场 box 重取 + 233 场未赛跳过）→ parse（seq_start 续号）
→ 台账过滤 → **0 场新比赛**（源与核心一致）→ `no_new_games`，哈希链/数据
零变化。幂等与检测路径全部按预期；真实新比赛出现时将以同一路径自动
续号入库。

storage：新增 `storage/collector/` 台账 JSONL（gitignored）；快照
`49d685dc0732`（26,119 场）不变；git clean。
