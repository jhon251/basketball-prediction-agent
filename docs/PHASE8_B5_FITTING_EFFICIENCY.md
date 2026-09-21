# Phase 8-B5 — 多联赛回测计算效率与训练窗口基础强化（已完成）

**本轮唯一目标**：消除 B4 暴露出的 `--train-seasons ≥ 1`（训练窗口参数拟合）过慢问题。
**硬约束**：不改任何模型公式、不改任何默认参数、不做任何结果驱动的调参、不新增模型 / 联赛 / 前端；
NBA 与 EuroLeague 必须各自独立运行；B4 的冻结回测结论必须逐位不变。

> 一句话结论：拟合网格把「每个候选各跑一遍 walk-forward 特征」改成「每个 λ 只跑一遍特征、所有候选读缓存」，
> 真实 EuroLeague 训练窗上 26 次强度网格 + 3 次 λ 扫描的特征重复计算从 **29 遍降到 3 遍**，
> 真实 331 场训练窗拟合 **421.7 s → 71.7 s（5.88×）**，且**拟合参数与三窗口冻结指标逐位相同**。

## 1. 慢在哪里（优化前的实测基线）

`.tmp/b5_baseline.py`（真实 DB、E2023 训练窗 331 场、tracemalloc 开启）:

| 项 | 耗时 | 峰值内存 | 特征计算次数 | 预测数 |
|---|---|---|---|---|
| 1× strength 遍历 | 39.09 s | 143.5 MB | 331 | 331 |
| 1× simulation 遍历（100 sims/场） | 128.19 s | 4.5 MB | 331 | 331 |
| **旧拟合（同窗实测，无 tracemalloc）** | **421.68 s ≈ 7.0 min** | — | **9599**（29 遍） | 9599 |

> 注：上表单遍耗时含 tracemalloc 开销，据此**推算**旧拟合约 1401 s；随后用 `git HEAD` 的旧实现
> 在同一训练窗实跑（无 tracemalloc）得 **421.68 s**，故以实测值为准。B4 中"`--train-seasons 1`
> 运行 >30 min 未收敛"是在更长窗口（多季训练窗）上观察到的。

旧 `fit_v1_params` 的重复计算：
- `estimate_logistic_scale`：21 个候选，每个候选 `BacktestEngine.run()` → **21 遍**特征（候选只改 logistic scale，
  与特征无关）；
- `estimate_shrinkage_k`：5 个候选 → **5 遍**；
- `sweep_decay_lambda`：3 个 λ → 3 遍（这 3 遍是必要的，特征依赖 λ）。

合计 29 遍 walk-forward 特征推导 = 29 × 331 = 9599 次逐场特征计算，其中 26 遍是纯重复。

## 2. 优化点（只动既有训练/拟合路径）

| # | 优化 | 位置 | 说明 |
|---|---|---|---|
| 1 | 训练窗特征缓存 `FittingDataset` | `evaluation/fitting.py`（新增） | 一次 walk-forward 只算一遍特征，按 `(league, decay_lambda, warmup_games)` 缓存 `GameFeatures` + 该场真实结果；候选参数只作用于模型层，直接在缓存上评估 |
| 2 | 网格共享一遍特征 | `estimate_logistic_scale` / `estimate_shrinkage_k` | 21 + 5 = 26 个候选共用同一份 λ=0 数据集 |
| 3 | λ 扫描复用 λ=0 数据集 | `fit_v1_params` → `sweep_decay_lambda(datasets=...)` | `warmup_games` 一致时 λ=0 分支直接复用网格那遍；λ=0.02 / 0.05 各自 1 遍（特征确实依赖 λ，不做任何近似） |
| 4 | 训练窗切片只做一次 | `_train_games` | 窗口过滤 + `(game_date, game_id)` 排序一次完成，供 HCA/网格/λ 扫描复用 |
| 5 | 可复用统计量 | `estimate_home_court_advantage` | 训练窗均值直接由同一份切片得到 |
| 6 | 联赛作用域贯穿拟合 | `_train_games` / `estimate_*` / `fit_v1_params` / `run_window_backtest` | 新增 `league=` 参数（默认 `None`＝旧行为）；NBA 与 EL 各自只用自己的比赛，**没有任何跨联赛缓存或训练** |
| 7 | 静默空窗口改为显式报错 | `run_window_backtest` | 传入裸字符串（如 `"EUROLEAGUE"`）以前匹配 0 行并"正常"输出空窗口，现在显式转换，拼错即 ValueError |

**明确未做**（避免越界 / 破坏等价性）：未改 `FeaturePipeline` 内部的历史推导（其 O(N²) 复杂度依旧，见 §7）、
未改预测器数学、未改候选集 / 候选顺序 / tie-breaking、未改默认参数、未加任何近似或采样。

