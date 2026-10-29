"""Phase 12-J: BasketNews report attach (players + availability) and the
DNP reconciliation audit."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from basketball_agent.collection.basketnews import (
    attach_report, latest_snapshot, save_snapshot,
)
from basketball_agent.data.collectors.basketnews_injuries import (
    load_snapshot_file, parse_report,
)
from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / (
    "basketnews_injuries_20261025.md")

TEAMS = (("IST", "Anadolu Efes Istanbul"),
         ("ZAL", "Zalgiris Kaunas"),
         ("BES", "Besiktas Istanbul"),
         ("ULK", "Fenerbahce Beko Istanbul"),
         ("RED", "Crvena Zvezda Meridianbet Belgrade"))


@pytest.fixture(autouse=True)
def _seed(db, tmp_path):
    from basketball_agent.data.schemas.game import Game
    from basketball_agent.data.schemas.team import Team

    repo = Repository(db)
    for code, name in TEAMS:
        repo.upsert_team(Team(team_id=f"EL-{code}",
                              league=League.EUROLEAGUE, name=name,
                              abbreviation=code))
    # Besiktas: round-5 game (Oct 25) and round-6 game (Oct 28);
    # Fenerbahce: round-7 game (Oct 30); both inside the 8-day window.
    for game_id, day, home, away in (
            ("E2026-0005", 25, "EL-BES", "EL-RED"),
            ("E2026-0006", 28, "EL-RED", "EL-BES"),
            ("E2026-0007", 30, "EL-ULK", "EL-IST")):
        repo.insert_game(Game(
            game_id=game_id, league=League.EUROLEAGUE, season="2026-27",
            game_date=datetime(2026, 10, day, 17, 0, tzinfo=UTC),
            home_team_id=home, away_team_id=away, home_score=None,
            away_score=None, status=GameStatus.SCHEDULED))
    # the season raw Header bundle supplies the round targeting map
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / "euroleague_E2026_raw.json").write_text(json.dumps({
        "dataset": "euroleague-live-v1", "season_code": "E2026",
        "headers": {"5": {"Round": "5"}, "6": {"Round": "6"},
                    "7": {"Round": "7"}, "20": {"Round": "5"}},
        "boxscores": {}, "empty_game_codes": []}), encoding="utf-8")
    return repo


def _report():
    return parse_report(load_snapshot_file(FIXTURE))


class TestAttachReport:
    def test_round_targeted_attach(self, db, tmp_path):
        delta = attach_report(db, _report(), now=NOW, window_days=8,
                              season_bundle_path=(
                                  tmp_path / "raw" /
                                  "euroleague_E2026_raw.json"))
        repo = Repository(db)
        # Allman OUT {5,6} -> both Besiktas games; Happ {5} -> round 5
        # only; Cordinier (open-ended OUT) rides IST's away game at ULK
        assert delta.availability_written == 6
        assert delta.players_registered == 6
        assert delta.skipped_available == 1   # Poirier Ready
        assert delta.skipped_round_mismatch == 2  # Happ@r6, Thompson@r7
        assert delta.records_without_game == 4  # ZAL rows + Thompson@r7
        rows = repo.list_availability()
        by_game: dict[str, int] = {}
        for a in rows:
            by_game[a.game_id] = by_game.get(a.game_id, 0) + 1
        assert by_game == {"E2026-0005": 2, "E2026-0006": 1,
                           "E2026-0007": 3}
        statuses = {(a.game_id, a.player_id): a.status for a in rows}
        allman = "ELP-kyle-allman"
        assert statuses["E2026-0005", allman] == "OUT"
        assert statuses["E2026-0006", allman] == "OUT"
        assert statuses["E2026-0007", "ELP-isaia-cordinier"] == "OUT"

    def test_open_ended_out_attaches_to_every_window_game(self, db,
                                                          tmp_path):
        delta = attach_report(db, _report(), now=NOW, window_days=8,
                              season_bundle_path=(
                                  tmp_path / "raw" /
                                  "euroleague_E2026_raw.json"))
        repo = Repository(db)
        avdalas = "ELP-neoklis-avdalas"  # "Out for season" = open-ended
        games = {a.game_id for a in repo.list_availability()
                 if a.player_id == avdalas}
        assert games == {"E2026-0007"}
        assert delta.availability_written == 6

    def test_rerun_is_idempotent(self, db, tmp_path):
        bundle = tmp_path / "raw" / "euroleague_E2026_raw.json"
        attach_report(db, _report(), now=NOW, window_days=8,
                      season_bundle_path=bundle)
        delta2 = attach_report(db, _report(), now=NOW, window_days=8,
                               season_bundle_path=bundle)
        assert delta2.availability_written == 0
        assert delta2.skipped_existing == 6
        assert delta2.players_registered == 0

    def test_missing_round_map_is_counted_not_guessed(self, db, tmp_path):
        # no season bundle -> targeted records cannot be placed; only
        # open-ended ones still attach
        delta = attach_report(db, _report(), now=NOW, window_days=8,
                              season_bundle_path=(
                                  tmp_path / "raw" / "missing.json"))
        assert delta.skipped_no_round_map == 5  # Allman x2, Happ x2, Thompson
        assert delta.availability_written == 3  # open-ended w/ games
        assert delta.ok

    def test_snapshot_archive_roundtrip(self, db, tmp_path):
        text = load_snapshot_file(FIXTURE)
        out = save_snapshot(text, out_dir=tmp_path / "snaps",
                            fetched_at=NOW, n_records=10)
        assert out.exists()
        assert (out.with_name(out.stem + ".manifest.json")).exists()
        assert latest_snapshot(out_dir=tmp_path / "snaps") == out


# ------------------------------------------------------------------ audit

def _box(*rows: tuple[str, str]) -> dict:
    return {"Stats": [{"Team": "BES",
                       "PlayersStats": [{"Player": name,
                                         "Minutes": minutes}
                                        for name, minutes in rows]}]}


class TestDnpAudit:
    @pytest.fixture
    def audited(self, db, tmp_path, _seed):
        from basketball_agent.data.schemas.game import Game, PlayerAvailability
        from basketball_agent.data.schemas.player import Player

        repo = Repository(db)
        # FINAL games (boxscore cached for one), a SCHEDULED one (pending)
        for game_id, day in (("E2026-0008", 23), ("E2026-0009", 23)):
            repo.insert_game(Game(
                game_id=game_id, league=League.EUROLEAGUE,
                season="2026-27",
                game_date=datetime(2026, 10, day, 17, 0, tzinfo=UTC),
                home_team_id="EL-BES", away_team_id="EL-RED",
                home_score=80, away_score=75, status=GameStatus.FINAL))
        cache = tmp_path / "boxscores"
        cache.mkdir()
        (cache / "E2026-0008.json").write_text(json.dumps(_box(
            ("DA SILVA, OSCAR", "0:00"),
            ("PLAYER, MARCUS", "21:33"))), encoding="utf-8")

        for pid, name in (("ELP-oscar-da-silva", "Oscar da Silva"),
                          ("ELP-marcus-player", "Marcus Player"),
                          ("ELP-pending-guy", "Pending Guy"),
                          ("ELP-missing-guy", "Missing Guy"),
                          ("ELP-fresh-guy", "Fresh Guy")):
            repo.upsert_player(Player(player_id=pid,
                                      league=League.EUROLEAGUE,
                                      name=name, current_team_id="EL-BES"))
        rows = [
            # confirmed: reported OUT, rostered, zero minutes
            ("ELP-oscar-da-silva", "E2026-0008", "OUT"),
            # false alarm: reported OUT, actually played 21 minutes
            ("ELP-marcus-player", "E2026-0008", "OUT"),
            # unresolved: reported OUT, name absent from the cached box
            ("ELP-missing-guy", "E2026-0008", "OUT"),
            # no cached boxscore for E2026-0009 (fetch-tested separately)
            ("ELP-fresh-guy", "E2026-0009", "OUT"),
            # pending: game not decided yet
            ("ELP-pending-guy", "E2026-0005", "OUT"),
        ]
        for pid, game_id, status in rows:
            repo.insert_availability(PlayerAvailability(
                player_id=pid, game_id=game_id, status=status,
                availability_probability=0.0 if status == "OUT" else 0.5,
                source_time=NOW, source="basketnews"))
        # non-OUT records carry no verdict (FINAL game only)
        repo.insert_availability(PlayerAvailability(
            player_id="ELP-pending-guy", game_id="E2026-0008",
            status="QUESTIONABLE", availability_probability=0.5,
            source_time=NOW, source="basketnews"))
        return repo, cache

    def test_precision_classification(self, db, tmp_path, audited):
        from basketball_agent.evaluation.dnp_audit import audit_basketnews_out

        _, cache = audited
        result = audit_basketnews_out(db, now=NOW, cache_dir=cache,
                                      fetch_missing=False)
        assert result.out_records_total == 5
        assert result.confirmed_out == 1        # rostered, 0 minutes
        assert result.confirmed_absent == 1     # absent from the 12-man
        assert len(result.false_alarms) == 1
        assert result.false_alarms[0]["minutes"] == 21
        assert len(result.pending) == 1
        assert result.no_boxscore == 1
        assert result.non_out_records == 1
        assert result.precision == pytest.approx(2 / 3)

    def test_boxscore_fetch_and_cache(self, db, tmp_path, audited):
        from basketball_agent.evaluation.dnp_audit import audit_basketnews_out

        _, cache = audited
        calls: list[str] = []

        def fetch(url: str) -> dict:
            calls.append(url)
            return _box(("GUY, FRESH", "0:00"))

        result = audit_basketnews_out(db, now=NOW, cache_dir=cache,
                                      fetch_missing=True, fetch_fn=fetch)
        # the missing boxscore was fetched exactly for its own game,
        # cached, and the absent player counts as confirmed
        assert [u for u in calls if "gameCode=" in u] == [
            "https://live.euroleague.net/api/Boxscore"
            "?gameCode=9&seasonCode=E2026"]
        assert (cache / "E2026-0009.json").exists()
        assert result.no_boxscore == 0
        assert result.confirmed_out == 2        # Oscar + Fresh (0 min)
        assert result.confirmed_absent == 1     # Missing Guy absent

    def test_no_records_no_crash(self, db, tmp_path):
        from basketball_agent.evaluation.dnp_audit import audit_basketnews_out

        result = audit_basketnews_out(db, now=NOW,
                                      cache_dir=tmp_path / "none",
                                      fetch_missing=False)
        assert result.precision is None
        assert result.summary()["decided"] == 0


class TestSnapshotAudit:
    """Historical mode: a dated snapshot reconciled against FINAL games
    of the record's targeted round (no availability rows involved)."""

    @pytest.fixture
    def round5_world(self, db, tmp_path, _seed):
        from basketball_agent.data.schemas.game import Game

        repo = Repository(db)
        repo.insert_game(Game(
            game_id="E2026-0020", league=League.EUROLEAGUE,
            season="2026-27",
            game_date=datetime(2026, 10, 13, 17, 0, tzinfo=UTC),
            home_team_id="EL-BES", away_team_id="EL-RED",
            home_score=71, away_score=69, status=GameStatus.FINAL))
        cache = tmp_path / "boxscores"
        cache.mkdir()
        (cache / "E2026-0020.json").write_text(json.dumps(_box(
            ("ALLMAN, KYLE", "0:00"),
            ("HAPP, ETHAN", "24:16"))), encoding="utf-8")
        snapshot = tmp_path / "snap.md"
        snapshot.write_text(
            "| Image 12: Besiktas Istanbul Besiktas Istanbul | | | | |\n"
            "| Pos | Player | Status | Round | Comments |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| PG | Kyle Allman | Out | Round 5 | — |\n"
            "| SF | Ethan Happ | Out | Round 5 | — |\n"
            "| C | Remote Guy | Out | Round 9 | — |\n"
            "| SF | Spare Player | Questionable | Round 5 | — |\n",
            encoding="utf-8")
        return cache, snapshot

    def test_round_targeted_reconciliation(self, db, tmp_path,
                                           round5_world):
        from datetime import datetime as dt

        from basketball_agent.evaluation.dnp_audit import audit_snapshot_out

        cache, snapshot = round5_world
        result = audit_snapshot_out(
            db, snapshot, as_of=dt(2026, 10, 15, tzinfo=UTC),
            lookback_days=7,
            season_bundle_path=tmp_path / "raw"
            / "euroleague_E2026_raw.json",
            cache_dir=cache, fetch_missing=False)
        assert result.out_records_total == 2  # Allman + Happ; round-9 skip
        assert result.confirmed_out == 1      # Allman: 0 minutes
        assert len(result.false_alarms) == 1  # Happ: played 24 minutes
        assert result.false_alarms[0]["minutes"] == 24
        assert result.non_out_records == 1    # Questionable: no verdict
        assert result.precision == pytest.approx(0.5)

    def test_as_of_window_excludes_other_rounds(self, db, tmp_path,
                                                round5_world):
        from datetime import datetime as dt

        from basketball_agent.evaluation.dnp_audit import audit_snapshot_out

        cache, snapshot = round5_world
        result = audit_snapshot_out(
            db, snapshot, as_of=dt(2026, 9, 30, tzinfo=UTC),
            lookback_days=7,
            season_bundle_path=tmp_path / "raw"
            / "euroleague_E2026_raw.json",
            cache_dir=cache, fetch_missing=False)
        assert result.out_records_total == 0  # round-5 game outside window
