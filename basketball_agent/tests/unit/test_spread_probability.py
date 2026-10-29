"""Phase 12-A: spread cover probability math (agents/spread_probability)."""
from __future__ import annotations

import math

import pytest

from basketball_agent.agents.spread_probability import (
    CoverProbabilities,
    SpreadLineError,
    cover_from_elo_normal,
    cover_from_samples,
    cover_from_normal,
    elo_expected_margin,
    fit_margin_sigma,
    validate_home_line,
)


class TestValidateHomeLine:
    def test_half_and_integer_lines_pass(self):
        assert validate_home_line(-5.5) == -5.5
        assert validate_home_line(0.0) == 0.0
        assert validate_home_line(12) == 12.0

    def test_quarter_line_fails_loud(self):
        with pytest.raises(SpreadLineError):
            validate_home_line(-5.7)

    def test_absurd_line_fails_loud(self):
        with pytest.raises(SpreadLineError):
            validate_home_line(-100.0)

    def test_nan_fails_loud(self):
        with pytest.raises(SpreadLineError):
            validate_home_line(float("nan"))


class TestCoverFromSamples:
    def test_integer_line_has_push_thirds(self):
        # margins 5,6,7 with home_line -6: 7 covers, 6 pushes, 5 loses
        cp = cover_from_samples([5, 6, 7], -6.0)
        assert cp.method == "simulation_samples"
        assert cp.home_cover == pytest.approx(1 / 3)
        assert cp.push == pytest.approx(1 / 3)
        assert cp.away_cover == pytest.approx(1 / 3)

    def test_half_line_has_no_push(self):
        cp = cover_from_samples([5, 6, 7], -5.5)
        assert cp.push == 0.0
        assert cp.home_cover == pytest.approx(2 / 3)
        assert cp.away_cover == pytest.approx(1 / 3)

    def test_favorite_home_line_positive(self):
        # home GETTING 3.5 (home_line +3.5): margin > -3.5 covers
        cp = cover_from_samples([-3, -4, 2], 3.5)
        assert cp.home_cover == pytest.approx(2 / 3)  # -3 and 2 cover
        assert cp.away_cover == pytest.approx(1 / 3)

    def test_empty_samples_fail(self):
        with pytest.raises(ValueError):
            cover_from_samples([], -5.5)


class TestCoverFromNormal:
    def test_symmetric_half_line(self):
        # mu=0, sigma=10, line -2.5 => P(m > 2.5) = 1 - Phi(0.25)
        cp = cover_from_normal(0.0, 10.0, -2.5)
        expected = 1.0 - 0.598706  # Phi(0.25)
        assert cp.home_cover == pytest.approx(expected, abs=1e-5)
        assert cp.push == 0.0
        assert cp.method == "elo_normal"

    def test_integer_line_push_mass(self):
        # mu=0 sigma=10 line -6: push = Phi(0.65)-Phi(0.55)
        cp = cover_from_normal(0.0, 10.0, -6.0)
        push = 0.742154 - 0.708840
        assert cp.push == pytest.approx(push, abs=1e-4)

    def test_bad_sigma_fails(self):
        with pytest.raises(ValueError):
            cover_from_normal(0.0, 0.0, -5.5)

    def test_sum_identity_stress(self):
        for line in (-12.0, -7.5, -1.0, 0.0, 3.5, 9.0):
            cp = cover_from_normal(4.3, 11.6, line)
            assert math.isclose(
                cp.home_cover + cp.push + cp.away_cover, 1.0, abs_tol=1e-9)


class TestEloNormal:
    def test_fifty_fifty_matches_symmetric_normal(self):
        cp = cover_from_elo_normal(0.5, 10.0, -2.5)
        ref = cover_from_normal(0.0, 10.0, -2.5)
        assert cp.home_cover == pytest.approx(ref.home_cover)

    def test_favorite_value(self):
        # p=0.7, sigma=11.6 -> mu ≈ 6.083; line -5.5 => P(m >= 6)
        mu = elo_expected_margin(0.7, 11.6)
        assert mu == pytest.approx(11.6 * 0.524401, abs=1e-3)
        cp = cover_from_elo_normal(0.7, 11.6, -5.5)
        # P(m > 5.5) = Phi((mu - 5.5)/sigma)
        from statistics import NormalDist
        expected = NormalDist().cdf((mu - 5.5) / 11.6)
        assert cp.home_cover == pytest.approx(expected, abs=1e-6)

    def test_degenerate_p_clamped_not_raised(self):
        # p=1 clamps to 1-1e-4 -> mu ≈ sigma * 3.719 ≈ 42.8 (NOT infinity)
        cp = cover_from_elo_normal(1.0, 11.5, -20.0)
        assert 0.0 <= cp.home_cover <= 1.0
        assert cp.home_cover > 0.95  # 1 - Phi((20.5-42.8)/11.5) ≈ 0.974

    def test_p_out_of_range_fails(self):
        with pytest.raises(ValueError):
            cover_from_elo_normal(1.2, 11.5, -5.5)


class TestFitMarginSigma:
    def test_short_history_returns_default(self):
        assert fit_margin_sigma([5, 6, 7]) == 11.5

    def test_long_history_fits(self):
        # 300 margins alternating +/-3 -> sample stdev ≈ 3.005
        margins = [3 if i % 2 == 0 else -3 for i in range(300)]
        assert fit_margin_sigma(margins) == pytest.approx(3.0, abs=1e-2)

    def test_degenerate_history_returns_default(self):
        margins = [5] * 300
        assert fit_margin_sigma(margins) == 11.5


class TestContract:
    def test_cover_probabilities_sum_check(self):
        with pytest.raises(ValueError):
            CoverProbabilities(
                home_cover=0.6, push=0.6, away_cover=0.6,
                home_line=-5.5, method="x")


