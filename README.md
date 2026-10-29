# Basketball Prediction Agent

[English](README.md) | [简体中文](README.zh-CN.md)

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-1097%20passing-brightgreen)

A pre-game basketball prediction system: statistical models + a
possession-level Monte Carlo simulator, wrapped in an agent that collects,
validates, predicts and audits itself. **Every probability comes from data,
explicit formulas or simulation — never from an LLM.**

**What it predicts for one game:**

| Output | Providers |
|---|---|
| Win probability (home/away) | Elo · team-strength · possession-level Monte Carlo |
| Spread cover (让分, incl. push on integer lines) | MC margin samples · continuity-corrected normal over Elo-implied margin |
| Totals over/under (大小球, e.g. 190 / 190.5) | MC per-game totals · normal smoothing (explicitly labelled) |
| Margin-range matrix (客胜 26+…1-5, 平, 主胜 1-5…26+) | sample counts · corrected normal |

**Leagues:** NBA · EuroLeague · ACB (Spain) · NBL (Australia) · CBA (China) · BSL (Türkiye)
— with per-league history isolation (leagues never share ratings or baselines),
upcoming-fixture collection, and an injury/availability layer for NBA and EuroLeague.

## Quick start

No API keys required for this path (EuroLeague's public endpoint is keyless).

```bash
pip install -e .

# 1) fetch raw season bundles (user-side, ~2 min per season)
python tools/fetch_euroleague_bundle.py --seasons E2024 E2025 --out-dir storage/raw/euroleague_live

# 2) validate (all-or-nothing) + ingest
basketball validate storage/raw/euroleague_live/euroleague_E2024_raw.json
basketball ingest   storage/raw/euroleague_live/euroleague_E2024_raw.json
basketball ingest   storage/raw/euroleague_live/euroleague_E2025_raw.json

# 3) current-season calendar (enumerates the live Header endpoint, settles played games)
basketball euroleague-fixtures --season E2026

# 4) web UI + local API (Chinese UI, English API fields)
basketball serve --open            # http://127.0.0.1:8765
```

In the UI: pick a league and two teams (Chinese or English aliases both work),
optionally enter a spread line (让 5.5 ⇒ `-5.5`) and/or a totals line (`190.5`),
hit **预测**. Click a scheduled game in the list to pre-fill the matchup.

### CLI prediction

```bash
basketball predict <game_id> --as-of 2024-11-19T12:00:00+00:00 \
    --n-simulations 10000 --seed 42 --save
basketball settle <game_id>        # after the game is FINAL
```

Same input + same data snapshot + same seed ⇒ byte-identical output.

### API

```bash
curl -X POST http://127.0.0.1:8765/api/predict \
  -H "Content-Type: application/json" \
  -d '{"game_id": "NBA26-0021", "home_line": -3.5, "total_line": 224.5}'
```

```json
{
  "win_probability": {"home": 0.577, "away": 0.423, "primary_family": "simulation"},
  "families":        {"elo": 0.612, "strength": 0.598, "simulation": 0.577},
  "spread":  {"home_line": -3.5, "elo_normal": {"home_cover": 0.53, "push": 0.0, "away_cover": 0.47}, "...": "..."},
  "totals":  {"total_line": 224.5, "simulation_samples": {"over": 0.32, "push": 0.0, "under": 0.68}, "...": "..."},
  "margin_ranges": {"buckets": [{"code": "away_26_plus", "range": "26+", "elo_normal": 0.021, "simulation_samples": 0.013}, "...": "..."]},
  "risk": {"confidence": 0.71, "flags": []},
  "explanation": "…"
}
```

Where provider choices disagree, **both are returned** (e.g. `elo_normal` vs
`simulation_samples`) instead of hiding the disagreement — the frontend shows
both, and a `model_disagreement` risk flag fires when it matters.

## How it works

```text
real history → validate → features (as-of-safe) → models (Elo / PPP / pace / shot mix)
→ possession-level simulation → Monte Carlo → distributions
→ win / spread / totals / margin-range probabilities
→ walk-forward backtest → calibration → honest confidence
```

* **Feature pipeline** — Elo, offensive/defensive ratings, pace, shooting,
  rebounding, turnovers; exponential time decay, rest days, availability.
  Features may only use information public at the prediction moment; any
  future information is a hard **FAIL** (leakage tests), never a warning.
* **Models → simulator** — team params become per-possession distributions
  (turnover / free-throw trip / 2PT or 3PT attempt, offensive-rebound
  continuation); the game clock bounds possessions, overtimes emerge
  naturally. 10k sims ≈ 1.7 s per game.
* **Evaluation** — walk-forward backtests only (no random splits),
  logloss/Brier/ECE against constant-probability baselines; results are
  reported even when a plain Elo rating beats the whole simulator.
* **Storage** — SQLite behind a repository interface; snapshots are
  byte-exact DB copies with SHA-256 manifests; predictions cite their snapshot.

### Measured findings (reported as measured, not as marketing)

* On 17 NBA seasons (20,440 games), **plain Elo out-predicts the frozen
  simulator** in every test window — and the system says so itself
  ([docs/MULTI_SEASON_RESULTS.md](docs/MULTI_SEASON_RESULTS.md)).
* Per-league spread-provider defaults were chosen by walk-forward line-probe
  logloss, not taste: `elo_normal` wins in NBA / EuroLeague / ACB / CBA / BSL;
  NBL is a statistical tie that keeps the simulation default
  ([docs/PHASE12_A_SPREAD_API.md](docs/PHASE12_A_SPREAD_API.md) §12-H).
* A simulator bug (sampler realized ~3.8% fewer points per possession than
  its recorded target) was found *by asking why totals always leaned under*,
  fixed, and re-calibrated: MC-vs-actual totals bias on a 30-game walk-forward
  went −12.5 → −2.2 points (§12-L). The contract tests now pin the closed
  form to the sampler within Monte-Carlo noise.

## Data

**No data is bundled.** Collectors run against public endpoints; each one
documents its source, auth and known quirks in
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md):

