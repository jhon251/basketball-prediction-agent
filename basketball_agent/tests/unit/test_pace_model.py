"""Unit tests for Model D (expected pace)."""
from __future__ import annotations

import pytest

from basketball_agent.models.pace_model import PaceConfig, PaceModel

from .model_helpers import make_league, make_team

pytestmark = pytest.mark.unit


class TestPaceModel:
    def test_neutral_matchup_equals_league_pace(self):
        league = make_league()
        assert PaceModel().expected_pace(make_team(), make_team("O"), league) == pytest.approx(100.0)

    def test_faster_teams_raise_pace(self):
        league = make_league()
        pace = PaceModel().expected_pace(
            make_team(pace=110.0), make_team("O", pace=105.0), league, PaceConfig(shrinkage_k=0.0)
        )
        assert pace == pytest.approx(100.0 * 1.1 * 1.05)

    def test_shrinkage(self):
        league = make_league()
        team = make_team(pace=110.0, games=2)
        opp = make_team("O", pace=105.0, games=2)
        raw = PaceModel().expected_pace(team, opp, league, PaceConfig(shrinkage_k=0.0))
        shrunk = PaceModel().expected_pace(team, opp, league, PaceConfig(shrinkage_k=5.0))
        # (2*110+5*100)/7 = 102.857; (2*105+5*100)/7 = 101.429
        assert shrunk == pytest.approx(100.0 * (102.857142 / 100) * (101.428571 / 100), rel=1e-6)
        assert shrunk < raw

    def test_clamping(self):
        league = make_league()
        pace = PaceModel().expected_pace(
            make_team(pace=300.0), make_team("O", pace=300.0), league, PaceConfig(shrinkage_k=0.0)
        )
        assert pace == pytest.approx(140.0)

    def test_missing_league_baseline_raises(self):
        with pytest.raises(ValueError):
            PaceModel().expected_pace(make_team(), make_team("O"), make_league(pace=None))
