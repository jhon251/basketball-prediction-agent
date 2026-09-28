# Phase 10-E — Production Data Collection & Scheduling（2026-09-28）

**性质**：生产采集层与调度层。冻结层（8-L～9-E、10-A～10-D）**零修改**。
新增 `basketball_agent/collection/`（state/sources/scheduler）+ CLI
`collector run/status` 与 `agent status`。

## 1. 架构与六联赛形态

```
CollectorScheduler.run(leagues)      ← 每联赛独立 try/except（隔离）
  ├─ NBA         discovery: GitHub 提交新鲜度（parquet 文件源；ingest=文档化手动刷新）
  ├─ EuroLeague  discovery: live.euroleague.net 可达性（ingest=既有 fetcher）
  ├─ ACB         discovery: 官方 --list-seasons 探针
  ├─ NBL         discovery: 官方 calendar API（逐场 status/比分指纹）
  ├─ CBA         discovery: 官方 data-server（赛季 + 月度场次计数；AES 解密）
  └─ BSL         discovery: TBF 官方 API（逐场 status/比分指纹）
```

- **状态持久化**：`storage/collector/<LEAGUE>.json`（原子写：tmp+replace），
  字段：heartbeat / last_status / last_success_at / last_new_games /
  last_updated_games / last_validation / last_errors / last_error /
  seasons_ingested / games_seen（源 id 指纹）/ last_detail / runs（滚动 10 次）。
- **幂等**：源游戏指纹（status+比分哈希）入 `games_seen`；重跑无变化 →
  `no_change` 零写入；比分/状态变化 → `updated` 计数（Scheduled→Live→Final
  转换即指纹变化，可观测）。
- **快照不污染**：只有 season-refresh gate（validate→ingest→snapshot）产生
  快照；discovery/no_change 永不写库。
- **赛季刷新门**（`refresh_season`）：仅当目标赛季在核心中 **0 场** 时允许
  （首季入驻）；经既有 fetcher（子进程，幂等 --resume）→ 冻结适配器 →
  validate → ingest → snapshot。TBF 标签 "2026-2027" 自动规范化为 "2026-27"
  与核心一致。已入库赛季拒绝（incremental top-up 为未实现项，见 §6）。
- **传输事实**：TBF/CBA 需 TLS 1.2（8-F/E 发现）；CBA 响应为 AES-ECB
  密文（前端公开密钥解密）；所有 discovery 客户端带重试退避。

## 2. 真实运行结果（2026-09-28 live smoke）

- 首轮：**NBL 发现 190 场（2026-27）**、**BSL 发现 240 场（2026-27，含
  7 场已完赛）**、NBA/EL/ACB 指纹基线建立；ACB/CBA 首轮失败（import 缺陷）
  被隔离记录，修复后 **6/6 OK**。
- **真实入库**：BSL 2026-27 经 refresh gate 入库 **7 场已完赛真实比赛**
  （2026-09-25 开季，TBF 原生 box），验证通过，快照 `49d685dc0732`
  （26,119 场）；未完赛 233 场由 fetcher 跳过（修复：无比分不抓 box）。
- **幂等复跑**：`collector run --leagues BSL` 第二轮 → `no_change`（指纹
  稳定，零写入）。
- **真实失败可观测**：过程中 TBF TLS rc=35 瞬断、GitHub 限流 403、CBA
  偶发 404/非 JSON 全部被记录到 state 的 last_errors（加重试退避后自愈，
  NBA GitHub 限流为外部配额）。

## 3. CLI

```powershell
collector run                                  # 全联赛 discovery pass
collector run --leagues BSL,CBA --ingest      # + season-refresh gate
collector status [--league BSL]                # heartbeat/state 只读
agent status                                   # 全局：DB/快照/collector/agent log/模型版本
```

## 4. 测试

新增 `tests/unit/test_collection.py`（15 项，全部 mock discovery，无网络）：
state 心跳/成功不回退/隔离/指纹合并/历史上限/赛季戳；scheduler 幂等
（二轮 no_change）、比分更新检测、Scheduled→Final 转换、单联赛失败隔离、
失败记 error 且 success 不回退、零场异常标记、manual_refresh 豁免；
refresh gate：已入库拒绝（含标签规范化 2026-2027→2026-27）、未知赛季
清晰失败、unsupported 联赛。

storage 零污染（`storage/collector/` gitignored）；git clean。

## 5. 已知限制 / 开放项

1. **赛季中增量 top-up 未实现**：game_id 按入库批次顺序分配，重复 ingest
   已入库赛季会重编号——collector 因此对已入库赛季拒绝刷新（open item：
   稳定源 id → game_id 映射增强）。
2. **BSL 2026-27 队伍 id 为新 per-season id**（TBF 无稳定俱乐部 id，8-F
   已证）：与 2024-25/2025-26 的 Elo 链接需要扩展 club continuity 映射
   （需对新季 16 队逐一验证后显式加入）。
3. NBA/EL/ACB 的 discovery 为探针级（新鲜度/可达性/赛季表），自动 ingest
   走各自既有 fetcher 命令（手动触发）。
4. Scheduler 周期运行为外部职责（任务计划程序/cron 调 `collector run`）；
   本阶段不内置常驻进程。
