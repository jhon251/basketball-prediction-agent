# Phase 8-B4 — EuroLeague 多赛季回测基础（实验记录）

日期：本轮会话 ｜ 状态：**基础完成，多赛季数据获取受阻（需用户侧联网）**
数据：真实 EuroLeague 2024-25（`E2024`，330 场）— **仅 1 个赛季在库**

## 1. 本轮交付（代码，不触碰 NBA）

| 交付物 | 说明 |
|---|---|
| `basketball_agent/evaluation/league_audit.py` | 单联赛审计（验收第 8 项 9 组检查）：game_id 格式/唯一、EL- 命名空间、赛季边界、competition、40/48 分钟规制+加时步长、timezone、缺失比赛、FINAL 一致性、provenance/leakage 采样（含跨联赛历史检查）。只读。 |
| `basketball league-audit --league {NBA,EUROLEAGUE}` | CLI 入口；FAIL 时退出码 1 |
| `tools/fetch_euroleague_bundle.py` | **用户侧联网**抓取器：`seasonCode=E<yyyy>` × `gameCode=1..N` 循环，输出与 `euroleague_live.py` 契约完全一致的 bundle（可 `--resume`） |
| 测试 | 单元 19 个（审计各失败路径 + bundle 契约）＋ 真实数据集成 5 个（EL 审计 / NBA 未受影响 / EL 窗口 / NBA 窗口） |

**未触碰**：NBA 采集器、NBA 模型（Elo/攻防/Pace/Score）、NBA 参数、NBA 回测结果、任何跨联赛共享（Elo/训练数据/calibration 均按 league 隔离，B3 已建立）。

## 2. 真实数据审计结果（EUROLEAGUE，330 场）

```text
games: 330 | seasons: 1 | status: PASS

season coverage:
season        games  regular  other  teams  range
2024-25         330      306     24     18  2024-10-03 .. 2025-05-25

[PASS] game_id.format      330 ids; format E<yyyy>-<gameCode>
[PASS] game_id.unique      duplicates: 0
[PASS] team.namespace      18 team ids; all carry EL- prefix
[PASS] team.registered     all referenced teams exist
[PASS] season.boundaries   1 season; overlaps: none; implausible: none
[PASS] competition.tags    untagged games: 0 of 330
[PASS] regulation.minutes  expected 200 min/team; OT rows: 24; violations: 0
[PASS] timezone.aware      all timestamps timezone-aware
[PASS] missing.games       306/306 regular-season games (schedule reference supplied)
[PASS] final.consistency   mismatches: 0; non-final: 0
[PASS] provenance.leakage  sampled 2 (E2024-0329/0330); failures: 0
```

**数据质量细节（实测）**
- 规制：常规 200 分钟/队（636 行）→ 加时 225（22 行 = 11 场）→ 双加时 250（2 行 = 1 场），无违规值。
- possessions 均值 72.7/队（40 分钟制，估算口径 `fga+0.44fta-orb+tov`）。
- competition 拆分：REGULAR_SEASON 306 / PLAY_IN 3 / PLAYOFF 21（窗口回测默认只取常规赛 306）。
- game_id `E2024-0001 .. E2024-0330`，无重复、无 NBA 风格 id 混入。
- 全部时间戳 tz-aware（tip-off 本地时间 → UTC，主场时区换算）。

## 3. 窗口实验结果（冻结默认参数，`--league EUROLEAGUE`，seed=7，100 sims/场）

| 窗口 | 评估场次 | 可预测 | random | elo | strength | simulation |
|---|---|---|---|---|---|---|
| 1 season（2024-25） | 306 | 261 | 0.6931 | 0.6381 | 0.6348 | **0.6320** |
| 3 seasons（2022-25） | 306 | 261 | 0.6931 | 0.6381 | 0.6348 | **0.6320** |
| all available（↔E2024） | 306 | 261 | 0.6931 | 0.6381 | 0.6348 | **0.6320** |

**完整指标（三窗口相同，因库内只有 E2024）**

| model | n | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|---|
| random | 261 | 0.5977 | 0.6931 | 0.2500 | 0.0977 | n/a | n/a |
| elo | 261 | **0.6245** | 0.6381 | 0.2247 | 0.0629 | n/a | n/a |
| strength | 261 | 0.6130 | 0.6348 | 0.2223 | 0.0597 | 9.85 | 12.71 |
| simulation | 261 | 0.6130 | **0.6320** | 0.2225 | 0.0500 | 9.85 | 12.71 |

