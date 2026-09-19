# Phase 8-B2 断点恢复说明（2026-09-19 关机时状态）

## 已完成（代码已提交，工作区 clean @ f13f287）

1. **competition 进入 schema** ✅ — `Competition` 枚举 + `Game.competition` 字段、
   schema v3 幂等迁移（按 NBA game_id 前缀回填 002/004/005/006，未知前缀保持 NULL），
   gamelog 适配器自动打标。已完成迁移验证 + 单元测试（401 项测试全部通过）。
2. **默认 REGULAR_SEASON 历史过滤** ✅ — FeaturePipeline / BacktestConfig /
   PredictionAgent 默认排除 play-in/playoff/cup 历史；legacy 未标记行保持兼容；
   显式 opt-in 参数已加。
3. **窗口化训练** ✅ — `fit_v1_params`/ensemble fit/λ sweep 支持 `train_start`；
   experiments CLI 支持 `--train-start` / `--train-seasons 1|3|5`；
   新子命令 `basketball window-backtest` 已可用。
   ⚠ 已修复：window_start 恰在赛季边界时拟合窗为空的 bug（f13f287）。
4. **多赛季真实 DB** ✅ — 全 17 季（20,440 场，2009-10 → 2025-26）已入库，
   快照 `8a70477780e3`；B1 单季状态保留在快照 `90d58d1a28fd`。
5. **读路径性能优化** ✅（输出恒等，手工回归测试钉住）— H≈19k 单场特征
   1.43s → 0.37s；关键测量：H=2k≈0.04s / H=11k≈0.14s / H=19k≈0.37s。
6. **数据质量审计** ✅ — 结果在下方"审计结论"，全部异常均对应真实历史事件。

## 断点：唯一未完成项 = 跑完 4 个窗口回测并记录结果

关机时 3 个后台回测被终止（只读任务，无损坏；部分输出在 .tmp/windows_*.txt，
均为未完成中间态，可忽略）。**明天开机后按顺序重跑以下 4 条命令**（可并行，
但全史窗口最重，建议先跑 A 组再跑 B 组）：

```powershell
$env:BASKETBALL_DB_PATH = "C:\lanqiu\storage\basketball.db"
cd C:\lanqiu

# A. 1-season 窗口（2012-13，拟合窗=2011-12 停摆赛季，约 20-30 分钟）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest `
    --window-start 2012-10-01T00:00:00+00:00 --window-end 2013-07-01T00:00:00+00:00 `
    --train-seasons 1 --n-simulations 100 --seed 7 --warmup 5

# B. 3-season 窗口（2022-23..2024-25 常规赛，冻结默认参数，约 40-60 分钟）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest `
    --window-start 2022-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 `
    --train-seasons 0 --n-simulations 100 --seed 7 --warmup 5

# C. 5-season 窗口（约 60-90 分钟）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest `
    --window-start 2020-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 `
    --train-seasons 0 --n-simulations 100 --seed 7 --warmup 5

# D. all-available（2009-10..2025-26 常规赛 19,210 场，约 2.5-3.5 小时）
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest `
    --window-start 2009-10-01T00:00:00+00:00 --window-end 2026-07-01T00:00:00+00:00 `
    --train-seasons 0 --n-simulations 100 --seed 7 --warmup 5
```

说明：`--train-seasons 0` 表示窗口比较用冻结默认参数（EXP-NBA-001 式基线对比，
与 B1 口径一致）；窗口化拟合本身已由单元测试 + A 命令（train-seasons=1）验证。

## 跑完后收尾清单

- [ ] 4 个窗口结果记入 `docs/REAL_DATA_REPORT_2025-26.md` 或新建
      `docs/MULTI_SEASON_RESULTS.md`（accuracy/logloss/Brier/ECE/margin MAE/total MAE/校准表）
- [ ] 与 B1 单季结果、random/elo/strength/simulation 基线对比表
- [ ] README Phase 8-B2 状态行 + DATA_REQUIREMENTS 更新
- [ ] 重跑全套测试 + 提交（结果文档一个 commit）
- [ ] 最终报告：多赛季覆盖 / 各窗口结果 / 最佳模型作为事实记录 / 新问题 /
      是否具备 EuroLeague 接入条件

## 数据质量审计结论（已完成，直接可用）

- 20,440 场全部 FINAL、0 重复、时区全部 aware、无未来日期、30 队全部规范 ID
  （NJN/NOH 等历史别名已正确归一）
- 恒等式违规 0（比分/篮板）、每场恰好 2 行球队统计
- competition 分布：REGULAR_SEASON 20,348 / PLAYOFF 85 / PLAY_IN 6 / CUP 1
  （**季后赛仅 2025-26 有——源数据不含 2009-24 季后赛 gamelog，非管线丢失**）
- 每赛季场次偏差全部对应真实历史：2011-12 停摆 66 场/队（990 场整）、
  2012-13 少 1 场（IND@BOS 因波士顿马拉松爆炸取消）、2019-20 疫情停摆
  （各队 64-75 场不等）、2020-21 缩水 72 场/队（1,080 场整）
- 常规赛/季后赛边界无越季交错；中性场地 11 场（常规赛+杯赛决赛，名义主客
  为适配器确定性约定）；processed meta 的 neutral 计数字段已修复
  （merge_raw_datasets 保留），本次入库文件该字段缺失已知悉
- 拟合参数（HCA/scale/k/λ）仅报告，未写回模型默认值

## 环境提醒

- 真实数据文件在 `C:\NBA_DATA\llimllib\`（gamelogs.parquet 17 季 + gamelog_2026.parquet，
  SHA-256 见 DATA_REQUIREMENTS.md）
- venv：`.\.venv\Scripts\python.exe`（pyarrow 25.0.1 可用，duckdb 未装）
- storage/ 与 .tmp/ 为 gitignore；真实 DB 与快照只在磁盘
