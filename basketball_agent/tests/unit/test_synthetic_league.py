"""Unit tests for the synthetic league generator (honestly-labeled test data)."""
from __future__ import annotations

import pytest

from basketball_agent.agents.validation_agent import DataValidationAgent
from basketball_agent.data.collectors.synthetic import SyntheticLeagueConfig, generate_league
from basketball_agent.data.schemas.dataset import Dataset

pytestmark = pytest.mark.unit


def make_dataset(seed: int = 2025) -> Dataset:
    teams, players, games, team_stats, player_stats = generate_league(
        SyntheticLeagueConfig(n_teams=6, games_per_pair=2, seed=seed)
    )
    return Dataset(
        games=tuple(games),
        team_stats=tuple(team_stats),
        player_stats=tuple(player_stats),
        teams=tuple(teams),
        players=tuple(players),
    )


class TestSyntheticLeague:
    def test_generated_data_passes_validation_cleanly(self, fixed_now):
        dataset = make_dataset()
        report = DataValidationAgent(now=fixed_now).validate(dataset)
        assert not report.has_errors, report.to_text()
        assert not report.warnings, report.to_text()
        assert len(dataset.games) == 30  # 6 teams, 2 games per pair
        assert len(dataset.team_stats) == 60
        assert len(dataset.player_stats) == 300  # 5 players x 2 teams x 30 games

    def test_scores_in_plausible_range(self):
        dataset = make_dataset()
        for g in dataset.games:
            assert 60 <= g.home_score <= 170
            assert 60 <= g.away_score <= 170

    def test_deterministic_given_seed(self):
        d1 = make_dataset(seed=7)
        d2 = make_dataset(seed=7)
        assert d1.games == d2.games
        assert d1.team_stats == d2.team_stats

    def test_different_seeds_differ(self):
        assert make_dataset(seed=1).games != make_dataset(seed=2).games

    def test_player_lines_below_theoretical_max(self):
        dataset = make_dataset()
        for ps in dataset.player_stats:
            assert ps.points <= 3 * ps.fga + ps.fta + 2
            assert ps.minutes <= 48.0

    def test_points_identity_by_construction(self):
        dataset = make_dataset()
        for ts in dataset.team_stats:
            assert ts.points == 2 * (ts.fg - ts.fg3) + 3 * ts.fg3 + ts.ft
            assert ts.orb + ts.drb == ts.trb

    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            SyntheticLeagueConfig(n_teams=1)
