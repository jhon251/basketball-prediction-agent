"""Risk Agent (spec §20).

Identifies uncertainty sources and reports a confidence score plus risk
flags.  It NEVER modifies model or simulation results — it is a pure
function of the features / parameters / Monte Carlo output, with every rule
an explicit threshold in :class:`RiskConfig`.
"""
from __future__ import annotations

from dataclasses import dataclass

from basketball_agent.features.base import GameFeatures
from basketball_agent.models.game_params import GamePredictionParams
from basketball_agent.simulation.monte_carlo import MonteCarloResult

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"

# Recalibrated 2026-10-24 (Phase 12-F): per-league p90 of the simulated
# margin std, measured on 25 recent real regular-season games per league
# (frozen params, 400 sims, seed=7; NBA p50 16.0 / NBL 14.6 / EL 13.9).
# The previous global 15.0 threshold flagged EVERY NBA game (std range
# 15.2–16.8) — a constant false alarm carrying no information.
MARGIN_STD_P90_BY_LEAGUE: dict[str, float] = {
    "NBA": 16.8,
    "CBA": 15.9,
    "NBL": 15.4,
    "BSL": 14.8,
    "ACB": 14.7,
    "EUROLEAGUE": 14.4,
}


@dataclass(frozen=True)
class RiskConfig:
    close_margin_threshold: float = 3.0
    model_disagreement_threshold: float = 0.10
    # Fallback for leagues without a measured entry (all six core leagues
    # are measured above); p90 of the simulated margin std distribution.
    margin_std_threshold: float = 15.0
    # Optional per-league overrides; None -> MARGIN_STD_P90_BY_LEAGUE.
    margin_std_threshold_by_league: dict[str, float] | None = None
    availability_warning: float = 0.9
    availability_high: float = 0.75
    min_sample_games: int = 10
    high_3pa_share: float = 0.45
    penalty_high: float = 15.0
    penalty_medium: float = 8.0
    penalty_low: float = 4.0
    min_confidence: float = 10.0
    max_confidence: float = 100.0


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str  # HIGH / MEDIUM / LOW
    message: str


@dataclass(frozen=True)
class RiskAssessment:
    confidence_score: float  # 0-100
    risk_flags: tuple[RiskFlag, ...]


class RiskAgent:
    """Assesses prediction risk; does not touch the predicted probabilities."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config = config or RiskConfig()

    def assess(
        self,
        features: GameFeatures,
        params: GamePredictionParams,
        mc: MonteCarloResult,
    ) -> RiskAssessment:
        cfg = self._config
        flags: list[RiskFlag] = []

        # 1) close matchup
        if abs(params.expected_margin) < cfg.close_margin_threshold:
            flags.append(RiskFlag(
                "close_matchup", SEVERITY_MEDIUM,
                f"expected margin ({params.expected_margin:+.1f}) is within "
                f"±{cfg.close_margin_threshold:.0f} points",
            ))

        # 2) model disagreement (simulation vs Elo / strength model)
        sim_prob = mc.home_win_probability
        if features.elo_win_prob_home is not None:
            if abs(sim_prob - features.elo_win_prob_home) > cfg.model_disagreement_threshold:
                flags.append(RiskFlag(
                    "model_disagreement", SEVERITY_MEDIUM,
                    f"simulation ({sim_prob:.2f}) and Elo "
                    f"({features.elo_win_prob_home:.2f}) disagree by >"
                    f"{cfg.model_disagreement_threshold:.0%}",
                ))
        if abs(sim_prob - params.strength_win_prob_home) > cfg.model_disagreement_threshold:
            flags.append(RiskFlag(
                "model_disagreement", SEVERITY_MEDIUM,
                f"simulation ({sim_prob:.2f}) and strength model "
                f"({params.strength_win_prob_home:.2f}) disagree by >"
                f"{cfg.model_disagreement_threshold:.0%}",
            ))

        # 3) high outcome variance — per-league p90 threshold (see
        # MARGIN_STD_P90_BY_LEAGUE provenance above); unlisted leagues
        # fall back to the scalar default.
        table = (cfg.margin_std_threshold_by_league
                 if cfg.margin_std_threshold_by_league is not None
                 else MARGIN_STD_P90_BY_LEAGUE)
        league_name = getattr(features, "game_league", None)
        std_threshold = (table.get(str(league_name), cfg.margin_std_threshold)
                         if league_name else cfg.margin_std_threshold)
        if mc.margin.std > std_threshold:
            flags.append(RiskFlag(
                "high_variance", SEVERITY_MEDIUM,
                f"simulated margin std ({mc.margin.std:.1f}) exceeds "
                f"{std_threshold:.1f} (league {league_name or 'default'} p90)",
            ))

        # 4) injury / availability uncertainty
        for side, share in (("home", features.home_available_share),
                            ("away", features.away_available_share)):
            if share < cfg.availability_high:
                flags.append(RiskFlag(
                    "injury_uncertainty", SEVERITY_HIGH,
                    f"{side} available-minutes share is {share:.0%} (<"
                    f"{cfg.availability_high:.0%})",
                ))
            elif share < cfg.availability_warning:
                flags.append(RiskFlag(
                    "injury_uncertainty", SEVERITY_MEDIUM,
                    f"{side} available-minutes share is {share:.0%} (<"
                    f"{cfg.availability_warning:.0%})",
                ))

        # 5) data uncertainty (small samples)
        for side, team in (("home", features.home), ("away", features.away)):
            if team.games_played < cfg.min_sample_games:
                flags.append(RiskFlag(
                    "data_uncertainty", SEVERITY_MEDIUM,
                    f"{side} team has only {team.games_played} games of history "
                    f"(<{cfg.min_sample_games})",
                ))

        # 6) fatigue uncertainty
        if features.home_back_to_back or features.away_back_to_back:
            sides = []
            if features.home_back_to_back:
                sides.append("home")
            if features.away_back_to_back:
                sides.append("away")
            flags.append(RiskFlag(
                "fatigue_uncertainty", SEVERITY_LOW,
                f"back-to-back for {', '.join(sides)}",
            ))

        # 7) extreme shooting dependence
        for side, team in (("home", features.home), ("away", features.away)):
            if team.p3a_share is not None and team.p3a_share > cfg.high_3pa_share:
                flags.append(RiskFlag(
                    "extreme_shooting_dependence", SEVERITY_LOW,
                    f"{side} team takes {team.p3a_share:.0%} of shots from three "
                    f"(> {cfg.high_3pa_share:.0%})",
                ))

        penalties = {
            SEVERITY_HIGH: cfg.penalty_high,
            SEVERITY_MEDIUM: cfg.penalty_medium,
            SEVERITY_LOW: cfg.penalty_low,
        }
        score = cfg.max_confidence - sum(penalties[f.severity] for f in flags)
        score = max(cfg.min_confidence, min(cfg.max_confidence, score))
        return RiskAssessment(confidence_score=score, risk_flags=tuple(flags))
