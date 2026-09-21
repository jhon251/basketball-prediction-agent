"""Phase 8-B5: the shared train-window fitting cache must not change results.

These tests pin the two properties that let B5 optimise the fit at all:

1. **Equivalence** — a :class:`FittingDataset` scored with a predictor returns
   exactly the summary the per-config :class:`BacktestEngine` run returns, for
   the strength grid *and* for the decay-lambda sweep.
2. **Bounded work** — the 21-scale + 5-shrinkage grid consumes ONE walk-forward
   feature pass instead of 26, and the fit never mixes leagues.

The legacy (pre-B5) per-candidate loops are re-implemented verbatim in this
module as the reference implementation, so a future edit that silently changes
the fitting protocol fails here instead of silently changing fitted parameters.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.collectors.synthetic import SyntheticLeagueConfig, generate_league
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import League
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.evaluation.backtest import (
    BacktestConfig,
    BacktestEngine,
    EloPredictor,
    SimulationPredictor,
    StrengthPredictor,
    sweep_decay_lambda,
)
from basketball_agent.evaluation.fitting import build_fitting_dataset
from basketball_agent.evaluation.metrics import log_loss
from basketball_agent.evaluation.param_tuning import (
    LOGISTIC_CANDIDATES,
    SHRINKAGE_CANDIDATES,
    _STRENGTH_GRID_WARMUP,
    estimate_logistic_scale,
    estimate_shrinkage_k,
    fit_v1_params,
)
from basketball_agent.features.feature_pipeline import FeaturePipeline
from basketball_agent.models.win_probability import WinProbabilityConfig

pytestmark = pytest.mark.integration

UTC = timezone.utc


@pytest.fixture
def synthetic_db(tmp_path, fixed_now):
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=3, seed=2025)
    )
    ds = Dataset(
        games=tuple(games), team_stats=tuple(team_stats), player_stats=tuple(player_stats),
        teams=tuple(teams), players=tuple(players),
    )
    raw = {
        "teams": [t.model_dump(mode="json") for t in ds.teams],
        "players": [p.model_dump(mode="json") for p in ds.players],
        "games": [g.model_dump(mode="json") for g in ds.games],
        "team_stats": [x.model_dump(mode="json") for x in ds.team_stats],
        "player_stats": [x.model_dump(mode="json") for x in ds.player_stats],
    }
    db = Database(tmp_path / "fitting.db")
    db.initialize()
    DataIngestor(db, now=fixed_now).ingest(raw)
    return db


def _ordered_games(repo: Repository):
    return sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))


class _ComputeCounter:
    """Counts every walk-forward feature derivation in the process."""

    def __init__(self, monkeypatch):
        self.n = 0
        self._original = FeaturePipeline.compute

        def wrapper(pipeline_self, game_id, as_of):
            self.n += 1
            return self._original(pipeline_self, game_id, as_of)

        monkeypatch.setattr(FeaturePipeline, "compute", wrapper)


# ---------------------------------------------------------------------------
# 1. Equivalence with the per-config engine path
# ---------------------------------------------------------------------------


class TestCacheEquivalence:
    def test_strength_metrics_match_engine_summary(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        ids = [g.game_id for g in games]
        dataset = build_fitting_dataset(repo, ids, decay_lambda=0.0, warmup_games=5)

        for cfg in (
            WinProbabilityConfig(shrinkage_k=0.0, home_court_advantage=0.0, margin_logistic_scale=8.0),
            WinProbabilityConfig(shrinkage_k=5.0, home_court_advantage=2.75, margin_logistic_scale=11.5),
            WinProbabilityConfig(shrinkage_k=20.0, home_court_advantage=-1.0, margin_logistic_scale=18.0),
        ):
            predictor = StrengthPredictor(config=cfg)
            cached = dataset.metrics(predictor)
            engine = BacktestEngine(
                repo, StrengthPredictor(config=cfg), BacktestConfig(warmup_games=5, decay_lambda=0.0)
            ).run(game_ids=ids)
            assert cached == engine.summary()
            assert cached["log_loss"] == engine.win_prob_metrics()["log_loss"]

    def test_simulation_metrics_match_engine_summary(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        ids = [g.game_id for g in games]
        dataset = build_fitting_dataset(repo, ids, decay_lambda=0.0, warmup_games=5)
        predictor = SimulationPredictor(n_simulations=100, seed=7)
        cached = dataset.metrics(predictor)
        engine = BacktestEngine(
            repo, SimulationPredictor(n_simulations=100, seed=7),
            BacktestConfig(n_simulations=100, seed=7, warmup_games=5, decay_lambda=0.0),
        ).run(game_ids=ids)
        assert cached == engine.summary()

    def test_cached_dataset_records_match_engine_records(self, synthetic_db):
        """Same game order, same skip reasons, same predictions."""
        repo = Repository(synthetic_db)
        ids = [g.game_id for g in _ordered_games(repo)]
        dataset = build_fitting_dataset(repo, ids, decay_lambda=0.0, warmup_games=5)
        predictor = EloPredictor()
        engine = BacktestEngine(
            repo, EloPredictor(), BacktestConfig(warmup_games=5, decay_lambda=0.0)
        ).run(game_ids=ids)
        cached = dataset.to_backtest_result(predictor)
        assert [r.game_id for r in cached.records] == [r.game_id for r in engine.records]
        assert [r.skipped_reason for r in cached.records] == \
               [r.skipped_reason for r in engine.records]
        for a, b in zip(cached.records, engine.records):
            if a.prediction is None:
                assert b.prediction is None
                continue
            assert a.prediction.prob_home == b.prediction.prob_home
            assert a.prediction.expected_margin == b.prediction.expected_margin

    def test_sweep_cached_equals_uncached(self, synthetic_db):
        repo = Repository(synthetic_db)
        ids = [g.game_id for g in _ordered_games(repo)]
        dataset = build_fitting_dataset(repo, ids, decay_lambda=0.0, warmup_games=5)
        cached = sweep_decay_lambda(
            repo, [0.0], n_simulations=100, warmup_games=5, seed=7, game_ids=ids,
            datasets={0.0: dataset},
        )
        uncached = sweep_decay_lambda(
            repo, [0.0], n_simulations=100, warmup_games=5, seed=7, game_ids=ids,
        )
        assert cached[0.0] == uncached[0.0]

    def test_sweep_cached_matches_other_lambda_engine(self, synthetic_db):
        """A lambda!=0 cache reproduces that lambda's own engine run."""
        repo = Repository(synthetic_db)
        ids = [g.game_id for g in _ordered_games(repo)]
        dataset = build_fitting_dataset(repo, ids, decay_lambda=0.05, warmup_games=5)
        cached = sweep_decay_lambda(
            repo, [0.05], n_simulations=100, warmup_games=5, seed=7, game_ids=ids,
            datasets={0.05: dataset},
        )
        uncached = sweep_decay_lambda(
            repo, [0.05], n_simulations=100, warmup_games=5, seed=7, game_ids=ids,
        )
        assert cached[0.05] == uncached[0.05]