| League | Source | Auth |
|---|---|---|
| NBA (history) | stats.nba.com gamelog / llimllib parquet mirrors | none |
| NBA (fixtures, injuries) | site.api.espn.com | none |
| EuroLeague | live.euroleague.net API | none |
| ACB | api2.acb.com (site's own public client key) | public key |
| NBL / BSL / CBA | league live endpoints (CBA: AES-JSON) | none |

You are responsible for complying with each source's terms of service; this
repository ships code only.

## Development notes

* **Tests:** `pip install -e ".[dev]"` then `pytest -q` — 1,097 tests, no
  network required (collectors are stubbed at the seam; live smokes are run
  manually and recorded in `docs/`).
* **Docs/** is a real development archive: per-phase design notes, decision
  records, calibration experiments and honest failure reports (e.g. Phase 8-B2
  "simulation ≈ random on the 5-season window"). Read
  [PHASE12_A_SPREAD_API.md](docs/PHASE12_A_SPREAD_API.md) for the prediction
  API layer and [DATA_SOURCES.md](docs/DATA_SOURCES.md) before collecting.

## Project layout

```text
basketball_agent/
├── data/            pydantic schemas, validators, SQLite DDL + repository, collectors
├── features/        as-of-safe feature pipeline
├── models/          Elo · offense/defense PPP · pace · possession-outcome models
├── simulation/      possession-level engine + Monte Carlo runner
├── agents/          prediction, validation, risk, ensemble, decision, explanation
├── evaluation/      walk-forward backtest, calibration, league audit, DNP audit
├── collection/      production loop, fixtures lifecycle, injury ingestion
├── api/             stdlib HTTP JSON API + static Chinese web UI
├── cli/             `basketball` entry point (predict/settle/serve/ingest/evaluate/…)
└── tests/           unit · integration · regression · leakage · simulation
tools/               user-side season fetchers (EuroLeague, ACB, NBL, CBA, BSL)
```

## License

[MIT](LICENSE) — research project, no warranty. Probabilities produced here
are for research; if you use them for betting you accept the risk, and you
still must comply with your data sources' and jurisdiction's terms.