## 3. 等价性证明

### 3.1 数学论证

HCA / logistic scale / shrinkage k **不参与特征计算**（特征只依赖比赛历史与 λ）。因此：
同一份 `game_ids`、同一 `decay_lambda`、同一 `warmup_games` ⇒ 同一跳过集合、同一 `FeaturePipeline.compute` 调用序列、
同一 `GameFeatures` 对象；再把**同一个** predictor 的 `predict()` 施加到**同一批**特征上，得到的
`BacktestRecord` 序列（含 skip 原因）与 `BacktestEngine.run()` 逐条一致，`summary()` 自然逐位相同。
`SimulationPredictor` 每场都用 `MonteCarloRunner(seed)` 重新播种，所以缓存路径的 MC 与逐遍 MC 也完全一致。

旧路径 `pairs` 计数（`prediction is not None and actual_home_win is not None`）== 新路径 `dataset.n_predicted`
（`StrengthPredictor.predict` 恒返回 `Prediction`，非跳过场次必有预测），因此 `< 5 → 跳过该候选` 的守卫行为不变；
`loss < best_loss - 1e-12` 的 tie-breaking 逐字保留，包括候选顺序。

### 3.2 回归测试（`basketball_agent/tests/integration/test_fitting_cache.py`，14 项）

| 测试 | 断言 |
|---|---|
| `test_strength_metrics_match_engine_summary` | 3 组 (k, HCA, scale) 配置：缓存 `summary()` == 引擎 `summary()` |
| `test_simulation_metrics_match_engine_summary` | 100 sims/seed=7：缓存 == 引擎 |
| `test_cached_dataset_records_match_engine_records` | 记录序列、skip 原因、`prob_home`、`expected_margin` 逐条相同 |
| `test_sweep_cached_equals_uncached`（λ=0 / λ=0.05） | 缓存扫描 == 逐 λ 引擎扫描 |
| `test_scale_grid_matches_legacy` | 与新写的**旧实现逐字参考版**（21 遍引擎）选出的 scale 相同 |
| `test_shrinkage_grid_matches_legacy` | 同上（k 网格） |
| `test_passed_dataset_is_used_verbatim` | 显式传入数据集时选择结果与旧实现相同 |
| `test_scale_grid_uses_a_single_feature_pass` | 21 候选只消耗 **1 遍**特征（精确等于 `n_feature_computations`） |
| `test_shrinkage_grid_uses_a_single_feature_pass` | 5 候选只消耗 **1 遍** |
| `test_fit_uses_one_pass_per_lambda` | `lambda_candidates=(0.0,)`、warmup=5 时：全程 **1 遍** |
| `test_fit_lambda_sweep_adds_one_pass_per_extra_lambda` | 2 个 λ ⇒ 恰好 **2 遍**（不隐藏额外遍历） |
| `test_mixed_leagues_are_refused` | 混合联赛 → `ValueError("cross-league")` |
| `test_dataset_is_single_league_and_ordered` | 单联赛 + 按 `(game_date, game_id)` 排序，与输入顺序无关 |

### 3.3 真实数据旧/新对照（同一个真实 EuroLeague 训练窗，`league=EUROLEAGUE`）

`.tmp/b5_legacy_real.py`（已提升为版本控制内的 `tools/fitting_efficiency_benchmark.py legacy`）：
旧实现取自 `git show HEAD:basketball_agent/evaluation/param_tuning.py`（逐字），
**唯一改动**是把旧模块的 `_train_games` 换成同语义的联赛过滤版本（联赛隔离是本轮新要求，不属于旧数值路径）。

| 训练窗 | 旧实现 | 新实现 | 结果 |
|---|---|---|---|
| E2023 前 67 场（2023-10-01 .. 2023-11-15） | 83.12 s（特征计算 1943 = 29×67） | 13.82 s（201 = 3×67） | 参数**完全相同**（HCA 3.716417910447761 / scale 8.0 / k 5.0 / λ 0.0 / val logloss 0.5933474110442364），**6.01×** |
| E2023 全季 331 场（2023-10-01 .. 2024-10-01） | 421.68 s | 71.72 s | **5.88×**，参数**完全相同**（HCA 3.972809667673716 / scale 8.0 / k 5.0 / λ 0.0 / val logloss 0.6171472957159607） |