# ---------------------------------------------------------------------------
# 2. Legacy reference implementation (pre-B5 loops) — the fitting protocol
# ---------------------------------------------------------------------------


def _legacy_scale(repo: Repository, cutoff: datetime, hca: float) -> float:
    games = [g for g in _ordered_games(repo) if g.game_date < cutoff]
    best_scale, best_loss = LOGISTIC_CANDIDATES[0], None
    for scale in LOGISTIC_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=5.0, home_court_advantage=hca, margin_logistic_scale=scale
        )
        recs = BacktestEngine(
            repo, StrengthPredictor(config=cfg), BacktestConfig(warmup_games=5)
        ).run(game_ids=[g.game_id for g in games]).records
        pairs = [
            (r.prediction.prob_home, r.actual_home_win)
            for r in recs if r.prediction is not None and r.actual_home_win is not None
        ]
        if len(pairs) < 5:
            continue
        loss = log_loss([y for _, y in pairs], [p for p, _ in pairs])
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_scale = loss, scale
    return best_scale


def _legacy_k(repo: Repository, cutoff: datetime, hca: float, scale: float) -> float:
    games = [g for g in _ordered_games(repo) if g.game_date < cutoff]
    best_k, best_loss = float(SHRINKAGE_CANDIDATES[0]), None
    for k in SHRINKAGE_CANDIDATES:
        cfg = WinProbabilityConfig(
            shrinkage_k=float(k), home_court_advantage=hca, margin_logistic_scale=scale
        )
        recs = BacktestEngine(
            repo, StrengthPredictor(config=cfg), BacktestConfig(warmup_games=5)
        ).run(game_ids=[g.game_id for g in games]).records
        pairs = [
            (r.prediction.prob_home, r.actual_home_win)
            for r in recs if r.prediction is not None and r.actual_home_win is not None
        ]
        if len(pairs) < 5:
            continue
        loss = log_loss([y for _, y in pairs], [p for p, _ in pairs])
        if best_loss is None or loss < best_loss - 1e-12:
            best_loss, best_k = loss, float(k)
    return best_k


