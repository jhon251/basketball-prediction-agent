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

        # --- efficiency scaling to hit the Model C target PPP --------
        raw = PossessionParams(
            p_tov=p_tov,
            p_fta=p_fta,
            p_fga=p_fga,
            p3a_share=p3a_share,
            p2_pct=p2,
            p3_pct=p3,
            ft_pct=ft,
            fta_per_event=cfg.fta_per_event,
            orb_pct=orb,
            expected_ppp=expected_ppp,
        )
        return rescale_to_ppp(raw, expected_ppp, cfg)


def rescale_to_ppp(
    params: PossessionParams,
    target: float,
    config: ScoreModelConfig | None = None,
) -> PossessionParams:
    """Rescale 2PT/3PT percentages so the implied PPP equals ``target``.

    The implied PPP is ``(a*s + b) / (1 - p_fga*orb*(1 - c*s))`` in the
    scale ``s``, which is linear — so ``s`` has an exact closed-form
    solution.  Used both by the score model itself and by the simulation
    layer's player adjustments (core-player efficiency, injuries).

    The continuation probability carries the ``p_fga`` factor: an
    offensive rebound can only follow a FIELD-GOAL miss, and a field
    goal goes up only with probability ``p_fga`` per opportunity
    (turnovers and free-throw trips end the possession).  Phase 12-L
    fixed both terms — the previous formula omitted ``p_fga``,
    overstated continuation, and made the sampler realize ~3.8% fewer
    points per possession than the recorded target (measured 2026-10-25
    on a live NBA game: sampler 1.0813 vs recorded 1.1240; corrected
    closed form predicts 1.0841 = empirical within noise).
    """
    cfg = config or ScoreModelConfig()
    p3a = params.p3a_share
    mix_points = p3a * params.p3_pct * 3.0 + (1.0 - p3a) * params.p2_pct * 2.0
    a = params.p_fga * mix_points
    b = params.p_fta * params.fta_per_event * params.ft_pct
    c = p3a * params.p3_pct + (1.0 - p3a) * params.p2_pct
    numerator = target * (1.0 - params.p_fga * params.orb_pct) - b
    denominator = a - target * params.p_fga * params.orb_pct * c
    scale = (numerator / denominator) if abs(denominator) > 1e-12 else 1.0
    p2 = clamp(params.p2_pct * scale, cfg.min_pct, cfg.max_pct)
    p3 = clamp(params.p3_pct * scale, cfg.min_pct, cfg.max_pct)

    made = p3a * p3 + (1.0 - p3a) * p2
    miss = 1.0 - made
    per_opp = params.p_fga * (p3a * p3 * 3.0 + (1.0 - p3a) * p2 * 2.0) \
        + params.p_fta * params.fta_per_event * params.ft_pct
    denom = 1.0 - params.p_fga * miss * params.orb_pct
    opportunities = 1.0 / denom if abs(denom) > 1e-12 else 1.0
    implied = per_opp * opportunities
    return PossessionParams(
        p_tov=params.p_tov,
        p_fta=params.p_fta,
        p_fga=params.p_fga,
        p3a_share=p3a,
        p2_pct=p2,
        p3_pct=p3,
        ft_pct=params.ft_pct,
        fta_per_event=params.fta_per_event,
        orb_pct=params.orb_pct,
        expected_ppp=implied,
    )
