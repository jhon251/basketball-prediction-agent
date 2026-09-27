# Phase 8-E — CBA（中国）首次真实数据接入（2026-09-27）

## 1. 数据源（含 B0 降级链实测反转）

- **B0 既定主源 RealGM 实测 BLOCKED（取证完整）**：直连 curl（带浏览器 UA）403 ×3（/international、league/12、league/40）；reader 服务仅命中 2025-01 的陈旧缓存快照（2024-25 赛季仅渲染 33-34 场/队，非完整赛程），schedule/games 路径直接被客户端拦截（ERR_BLOCKED_BY_CLIENT）；代理通道（r.jina.ai / allorigins）连接层失败。
- 其余降级项同样实测排除：Basketball-Reference 连接层失败（000 无响应体）；SciDB "2022-2023 CBA Official Data" 为**裁判执裁数据**（非球队 box score），不满足 schema。
- **实际主源（按 B0 降级链末端"官方站点"）：cbaleague.com 官方数据 API**。SPA 后端 `data-server.cbaleague.com/api`，所有 JSON 响应为 AES-128/ECB/PKCS7 加密（B0 记录属实）；密钥位于站点公开前端 bundle（`assets/index.e20827fb.js`）。`tools/fetch_cba_bundle.py` 内置零依赖纯 Python AES-128 解密（启动即跑 FIPS-197 C.1 + SP800-38A F.1.2 自检，且曾以 openssl 对真实密文差分校验）。
- 端点（全部实测）：`com-code-tables/getTeam`（20 队）、`schedules/month`（官方逐日场次 → 完整性参照）、`schedules/day`（逐场主客/比分/类型/状态/节次）、`team-match-datas/match-logs-basic/{teamId}`（逐场**球队级官方 box score**，stage 1=常规赛 / 2=季后赛）。无伪造、无替代源硬凑。
- matchTypeId 映射：1=常规赛→REGULAR_SEASON，2=季后赛→PLAYOFF，3=季前赛、4=全明星→丢弃+计数，9=保级赛→CUP（历史保留）。

## 2. 数据规模与质量（CBA 2025-26，官方 API，真实数据）

| 项 | 值 |
|---|---|
| 赛季范围 | **2025-12-12 .. 2026-06-05**（揭幕战→总决赛，单季完整） |
| 保留场次 | **453**（420 常规赛 + 33 季后赛；2 场全明星周末由适配器丢弃并计数） |
| 球队 | 20（`CBA-` 前缀官方俱乐部 id，如 CBA-29128 浙江浙商证券） |
| 快照 | **`20e1e2a1df57`**（全库 24,929 场） |
| raw bundle | `C:\CBA_DATA\cba_cba25_raw.json`，SHA-256 `b4129ab1bcece731fc0bd6aef14d554865bdd90a07d9b56b59ff2c51d6edcf7c`；manifest 源标签 `cba_api` |
| 验证 | validate **PASS（0 errors / 0 warnings）**；联赛审计 **PASS（11/11，无 WARN）** |

质量细节（全部为实测）：

* 906 行球队 box（453 场 × 主客）与官方赛程**两源比分交叉核对：0 不一致**（赛程行 vs 两队各自 match-log 行的三方一致）；join 完整率 453/453。
* 得分恒等式（2P/3P 合并）与篮板恒等式（orb+drb=trb）**906 行 0 违规**；重复 0；时区全部 aware（北京时间 +08:00 保留）。
* 完整性：官方 `schedules/month` 逐日场次合计与实际入库一致，常规赛 42 轮 × 20 队 = 420 场**零缺失**。
* 加时 22 场（含 2 场双加时）：官方 quarter/minutes 字段自洽（minutes = 40 + 5×加时；球队分钟 = (40+5×OT)×5 = 200+25×OT），44 行加时统计 0 违例。
* **赛制修订（B0 假设错误，实测证据）**：CBA 2024-25 及以前为 4×12 分钟（官方 minutes=48 样本为证）；**2025-26 起改用 FIBA 4×10（2400 秒）**。`LEAGUE_REGULATION_SECONDS["CBA"]=2400` 按本季实测登记；`REGULATION_TEAM_MINUTES[CBA]=200` 恰与实测一致。多赛季 CBA 需按赛季区分 regulation（已在代码注释与 DATA_SOURCES 记录）。
* 流程瑕疵（如实记录）：首次 ingest 漏带 `--source` 导致 manifest 源标签误挂 `nba_games`；已按存储纪律外科回滚（定向删除 453 场及其统计行、raw/processed/快照三处工件），复验回到 8-D 末态（24,476 场、快照 2e1fcac1f341）后以 `--source cba_api` 正确重入。全库无重复、无残留。