## 4. 前后性能对照（真实数据，同一颗 CPU，无 tracemalloc 干扰）

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 拟合特征遍历遍数（331 场训练窗） | 29 遍 | **3 遍** | −89.7% |
| 拟合特征计算次数 / 预测数 | 9599 | **993** | −89.7% |
| 单季 `--train-seasons 1` 端到端（含 301 场窗口回测） | >30 min CPU 未收敛（B4 实测被终止） | **92.19 s** | 可完成 |
| 其中拟合本身（331 场） | 421.68 s（7.03 min，实测） | **71.72 s（1.20 min）** | **5.88×** |
| 拟合峰值工作集 | —（旧实现未测到完成） | 217.1 MB（含窗口回测的整个进程） | 记录 |
| 窗口回测（train-seasons=0） | 1 季 21.8 s / 3 季 65.5 s / 全量 111.9 s | 同左（本次实测 24.8 / 65.5 / 111.9 s，波动来自并发） | 无变化 |

优化后拟合的剩余成本主要是 **λ 扫描的 3 遍 Monte Carlo（每场 100 sims）**，这部分是实验本身要求的，
不属于重复计算，本轮不做近似压缩（见 §7）。

**存储完整性**：全部 B5 验证均为只读。`storage/` 内最新写入时间仍是 B4 入库时刻（`2026-09-21 18:29:15`），
本轮结束时 `storage/basketball.db` SHA-256 = `A02360486A190B60AA869D2D94C2DD7F7F25A391C174576D3CE990A2F7300007`（未变更，
B5 不跑任何 ingest / snapshot / refit）。

## 5. 冻结结果未变（B4 三窗口逐位复现）

`--train-seasons 0`（冻结默认参数）、`--league EUROLEAGUE`、seed=7、100 sims/场，本轮实跑结果与 B4 文档对照：

| 窗口 | n（可预测） | elo logloss | strength | simulation | margin MAE / total MAE |
|---|---|---|---|---|---|
| 1 季 2024-25 | 306 (301) | 0.6240176912682203（B4: 0.6240） | 0.6414579511534281（0.6415） | 0.6303873532011838（0.6304） | 9.45676908779828 / 13.525922793857646（9.46 / 13.53） |
| 3 季 2022-25 | 918 (904) | 0.6061770732353025（0.6062） | 0.6322101187361041（0.6322） | 0.6217977187978005（0.6218） | 9.299248987249847 / 13.249735546176336（9.30 / 13.25） |
| 全量 2019-25 | 1753 (1685) | 0.6183132852449301（0.6183） | 0.6365129512131518（0.6365） | 0.6303480746519481（0.6303） | 9.560995954566026 / 13.449548084020991（9.56 / 13.45） |

random 恒为 ln2 = 0.6931471805599453（±1e-15 浮点尾差）；**Elo 仍是三个窗口的最佳预测器**，
B4 的关键结论（历史深度决定 Elo/sim 排序）不受本轮影响。

**训练窗拟合结果（首次真正跑完，仅供参考，不应用于默认模型）**：
EL 训练窗 = `[2023-10-05, 2024-10-01)`，331 场。

| 参数 | 值 |
|---|---|
| home_court_advantage | 3.972809667673716 |
| margin_logistic_scale | 8.0（**命中网格下界**，如实记录：真实最优点可能 ≤8.0，但候选集是冻结的历史定义，本轮不得改动） |
| shrinkage_k | 5.0 |
| decay_lambda | 0.0 |
| 训练窗验证 logloss | 0.6171472957159607 |

## 6. 联赛隔离（要求 4/5）

- 拟合数据集**按构造只允许单一联赛**：`build_fitting_dataset` 若发现窗口内混合联赛直接 `ValueError`
  （"cross-league fitting is forbidden; scope the fit with league=..."），不静默取并集、不取多数派。
- `run_window_backtest(league=...)` 把联赛一路传入 `fit_v1_params`，因此 EL 窗口的拟合**只用 EL 比赛**；
  共享 DB 中的 NBA 比赛不会进入 EL 训练集，反之亦然。
- 缓存对象带 `league` 字段，`estimate_*` 在收到不匹配联赛的数据集时直接报错（不会跨联赛复用）。
- 本轮**没有**改动 NBA 的模型 / 参数 / 结果，也没有重跑 NBA 窗口（仅跑测试套件验证未回归）。
- 附带说明：`run_phase7_experiments`（EXP-NBA-00x）设计上是 NBA 单联赛实验。在同时装有 NBA 与 EL 的
  共享 DB 上、且 `train_start=None` 时，它现在会**显式报错**而不是静默地把两个联赛混在一起训练——
  这是本轮"禁止跨联赛训练"约束的必然结果，属于正确的失败方式。

## 7. 未优化 / 已知限制（如实报告）

