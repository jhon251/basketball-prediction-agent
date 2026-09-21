"""B4 league-audit tests: hermetic fixtures + failure paths (no real data needed)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from basketball_agent.data.db.connection import Database
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import Competition, GameStatus, League
from basketball_agent.data.schemas.game import Game, TeamGameStats
from basketball_agent.data.schemas.team import Team
from basketball_agent.evaluation.league_audit import FAIL, PASS, WARN, audit_league, format_audit

pytestmark = pytest.mark.unit

UTC = timezone.utc
HOME_TEAM, AWAY_TEAM = "EL-AAA", "EL-BBB"


def _stats(game_id: str, team_id: str, is_home: bool, minutes: int = 200,
           points: int = 112, **over) -> TeamGameStats:
    base = dict(
        game_id=game_id, team_id=team_id, is_home=is_home, minutes=minutes, points=points,
        possessions=72.0, fg=40, fga=85, fg3=12, fg3a=30, ft=20, fta=24,
        orb=9, drb=33, trb=42, ast=25, tov=12, stl=8, blk=5, pf=19,
    )
    base.update(over)
    return TeamGameStats(**base)


def _game(season_code: str, code: int, date: datetime, *, competition=Competition.REGULAR_SEASON,
          home=HOME_TEAM, away=AWAY_TEAM, status=GameStatus.FINAL) -> Game:
    return Game(
        game_id=f"{season_code}-{code:04d}", league=League.EUROLEAGUE, season=(
            "2023-24" if season_code == "E2023" else "2024-25"),
        game_date=date, home_team_id=home, away_team_id=away,
        home_score=112 if status is GameStatus.FINAL else None,
        away_score=105 if status is GameStatus.FINAL else None,
        status=status, competition=competition,
    )


@pytest.fixture
def el_db(tmp_path) -> Database:
    """Two EuroLeague seasons, EL- namespace, 200-min regulation, one OT game."""
    db = Database(tmp_path / "el.db")
    db.initialize()
    repo = Repository(db)
    for team_id, name in ((HOME_TEAM, "Synthetic EL AAA"), (AWAY_TEAM, "Synthetic EL BBB")):
        repo.upsert_team(Team(team_id=team_id, league=League.EUROLEAGUE, name=name,
                              abbreviation=team_id.removeprefix("EL-")))
    start = datetime(2023, 10, 5, 18, 0, tzinfo=UTC)
    games = []
    for season_code, base in (("E2023", start), ("E2024", start + timedelta(days=380))):
        for i in range(4):
            games.append(_game(season_code, i + 1, base + timedelta(days=7 * i)))
    # one overtime game in E2024 (200 + 25)
    games.append(_game("E2024", 5, start + timedelta(days=410), home=AWAY_TEAM, away=HOME_TEAM))
    for game in games:
        repo.insert_game(game)
        repo.insert_team_stats(_stats(game.game_id, game.home_team_id, True,
                                      minutes=225 if game.game_id == "E2024-0005" else 200))
        repo.insert_team_stats(_stats(game.game_id, game.away_team_id, False,
                                      points=105, fg=38, fga=88, fg3=13, fg3a=36,
                                      ft=16, fta=20, orb=10, drb=30, trb=40,
                                      minutes=225 if game.game_id == "E2024-0005" else 200))
    return db


class TestLeagueAuditPass:
    def test_clean_euroleague_fixture_passes(self, el_db):
        audit = audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=1)
        assert audit.status == PASS, [c for c in audit.checks if c.status == FAIL]
        assert audit.n_games == 9
        assert len(audit.seasons) == 2
        assert {s.season for s in audit.seasons} == {"2023-24", "2024-25"}
        names = {c.name for c in audit.checks}
        assert {"game_id.format", "game_id.unique", "team.namespace", "team.registered",
                "season.boundaries", "competition.tags", "regulation.minutes",
                "timezone.aware", "missing.games", "final.consistency",
                "provenance.leakage"} <= names

    def test_audit_never_mixes_leagues(self, el_db):
        # an NBA league audit on an EL-only DB finds zero games and passes vacuously
        audit = audit_league(Repository(el_db), League.NBA, leakage_sample=0)
        assert audit.n_games == 0
        assert audit.status == PASS

    def test_missing_games_warn_without_reference(self, el_db):
        audit = audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=0)
        check = next(c for c in audit.checks if c.name == "missing.games")
        assert check.status == WARN

    def test_format_audit_renders(self, el_db):
        text = format_audit(audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=0))
        assert "LEAGUE AUDIT — EUROLEAGUE" in text
        assert "season coverage:" in text
        assert "[PASS]" in text


class TestLeagueAuditFailures:
    def test_nba_style_id_inside_euroleague_fails(self, el_db):
        repo = Repository(el_db)
        repo.insert_game(Game(
            game_id="0022400001", league=League.EUROLEAGUE, season="2024-25",
            game_date=datetime(2024, 11, 1, 18, 0, tzinfo=UTC),
            home_team_id=HOME_TEAM, away_team_id=AWAY_TEAM,
            home_score=90, away_score=85, status=GameStatus.FINAL,
            competition=Competition.REGULAR_SEASON,
        ))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        assert audit.status == FAIL
        assert any(c.name == "game_id.format" and c.status == FAIL for c in audit.checks)

    def test_missing_el_prefix_fails(self, el_db):
        repo = Repository(el_db)
        repo.upsert_team(Team(team_id="RMA", league=League.EUROLEAGUE, name="Real Madrid",
                              abbreviation="RMA"))
        repo.insert_game(_game("E2024", 90, datetime(2024, 12, 1, 20, 0, tzinfo=UTC),
                               home="RMA", away=AWAY_TEAM))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        assert any(c.name == "team.namespace" and c.status == FAIL for c in audit.checks)

    def test_wrong_regulation_minutes_warns_within_bounds(self, el_db):
        """240 for a 40-minute league is a source-data error but physically
        possible, so it is retained verbatim and reported as a quirk."""
        repo = Repository(el_db)
        repo.insert_game(_game("E2024", 91, datetime(2024, 12, 3, 20, 0, tzinfo=UTC)))
        repo.insert_team_stats(_stats("E2024-0091", HOME_TEAM, True, minutes=240))
        repo.insert_team_stats(_stats("E2024-0091", AWAY_TEAM, False, minutes=240, points=105))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        check = next(c for c in audit.checks if c.name == "regulation.minutes")
        assert check.status == WARN and "240" in check.detail

    def test_regulation_quirk_within_bounds_warns_not_fails(self, el_db):
        """195/240-style source aggregation quirks are retained and reported,
        never silently accepted and never treated as impossible values."""
        repo = Repository(el_db)
        repo.insert_game(_game("E2024", 92, datetime(2024, 12, 5, 20, 0, tzinfo=UTC)))
        repo.insert_team_stats(_stats("E2024-0092", HOME_TEAM, True, minutes=195))
        repo.insert_team_stats(_stats("E2024-0092", AWAY_TEAM, False, minutes=195, points=105))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        check = next(c for c in audit.checks if c.name == "regulation.minutes")
        assert check.status == WARN
        assert "195" in check.detail
        assert audit.status == PASS  # a documented quirk must not fail the audit

    def test_impossible_minutes_fail(self, el_db):
        """179 (below any legal period sum) is impossible -> hard FAIL."""
        repo = Repository(el_db)
        repo.insert_game(_game("E2024", 93, datetime(2024, 12, 6, 20, 0, tzinfo=UTC)))
        repo.insert_team_stats(_stats("E2024-0093", HOME_TEAM, True, minutes=179))
        repo.insert_team_stats(_stats("E2024-0093", AWAY_TEAM, False, minutes=200, points=105))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        check = next(c for c in audit.checks if c.name == "regulation.minutes")
        assert check.status == FAIL
        assert "179" in check.detail

    def test_incomplete_season_fails_with_reference(self, el_db):
        audit = audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=0,
                             expected_regular_season_games=100)
        check = next(c for c in audit.checks if c.name == "missing.games")
        assert check.status == FAIL

    def test_live_game_with_scores_fails_final_consistency(self, el_db):
        repo = Repository(el_db)
        repo.insert_game(Game(
            game_id="E2024-0093", league=League.EUROLEAGUE, season="2024-25",
            game_date=datetime(2024, 12, 7, 20, 0, tzinfo=UTC),
            home_team_id=HOME_TEAM, away_team_id=AWAY_TEAM,
            home_score=50, away_score=48, status=GameStatus.LIVE,
            competition=Competition.REGULAR_SEASON,
        ))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        assert any(c.name == "final.consistency" and c.status == FAIL for c in audit.checks)

    def test_untagged_competition_warns_but_does_not_fail(self, el_db):
        repo = Repository(el_db)
        repo.insert_game(_game("E2024", 94, datetime(2024, 12, 9, 20, 0, tzinfo=UTC),
                               competition=None))
        repo.insert_team_stats(_stats("E2024-0094", HOME_TEAM, True))
        repo.insert_team_stats(_stats("E2024-0094", AWAY_TEAM, False, points=105))
        audit = audit_league(repo, League.EUROLEAGUE, leakage_sample=0)
        check = next(c for c in audit.checks if c.name == "competition.tags")
        assert check.status == WARN
        assert audit.status == PASS


class TestLeakageSampling:
    def test_sample_runs_and_passes(self, el_db):
        audit = audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=2)
        assert audit.leakage_sampled, "expected an as-of feature computation"
        assert audit.leakage_failures == ()

    def test_sample_disabled(self, el_db):
        audit = audit_league(Repository(el_db), League.EUROLEAGUE, leakage_sample=0)
        assert audit.leakage_sampled == ()
        assert audit.leakage_failures == ()
