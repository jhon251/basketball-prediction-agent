"""Agent Decision Contract (Phase 9-A).

The stable, traceable interface between the FROZEN Prediction Core
(``PredictionAgent`` and everything under ``models/`` / ``simulation/``,
frozen at Phase 8-L) and the Agent layer (risk, explanation, future
decision agents).

Boundary rules (docs/PHASE9_A_AGENT_CONTRACT.md):

* the Agent layer CONSUMES core outputs; it never recomputes
  probabilities, overrides the primary family, or selects models;
* every contract field is either a pass-through of a frozen-core value or
  a pure arithmetic fact derived from such values (e.g. ``|p_a - p_b|``) —
  nothing here is a new model, a calibrated uncertainty, or a quality
  score; fields the core does not provide are ``None`` and documented as
  not-provided (never fabricated);
* ``ValidationStatus`` preserves the Phase 8-L semantics exactly:
  ``fallback`` never becomes ``validated``, and no league is ``validated``
  today (the value is reserved and never auto-assigned);
* six-league isolation: one contract instance describes exactly one
  game in one league; all league-scoped state comes from the frozen
  per-league family configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from basketball_agent.agents.family_config import (
    FAMILY_CONFIG,
    FamilyEvidence,
    PredictionFamily,
    resolve_family,
)
from basketball_agent.agents.prediction_agent import GamePrediction
from basketball_agent.features.base import FeatureProvenance
from basketball_agent.simulation.monte_carlo import DistributionStats


class ValidationStatus(str, Enum):
    """Decision-layer status of one league's prediction setup.

    Mapping from the frozen family configuration (Phase 8-K/8-L):

    * ``CONFIGURED``   — the league has an explicit family entry with
      complete evidence provenance;
    * ``VALIDATED``    — RESERVED.  No league holds this today; it may only
      be assigned by an explicit future protocol, never automatically
      (``fallback`` must never be re-labelled ``validated``);
    * ``FALLBACK``     — the league is unknown/unconfigured and resolved to
      ``DEFAULT_FAMILY``; the family is the inherited production behavior,
      NOT a validated choice;
    * ``UNRESOLVED``   — the configuration machinery knows the league but
      no family decision exists;
    * ``INSUFFICIENT_EVIDENCE`` — an explicit decision that the available
      evidence does not support any family choice.
    """

    CONFIGURED = "configured"
    VALIDATED = "validated"
    FALLBACK = "fallback"
    UNRESOLVED = "unresolved"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def league_validation_status(league) -> ValidationStatus:
    """Map one league to its decision-layer validation status (static)."""
    if isinstance(league, str):
        key = _league_key_or_none(league)
    else:
        key = league
    entry = FAMILY_CONFIG.get(key) if key is not None else None
    if entry is None:
        return ValidationStatus.FALLBACK
    if entry.family is None:
        return ValidationStatus.INSUFFICIENT_EVIDENCE
    return ValidationStatus.CONFIGURED


def _league_key_or_none(league: str):
    from basketball_agent.data.schemas.base import League as _League

    try:
        return _League(league)
    except ValueError:
        return None


@dataclass(frozen=True)
class CalibrationProvenance:
    """Provenance of the calibration applied to a prediction, if any.

    ``None`` (absent) means the prediction used the frozen defaults —
    which is itself provenance and is recorded on the context as
    ``calibration=None`` with ``calibration_source="frozen_defaults"``.
    """

    params_file: str
    fitted_window: tuple[str, str]     # from FittedParams.training_window
    fitted_at: str
    validation_logloss: float
    boundary_hits: tuple[str, ...]

    @property
    def source_label(self) -> str:
        return f"calibrated:{self.params_file}"


@dataclass(frozen=True)
class PredictionContext:
    """Everything the decision layer needs to know about WHERE a
    prediction came from.  ``as_of`` is the strict information boundary:
    nothing in the contract may depend on data after it."""

    game_id: str
    league: str
    season: str | None
    home_team_id: str
    away_team_id: str
    as_of: str                                   # ISO-8601, timezone-aware
    primary_family: str
    primary_family_source: str                   # "configured" | "override" | "fallback"
    validation_status: ValidationStatus
    calibration: CalibrationProvenance | None    # None = frozen defaults
    calibration_source: str                      # "frozen_defaults" or source_label
    snapshot_id: str | None
    regulation_seconds: float
    feature_provenance: FeatureProvenance        # pass-through reference (frozen core)


@dataclass(frozen=True)
class SimulationDistribution:
    """The distribution-provider interface (Phase 9-A requirement 3).

    A pure pass-through of the frozen ``MonteCarloResult``.  Independent
    of the primary family: even when ``primary_family = elo`` this object
    is fully populated.

    ``ot_probability`` is ``None`` BY CONTRACT: the frozen core does not
    aggregate overtime frequency into ``MonteCarloResult``, and this layer
    must not invent a substitute.  (Recorded as an open extension point.)
    """

    n_simulations: int
    seed: int
    home_win_probability: float
    away_win_probability: float
    tie_probability: float
    home_score: DistributionStats
    away_score: DistributionStats
    margin: DistributionStats
    total: DistributionStats
    home_score_buckets: tuple
    away_score_buckets: tuple
    n_runs: int
    ot_probability: float | None = None          # not provided by frozen core

    @classmethod
    def from_monte_carlo(cls, mc) -> "SimulationDistribution":
        return cls(
            n_simulations=mc.n_simulations,
            seed=mc.seed,
            home_win_probability=mc.home_win_probability,
            away_win_probability=mc.away_win_probability,
            tie_probability=mc.tie_probability,
            home_score=mc.home_score,
            away_score=mc.away_score,
            margin=mc.margin,
            total=mc.total,
            home_score_buckets=mc.home_score_buckets,
            away_score_buckets=mc.away_score_buckets,
            n_runs=len(mc.runs),
        )


def _diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return abs(a - b)


@dataclass(frozen=True)
class DiagnosticEvidence:
    """Factual model-comparison evidence (Phase 9-A requirement 4).

    Every field is a raw fact (a probability, an absolute difference, a
    standard deviation).  This layer does NOT interpret: disagreement is
    not "model error", spread is not "risk", and nothing here is a quality
    score.  Consumers (RiskAgent views, explanation) draw their own
    explicitly documented conclusions.
    """

    p_elo: float | None
    p_strength: float
    p_sim: float
    diff_elo_strength: float | None
    diff_elo_sim: float | None
    diff_strength_sim: float
    primary_family: str
    diff_primary_vs_elo: float | None
    diff_primary_vs_strength: float
    diff_primary_vs_sim: float
    directional_disagreement: bool      # FACT: some pair of model probs sits on opposite sides of 0.5
    margin_std: float                   # simulation margin spread (fact, not "risk")
    total_std: float
    provenance: FeatureProvenance       # pass-through reference

    @classmethod
    def from_prediction(cls, p: GamePrediction) -> "DiagnosticEvidence":
        p_elo = p.features.elo_win_prob_home
        p_str = p.params.strength_win_prob_home
        p_sim = p.monte_carlo.home_win_probability
        primary = p.win_probability_home
        probs = [q for q in (p_elo, p_str, p_sim) if q is not None]
        directional = any(
            (a >= 0.5) != (b >= 0.5)
            for i, a in enumerate(probs) for b in probs[i + 1:]
        )
        return cls(
            p_elo=p_elo,
            p_strength=p_str,
            p_sim=p_sim,
            diff_elo_strength=_diff(p_elo, p_str),
            diff_elo_sim=_diff(p_elo, p_sim),
            diff_strength_sim=_diff(p_str, p_sim),
            primary_family=p.primary_family,
            diff_primary_vs_elo=_diff(primary, p_elo),
            diff_primary_vs_strength=_diff(primary, p_str),
            diff_primary_vs_sim=_diff(primary, p_sim),
            directional_disagreement=directional,
            margin_std=p.monte_carlo.margin.std,
            total_std=p.monte_carlo.total.std,
            provenance=p.features.provenance,
        )


@dataclass(frozen=True)
class PredictionResult:
    """The frozen core's primary output, unified for Agent consumption.

    Secondary/diagnostic information is preserved via ``diagnostics`` and
    ``simulation_distribution`` — the contract ADDS structure, it deletes
    nothing.
    """

    game_id: str
    primary_family: str
    primary_family_source: str
    primary_prob_home: float
    primary_prob_away: float
    predicted_winner: str                  # "home" | "away" | "tie" (factual threshold at 0.5)
    expected_home_score: float
    expected_away_score: float
    expected_margin: float
    expected_total: float
    # pass-through of the frozen RiskAgent's threshold-based confidence
    # score (0-100, explicit rule config).  It is NOT a calibrated
    # uncertainty and MUST NOT be re-labelled as one.
    risk_confidence_score: float
    diagnostics: DiagnosticEvidence
    simulation_distribution: SimulationDistribution

    @classmethod
    def from_prediction(cls, p: GamePrediction) -> "PredictionResult":
        prob = p.win_probability_home
        winner = "home" if prob > 0.5 else ("away" if prob < 0.5 else "tie")
        return cls(
            game_id=p.game_id,
            primary_family=p.primary_family,
            primary_family_source=p.primary_family_source,
            primary_prob_home=prob,
            primary_prob_away=1.0 - prob,
            predicted_winner=winner,
            expected_home_score=p.expected_home_score,
            expected_away_score=p.expected_away_score,
            expected_margin=p.expected_margin,
            expected_total=p.expected_total,
            risk_confidence_score=p.risk.confidence_score,
            diagnostics=DiagnosticEvidence.from_prediction(p),
            simulation_distribution=SimulationDistribution.from_monte_carlo(p.monte_carlo),
        )


@dataclass(frozen=True)
class DecisionRisk:
    """Factual decision-layer risk view (Phase 9-A requirement: minimal).

    All fields are facts with explicit definitions; none is a win-rate,
    profit estimate, or model-quality score:

    * ``max_pairwise_disagreement`` — largest |Δp| between any two model
      probabilities (None when fewer than two models produced a value);
    * ``margin_std`` / ``total_std`` — simulation spread;
    * ``tossup_indicator`` — DEFINITION: primary probability inside the
      fixed band [0.45, 0.55];
    * ``core_risk_flags`` — pass-through of the frozen RiskAgent flags;
    * ``validation_warning`` — set iff the league is not CONFIGURED.
    """

    max_pairwise_disagreement: float | None
    margin_std: float
    total_std: float
    tossup_indicator: bool
    core_risk_flags: tuple
    validation_warning: str | None


def assess_decision(result: PredictionResult,
                    status: ValidationStatus,
                    core_risk) -> DecisionRisk:
    diag = result.diagnostics
    candidates = [d for d in (diag.diff_elo_strength, diag.diff_elo_sim,
                              diag.diff_strength_sim) if d is not None]
    warning = None
    if status is ValidationStatus.FALLBACK:
        warning = ("league unconfigured: primary family is the inherited "
                   "default (fallback), not a validated choice")
    elif status is ValidationStatus.UNRESOLVED:
        warning = "league family unresolved"
    elif status is ValidationStatus.INSUFFICIENT_EVIDENCE:
        warning = "league family marked insufficient_evidence"
    return DecisionRisk(
        max_pairwise_disagreement=max(candidates) if candidates else None,
        margin_std=diag.margin_std,
        total_std=diag.total_std,
        tossup_indicator=0.45 <= result.primary_prob_home <= 0.55,
        core_risk_flags=tuple(core_risk.risk_flags),
        validation_warning=warning,
    )