| 项 | 状态 |
|---|---|
| `FeaturePipeline` 内部逐场历史推导（O(N²) 量级） | **本轮未动**。它是剩余特征成本的主体，但改动会触及特征数学，与"结果逐位不变"冲突；只在上层消除了 26/29 的重复遍历 |
| λ 扫描的 3 遍 Monte Carlo | 属于实验定义（λ 必须由历史回测决定），无法在不改变实验的前提下压缩 |
| `margin_logistic_scale = 8.0` 命中下界 | 网格 `8.0..18.0 step 0.5` 来自历史冻结定义，本轮不得扩边（否则等于调参）；如实记录 |
| NBA 侧拟合（`train-seasons≥1`）未在共享 DB 上实跑 | 按指示不触碰 NBA 结果；只保证代码路径同样受益且测试通过 |
| 市场基线 / 缺失比赛参照 | 仍不可用（不伪造），与 B4 相同 |

## 8. 复现命令

```powershell
# 优化前基线（真实 DB，tracemalloc 开启，逐遍成本）
.\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py baseline

# 旧/新拟合等价性（真实 EL 训练窗；旧实现逐字取自 git HEAD）
.\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py legacy --window small     # 67 场
.\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py legacy --window season    # 331 场

# 冻结结果复现（三窗口）+ 首次真正跑完的单季拟合
.\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py windows
.\.venv\Scripts\python.exe tools\fitting_efficiency_benchmark.py fit

# 回归测试
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider basketball_agent/tests/integration/test_fitting_cache.py
```

CLI 等价命令（与 B4 一致，`--train-seasons 1` 现在能在合理时间完成）：

```powershell
.\.venv\Scripts\python.exe -m basketball_agent.cli.main window-backtest --league EUROLEAGUE `
  --window-start 2024-10-01T00:00:00+00:00 --window-end 2025-07-01T00:00:00+00:00 `
  --train-seasons 1 --n-simulations 100 --seed 7
```

## 9. 本轮交付物

| 文件 | 变更 |
|---|---|
| `basketball_agent/evaluation/fitting.py` | 新增：训练窗特征缓存数据集（单联赛保证 + 与引擎等价的 `BacktestResult`/`summary`） |
| `basketball_agent/evaluation/param_tuning.py` | 网格/λ 扫描改为复用缓存；新增 `league` 作用域；候选集、tie-breaking、返回类型与文本完全不变 |
| `basketball_agent/evaluation/backtest.py` | `sweep_decay_lambda(..., datasets=None)` 可选缓存入参（不传则原逻辑逐字不变） |
| `basketball_agent/evaluation/experiments.py` | 窗口拟合传入 `league`；裸字符串 league 改为显式转换（不再静默空窗口） |
| `basketball_agent/evaluation/__init__.py` | 导出新缓存类型 |
| `basketball_agent/tests/integration/test_fitting_cache.py` | 新增 14 项回归测试（等价性 / 旧实现对照 / 遍历次数上界 / 联赛隔离） |
| `tools/fitting_efficiency_benchmark.py` | 新增：baseline / legacy / windows / fit 四个证据子命令（只读） |
| `docs/PHASE8_B5_FITTING_EFFICIENCY.md` | 本文档 |

## 10. 验收对照（本轮）

| 要求 | 结果 |
|---|---|
| 只优化既有训练/拟合路径，不新增模型、不调参 | ✅ 无新模型；候选集/默认参数/公式未改 |
| 数学结果逐位一致（或显式浮点等价）+ 回归测试 | ✅ 14 项回归测试；真实 67 场与 331 场旧/新拟合参数完全相同 |
| 针对重复特征计算 / 重复网格遍历 / 可缓存中间量 / 训练窗切片 / 可复用统计量 | ✅ 29 遍 → 3 遍（§2、§4） |
| NBA 与 EuroLeague 各自独立运行 | ✅ 拟合按 `league` 作用域；混合联赛直接拒绝（§6） |
| 冻结默认结果不变（B4 三窗口） | ✅ 逐位复现（§5） |
| EL `train-seasons=1` 合理时间完成 | ✅ 92.19 s（B4 中 >30 min 未收敛） |
| 记录 before/after runtime、峰值内存、预测数、关键指标差异 | ✅ §4（关键指标差异 = 0） |
| provenance / leakage 不变 | ✅ `league-audit --league EUROLEAGUE --leakage-sample 3`：status PASS，leakage failures 0，与 B4 逐字相同 |
| 全部测试通过 | ✅ `462 passed, 1 skipped, 0 failed`（B4 为 448+1；新增的 14 项即本轮的回归测试） |
| 不新增联赛（ACB/NBL/CBA/BSL）、不改前端/API/自动重训 | ✅ 未触及 |
| 存储不被污染、git 工作区可提交 | ✅ `storage/` 未被写入（DB 哈希与 mtime 未变）；`.tmp/` 已在 `.gitignore` 内 |
