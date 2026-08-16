"""Integration tests for the real-data pipeline (CSV -> raw -> ingest ->
snapshot -> prediction audit chain) and temporal integrity (spec §5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.data.collectors.manifest import RawDataStore
from basketball_agent.data.collectors.nba_csv import load_nba_games_csv
from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.loader import DataIngestor
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game
from basketball_agent.data.snapshots.manager import SnapshotManager
from basketball_agent.evaluation.coverage import build_coverage_report, format_coverage_report
from basketball_agent.evaluation.leakage import check_temporal_integrity

pytestmark = pytest.mark.integration

UTC = timezone.utc


def make_real_csv(path, n_games: int = 18, seed: int = 3) -> list[datetime]:
    """Build a multi-week, internally consistent NBA-style CSV (real format)."""
    import random

    rng = random.Random(seed)
    teams = ["LAL", "BOS", "GSW", "MIA", "DEN", "PHX"]
    lines = [
        "game_date,home_team,away_team,home_score,away_score,"
        "home_fg,home_fga,home_fg3,home_fg3a,home_ft,home_fta,home_orb,home_drb,home_trb,"
        "home_ast,home_tov,home_pf,away_fg,away_fga,away_fg3,away_fg3a,away_ft,away_fta,"
        "away_orb,away_drb,away_trb,away_ast,away_tov,away_pf"
    ]
    dates: list[datetime] = []
    day = datetime(2024, 10, 25, tzinfo=UTC)
    pairs = [(a, b) for i, a in enumerate(teams) for b in teams[i + 1:]]
    for k in range(n_games):
        home, away = pairs[k % len(pairs)]
        if (k // len(pairs)) % 2 == 1:
            home, away = away, home
        if home == away:
            continue
        # strengths: LAL/BOS strong, PHX weak — deterministic simple teams
        strength = {"LAL": 1.06, "BOS": 1.05, "GSW": 1.02, "MIA": 0.99, "DEN": 1.0, "PHX": 0.93}
        hp = strength[home] * rng.uniform(0.9, 1.1)
        ap = strength[away] * rng.uniform(0.9, 1.1)
        pace = 99 + rng.uniform(-2, 2)
        fga = round(pace * 0.84)
        fg3a = round(fga * 0.38)
        fta = round(pace * 0.22)
        # generate consistent makes via shooting percentages
        p2, p3, ftp = 0.52 * hp, 0.36 * hp, 0.78
        fg3 = sum(1 for _ in range(fg3a) if rng.random() < min(p3, 0.6))
        fg2 = sum(1 for _ in range(fga - fg3a) if rng.random() < min(p2, 0.7))
        ft = sum(1 for _ in range(fta) if rng.random() < ftp)
        hs = 2 * fg2 + 3 * fg3 + ft
        fg = fg2 + fg3
        miss = fga - fg
        orb = round(miss * 0.24 * rng.uniform(0.8, 1.2))
        drb = round(miss * 0.76)
        trb = orb + drb
        ast = round(fg * 0.55)
        tov = round(pace * 0.13 * rng.uniform(0.85, 1.15))
        pf = round(rng.uniform(15, 24))

        p2a, p3a, ftpa = 0.52 * ap, 0.36 * ap, 0.78
        afg3 = sum(1 for _ in range(round(round(pace * 0.84) * 0.38)) if rng.random() < min(p3a, 0.6))
        afga = round(pace * 0.84)
        afg3a = round(afga * 0.38)
        afg2 = sum(1 for _ in range(afga - afg3a) if rng.random() < min(p2a, 0.7))
        afta = round(pace * 0.22)
        aft = sum(1 for _ in range(afta) if rng.random() < ftpa)
        as_ = 2 * afg2 + 3 * afg3 + aft
        amiss = afga - (afg2 + afg3)
        aorb = round(amiss * 0.24 * rng.uniform(0.8, 1.2))
        adrb = round(amiss * 0.76)
        aast = round((afg2 + afg3) * 0.55)
        atov = round(pace * 0.13 * rng.uniform(0.85, 1.15))
        apf = round(rng.uniform(15, 24))

        date = day + timedelta(days=k)
        dates.append(date)
        lines.append(
            f"{date:%Y-%m-%d},{home},{away},{hs},{as_},"
            f"{fg},{fga},{fg3},{fg3a},{ft},{fta},{orb},{drb},{trb},{ast},{tov},{pf},"
            f"{afg2 + afg3},{afga},{afg3},{afg3a},{aft},{afta},{aorb},{adrb},"
            f"{aorb + adrb},{aast},{atov},{apf}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dates


@pytest.fixture
def real_db(tmp_path, fixed_now):
    csv_path = tmp_path / "nba_games_2024.csv"
    make_real_csv(csv_path)
    db = Database(tmp_path / "real.db")
    db.initialize()
    store = RawDataStore(tmp_path / "raw")
    manifest = store.register("nba_games", csv_path, source_version="fixture-v1")
    raw = load_nba_games_csv(csv_path)
    DataIngestor(db, now=fixed_now).ingest(raw)
    snapshot = SnapshotManager(tmp_path / "snapshots").create(db.path, tag="ingest:nba_games")
    return db, manifest, snapshot, csv_path


class TestRealDataPipeline:
    def test_csv_to_snapshot_audit_chain(self, real_db, fixed_now):
        db, manifest, snapshot, _ = real_db
        repo = Repository(db)
        # prediction binds the snapshot id and full provenance
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        target = games[12]  # mid-window game with history for both teams
        agent = PredictionAgent(repo, n_simulations=100, seed=7, snapshot_id=snapshot.snapshot_id)
        prediction = agent.predict(target.game_id, as_of=target.game_date)
        assert prediction.snapshot_id == snapshot.snapshot_id
        assert prediction.model_version
        # audit chain: provenance games all strictly before as_of (§5)
        for gid in prediction.features.provenance.games_used:
            assert repo.get_game(gid).game_date < target.game_date
        leak = check_temporal_integrity(prediction.features, repo)
        assert leak.status == "PASS"

    def test_mid_season_prediction_uses_only_prior_games(self, real_db, fixed_now):
        db, _, _, _ = real_db
        repo = Repository(db)
        games = sorted(repo.list_games(), key=lambda g: (g.game_date, g.game_id))
        target = games[10]
        agent = PredictionAgent(repo, n_simulations=50, seed=7)
        prediction = agent.predict(target.game_id, as_of=target.game_date)
        # the target's own stats and any later games are excluded
        assert target.game_id not in prediction.features.provenance.games_used
        for gid in prediction.features.provenance.games_used:
            assert repo.get_game(gid).game_date < target.game_date
        # team features only use prior games
        n_prior = sum(
            1 for g in games
            if g.game_date < target.game_date and target.home_team_id in (g.home_team_id, g.away_team_id)
        )
        assert prediction.features.home.games_played == n_prior

    def test_coverage_report(self, real_db):
        db, _, _, _ = real_db
        report = build_coverage_report(Repository(db))
        assert report["games"] == 18
        assert report["teams"] == 6
        assert report["missing_scores"] == 0
        assert report["missing_stats"] == 0
        assert report["duplicate_games"] == 0
        assert report["average_games_per_team"] == pytest.approx(6.0)
        text = format_coverage_report(report)
        assert "NBA DATA COVERAGE REPORT" in text
        assert "Date range:" in text

    def test_manifest_provenance(self, real_db, tmp_path):
        db, manifest, _, csv_path = real_db
        assert manifest.status == "AVAILABLE"
        assert manifest.source_version == "fixture-v1"
        # raw file is a verbatim copy of the input
        raw_copy = tmp_path / "raw" / "nba_games" / csv_path.name
        assert raw_copy.read_text(encoding="utf-8") == csv_path.read_text(encoding="utf-8")
