"""Model E: possession-outcome distribution (per-team parameters).

Every probability here is estimated from historical data (the decay-weighted
team/league features) and adjusted for the matchup with explicit
multiplicative ratios against league averages — never guessed by an LLM.

Convention (feeds the Phase 4 simulation):

* a *possession* is a sequence of *opportunities*; after a missed field goal
  the offense keeps the possession with probability ``orb_pct`` and gets a
  new opportunity;
* each opportunity ends in exactly one of: turnover (``p_tov``), free-throw
  trip (``p_fta``, ``fta_per_event`` attempts each) or field-goal attempt
  (``p_fga`` = 1 - p_tov - p_fta, split into 2PT/3PT by ``p3a_share``);
* per-opportunity probabilities are converted from per-possession aggregate
  rates via the ORB continuation recursion
  ``E[opportunities] = 1 / (1 - miss_rate * orb_pct)``;
* the 2PT/3PT percentages are scaled by one exact efficiency factor so the
  implied points per possession equals the target PPP from Model C.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.features.base import LeagueFeatureSet, TeamFeatureSet
from basketball_agent.models._stats import clamp, shrink


@dataclass(frozen=True)
class PossessionParams:
    """Per-team possession-outcome probabilities for the simulator."""

    p_tov: float          # P(turnover) per opportunity
    p_fta: float          # P(free-throw trip) per opportunity
    p_fga: float          # P(field-goal attempt) per opportunity
    p3a_share: float      # share of FGA that are 3PT attempts
    p2_pct: float         # 2PT field-goal percentage
    p3_pct: float         # 3PT field-goal percentage
    ft_pct: float         # free-throw percentage
    fta_per_event: float  # expected FTA per free-throw trip
    orb_pct: float        # offensive rebound chance after a missed FGA
    expected_ppp: float   # implied points per possession (≈ Model C target)

    @property
    def probabilities_sum_to_one(self) -> bool:
        return abs(self.p_tov + self.p_fta + self.p_fga - 1.0) < 1e-9


@dataclass(frozen=True)
class ScoreModelConfig:
    shrinkage_k: float = 5.0
    fta_per_event: float = 2.0
    min_pct: float = 0.05
    max_pct: float = 0.85
    min_tov_p: float = 0.0
    max_tov_p: float = 0.6
    min_fta_p: float = 0.0
    max_fta_p: float = 0.6
    min_fga_p: float = 0.05
    max_orb_pct: float = 0.6


class ScoreModel:
    def possession_params(
        self,
        team: TeamFeatureSet,
        opp: TeamFeatureSet,
        league: LeagueFeatureSet,
        expected_ppp: float,
        config: ScoreModelConfig | None = None,
    ) -> PossessionParams:
        cfg = config or ScoreModelConfig()
        if league.tov_pct is None or league.opp_tov_pct is None or league.drtg is None:
            raise ValueError("league baseline is missing")
        n, m = team.games_played, opp.games_played
        k = cfg.shrinkage_k

        # --- matchup-adjusted per-possession rates -------------------------
        tov_rate = (
            shrink(team.tov_pct, league.tov_pct, n, k)
            * shrink(opp.opp_tov_pct, league.opp_tov_pct, m, k)
            / league.opp_tov_pct
        )
        def_ratio = shrink(opp.drtg, league.drtg, m, k) / league.drtg
        p2 = clamp(shrink(team.p2_pct, league.p2_pct, n, k) * def_ratio, cfg.min_pct, cfg.max_pct)
        p3 = clamp(shrink(team.fg3_pct, league.fg3_pct, n, k) * def_ratio, cfg.min_pct, cfg.max_pct)
        ft = clamp(shrink(team.ft_pct, league.ft_pct, n, k), cfg.min_pct, cfg.max_pct)
        p3a_share = clamp(shrink(team.p3a_share, league.p3a_share, n, k), 0.0, 1.0)
        orb = clamp(
            shrink(team.orb_pct, league.orb_pct, n, k)
            * (1.0 - shrink(opp.drb_pct, league.drb_pct, m, k))
            / (1.0 - league.drb_pct),
            0.0,
            cfg.max_orb_pct,
        )
        fga_rate = shrink(team.fga_rate, league.fga_rate, n, k)
        fta_rate = shrink(team.fta_rate, league.fta_rate, n, k)

        # --- per-opportunity conversion (ORB continuation recursion) -------
        made_rate = p3a_share * p3 + (1.0 - p3a_share) * p2
        miss_rate = 1.0 - made_rate
        continuation = 1.0 - miss_rate * orb
        p_tov = clamp(tov_rate * continuation, cfg.min_tov_p, cfg.max_tov_p)
        p_fta = clamp(
            (fta_rate / cfg.fta_per_event) * continuation, cfg.min_fta_p, cfg.max_fta_p
        )
        p_fga = clamp(1.0 - p_tov - p_fta, cfg.min_fga_p, 1.0)

        # --- exact efficiency scaling to hit the Model C target PPP --------
        # implied(s) = (A*s + B) / (1 - orb*(1 - C*s))  -> solve for s
        mix_points = p3a_share * p3 * 3.0 + (1.0 - p3a_share) * p2 * 2.0
        a = p_fga * mix_points
        b = p_fta * cfg.fta_per_event * ft
        c = p3a_share * p3 + (1.0 - p3a_share) * p2  # made rate per unit s
        numerator = expected_ppp * (1.0 - orb) - b
        denominator = a - expected_ppp * orb * c
        scale = (numerator / denominator) if abs(denominator) > 1e-12 else 1.0
        p2 = clamp(p2 * scale, cfg.min_pct, cfg.max_pct)
        p3 = clamp(p3 * scale, cfg.min_pct, cfg.max_pct)

        # --- implied PPP with the final parameters -------------------------
        made = p3a_share * p3 + (1.0 - p3a_share) * p2
        miss = 1.0 - made
        per_opp = p_fga * (p3a_share * p3 * 3.0 + (1.0 - p3a_share) * p2 * 2.0) \
            + p_fta * cfg.fta_per_event * ft
        denom = 1.0 - miss * orb
        opportunities = 1.0 / denom if abs(denom) > 1e-12 else 1.0
        implied = per_opp * opportunities

        return PossessionParams(
            p_tov=p_tov,
            p_fta=p_fta,
            p_fga=p_fga,
            p3a_share=p3a_share,
            p2_pct=p2,
            p3_pct=p3,
            ft_pct=ft,
            fta_per_event=cfg.fta_per_event,
            orb_pct=orb,
            expected_ppp=implied,
        )
