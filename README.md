# Basketball Simulation & Prediction Agent V1

统计建模 + Monte Carlo 沙盘推演的篮球赛前预测系统。
A pre-game basketball prediction system built on statistical modeling and Monte Carlo simulation.

```text
真实历史数据 → 清洗验证 → 特征 → 统计模型 → 比赛参数
→ Possession-Level Simulation → Monte Carlo → 结果分布
→ 胜负/分差/总分/比分概率 → Calibration + Backtest
```

## Core principles (规格书 §2)

1. **No LLM-generated probabilities.** Every probability comes from historical
   data, explicit formulas, or Monte Carlo simulation. Agents only orchestrate,
   validate, interpret and report.
2. **Zero data leakage.** Features may only use information public at the
   prediction moment. Any future information in a feature is a hard **FAIL**,
   never a warning.
3. **Reproducibility.** Same game + same data snapshot + same model version +
   same seed ⇒ identical output. Deterministic and stochastic modes are both
   supported.
4. **Honest evaluation.** Walk-forward backtest, calibration and baselines are
   mandatory; results are reported even when the simulation does not beat a
   simple baseline.

## Storage

V1 uses **SQLite** (zero-config, file-based, snapshot-friendly). The
`Repository` layer isolates all SQL so a server database can replace SQLite
later without touching models. Snapshots are byte-exact DB copies with a
SHA-256-checksummed manifest; every prediction must cite its snapshot id.

## Layout

```text
basketball_agent/
├── config/          settings (paths, default seed / n_simulations)
├── data/
│   ├── schemas/     pydantic models (Game, TeamGameStats, PlayerGameStats, PlayerAvailability, ...)
│   ├── validators/  data-quality checks + raw-record parsing
│   ├── db/          SQLite DDL, connection manager, repository, guarded ingestion
│   ├── snapshots/   snapshot manager (byte-exact copy + manifest)
│   └── collectors/  real data sources (later phase)
├── features/        feature pipeline (Phase 2)
├── models/          Elo / offense-defense / pace / score models (Phase 3)
├── simulation/      possession-level engine + Monte Carlo (Phase 4)
├── agents/          validation agent (done); risk / ensemble etc. (later)
├── evaluation/      backtest, metrics, calibration, leakage (Phase 5)
├── api/             API layer (later)
├── cli/             ``basketball predict <game_id>`` (final phase)
└── tests/           unit / integration / regression / leakage / simulation
```

## Phase status

- [x] **Phase 1** — skeleton, schemas, data validation, SQLite + snapshots, test framework
- [x] **Phase 2** — feature pipeline (Elo strength, net/off/def ratings, recent form, pace, shooting, rebounding, turnovers, time decay, rest days, availability)
- [x] **Phase 3** — models B/C/D/E (team strength + win prob, offense/defense PPP, pace, possession-outcome distribution) assembled into simulation input params
- [x] **Phase 4** — possession-level simulation engine + Monte Carlo (10k sims ≈ 1.7s), §19 distribution stats, §28 sanity tests
- [x] **Phase 5** — walk-forward backtest (no random splits), §24 metrics, reliability diagram + ECE, three baselines, automated leakage/reality FAIL checks, decay-lambda sweep, synthetic league test bed
- [x] **Phase 6** — risk agent (confidence + flags), trainable ensemble, prediction agent (reality/leakage gates), prediction settlement loop, `basketball predict` CLI, §32 acceptance checklist tests
- [~] **Phase 7 (V1.1 real NBA data)** — all offline infrastructure DONE (raw manifests, real-CSV collector with NBA franchise map, coverage report, train-only parameter fitting, EXP-NBA-001..004, error analysis, §21 report, `ingest/coverage/experiments` CLI, full audit chain); **blocked on data**: no NBA data source reachable from this sandbox — see `DATA_REQUIREMENTS.md` (SOURCE_STATUS: UNAVAILABLE) for exactly what to provide.
- [x] **Phase 8-A (pre-real-data hardening)** — test isolation fixed (ingest tests no longer write to the real `storage/processed/`), storage pollution removed, `bad.csv` moved out of the raw-source scan, invalid-Parquet reads fail cleanly, `basketball settle <game_id>` CLI added (§33 chain now fully command-driven), dependencies pruned to what is actually imported (pydantic, pandas), Phase 8-B command chain + player-availability data contract documented in `DATA_REQUIREMENTS.md`.
- [x] **Phase 8-B1 (first real NBA data)** — real 2025-26 season ingested via the new stats.nba.com gamelog adapter (1,322 games, 30 teams, snapshot `90d58d1a28fd`); first real walk-forward experiments (Elo beats simulation on the 592-game test window — reported honestly), first real predict → settle → aggregate loop executed. Fitted parameters are reported only, never written over model defaults.
- [x] **Phase 8-B2 (multi-season foundation)** — all 17 seasons ingested (20,440 real games, 2009-10 → 2025-26, snapshot `8a70477780e3`); `competition` field in the schema (REGULAR_SEASON/PLAY_IN/PLAYOFF/CUP, backfilled from game_id prefixes, legacy rows stay untagged); default prediction/training history is regular-season-only with explicit opt-in for other segments; windowed training (`--train-start` / `--train-seasons 1|3|5|all`) + `window-backtest` CLI; output-identical read-path optimizations (~4x faster backtests). Multi-season results (`docs/MULTI_SEASON_RESULTS.md`): **Elo is the best predictor in every window; frozen-default simulation never beats Elo and is ≈ random on the 5-season window** — reported as measured, parameters unchanged.