**为何 3/all 与 1 季完全相同**：库内 EuroLeague 只有 `E2024` 一个赛季，3 季与 all 窗口在该联赛下解析出的评估集合与 1 季窗口一致（306 场）。这不是"跑通三个窗口"的成绩单，而是**如实暴露数据缺口**——机制可运行，数据未到位。

## 4. 模型比较与新发现（不预设结论）

1. **在 EuroLeague 上 Elo 并未优于 strength/simulation**：logloss elo 0.6381 vs strength 0.6348 vs sim 0.6320（与 NBA B2 的结论方向相反）。
2. **但该差异不构成统计结论**：n=261 时 logloss 的标准误量级约 0.03，而 elo↔sim 差值仅 0.006（<0.25 SE）——**样本量不足以判定排序**。需要 E2019–E2023 才能给出多赛季判断；本阶段不下"EL 中 Elo 更差"的结论。
3. **accuracy 与 logloss 排序不一致**（elo acc 最高 0.6245，但 logloss/ECE 较差）：Elo 更爱冒险（ECE 0.0629），strength/sim 更保守（ECE 0.050–0.060）。
4. **simulation ≈ strength**（0.6320 vs 0.6348，MAE 完全相同）：与 NBA 一致——MC 只加噪声不改均值。
5. **EL 的 total MAE（12.71）明显优于 NBA（15.2–20.3）**，margin MAE（9.85）也更好：40 分钟制 + 较低回合数（72.7/队）使比分分布更窄，天然更易预测。
6. 全部窗口 leakage/provenance PASS；特征历史严格限定在**同联赛**（跨联赛历史检查为 0 违规）。

## 5. 未完成 / 受阻（如实）

| 项 | 状态 | 说明 |
|---|---|---|
| 接入 E2019–E2023 | ❌ **受阻** | 沙箱无外网；`live.euroleague.net` 实测 UNREACHABLE。抓取器已就绪，需用户侧网络执行 |
| 多赛季 walk-forward（3/all 真有 3+ 季） | ❌ 受阻 | 依赖上一条 |
| 参数拟合（`--train-seasons ≥1`） | 未执行 | EL 首季之前无历史赛季可拟合；需 ≥2 季 |
| 市场基线 | 不可用 | 无免费历史赔率（不伪造） |
| 其他联赛（ACB/NBL/CBA/BSL） | 未触及 | 按指示不做 |

**用户侧恢复步骤（一次命令完成采集，再走既有链路）**：

```powershell
# 1) 抓取（需联网；建议逐个赛季，可 --resume 续传）
python tools/fetch_euroleague_bundle.py --seasons E2019 E2020 E2021 E2022 E2023 `
    --out-dir C:\lanqiu\storage\raw\euroleague_live --resume

# 2) 校验 + 入库（项目 venv；每个赛季一个 bundle）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main validate C:\lanqiu\storage\raw\euroleague_live\euroleague_E2023_raw.json
.\.venv\Scripts\python.exe -m basketball_agent.cli.main ingest   C:\lanqiu\storage\raw\euroleague_live\euroleague_E2023_raw.json

# 3) 多赛季审计 + 三窗口
.\.venv\Scripts\python.exe -m basketball_agent.cli.main league-audit --league EUROLEAGUE --expected-regular 306 --leakage-sample 3
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league EUROLEAGUE --window-start 2022-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 --train-seasons 0 --n-simulations 100 --seed 7
```

## 6. 验收对照

- NBA 原有测试全部通过 → ✅（445 passed / 1 skipped / 0 failed；新增测试未改动 NBA 断言）
- EuroLeague 多赛季真实数据可追溯 → ⚠️ 单季可追溯（含 manifest/SHA-256/快照）；多季**受阻**
- 1/3/all 窗口均可运行 → ✅ 机制运行成功（3/all 因数据只有 1 季而与 1 季同集，已如实标注）
- leakage/provenance PASS → ✅（审计 11/11 PASS；样本场次 as-of 特征计算无未来信息、无跨联赛历史）
- storage 无污染 → ✅（实验全程使用 `.tmp` 数据库副本；`storage/basketball.db` SHA-256 运行前后一致）
- git clean → 见提交记录（本轮新增/修改文件已提交）
