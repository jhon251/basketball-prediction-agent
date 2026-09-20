# Multi-Season NBA Backtest Results（Phase 8-B2 实验记录）

日期：2026-09-20 ｜ 数据：真实 NBA 2009-10 → 2025-26 常规赛（llimllib/nba_data，
20,440 场全量入库，快照 `8a70477780e3`）｜ 协议：严格 walk-forward，每场预测
只使用其开球日之前的常规赛历史（play-in/季后赛/杯赛默认排除），无随机切分，
评估器为 `basketball window-backtest`（冻结默认参数，seed=7，100 sims/场）。

## 窗口定义

| 窗口 | 评估范围 | 场次 | 拟合 |
|---|---|---|---|
| 1-season | 2012-13 常规赛 | 1,229 | `--train-seasons 1`（拟合窗=2011-12，990 场）|
| 3-season | 2022-23 .. 2024-25 | 3,690 | 冻结默认参数 |
| 5-season | 2020-21 .. 2024-25 | 6,000 | 冻结默认参数 |
| all | 2009-10 .. 2025-26 | 20,348（预测 20,267，81 场 warmup 跳过）| 冻结默认参数 |

## 结果总表（win-probability logloss，越低越好）

| 窗口 | random | **elo** | strength | simulation |
|---|---|---|---|---|
| 1-season (2012-13) | 0.6931 | **0.6000** | 0.6451 | 0.6470 |
| 3-season (2022-25) | 0.6931 | **0.6464** | 0.6809 | 0.6878 |
| 5-season (2020-25) | 0.6931 | **0.6561** | 0.6869 | 0.6930 |
| all (2009-26)      | 0.6931 | **0.6291** | 0.6632 | 0.6675 |

### 完整指标

**1-season（2012-13，n=1,229；拟合仅报告未应用：HCA=2.82 scale=8.0 k=0 λ=0，val_logloss 0.6389）**

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.6119 | 0.6931 | 0.2500 | 0.1119 | n/a | n/a |
| elo | 0.6794 | 0.6000 | 0.2061 | 0.0387 | n/a | n/a |
| strength | 0.6395 | 0.6451 | 0.2267 | 0.0603 | 10.01 | 15.23 |
| simulation | 0.6314 | 0.6470 | 0.2278 | 0.0566 | 10.01 | 15.23 |

**3-season（2022-23..2024-25，n=3,690）**

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5561 | 0.6931 | 0.2500 | 0.0561 | n/a | n/a |
| elo | 0.6485 | 0.6464 | 0.2249 | 0.0706 | n/a | n/a |
| strength | 0.5634 | 0.6809 | 0.2439 | 0.0128 | 11.87 | 20.30 |
| simulation | 0.5556 | 0.6878 | 0.2472 | 0.0368 | 11.87 | 20.30 |

**5-season（2020-21..2024-25，n=6,000）**

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5513 | 0.6931 | 0.2500 | 0.0513 | n/a | n/a |
| elo | 0.6393 | 0.6561 | 0.2289 | 0.0800 | n/a | n/a |
| strength | 0.5528 | 0.6869 | 0.2468 | 0.0200 | 12.03 | 19.88 |
| simulation | 0.5508 | 0.6930 | 0.2496 | 0.0426 | 12.03 | 19.88 |

**all（2009-10..2025-26，n=20,348，predicted 20,267）**

| model | acc | logloss | brier | ece | margin MAE | total MAE |
|---|---|---|---|---|---|---|
| random | 0.5740 | 0.6931 | 0.2500 | 0.0740 | n/a | n/a |
| elo | 0.6587 | 0.6291 | 0.2177 | 0.0558 | n/a | n/a |
| strength | 0.5977 | 0.6632 | 0.2354 | 0.0224 | 10.97 | 17.60 |
| simulation | 0.5925 | 0.6675 | 0.2375 | 0.0206 | 10.97 | 17.60 |

## 实验事实（如实记录，不作主观修饰）

1. **Elo 在全部四个窗口都是最佳预测器**（logloss 0.6000 / 0.6464 / 0.6561 / 0.6291）。
2. **冻结默认参数的 simulation 从未击败 Elo**；在 5 季窗口上 simulation
   （0.6930）与随机基线（0.6931）**无实质差异**——λ=0 且无窗口化的长历史
   平均（默认收缩 k=5）把 strength/sim 的输入稀释到接近无信息。
3. simulation ≈ strength 在所有窗口成立（两者共享解析 margin；MC 只加噪声不改均值）。
4. **窗口越短（历史越"新鲜"），Elo 相对优势越大**：1-season 窗（评估赛季的
   历史只有 1 季）elo 0.6000 vs 全史口径明显更好——历史长度/衰减（λ）是当前
   最大的改进杠杆，属参数标定问题（B2 不动参数，留给后续 calibration 阶段）。
5. **与 B1 单季报告（elo 0.5618 / sim 0.5767，acc 0.73）不可直接比较**：B1 的
   测试窗（2026-02..06）混入了季后赛/附加赛（85+6 场），季后赛强弱分明的
   对阵更可预测，抬高了全部模型的表面精度；本阶段纯常规赛口径下随机基线
   acc=0.551-0.574（= 真实主队胜率）才是正确的难度参照。B1 数字与本期数字
   都是真实结果，只是评估口径不同（这正是 competition 字段拆分要暴露的问题）。
6. strength/simulation 的 ECE 很低（0.01-0.04）但 accuracy 也低——预测压缩在
   0.5 附近（默认 scale=11.5 + 长 λ=0 历史的平均效应），"校准好"是因为几乎
   不冒险，不是预测能力强。
7. margin/total MAE 随窗口变化不大（10.0-12.0 / 15.2-20.3 分）。
8. 全部窗口均无泄漏：特征 provenance 逐场 as-of；results 由
   `run_window_backtest` 单遍共享特征产生，与四个独立 BacktestEngine
   **位级一致**（等价性单元测试钉住）。

## 复现命令

```powershell
$env:BASKETBALL_DB_PATH = "C:\lanqiu\storage\basketball.db"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest `
    --window-start 2012-10-01T00:00:00+00:00 --window-end 2013-07-01T00:00:00+00:00 `
    --train-seasons 1 --n-simulations 100 --seed 7 --warmup 5   # 1-season
# 3-season: --window-start 2022-10-01 --window-end 2025-07-01 --train-seasons 0
# 5-season: --window-start 2020-10-01 --window-end 2025-07-01 --train-seasons 0
# all:      --window-start 2009-10-01 --window-end 2026-07-01 --train-seasons 0
```

## 对后续阶段的含义（非本阶段执行）

- 下一个 calibration 阶段的明确靶子：decay λ > 0、训练窗窗口化（已被证明可运行）、
  以及把 B1 风格拟合参数（HCA/scale/k）在纯常规赛协议下重新评估。
- simulation 层若在窗口化+调参后仍持续不敌 Elo，按规格书纪律如实降级其在
  ensemble 中的权重，不硬保仿真。
