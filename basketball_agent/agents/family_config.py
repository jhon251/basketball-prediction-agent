"""Per-league prediction-family configuration (Phase 8-K).

The production primary probability must come from an EXPLICIT, traceable
family choice — never from an implicit default disguised as a blend.

Semantics (Phase 8-K):

* ``family`` selects which single model provides the PRIMARY win
  probability: ``elo`` / ``strength`` / ``simulation``;
* ``ensemble`` exists only as an explicit opt-in (programmatic
  ``EnsembleWeights``); it is an experimental/diagnostic combination layer
  and is never a configured default family;
* the pre-8-K production behavior — ``EnsembleAgent`` default weights
  ``(0, 0, 1)`` — is exactly the SIMULATION family.  All six ingested
  leagues are therefore configured to ``simulation`` so that nothing
  changes numerically; the configuration exists to make that fact explicit
  and to give future family decisions a governed place to live;
* per Phase 8-K the existing per-league evidence (Phase 8-G/H/I/J) is
  RECORDED, not acted on: no family was switched in this phase, and no
  family may ever be selected from future test-window results.  The
  evidence structure deliberately carries only train/validation windows
  and references to archived experiments/parameter files.

Leagues without a configured entry (or with ``family=None``) resolve to
``DEFAULT_FAMILY`` with ``source="fallback"`` — the documented safe
fallback is the historical production behavior (simulation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from basketball_agent.data.schemas.base import League


class PredictionFamily(str, Enum):
    ELO = "elo"
    STRENGTH = "strength"
    SIMULATION = "simulation"
    ENSEMBLE = "ensemble"  # explicit opt-in only; never a configured default


#: Historical production behavior: the EnsembleAgent's default weights
#: (0, 0, 1) make the primary probability exactly the simulation win rate.
#: Keeping this as the fallback preserves bit-identical behavior for every
#: league that has no explicit family decision yet.
DEFAULT_FAMILY = PredictionFamily.SIMULATION


@dataclass(frozen=True)
class FamilyEvidence:
    """Provenance of one family choice (Phase 8-K requirement 7).

    Only train/validation windows may appear here — a ``test_window`` field
    is deliberately absent so a future-test selection has nowhere to be
    recorded.  References point at archived docs/parameter files.
    """

    rationale: str
    train_window: str                      # e.g. "2009-10 .. 2024-25 (league seasons)"
    validation_window: str                 # e.g. "C2 dual-window protocol: 2024-25 & 2025-26 regular seasons"
    references: tuple[str, ...]            # archived docs / param files
    decided_in: str                        # phase that produced the evidence (NOT the switch)


@dataclass(frozen=True)
class LeagueFamilyConfig:
    """One league's explicit family configuration.

    ``family=None`` means "no evidence-based decision yet" — the league
    resolves to ``DEFAULT_FAMILY`` with ``status="unresolved"``.
    """

    league: League
    family: PredictionFamily | None
    evidence: FamilyEvidence | None = None
    status: str = "configured"             # "configured" | "unresolved"


def _simulation_inherited(league: League, extra: str) -> LeagueFamilyConfig:
    return LeagueFamilyConfig(
        league=league,
        family=PredictionFamily.SIMULATION,
        evidence=FamilyEvidence(
            rationale=(
                "Production continuity (Phase 8-K rule: convert evidence to "
                "explicit config, do not re-decide): the pre-8-K production "
                "primary probability was the simulation win rate via "
                "EnsembleAgent default weights (0,0,1).  Per-league evidence "
                "is recorded below; switching families is a future explicit "
                "decision.  " + extra
            ),
            train_window="varies by league (see references)",
            validation_window="see references (each phase doc states its windows)",
            references=(
                "docs/PHASE8_J_ARCHITECTURE_AUDIT.md",
                "docs/PHASE8_I_STABILITY_INDEPENDENCE.md",
                "docs/PHASE8_G_SIX_LEAGUE_SUMMARY.md",
                extra,
            ),
            decided_in="Phase 8-K (semantics; family choice inherited)",
        ),
    )


#: The six ingested leagues.  Every entry keeps the inherited SIMULATION
#: family; the ``extra`` reference records that league's candidate evidence
#: so a future governed decision has its starting point in one place.
FAMILY_CONFIG: dict[League, LeagueFamilyConfig] = {
    League.NBA: _simulation_inherited(
        League.NBA,
        "candidates: fitted strength+simulation dual-window (C2, "
        "storage/params/c2_nba_2025-26_1s.json); frozen Elo best in all "
        "17 seasons (8-I)"),
    League.EUROLEAGUE: _simulation_inherited(
        League.EUROLEAGUE,
        "candidates: fitted strength dual-window (C2, "
        "storage/params/c2_euroleague_2023-24_1s.json); frozen Elo best in "
        "4/6 seasons (8-I)"),
    League.ACB: _simulation_inherited(
        League.ACB,
        "candidates: fitted strength dual-window (C2, "
        "storage/params/c2_acb_2024-25_1s.json); frozen Elo best in 4/6 "
        "seasons (8-I)"),
    League.NBL: _simulation_inherited(
        League.NBL,
        "evidence: simulation best frozen in both seasons; fitted strength "
        "dual-window candidate (8-H, storage/params/phase8h_nbl_2025-26_A.json)"),
    League.CBA: _simulation_inherited(
        League.CBA,
        "evidence: frozen Elo best in both seasons (8-H/8-I); fitted "
        "simulation dual-window candidate (8-H); 8-E lambda=0.02 candidate "
        "not reproduced on two seasons (recorded)"),
    League.BSL: _simulation_inherited(
        League.BSL,
        "evidence: frozen per-season order flipped (sim 2024-25 / elo "
        "2025-26, 8-I); fitted strength+simulation dual-window candidates "
        "(8-H); 8-F negative result retained in archive"),
}


@dataclass(frozen=True)
class FamilyResolution:
    """Result of resolving one league's family."""

    family: PredictionFamily
    source: str                 # "configured" | "fallback"
    status: str                 # "configured" | "unresolved"
    evidence: FamilyEvidence | None


def resolve_family(league: League | str | None) -> FamilyResolution:
    """Resolve the prediction family for one league (static, data-free).

    No runtime data is consulted (no games, no dates, no results) — the
    resolution is a pure lookup so a family can never silently depend on
    test outcomes.  Unknown/unconfigured leagues fall back to
    ``DEFAULT_FAMILY`` (the historical simulation behavior) and are marked
    ``unresolved`` so callers can log the fallback.
    """
    if isinstance(league, League):
        key: League | None = league
    elif isinstance(league, str):
        try:
            key = League(league)
        except ValueError:
            key = None
    else:
        key = None
    entry = FAMILY_CONFIG.get(key) if key is not None else None
    if entry is None or entry.family is None:
        return FamilyResolution(
            family=DEFAULT_FAMILY, source="fallback", status="unresolved",
            evidence=None,
        )
    return FamilyResolution(
        family=entry.family, source="configured", status=entry.status,
        evidence=entry.evidence,
    )
