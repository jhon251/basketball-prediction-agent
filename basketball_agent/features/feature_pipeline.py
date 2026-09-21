"""Feature pipeline: assemble every feature for one upcoming game, as-of.

Leakage discipline (spec §2.2) is structural here:

* history = FINAL games with ``game_date < as_of``, target game excluded;
* team/player stats are pulled with the repository's ``as_of`` filter AND
  re-filtered against that same history;
* availability is pulled with ``known_at=as_of``;
* ``GameFeatures.provenance`` records exactly which games/records were used
  so the leakage-detection system can audit every feature computation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus
from basketball_agent.data.schemas.game import TeamGameStats
from basketball_agent.features.base import (
    FeatureComputationError,
    FeatureProvenance,
    GameFeatures,
    LeagueFeatureSet,
    TeamFeatureSet,
)
from basketball_agent.features.player.availability import available_minutes_share
from basketball_agent.features.schedule.rest import is_back_to_back, rest_days
from basketball_agent.features.team.aggregates import aggregate_team
from basketball_agent.features.team.boxscore import TeamGameDerived, derive
from basketball_agent.features.team.form import last_n_stats
from basketball_agent.models.elo import EloConfig, compute_ratings, expected_score

# Module-level memo for derive(): it is a pure function of the immutable,
# validated stat rows (frozen pydantic models, hashable by value), so the
# cache key is the row pair itself — safe across databases and pipelines and
# bit-identical to recomputing (Phase 8-B2 performance cache).
_DERIVED_PAIR_CACHE: dict[tuple, tuple] = {}


def _derive_pair(home, away, game_date):
    """Both sides of one game through derive(), memoized by its inputs."""
    key = (home, away, game_date)
    cached = _DERIVED_PAIR_CACHE.get(key)
    if cached is None:
        cached = (derive(home.game_id, game_date, home, away),
                  derive(home.game_id, game_date, away, home))
        _DERIVED_PAIR_CACHE[key] = cached
    return cached


class FeaturePipeline:
    """Computes :class:`GameFeatures` for one game at one timestamp.

    Parameters are fixed per pipeline instance so a run is reproducible and
    audit-able (same snapshot + same params => identical features).
    """

    def __init__(
        self,
        repo: Repository,
        *,
        decay_lambda: float = 0.0,
        elo_config: EloConfig | None = None,
        window_days: float | None = None,
        include_non_regular_history: bool = False,
    ) -> None:
        if decay_lambda < 0:
            raise ValueError("decay_lambda must be >= 0")
        if window_days is not None and window_days <= 0:
            raise ValueError("window_days must be positive")
        self._repo = repo
        self._decay_lambda = decay_lambda
        self._elo_config = elo_config or EloConfig()
        self._window_days = window_days
        # Phase 8-B2: default history excludes non-regular competition segments
        # (play-in / playoff / cup).  Untagged legacy rows stay eligible so
        # existing datasets keep working; other segments are an explicit opt-in.
        self._include_non_regular_history = include_non_regular_history

    # ------------------------------------------------------------------ public

    def compute(self, game_id: str, as_of: datetime) -> GameFeatures:
        """Compute features for ``game_id`` using only info available at ``as_of``."""
        game = self._repo.get_game(game_id)
        if game is None:
            raise FeatureComputationError(f"game not found: {game_id}")
        if as_of > game.game_date:
            raise FeatureComputationError(
                f"as_of ({as_of.isoformat()}) must not be after game start "
                f"({game.game_date.isoformat()}): a prediction happens before the game"
            )

        history = self._eligible_history(game_id, as_of, league=game.league)
        games_by_id = {g.game_id: g for g in history}
        stats_by_game = self._team_stats_by_game(games_by_id, as_of)
        derived = self._derive_per_team(stats_by_game, games_by_id)
        ratings, _ = compute_ratings(history, self._elo_config)
        league = self._build_league_features(derived, as_of)

        home_features = self._build_team_features(game.home_team_id, derived, ratings, as_of)
        away_features = self._build_team_features(game.away_team_id, derived, ratings, as_of)

        home_rest = rest_days(game.home_team_id, game.game_date, history)
        away_rest = rest_days(game.away_team_id, game.game_date, history)

        league_home_win_pct = self._league_home_win_pct(history)

        availability = self._repo.list_availability(game_id=game_id, known_at=as_of)
        minutes_by_player = self._player_minutes(as_of, games_by_id)
        home_share = available_minutes_share(availability, {
            pid: w for pid, (tid, w) in minutes_by_player.items() if tid == game.home_team_id
        })
        away_share = available_minutes_share(availability, {
            pid: w for pid, (tid, w) in minutes_by_player.items() if tid == game.away_team_id
        })

        expected_pace = None
        if home_features.pace is not None and away_features.pace is not None:
            expected_pace = (home_features.pace + away_features.pace) / 2.0

        elo_win_prob_home = None
        if home_features.elo is not None and away_features.elo is not None:
            elo_win_prob_home = expected_score(
                home_features.elo, away_features.elo, self._elo_config
            )

        nrtg_diff = None
        if home_features.nrtg is not None and away_features.nrtg is not None:
            nrtg_diff = home_features.nrtg - away_features.nrtg

        provenance = FeatureProvenance(
            games_used=tuple(g.game_id for g in history),
            team_stats_used=sum(len(v) for v in stats_by_game.values()),
            availability_used=tuple((r.player_id, r.source_time.isoformat()) for r in availability),
            decay_lambda=self._decay_lambda,
            elo_k=self._elo_config.k,
            elo_home_advantage=self._elo_config.home_advantage,
            elo_initial=self._elo_config.initial_rating,
        )

        return GameFeatures(
            game_id=game_id,
            as_of=as_of,
            home_team_id=game.home_team_id,
            away_team_id=game.away_team_id,
            home=home_features,
            away=away_features,
            league=league,
            game_league=game.league.value,
            home_pace=home_features.pace,
            away_pace=away_features.pace,
            expected_pace=expected_pace,
            elo_win_prob_home=elo_win_prob_home,
            nrtg_diff=nrtg_diff,
            home_rest_days=home_rest,
            away_rest_days=away_rest,
            home_back_to_back=is_back_to_back(home_rest),
            away_back_to_back=is_back_to_back(away_rest),
            league_home_win_pct=league_home_win_pct,
            home_available_share=home_share,
            away_available_share=away_share,
            provenance=provenance,
        )

    # ---------------------------------------------------------------- helpers

    def _eligible_history(self, game_id: str, as_of: datetime, league=None):
        """FINAL games strictly before as_of, target game excluded.

        History is scoped to the TARGET GAME'S OWN LEAGUE (Phase 8-B3): NBA
        and EuroLeague histories never mix, so no Elo/ratings/baselines can
        be shared across leagues even in one database.

        With ``window_days`` set, only games within the rolling window are
        kept.  Defense in depth on top of the repository's ``as_of`` filter:
        any violation here raises AssertionError, i.e. FAIL, per spec §2.2.
        """
        history = [
            g for g in self._repo.list_games(as_of=as_of, status=GameStatus.FINAL, league=league)
            if g.game_date < as_of and g.game_id != game_id
        ]
        if not self._include_non_regular_history:
            from basketball_agent.data.schemas.base import NON_REGULAR_COMPETITIONS

            history = [
                g for g in history
                if g.competition is None or g.competition not in NON_REGULAR_COMPETITIONS
            ]
        if self._window_days is not None:
            window_start = as_of - timedelta(days=self._window_days)
            history = [g for g in history if g.game_date >= window_start]
        for g in history:
            assert g.game_date < as_of, "leakage: history game at/after as_of"
            assert g.game_id != game_id, "leakage: target game included in its own features"
        return history

    def _team_stats_by_game(self, games_by_id: dict, as_of: datetime) -> dict[str, list[TeamGameStats]]:
        stats_by_game: dict[str, list[TeamGameStats]] = defaultdict(list)
        for ts in self._repo.list_team_stats(as_of=as_of):
            if ts.game_id in games_by_id:
                stats_by_game[ts.game_id].append(ts)
        return dict(stats_by_game)

    def _derive_per_team(self, stats_by_game, games_by_id) -> dict[str, list[TeamGameDerived]]:
        derived: dict[str, list[TeamGameDerived]] = defaultdict(list)
        for gid, rows in stats_by_game.items():
            if len(rows) != 2:
                continue  # validation normally prevents this; skip defensively
            game = games_by_id[gid]
            try:
                home = next(r for r in rows if r.team_id == game.home_team_id)
                away = next(r for r in rows if r.team_id == game.away_team_id)
            except StopIteration:
                continue
            d_home, d_away = _derive_pair(home, away, game.game_date)
            derived[home.team_id].append(d_home)
            derived[away.team_id].append(d_away)
        return dict(derived)

    def _build_league_features(
        self, derived: dict[str, list[TeamGameDerived]], as_of: datetime
    ) -> LeagueFeatureSet:
        """League averages over every derived team-game row in the history."""
        all_rows = [row for rows in derived.values() for row in rows]
        agg = aggregate_team(all_rows, as_of, self._decay_lambda)
        return LeagueFeatureSet(
            ortg=agg["ortg"],
            drtg=agg["drtg"],
            nrtg=agg["nrtg"],
            pace=agg["pace"],
            efg_pct=agg["efg_pct"],
            ts_pct=agg["ts_pct"],
            fg3_pct=agg["fg3_pct"],
            ft_pct=agg["ft_pct"],
            orb_pct=agg["orb_pct"],
            drb_pct=agg["drb_pct"],
            trb_pct=agg["trb_pct"],
            tov_pct=agg["tov_pct"],
            opp_tov_pct=agg["opp_tov_pct"],
            fga_rate=agg["fga_rate"],
            fta_rate=agg["fta_rate"],
            p3a_share=agg["p3a_share"],
            p2_pct=agg["p2_pct"],
            n_team_games=agg["games_played"],
        )

    def _build_team_features(
        self,
        team_id: str,
        derived: dict[str, list[TeamGameDerived]],
        ratings: dict[str, float],
        as_of: datetime,
    ) -> TeamFeatureSet:
        rows = derived.get(team_id, [])
        if not rows:
            raise FeatureComputationError(
                f"no historical games for team {team_id} as of {as_of.isoformat()}"
            )
        ordered = sorted(rows, key=lambda r: r.game_date)
        agg = aggregate_team(ordered, as_of, self._decay_lambda)
        form5 = last_n_stats(ordered, 5)
        form10 = last_n_stats(ordered, 10)
        season_wins = sum(1 for r in ordered if r.margin > 0)
        return TeamFeatureSet(
            team_id=team_id,
            games_played=agg["games_played"],
            last_game_date=agg["last_game_date"],
            elo=ratings.get(team_id),
            ortg=agg["ortg"],
            drtg=agg["drtg"],
            nrtg=agg["nrtg"],
            pace=agg["pace"],
            efg_pct=agg["efg_pct"],
            ts_pct=agg["ts_pct"],
            fg3_pct=agg["fg3_pct"],
            ft_pct=agg["ft_pct"],
            orb_pct=agg["orb_pct"],
            drb_pct=agg["drb_pct"],
            trb_pct=agg["trb_pct"],
            tov_pct=agg["tov_pct"],
            opp_tov_pct=agg["opp_tov_pct"],
            fga_rate=agg["fga_rate"],
            fta_rate=agg["fta_rate"],
            p3a_share=agg["p3a_share"],
            p2_pct=agg["p2_pct"],
            form_last5_win_pct=form5[0],
            form_last5_avg_margin=form5[1],
            form_last10_win_pct=form10[0],
            form_last10_avg_margin=form10[1],
            season_win_pct=season_wins / len(ordered),
            season_avg_margin=sum(r.margin for r in ordered) / len(ordered),
        )

    @staticmethod
    def _league_home_win_pct(history) -> float | None:
        if not history:
            return None
        home_wins = sum(1 for g in history if g.home_score > g.away_score)
        return home_wins / len(history)

    def _player_minutes(self, as_of: datetime, games_by_id: dict) -> dict[str, tuple[str, float]]:
        """player_id -> (team_id, avg minutes over last 5 known games).

        Only games inside the eligible history are considered, so target-game
        performances can never leak into availability weights.
        """
        by_player: dict[str, list[tuple[datetime, float, str]]] = defaultdict(list)
        for ps in self._repo.list_player_stats(as_of=as_of):
            game = games_by_id.get(ps.game_id)
            if game is None:
                continue
            by_player[ps.player_id].append((game.game_date, ps.minutes, ps.team_id))
        out: dict[str, tuple[str, float]] = {}
        for player_id, entries in by_player.items():
            entries.sort(key=lambda e: e[0])
            window = entries[-5:]
            avg_minutes = sum(e[1] for e in window) / len(window)
            out[player_id] = (entries[-1][2], avg_minutes)
        return out
