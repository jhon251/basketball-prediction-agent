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
- [ ] Phase 2 — feature pipeline (team strength, form, pace, shooting, rebounding, turnovers, time decay)
- [ ] Phase 3 — Elo / offense-defense / pace / score models
- [ ] Phase 4 — possession-level simulation + Monte Carlo
- [ ] Phase 5 — walk-forward backtest, calibration, baselines, leakage detection
- [ ] Phase 6 — risk/ensemble agents, `basketball predict` CLI, V1 acceptance

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
