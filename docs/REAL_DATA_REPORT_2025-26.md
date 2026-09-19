# Phase 7 Final Report

## Data

Source: nba_data
Status: AVAILABLE
Date Range: ('2025-10-21', '2026-06-13')
Seasons: 2025-26
Games: 1322
Teams: 30
Missing: unavailable without a schedule reference (no schedule source ingested) (missing scores: 0, missing stats: 0)
Duplicates: 0

### Raw source manifests

- `nba_data` / gamelog_2026.parquet — AVAILABLE, retrieved 2026-09-19T12:43:06, sha256 f992e6659dee93cf…, version: n/a

## Temporal Integrity

Leakage Tests: 605061
Passed: 605061
Failed: 0

## Backtest

Train: .. 2026-02-01
Validation: embedded in train-window walk-forward
Test: 2026-02-01 .. end of data

## Results

| Model | Accuracy | LogLoss | Brier | ECE |
|---|---|---|---|---|
| random | 0.5574 | 0.6931 | 0.2500 | 0.0574 |
| elo | 0.7365 | 0.5618 | 0.1889 | 0.0697 |
| strength | 0.7264 | 0.5714 | 0.1926 | 0.0825 |
| simulation | 0.7264 | 0.5767 | 0.1947 | 0.0809 |

## Score Prediction

Home MAE: 9.6080
Away MAE: 9.8871
Total MAE: 15.0038
Margin MAE: 11.9993

## Calibration

| bucket | predicted | actual | n |
|---|---|---|---|
| 0.10-0.20 | 0.152 | 0.000 | 7 |
| 0.20-0.30 | 0.266 | 0.030 | 33 |
| 0.30-0.40 | 0.354 | 0.261 | 69 |
| 0.40-0.50 | 0.453 | 0.364 | 107 |
| 0.50-0.60 | 0.541 | 0.602 | 98 |
| 0.60-0.70 | 0.648 | 0.699 | 143 |
| 0.70-0.80 | 0.746 | 0.846 | 91 |
| 0.80-0.90 | 0.831 | 0.814 | 43 |
| 0.90-1.00 | 0.913 | 1.000 | 1 |

## Experiments

### EXP-NBA-001
Description: Current V1 on real NBA data (frozen defaults)
Window: ('2026-02-01T00:00:00+00:00', 'end of data')


### EXP-NBA-002
Description: Decay lambda sweep (train window only)
Window: 2026-02-01T00:00:00+00:00

Chosen lambda: 0.05 (validation logloss 0.6044)

### EXP-NBA-003
Description: Ensemble vs individual models (weights fitted on train only)
Window: ('2026-02-01T00:00:00+00:00', 'end of data')

Weights: {'elo': 0.0, 'strength': 1.0, 'sim': 0.0}
Test logloss: {'elo': 0.5617916361771362, 'strength': 0.5820907476622874, 'simulation': 0.5766660833309506, 'ensemble': 0.5820907476622874}

### EXP-NBA-004
Description: Simulation vs baselines on the test window
Window: ('2026-02-01T00:00:00+00:00', 'end of data')

Comparison: {'simulation vs random': 'simulation BETTER by 0.116 logloss', 'simulation vs elo': 'simulation WORSE by 0.015 logloss', 'simulation vs strength': 'simulation ≈ baseline (within 0.01)'}

## Frozen Parameters

| parameter | value |
|---|---|
| HCA | 1.59 |
| logistic scale | 10.00 |
| shrinkage k | 2.0 |
| decay lambda | 0.050 |
| training window | 2025-10-21 .. 2026-02-01 |
| validation logloss | 0.6044 |

## Error Analysis

- home_favorites (p>=0.5): n=376, acc=0.7234, logloss=0.5810, brier=0.1965
- away_favorites (p<0.5): n=216, acc=0.7315, logloss=0.5691, brier=0.1916
- confident (p>=0.65 or p<=0.35): n=276, acc=0.8188, logloss=0.4915, brier=0.1553
- tossups (0.45<p<0.55): n=122, acc=0.5492, logloss=0.6867, brier=0.2468
- close games (|margin|<=5): n=121, acc=0.6281, logloss=0.6588, brier=0.2320
- blowouts (|margin|>=15): n=251, acc=0.7968, logloss=0.5096, brier=0.1649
- high total (>=median): n=299, acc=0.7358, logloss=0.5556, brier=0.1855
- low total (<median): n=293, acc=0.7167, logloss=0.5981, brier=0.2042
- home teams: n=592, acc=0.7264, logloss=0.5767, brier=0.1947
- away teams: n=592, acc=0.2736, logloss=0.5767, brier=0.1947
- season 2025-26: n=592, acc=0.7264, logloss=0.5767, brier=0.1947

## Conclusion

1. V1 on real NBA data: YES — simulation logloss 0.5767 beats the random baseline 0.6931.
2. Simulation vs baselines: worse than/equal to simulation vs elo.
3. Ensemble vs best single model: NO — ensemble 0.5821 does not beat best single 0.5618.
4. Parameters needing adjustment: see Frozen Parameters / EXP-NBA-002 sweep.
5. Model weaknesses: see Error Analysis groups with the worst logloss.
6. Next step: fix the weakest error-analysis group before adding features.

## Known Limitations

- data is the registered source only (see SOURCE_STATUS); no market baseline available (not fabricated)
- missing-games accounting requires an official schedule reference

exit=0