## CLI

```powershell
$env:BASKETBALL_DB_PATH = "C:\lanqiu\storage\basketball.db"
.\.venv\Scripts\python.exe -m basketball_agent.cli.main predict <game_id> `
    --as-of 2024-11-19T12:00:00+00:00 --n-simulations 10000 --seed 42 --save
```

(`basketball predict ...` also works after `pip install -e .`.)  The report follows
spec §34; `--save` persists the prediction so it can be settled against the
final score with `basketball settle <game_id>` once the game is FINAL
(spec §33: Prediction → Game → Result → Evaluation → Calibration
→ Model Improvement).  The full real-data command chain
(`validate → ingest → coverage → experiments/report → predict --save → settle`)
is documented in `DATA_REQUIREMENTS.md`.

## V1 acceptance (spec §32)

Programmatically verified by `tests/integration/test_v1_acceptance.py`:

```
[x] 数据 Schema 完成        [x] 数据验证完成         [x] Feature Pipeline 完成
[x] Elo 完成               [x] Offense/Defense Model 完成
[x] Pace Model 完成        [x] Score Model 完成
[x] Possession Simulation 完成   [x] Monte Carlo 完成
[x] Risk Agent 完成        [x] Ensemble 完成
[x] Reality Validation 完成 [x] Leakage Detection 完成
[x] Walk-forward Backtest 完成  [x] Calibration 完成
[x] Baseline 完成          [x] Reproducibility 完成
[x] 赛前预测生成完成 (§33)   [x] 完整测试完成
```

## Development

This workspace is offline (no PyPI access). The base Python at `C:\python3.13`
already carries the scientific stack (pydantic, numpy, pandas, scipy,
scikit-learn, ...), so the venv reuses it and only pytest is installed from
local wheels (copied from the machine's pip cache into `.wheels/`).

```powershell
python -m venv --without-pip --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .wheels "pytest==9.1.1"
.\.venv\Scripts\python.exe -m pytest -q
```

Note for this sandbox: the DSH file sandbox turns `os.mkdir` mode bits into
real ACLs, so libraries creating temp dirs with mode `0o700` (tempfile, pip,
pytest basetemp) produce unlistable directories. The root `conftest.py`
neutralizes this for the test session (forces `0o777`, fresh per-run
basetemp). It adapts the environment only — no test assertions are affected.

## Validation gate

Ingestion is `parse → validate → load`. A single ERROR blocks the entire
batch (one transaction, all-or-nothing). Check ids are stable machine-readable
strings (e.g. `temporal.availability_source_time`, `consistency.team_stats`).
