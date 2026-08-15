"""Leakage detection + reality validation (spec §2.2, §22).

Any future information in a feature computation is a FAIL, never a warning.
Both checks return a :class:`LeakageReport` whose ``status`` is ``PASS`` or
``FAIL``; the prediction flow must refuse to emit a prediction when either
report FAILs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from basketball_agent.data.db.repository import Repository
from basketball_agent.features.base import GameFeatures


@dataclass(frozen=True)
class LeakageReport:
    violations: tuple[str, ...]
    checks_performed: tuple[str, ...]

    @property
    def status(self) -> str:
        return "FAIL" if self.violations else "PASS"


def check_temporal_integrity(features: GameFeatures, repo: Repository) -> LeakageReport:
    """Verify every feature input was public at ``features.as_of``.

    * every game in the provenance must exist and be dated strictly before
      ``as_of``;
    * every availability record must have ``source_time <= as_of``;
    * the game being predicted must not be among its own inputs.
    """
    violations: list[str] = []
    checks: list[str] = []
    as_of = features.as_of

    checks.append("leakage.games_before_as_of")
    for gid in features.provenance.games_used:
        game = repo.get_game(gid)
        if game is None:
            violations.append(f"provenance references unknown game {gid}")
        elif game.game_date >= as_of:
            violations.append(
                f"feature game {gid} dated {game.game_date.isoformat()} is not before as_of "
                f"{as_of.isoformat()}"
            )

    checks.append("leakage.target_excluded")
    if features.game_id in features.provenance.games_used:
        violations.append(
            f"target game {features.game_id} appears in its own feature provenance"
        )

    checks.append("leakage.availability_source_time")
    for player_id, source_time_iso in features.provenance.availability_used:
        try:
            source_time = datetime.fromisoformat(source_time_iso)
        except ValueError:
            violations.append(f"unparseable availability source_time {source_time_iso!r}")
            continue
        if source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=timezone.utc)
        if source_time > as_of:
            violations.append(
                f"availability for {player_id} published at {source_time.isoformat()} "
                f"is after as_of {as_of.isoformat()}"
            )

    return LeakageReport(violations=tuple(violations), checks_performed=tuple(checks))


def reality_check(repo: Repository, game_id: str, as_of: datetime) -> LeakageReport:
    """Verify the prediction target is real and the timing is sane (§22)."""
    violations: list[str] = []
    checks = [
        "reality.game_exists",
        "reality.teams_registered",
        "reality.game_date_sane",
        "reality.prediction_before_game",
    ]
    game = repo.get_game(game_id)
    if game is None:
        violations.append(f"game {game_id} does not exist in the data snapshot")
        return LeakageReport(violations=tuple(violations), checks_performed=tuple(checks))
    for team_id in (game.home_team_id, game.away_team_id):
        if repo.get_team(team_id) is None:
            violations.append(f"team {team_id} is not registered in the data snapshot")
    if game.game_date.year < 1900:
        violations.append(f"implausible game date {game.game_date.isoformat()}")
    if as_of > game.game_date:
        violations.append(
            f"prediction timestamp {as_of.isoformat()} is after game start "
            f"{game.game_date.isoformat()}"
        )
    return LeakageReport(violations=tuple(violations), checks_performed=tuple(checks))
