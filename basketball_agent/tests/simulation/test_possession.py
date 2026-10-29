"""Unit tests for possession-level event resolution."""
from __future__ import annotations

import random

import pytest

from basketball_agent.models.score_model import PossessionParams, rescale_to_ppp
from basketball_agent.simulation.possession import PossessionEvent, resolve_opportunity

pytestmark = pytest.mark.simulation


def make_params(**overrides) -> PossessionParams:
    base = dict(
        p_tov=0.12, p_fta=0.10, p_fga=0.78, p3a_share=0.40,
        p2_pct=0.50, p3_pct=0.35, ft_pct=0.75,
        fta_per_event=2.0, orb_pct=0.25, expected_ppp=1.05,
    )
    base.update(overrides)
    return PossessionParams(**base)


class TestResolveOpportunity:
    def test_event_frequencies_match_params(self):
        params = make_params()
        rng = random.Random(42)
        n = 50000
        results = [resolve_opportunity(rng, params) for _ in range(n)]
        tov = sum(1 for r in results if r.event is PossessionEvent.TURNOVER)
        fta = sum(1 for r in results if r.event is PossessionEvent.FREE_THROW)
        assert tov / n == pytest.approx(0.12, abs=0.01)
        assert fta / n == pytest.approx(0.10, abs=0.01)

    def test_three_point_share(self):
        params = make_params()
        rng = random.Random(7)
        n = 50000
        fga3 = fga2 = 0
        for _ in range(n):
            r = resolve_opportunity(rng, params)
            if r.event is PossessionEvent.THREE_PT_MADE or r.event is PossessionEvent.THREE_PT_MISS:
                fga3 += 1
            elif r.event is PossessionEvent.TWO_PT_MADE or r.event is PossessionEvent.TWO_PT_MISS:
                fga2 += 1
        assert fga3 / (fga3 + fga2) == pytest.approx(0.40, abs=0.02)

    def test_made_percentages(self):
        # orb_pct=0 so every missed attempt surfaces as a MISS event
        params = make_params(orb_pct=0.0)
        rng = random.Random(11)
        n = 50000
        made2 = att2 = made3 = att3 = 0
        for _ in range(n):
            r = resolve_opportunity(rng, params)
            if r.event in (PossessionEvent.THREE_PT_MADE, PossessionEvent.THREE_PT_MISS):
                att3 += 1
                made3 += r.points > 0
            elif r.event in (PossessionEvent.TWO_PT_MADE, PossessionEvent.TWO_PT_MISS):
                att2 += 1
                made2 += r.points > 0
        assert made2 / att2 == pytest.approx(0.50, abs=0.02)
        assert made3 / att3 == pytest.approx(0.35, abs=0.02)

    def test_offensive_rebound_rate(self):
        params = make_params()
        rng = random.Random(3)
        n = 50000
        misses = orbs = 0
        for _ in range(n):
            r = resolve_opportunity(rng, params)
            if r.event in (PossessionEvent.TWO_PT_MISS, PossessionEvent.THREE_PT_MISS):
                misses += 1
            if r.event is PossessionEvent.OFFENSIVE_REBOUND:
                orbs += 1
        # P(ORB | missed attempt) = orbs / (orbs + plain misses)
        assert orbs / (orbs + misses) == pytest.approx(0.25, abs=0.02)

    def test_turnover_never_scores_and_ends_possession(self):
        params = make_params(p_tov=1.0, p_fta=0.0, p_fga=0.0)
        rng = random.Random(0)
        for _ in range(100):
            r = resolve_opportunity(rng, params)
            assert r.event is PossessionEvent.TURNOVER
            assert r.points == 0
            assert r.possession_over

    def test_orb_keeps_possession(self):
        params = make_params(p_tov=0.0, p_fta=0.0, p_fga=1.0,
                             p2_pct=0.0, p3_pct=0.0, orb_pct=1.0)
        rng = random.Random(0)
        r = resolve_opportunity(rng, params)
        assert r.event is PossessionEvent.OFFENSIVE_REBOUND
        assert not r.possession_over

    def test_free_throw_points_bounded(self):
        params = make_params(p_tov=0.0, p_fga=0.0, p_fta=1.0, ft_pct=1.0)
        rng = random.Random(0)
        for _ in range(100):
            r = resolve_opportunity(rng, params)
            assert r.event is PossessionEvent.FREE_THROW
            assert 0 <= r.ftm <= r.fta
            assert r.points == r.ftm


class TestRescaleToPpp:
    def test_implied_ppp_matches_target(self):
        params = make_params()
        rescaled = rescale_to_ppp(params, 1.2)
        assert rescaled.expected_ppp == pytest.approx(1.2, abs=1e-9)

    def test_event_probabilities_unchanged(self):
        params = make_params()
        rescaled = rescale_to_ppp(params, 1.2)
        assert rescaled.p_tov == params.p_tov
        assert rescaled.p_fta == params.p_fta
        assert rescaled.p_fga == params.p_fga
        assert rescaled.p3a_share == params.p3a_share
        assert rescaled.orb_pct == params.orb_pct
        assert rescaled.ft_pct == params.ft_pct

    def test_higher_target_raises_percentages(self):
        params = make_params()
        low = rescale_to_ppp(params, 0.9)
        high = rescale_to_ppp(params, 1.3)
        assert high.p2_pct > low.p2_pct
        assert high.p3_pct > low.p3_pct

    def test_hand_computed_scale(self):
        # made = 0.44, miss = 0.56; continuation carries p_fga (12-L):
        # opps = 1/(1 - 0.78*0.56*0.25) = 1/0.8908   (pre-12-L: 1/0.86)
        # per_opp = 0.78*1.02 + 0.15 = 0.9456 -> implied = 1.0615177...
        break_even = 0.9456 / (1 - 0.78 * 0.56 * 0.25)
        params = make_params()
        assert rescale_to_ppp(params, break_even).expected_ppp == pytest.approx(
            1.0615177368657387, abs=1e-9
        )
        # scale for the break-even target must be 1.0 -> pcts unchanged
        rescaled = rescale_to_ppp(params, break_even)
        assert rescaled.p2_pct == pytest.approx(0.50, abs=1e-9)


class TestRescaleSamplerContract:
    """Phase 12-L: the rescale's closed form must equal the SAMPLER's
    true expectation.  The pre-12-L formula omitted ``p_fga`` from the
    continuation probability and realized ~3.8% fewer points per
    possession than it recorded (the totals under-lean bug)."""

    def test_exact_recursion_matches_recorded_implied(self):
        # E = per_opp / (1 - p_fga * orb * (1 - made)) — the exact
        # expectation of the resolve_opportunity continuation process
        params = rescale_to_ppp(make_params(), 1.20)
        made = (params.p3a_share * params.p3_pct
                + (1 - params.p3a_share) * params.p2_pct)
        per_opp = (
            params.p_fga * (params.p3a_share * params.p3_pct * 3.0
                            + (1 - params.p3a_share) * params.p2_pct * 2.0)
            + params.p_fta * params.fta_per_event * params.ft_pct)
        expected = per_opp / (1 - params.p_fga * params.orb_pct * (1 - made))
        assert params.expected_ppp == pytest.approx(expected, abs=1e-12)

    def test_empirical_sampler_matches_recorded_implied(self):
        params = rescale_to_ppp(make_params(), 1.20)
        rng = random.Random(7)
        n = 100_000
        points = 0
        for _ in range(n):
            while True:
                r = resolve_opportunity(rng, params)
                points += r.points
                if r.possession_over:
                    break
        assert points / n == pytest.approx(params.expected_ppp, abs=0.015)
