"""Market baseline backtest tests (Phase 11-A.4).

Covers the frozen walk-forward × closing-odds chain:

a) sample matching  — on an odds-bearing fixture all five models'
   ``subset=with_odds`` rows cover the SAME games (n equal,
   n_with_odds == n);
b) fail-loud       — a missing odds table raises ``OddsDataUnavailable``
   instead of silently degrading to an empty market column;
c) identity        — the exported market probability is exactly
   ``moneyline_pair_to_fair`` applied to the stored CLOSE pair;
d) entries         — ``walk_forward_entries`` exports complete per-game
   records and the aggregate rows are consistent with them.

The fixture is the synthetic league (self-consistent box scores by
construction); odds rows are synthetic market prices, never features.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.collectors.synthetic import (
    SyntheticLeagueConfig,
    generate_league,
)
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.odds_repository import OddsRepository
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.dataset import Dataset
from basketball_agent.evaluation.devig import moneyline_pair_to_fair
from basketball_agent.evaluation.market_baseline import (
    ENTRY_KEYS,
    OddsDataUnavailable,
    load_odds_map,
    market_baseline_backtest,
    walk_forward_entries,
)

UTC = timezone.utc
WINDOW_START = datetime(2024, 11, 1, tzinfo=UTC)
WINDOW_END = datetime(2024, 12, 1, tzinfo=UTC)
MODELS = ("random", "elo", "strength", "simulation", "market")
N_SIM, SEED, WARMUP = 20, 7, 2


def _ingest_synthetic(db: Database) -> None:
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=2025))
    ds = Dataset(games=tuple(games), team_stats=tuple(team_stats),
                 player_stats=tuple(player_stats), teams=tuple(teams),
                 players=tuple(players))
    raw = {k: [m.model_dump(mode="json") for m in v] for k, v in
           (("teams", ds.teams), ("players", ds.players), ("games", ds.games),
            ("team_stats", ds.team_stats), ("player_stats", ds.player_stats))}
    DataIngestor(db, now=datetime(2025, 3, 1, tzinfo=UTC)).ingest(raw)


def _insert_synthetic_odds(db: Database, first: int, last: int) -> int:
    """Odds for FINAL games at sorted positions [first, last)."""
    games = sorted(
        Repository(db).list_games(status=GameStatus.FINAL, league=League.NBA),
        key=lambda g: (g.game_date, g.game_id))
    rows = []
    for i, g in enumerate(games[first:last], start=first):
        am_h, am_a = -(105 + i), 100 + i
        dec_h = 1 + 100.0 / abs(am_h)
        dec_a = 1 + am_a / 100.0
        rows.append({
            "game_id": g.game_id, "league": "NBA", "bookmaker": "fixture",
            "market": "MONEYLINE", "kind": "CLOSE", "line": None,
            "price_decimal": min(dec_h, dec_a),
            "price_home": am_h, "price_away": am_a,
            "captured_at": (g.game_date - timedelta(hours=1)).isoformat(),
            "source": "test",
        })
    inserted, _ = OddsRepository(db).insert_odds(rows)
    return inserted


@pytest.fixture(scope="module")
def baseline_db(tmp_path_factory) -> Database:
    """Synthetic league; odds on a mid-season slice of games."""
    db = Database(tmp_path_factory.mktemp("mb") / "baseline.db")
    db.initialize()
    _ingest_synthetic(db)
    _insert_synthetic_odds(db, first=8, last=18)
    return db


@pytest.fixture(scope="module")
def baseline_entries(baseline_db) -> list[dict]:
    return walk_forward_entries(
        baseline_db, WINDOW_START, WINDOW_END,
        league=League.NBA, n_simulations=N_SIM, seed=SEED, warmup_games=WARMUP)


@pytest.fixture(scope="module")
def baseline_rows(baseline_db) -> list:
    return market_baseline_backtest(
        baseline_db, WINDOW_START, WINDOW_END,
        league=League.NBA, n_simulations=N_SIM, seed=SEED, warmup_games=WARMUP)


class TestSampleMatching:
    def test_with_odds_rows_cover_same_games(self, baseline_rows):
        """(a) the five with_odds rows are the SAME sample: n equal and
        n_with_odds == n (the fair cross-model/market comparison set)."""
        with_odds = [r for r in baseline_rows if r.subset == "with_odds"]
        assert sorted(r.model for r in with_odds) == sorted(MODELS)
        n_games = {r.n_games for r in with_odds}
        assert len(n_games) == 1
        n = n_games.pop()
        assert n > 0
        assert all(r.n_with_odds == n for r in with_odds)

    def test_all_rows_report_odds_coverage(self, baseline_rows, baseline_entries):
        """subset=all rows carry the same n_with_odds (reference only)."""
        n_expected = sum(1 for e in baseline_entries
                         if e["p_market"] is not None)
        assert 0 < n_expected < len(baseline_entries)  # both subsets exercised
        for r in baseline_rows:
            if r.subset == "all" and r.model != "market":
                assert r.n_games == len(baseline_entries)
            assert r.n_with_odds == n_expected


class TestFailLoud:
    def test_missing_odds_table_raises(self, ingested_db):
        """(b) without a game_odds table the whole chain fails loudly."""
        with ingested_db.transaction() as conn:
            conn.execute("DROP TABLE game_odds")
        with pytest.raises(OddsDataUnavailable, match="odds table"):
            load_odds_map(ingested_db)
        with pytest.raises(OddsDataUnavailable, match="odds table"):
            walk_forward_entries(
                ingested_db, datetime(2024, 11, 1, tzinfo=UTC),
                datetime(2024, 12, 1, tzinfo=UTC), warmup_games=1)
        with pytest.raises(OddsDataUnavailable, match="odds table"):
            market_baseline_backtest(
                ingested_db, datetime(2024, 11, 1, tzinfo=UTC),
                datetime(2024, 12, 1, tzinfo=UTC), warmup_games=1)


class TestMarketIdentity:
    def test_market_probability_equals_devig(self, baseline_db, baseline_entries):
        """(c) p_market == moneyline_pair_to_fair(price_home, price_away)."""
        odds_map = load_odds_map(baseline_db)
        with_market = [e for e in baseline_entries
                       if e["p_market"] is not None]
        assert with_market, "fixture must exercise the odds join"
        matched_ids = {gid for gid in odds_map
                       if any(e["game_id"] == gid for e in baseline_entries)}
        assert len(with_market) == len(matched_ids)
        for e in with_market:
            am_h, am_a = odds_map[e["game_id"]]
            fair_home, fair_away, margin = moneyline_pair_to_fair(am_h, am_a)
            assert e["p_market"] == fair_home
            # the de-vig identity the symmetric ROI bet relies on
            assert fair_home + fair_away == pytest.approx(1.0)
            assert margin > 0


class TestEntriesExport:
    def test_entry_schema_complete(self, baseline_entries):
        """(d) every entry carries exactly the contracted keys and types."""
        assert baseline_entries
        for e in baseline_entries:
            assert set(e.keys()) == set(ENTRY_KEYS)
            assert isinstance(e["game_id"], str) and e["game_id"]
            assert e["season"] == "2024-25"
            assert isinstance(e["outcome"], bool)
            assert e["p_random"] == 0.5
            for model in ("elo", "strength", "simulation"):
                assert 0.0 < e[f"p_{model}"] < 1.0
            assert e["p_market"] is None or 0.0 < e["p_market"] < 1.0

    def test_entries_chronological_and_unique(self, baseline_entries):
        ids = [e["game_id"] for e in baseline_entries]
        assert len(ids) == len(set(ids))
        dates = [gid.split("_", 1)[0] for gid in ids]
        assert dates == sorted(dates)

    def test_window_filtering_and_empty_window(self, baseline_db):
        inside = walk_forward_entries(
            baseline_db, datetime(2024, 11, 20, tzinfo=UTC),
            datetime(2024, 12, 1, tzinfo=UTC),
            league=League.NBA, n_simulations=N_SIM, seed=SEED,
            warmup_games=WARMUP)
        assert inside and all(
            datetime.strptime(e["game_id"].split("_", 1)[0], "%Y%m%d")
            .replace(tzinfo=UTC) >= datetime(2024, 11, 20, tzinfo=UTC)
            for e in inside)
        empty = market_baseline_backtest(
            baseline_db, datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2030, 2, 1, tzinfo=UTC), league=League.NBA)
        assert empty == []

    def test_aggregates_derived_from_entries(self, baseline_rows,
                                             baseline_entries):
        """BaselineRow numbers must be a pure aggregation of the entries."""
        n_odds = sum(1 for e in baseline_entries
                     if e["p_market"] is not None)
        by = {(r.model, r.subset): r for r in baseline_rows}
        for model in MODELS:
            w, a = by[(model, "with_odds")], by[(model, "all")]
            assert w.n_games == n_odds and w.n_with_odds == n_odds
            expected_all = n_odds if model == "market" else len(baseline_entries)
            assert a.n_games == expected_all