class TestLegacyFittingProtocol:
    """The optimised grid must select exactly what the old loops selected."""

    @pytest.fixture
    def cut(self, synthetic_db):
        games = _ordered_games(Repository(synthetic_db))
        return games[int(len(games) * 0.6)].game_date

    def test_scale_grid_matches_legacy(self, synthetic_db, cut):
        repo = Repository(synthetic_db)
        legacy = _legacy_scale(repo, cut, hca=2.0)
        assert estimate_logistic_scale(repo, cut, 2.0) == legacy

    def test_shrinkage_grid_matches_legacy(self, synthetic_db, cut):
        repo = Repository(synthetic_db)
        scale = _legacy_scale(repo, cut, hca=2.0)
        legacy = _legacy_k(repo, cut, hca=2.0, scale=scale)
        assert estimate_shrinkage_k(repo, cut, 2.0, scale) == legacy

    def test_passed_dataset_is_used_verbatim(self, synthetic_db, cut):
        """An explicitly supplied dataset must drive the same selection."""
        repo = Repository(synthetic_db)
        games = [g for g in _ordered_games(repo) if g.game_date < cut]
        dataset = build_fitting_dataset(
            repo, [g.game_id for g in games],
            decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP,
        )
        assert estimate_logistic_scale(repo, cut, 2.0, dataset=dataset) == \
               _legacy_scale(repo, cut, 2.0)
        assert estimate_shrinkage_k(repo, cut, 2.0, 11.5, dataset=dataset) == \
               _legacy_k(repo, cut, 2.0, 11.5)


# ---------------------------------------------------------------------------
# 3. Work is bounded, and stays inside one league
# ---------------------------------------------------------------------------


class TestBoundedWork:
    def test_scale_grid_uses_a_single_feature_pass(self, synthetic_db, monkeypatch):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        cutoff = games[int(len(games) * 0.6)].game_date

        counter = _ComputeCounter(monkeypatch)
        estimate_logistic_scale(repo, cutoff, 2.0)
        n = counter.n

        train_ids = [g.game_id for g in games if g.game_date < cutoff]
        expected = build_fitting_dataset(
            repo, train_ids, decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP
        ).n_feature_computations
        assert expected > 0
        assert n == expected, "the 21-candidate scale grid must reuse one feature pass"
        assert n < len(LOGISTIC_CANDIDATES) * expected

    def test_shrinkage_grid_uses_a_single_feature_pass(self, synthetic_db, monkeypatch):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        cutoff = games[int(len(games) * 0.6)].game_date
        scale = estimate_logistic_scale(repo, cutoff, 2.0)

        counter = _ComputeCounter(monkeypatch)
        estimate_shrinkage_k(repo, cutoff, 2.0, scale)
        n = counter.n

        train_ids = [g.game_id for g in games if g.game_date < cutoff]
        expected = build_fitting_dataset(
            repo, train_ids, decay_lambda=0.0, warmup_games=_STRENGTH_GRID_WARMUP
        ).n_feature_computations
        assert n == expected, "the 5-candidate shrinkage grid must reuse one feature pass"

    def test_fit_uses_one_pass_per_lambda(self, synthetic_db, monkeypatch):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        cutoff = games[int(len(games) * 0.6)].game_date
        train_ids = [g.game_id for g in games if g.game_date < cutoff]
        per_pass = build_fitting_dataset(
            repo, train_ids, decay_lambda=0.0, warmup_games=5
        ).n_feature_computations

        counter = _ComputeCounter(monkeypatch)
        fit_v1_params(
            repo, cutoff, lambda_candidates=(0.0,), n_simulations=20, seed=7, warmup_games=5
        )
        # warmup 5 == the strength grid's fixed warmup, so the lambda=0 leg of
        # the sweep reuses the very same pass: exactly one pass in total.
        assert counter.n == per_pass

    def test_fit_lambda_sweep_adds_one_pass_per_extra_lambda(self, synthetic_db, monkeypatch):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        cutoff = games[int(len(games) * 0.6)].game_date
        train_ids = [g.game_id for g in games if g.game_date < cutoff]
        per_pass = build_fitting_dataset(
            repo, train_ids, decay_lambda=0.0, warmup_games=5
        ).n_feature_computations

        counter = _ComputeCounter(monkeypatch)
        fit_v1_params(
            repo, cutoff, lambda_candidates=(0.0, 0.02), n_simulations=20, seed=7, warmup_games=5
        )
        assert counter.n == 2 * per_pass


class TestLeaguePurity:
    def test_mixed_leagues_are_refused(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        foreign = games[0].model_copy(update={"league": League.EUROLEAGUE})

        class _StubRepo:
            def get_game(self, game_id):
                return foreign if game_id == games[0].game_id else repo.get_game(game_id)

        with pytest.raises(ValueError, match="cross-league"):
            build_fitting_dataset(
                _StubRepo(), [g.game_id for g in games[:3]], warmup_games=0
            )

    def test_dataset_is_single_league_and_ordered(self, synthetic_db):
        repo = Repository(synthetic_db)
        games = _ordered_games(repo)
        shuffled = list(reversed([g.game_id for g in games]))
        dataset = build_fitting_dataset(repo, shuffled, warmup_games=5)
        assert dataset.league == "NBA"
        assert [s.game_id for s in dataset.slots] == [g.game_id for g in games]