class TestMarginRanges:
    def test_samples_partition_and_order(self):
        from basketball_agent.agents.spread_probability import (
            MARGIN_RANGE_BUCKETS, margin_ranges_from_samples,
        )
        rows = margin_ranges_from_samples(
            [-30, -25, -18, -12, -7, -3, 0, 2, 8, 13, 17, 24, 30])
        assert [r["code"] for r in rows] == [
            b[0] for b in MARGIN_RANGE_BUCKETS]
        # one sample per bucket -> every bucket exactly 1/13
        for r in rows:
            assert r["probability"] == pytest.approx(1 / 13)

    def test_sample_edge_values(self):
        # 20 belongs to 16-20; 21 to 21-25; 26 to 26+; 1 and 5 to 1-5
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_samples,
        )
        rows = margin_ranges_from_samples([20, 21, 26, 1, 5])
        by = {r["code"]: r["probability"] for r in rows}
        assert by["home_16_20"] == pytest.approx(0.2)
        assert by["home_21_25"] == pytest.approx(0.2)
        assert by["home_26_plus"] == pytest.approx(0.2)
        assert by["home_1_5"] == pytest.approx(0.4)

    def test_samples_sum_to_one_with_ties(self):
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_samples,
        )
        rows = margin_ranges_from_samples([0, 0, 3, -30, 11])
        assert sum(r["probability"] for r in rows) == pytest.approx(1.0)
        by = {r["code"]: r["probability"] for r in rows}
        assert by["tie"] == pytest.approx(0.4)

    def test_empty_fails(self):
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_samples,
        )
        with pytest.raises(ValueError):
            margin_ranges_from_samples([])

    def test_normal_symmetric_mean(self):
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_normal,
        )
        rows = margin_ranges_from_normal(0.0, 12.0)
        by = {r["code"]: r["probability"] for r in rows}
        assert by["home_1_5"] == pytest.approx(by["away_1_5"], abs=1e-9)
        assert by["home_26_plus"] == pytest.approx(by["away_26_plus"],
                                                   abs=1e-9)
        assert sum(r["probability"] for r in rows) == pytest.approx(
            1.0, abs=1e-9)

    def test_normal_favorite_skews_home(self):
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_normal,
        )
        rows = margin_ranges_from_normal(6.0, 12.0)
        by = {r["code"]: r["probability"] for r in rows}
        assert by["home_6_10"] > by["away_6_10"]
        assert by["home_26_plus"] > by["away_26_plus"]

    def test_normal_bad_sigma_fails(self):
        from basketball_agent.agents.spread_probability import (
            margin_ranges_from_normal,
        )
        with pytest.raises(ValueError):
            margin_ranges_from_normal(0.0, 0.0)


class TestTotals:
    """Phase 12-K: 大小球 over/under providers."""

    def test_validate_accepts_half_points(self):
        from basketball_agent.agents.spread_probability import (
            validate_total_line,
        )
        assert validate_total_line(190) == 190.0
        assert validate_total_line("190.5") == 190.5

    def test_validate_rejects_quarter_and_absurd(self):
        from basketball_agent.agents.spread_probability import (
            TotalLineError, validate_total_line,
        )
        with pytest.raises(TotalLineError):
            validate_total_line(190.3)
        with pytest.raises(TotalLineError):
            validate_total_line(79.5)
        with pytest.raises(TotalLineError):
            validate_total_line(400.5)

    def test_samples_over_under_push(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_samples,
        )
        r = totals_from_samples([189, 190, 191, 195, 185], 190.5)
        assert (r.over, r.push, r.under) == (0.4, 0.0, 0.6)
        r2 = totals_from_samples([190, 190, 191, 195], 190)
        assert (r2.over, r2.push, r2.under) == (0.5, 0.5, 0.0)
        for x in (r, r2):
            assert x.over + x.push + x.under == pytest.approx(1.0)

    def test_samples_empty_fails(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_samples,
        )
        with pytest.raises(ValueError):
            totals_from_samples([], 190.5)

    def test_normal_integer_line_has_push(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_normal,
        )
        r = totals_from_normal(191.0, 16.0, 190)
        assert r.push > 0.0
        assert r.over + r.push + r.under == pytest.approx(1.0)

    def test_normal_half_line_no_push(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_normal,
        )
        r = totals_from_normal(191.0, 16.0, 190.5)
        assert r.push == 0.0
        assert r.over + r.under == pytest.approx(1.0)

    def test_normal_line_at_mean_is_symmetric(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_normal,
        )
        # the continuity boundary sits on the half-integer, so symmetry
        # requires the MEAN on that boundary too (integer mean is not
        # symmetric around a half-point line)
        r = totals_from_normal(191.5, 16.0, 191.5)
        assert r.over == pytest.approx(r.under, abs=1e-9)

    def test_normal_bad_sigma_fails(self):
        from basketball_agent.agents.spread_probability import (
            totals_from_normal,
        )
        with pytest.raises(ValueError):
            totals_from_normal(191.0, 0.0, 190.5)
        with pytest.raises(ValueError):
            totals_from_normal(191.0, float("nan"), 190.5)

    def test_fit_total_sigma(self):
        from basketball_agent.agents.spread_probability import (
            fit_total_sigma,
        )
        assert fit_total_sigma([200.0] * 10) > 0  # fallback default
        assert fit_total_sigma([200.0] * 10) == fit_total_sigma([1.0])
        long_hist = [190.0 + (i % 7) for i in range(300)]
        assert fit_total_sigma(long_hist) == pytest.approx(
            fit_total_sigma(long_hist))  # deterministic
        import statistics as _st
        assert fit_total_sigma(long_hist) == pytest.approx(
            _st.stdev(long_hist))
