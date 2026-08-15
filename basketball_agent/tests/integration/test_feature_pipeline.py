"""Integration tests for the FeaturePipeline over the sample dataset."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League, PlayerAvailabilityStatus
from basketball_agent.data.schemas.game import Game, PlayerAvailability
from basketball_agent.features import FeatureComputationError, FeaturePipeline
from basketball_agent.models.elo import EloConfig, expected_score

pytestmark = pytest.mark.integration

UTC = timezone.utc


def make_pipeline(db, **kwargs) -> FeaturePipeline:
    return FeaturePipeline(Repository(db), **kwargs)


class TestFeaturePipeline:
    def test_full_features_for_scheduled_game(self, scheduled_target):
        feats = make_pipeline(scheduled_target).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        assert feats.provenance.games_used == (
            "20241106_LAL_BOS", "20241110_LAL_GSW", "20241115_GSW_BOS",
        )
        assert feats.home.games_played == 2
        assert feats.away.games_played == 2
        # Pace: LAL games at 101.5 / 98.5, BOS at 101.5 / 97.5
        assert feats.home.pace == pytest.approx(100.0)
        assert feats.away.pace == pytest.approx(99.5)
        assert feats.expected_pace == pytest.approx(99.75)
        # Elo (hand-computed values, see test_elo.py)
        assert feats.home.elo == pytest.approx(1509.242, abs=0.05)
        assert feats.away.elo == pytest.approx(1510.393, abs=0.05)
        # Elo win prob is the formula applied to the pipeline's own ratings
        assert feats.elo_win_prob_home == pytest.approx(
            expected_score(feats.home.elo, feats.away.elo, EloConfig()), abs=1e-9
        )
        # Rest (target tips off at 02:00 on 11-20)
        assert feats.home_rest_days == pytest.approx(10 + 2 / 24)
        assert feats.away_rest_days == pytest.approx(5 + 2 / 24)
        assert feats.home_back_to_back is False
        assert feats.away_back_to_back is False
        # League context: 2 of 3 home teams won
        assert feats.league_home_win_pct == pytest.approx(2 / 3)
        # No availability records for the target game -> full availability
        assert feats.home_available_share == pytest.approx(1.0)
        assert feats.away_available_share == pytest.approx(1.0)
        assert feats.provenance.availability_used == ()

    def test_league_baseline_features(self, scheduled_target):
        feats = make_pipeline(scheduled_target).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        league = feats.league
        assert league.n_team_games == 6
        assert league.ortg == pytest.approx(104.3393, abs=1e-4)
        assert league.drtg == pytest.approx(104.3393, abs=1e-4)
        assert league.pace == pytest.approx(99.166667, abs=1e-5)
        assert league.fga_rate == pytest.approx(0.841906, abs=1e-5)
        assert league.fta_rate == pytest.approx(0.220355, abs=1e-5)
        assert league.tov_pct == pytest.approx(0.136272, abs=1e-5)
        assert league.orb_pct == pytest.approx(0.235645, abs=1e-5)
        assert league.drb_pct == pytest.approx(0.764355, abs=1e-5)
        assert league.p3a_share == pytest.approx(0.374619, abs=1e-5)
        assert league.p2_pct == pytest.approx(0.488921, abs=1e-5)
        assert league.fg3_pct == pytest.approx(0.364772, abs=1e-5)

    def test_outcome_rate_features(self, scheduled_target):
        feats = make_pipeline(scheduled_target).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        assert feats.home.fga_rate == pytest.approx(0.824832, abs=1e-5)
        assert feats.home.fta_rate == pytest.approx(0.219822, abs=1e-5)
        assert feats.home.p3a_share == pytest.approx(0.376471, abs=1e-5)
        assert feats.home.p2_pct == pytest.approx(0.504545, abs=1e-5)
        assert feats.away.fga_rate == pytest.approx(0.890556, abs=1e-5)
        assert feats.away.p2_pct == pytest.approx(0.485839, abs=1e-5)

    def test_form_and_season_aggregates(self, scheduled_target):
        feats = make_pipeline(scheduled_target).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        # LAL: margins +7, +3
        assert feats.home.form_last5_win_pct == pytest.approx(1.0)
        assert feats.home.form_last5_avg_margin == pytest.approx(5.0)
        assert feats.home.season_win_pct == pytest.approx(1.0)
        # BOS: margins -7, +15
        assert feats.away.season_win_pct == pytest.approx(0.5)
        assert feats.away.season_avg_margin == pytest.approx(4.0)

    def test_availability_adjusts_share(self, scheduled_target):
        repo = Repository(scheduled_target)
        src = datetime(2024, 11, 18, 12, 0, tzinfo=UTC)
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-1", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.OUT, availability_probability=0.0,
            source_time=src,
        ))
        repo.insert_availability(PlayerAvailability(
            player_id="LAL-2", game_id="20241120_LAL_BOS",
            status=PlayerAvailabilityStatus.AVAILABLE, availability_probability=1.0,
            source_time=src,
        ))
        feats = make_pipeline(scheduled_target).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        # LAL-1 avg minutes (36+35)/2=35.5, LAL-2 (30+28)/2=29 -> share = 29/64.5
        assert feats.home_available_share == pytest.approx(29 / 64.5)
        assert feats.away_available_share == pytest.approx(1.0)
        assert len(feats.provenance.availability_used) == 2

    def test_decay_lambda_changes_aggregates_in_expected_direction(self, scheduled_target):
        uniform = make_pipeline(scheduled_target, decay_lambda=0.0).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        decayed = make_pipeline(scheduled_target, decay_lambda=0.5).compute(
            "20241120_LAL_BOS", datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        )
        # LAL's recent ORtg (102.02) is lower than its earlier one (110.89),
        # so decaying toward the recent game must lower the aggregate.
        assert decayed.home.ortg < uniform.home.ortg
        assert decayed.provenance.games_used == uniform.provenance.games_used
        assert decayed.provenance.decay_lambda == 0.5
        assert uniform.provenance.decay_lambda == 0.0

    def test_as_of_window_limits_history(self, ingested_db):
        # as_of between g2 and g3, predicting g3 itself: only g1/g2 are
        # eligible history and g3's own stats are excluded.
        feats = make_pipeline(ingested_db).compute(
            "20241115_GSW_BOS", datetime(2024, 11, 12, 12, 0, tzinfo=UTC)
        )
        assert feats.provenance.games_used == ("20241106_LAL_BOS", "20241110_LAL_GSW")
        assert feats.home.games_played == 1  # GSW: g2 only
        assert feats.away.games_played == 1  # BOS: g1 only
        assert feats.home.elo == pytest.approx(1496.365, abs=0.05)
        assert feats.away.elo == pytest.approx(1494.392, abs=0.05)

    def test_insufficient_history_raises(self, ingested_db):
        repo = Repository(ingested_db)
        repo.insert_game(Game(
            game_id="20241112_GSW_LAL", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 11, 12, 2, 0, tzinfo=UTC),
            home_team_id="GSW", away_team_id="LAL",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        ))
        # as_of before GSW's first game (g2 on 11-10): GSW has no history.
        with pytest.raises(FeatureComputationError):
            make_pipeline(ingested_db).compute(
                "20241112_GSW_LAL", datetime(2024, 11, 8, 12, 0, tzinfo=UTC)
            )

    def test_unknown_game_raises(self, ingested_db):
        with pytest.raises(FeatureComputationError):
            make_pipeline(ingested_db).compute(
                "NOPE", datetime(2024, 11, 11, 12, 0, tzinfo=UTC)
            )

    def test_as_of_after_game_start_raises(self, ingested_db):
        with pytest.raises(FeatureComputationError):
            make_pipeline(ingested_db).compute(
                "20241106_LAL_BOS", datetime(2024, 11, 6, 12, 0, tzinfo=UTC)
            )

    def test_deterministic_across_identical_databases(self, scheduled_target, sample_dataset, fixed_now, tmp_path):
        db2 = Database(tmp_path / "db2.db")
        db2.initialize()
        from basketball_agent.tests.conftest import raw_from_dataset
        from basketball_agent.data.db.loader import DataIngestor
        DataIngestor(db2, now=fixed_now).ingest(raw_from_dataset(sample_dataset))
        Repository(db2).insert_game(Game(
            game_id="20241120_LAL_BOS", league=League.NBA, season="2024-25",
            game_date=datetime(2024, 11, 20, 2, 0, tzinfo=UTC),
            home_team_id="LAL", away_team_id="BOS",
            home_score=None, away_score=None, status=GameStatus.SCHEDULED,
        ))
        as_of = datetime(2024, 11, 19, 12, 0, tzinfo=UTC)
        f1 = make_pipeline(scheduled_target).compute("20241120_LAL_BOS", as_of)
        f2 = make_pipeline(db2).compute("20241120_LAL_BOS", as_of)
        assert f1 == f2