## 3. 独立 walk-forward 基线（全季 420 场常规赛，预测 369 场；冻结默认参数）

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.6206 | 0.6931 | 0.2500 | 0.1206 | n/a | n/a |
| elo | 0.6965 | 0.5442 | 0.1831 | 0.0814 | n/a | n/a |
| strength | 0.7154 | 0.5508 | 0.1860 | 0.1042 | 10.14 | 14.31 |
| **simulation** | **0.7182** | **0.5427** | **0.1850** | **0.0709** | 10.14 | 14.31 |

random acc=0.6206：CBA 主场优势极强（真实数据特征）。simulation 在 acc/logloss/brier/ECE 全部最优。

## 4. 独立校准（前半季拟合 → 后半季测试；未复制任何其它联赛参数）

- 拟合（训练窗 2025-12-12..2026-02-01，仅 CBA 常规赛）：**HCA=4.16, scale=7.50, k=5.0, λ=0.020**；边界命中：无；train val_logloss 0.5884。
- λ sweep（训练窗内）：0→0.5893，**0.02→0.5884（内点最优）**，0.05→0.5907。CBA 与 NBA 同选 λ=0.02（非 0 内点），与 EL/ACB/NBL 的 λ=0 不同——各联赛独立拟合、结果本就允许不同。
- 测试（2026-02-01 起后半季 207 场常规赛）：

| variant | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| frozen_elo | 0.7198 | 0.5090 | 0.1690 | 0.0849 | n/a | n/a |
| frozen_strength | 0.7101 | 0.5239 | 0.1749 | 0.1376 | 9.05 | 13.93 |
| frozen_simulation | 0.7295 | 0.5063 | 0.1707 | 0.0708 | 9.05 | 13.93 |
| fitted_elo | 0.7198 | 0.5090 | 0.1690 | 0.0849 | n/a | n/a |
| **fitted_strength** | 0.7101 | **0.4833** | **0.1635** | 0.0880 | 9.25 | 13.47 |
| **fitted_simulation** | 0.7343 | **0.4843** | **0.1627** | **0.0608** | 9.25 | 13.47 |

- fitted_strength 与 fitted_simulation 的 logloss/brier/ECE **全部改善**（单测试窗；C2 的双窗采纳规则在单季联赛下以单窗记录为候选证据）。fitted_elo 与 frozen_elo 逐项相同（拟合参数未改变该族输出，如实记录）。
- HCA=4.16 分（vs NBA 1.7-2.1 / NBL 1.77 / EL≈4）：CBA 主场优势为五联赛最强，与 random 基线高准确率相互印证——**独立拟合得到的差异，非参数复制**。
- 无 Platt/isotonic 需求（ECE 随 logloss 同向改善，无 C3 候选信号）。

## 5. 闭环（显式 --params）

`predict CBA25-0453（总决赛，as-of 赛前）--params storage/params/phase8e_cba_2025-26_1s.json --save` → 主队（上海久事）85.3% → 真实 103-82 → **CORRECT**，brier 0.0217；settle→aggregate 正常入账（id=6）。赛后时刻预测被现实校验正确拒绝（FAIL-as-designed 复验）。

## 6. 隔离与回归

- 特征管线按目标场次联赛硬隔离（B3 结构）：CBA 数据不进入 NBA/EL/ACB/NBL 的 Elo/基线/校准；`window-backtest --league CBA` / `calibrate --league CBA` 独立评估（跨联赛拟合被结构性禁止）。
- NBA/EL/ACB/NBL 数据、模型、参数、既有校准文件零改动（本轮仅新增 CBA 采集层/枚举映射/regulation 登记与 CLI 选项）。
- 全量测试 **600 通过 + 1 既有环境分支跳过**（新增 25 项 CBA 测试，金样为真实 2025-26 场次：揭幕战与双加时场）；storage 零污染复验（raw/processed/snapshot 三处仅含 `cba_api` 正名工件）；git clean。
- NBA k=40 边界维持 C2 决议：仅记录，未做任何变动。

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe tools\fetch_cba_bundle.py --season 2025 --out-dir C:\CBA_DATA --resume
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\CBA_DATA\cba_cba25_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\CBA_DATA\cba_cba25_raw.json --source cba_api --snapshot-tag "ingest:cba_api"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league CBA --expected-regular 420
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league CBA --window-start 2025-12-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 --train-seasons 0
.\.venv\Scripts\python.exe -m basketball_agent.cli.main calibrate --league CBA --train-start 2025-12-01T00:00:00+00:00 --cutoff 2026-02-01T00:00:00+00:00 --test-start 2026-02-01T00:00:00+00:00 --test-end 2026-07-01T00:00:00+00:00 --save-params storage/params/phase8e_cba_2025-26_1s.json
```
