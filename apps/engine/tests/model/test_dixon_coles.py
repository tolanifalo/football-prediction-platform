"""The Dixon-Coles correction (MODEL-EXPERIMENTS.md §3).

The load-bearing test is `test_the_correction_preserves_total_probability`:
the standard tau is constructed so the four adjustments cancel exactly, and if
that ever stops holding, renormalisation would quietly absorb the error and
every corrected probability would be wrong by an amount nobody could see.
"""

from __future__ import annotations

import math

import pytest

from engine.model.dixon_coles import (
    LOW_SCORES,
    InvalidRho,
    apply_correction,
    clamp_rho,
    estimate_rho,
    feasible_rho,
    feasible_rho_over,
    tau,
    validate_rho,
)
from engine.model.poisson import derive_markets, scoreline_matrix

LH, LA = 1.55, 1.15
RHO = -0.08
TOL = 1e-12


class TestTau:
    def test_the_four_cells_match_the_published_formulation(self) -> None:
        assert tau(0, 0, LH, LA, RHO) == pytest.approx(1.0 - LH * LA * RHO)
        assert tau(0, 1, LH, LA, RHO) == pytest.approx(1.0 + LH * RHO)
        assert tau(1, 0, LH, LA, RHO) == pytest.approx(1.0 + LA * RHO)
        assert tau(1, 1, LH, LA, RHO) == pytest.approx(1.0 - RHO)

    def test_every_other_cell_is_exactly_one(self) -> None:
        for home, away in ((0, 2), (2, 0), (1, 2), (2, 1), (2, 2), (5, 3)):
            assert tau(home, away, LH, LA, RHO) == 1.0

    def test_rho_zero_makes_every_tau_one(self) -> None:
        for home, away in LOW_SCORES:
            assert tau(home, away, LH, LA, 0.0) == 1.0

    def test_a_negative_rho_lifts_the_draws_and_lowers_the_one_nils(self) -> None:
        """The direction Dixon and Coles reported on 1990s English data."""
        assert tau(0, 0, LH, LA, -0.1) > 1.0
        assert tau(1, 1, LH, LA, -0.1) > 1.0
        assert tau(0, 1, LH, LA, -0.1) < 1.0
        assert tau(1, 0, LH, LA, -0.1) < 1.0

    def test_a_positive_rho_reverses_that(self) -> None:
        assert tau(0, 0, LH, LA, 0.1) < 1.0
        assert tau(1, 1, LH, LA, 0.1) < 1.0
        assert tau(0, 1, LH, LA, 0.1) > 1.0


class TestConstraints:
    def test_the_feasible_interval_is_where_every_tau_is_positive(self) -> None:
        lower, upper = feasible_rho(LH, LA)
        assert lower == pytest.approx(max(-1 / LH, -1 / LA))
        assert upper == pytest.approx(min(1 / (LH * LA), 1.0))
        for rho in (lower + 1e-9, (lower + upper) / 2, upper - 1e-9):
            for home, away in LOW_SCORES:
                assert tau(home, away, LH, LA, rho) > 0.0

    def test_just_outside_the_interval_a_tau_turns_negative(self) -> None:
        lower, upper = feasible_rho(LH, LA)
        assert min(tau(h, a, LH, LA, lower - 1e-6) for h, a in LOW_SCORES) < 0.0
        assert min(tau(h, a, LH, LA, upper + 1e-6) for h, a in LOW_SCORES) < 0.0

    def test_an_out_of_range_rho_is_rejected(self) -> None:
        lower, upper = feasible_rho(LH, LA)
        with pytest.raises(InvalidRho):
            validate_rho(lower - 0.01, LH, LA)
        with pytest.raises(InvalidRho):
            validate_rho(upper + 0.01, LH, LA)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_rho_is_rejected(self, bad: float) -> None:
        with pytest.raises(InvalidRho):
            validate_rho(bad, LH, LA)

    def test_a_valid_rho_is_returned_unchanged(self) -> None:
        assert validate_rho(RHO, LH, LA) == RHO

    def test_the_interval_over_many_fixtures_is_the_intersection(self) -> None:
        rates = [(1.5, 1.2), (0.4, 0.3), (3.0, 2.5)]
        lower, upper = feasible_rho_over(rates)
        for lam_h, lam_a in rates:
            fixture_lower, fixture_upper = feasible_rho(lam_h, lam_a)
            assert lower >= fixture_lower and upper <= fixture_upper

    def test_clamping_pulls_an_infeasible_rho_just_inside(self) -> None:
        lower, upper = feasible_rho(0.3, 0.2)
        clamped = clamp_rho(-5.0, 0.3, 0.2)
        assert lower < clamped < upper
        for home, away in LOW_SCORES:
            assert tau(home, away, 0.3, 0.2, clamped) > 0.0

    def test_no_rates_cannot_constrain_rho(self) -> None:
        with pytest.raises(InvalidRho):
            feasible_rho_over([])


class TestCorrection:
    def test_rho_zero_returns_the_independent_matrix_unchanged(self) -> None:
        matrix = scoreline_matrix(LH, LA)
        assert apply_correction(matrix, 0.0) is matrix

    def test_the_correction_preserves_total_probability(self) -> None:
        """The identity that makes this the standard formulation.

        Applying tau to the four cells changes the grid total only by what the
        truncation at MAX_GOALS leaves out - every rho term cancels
        algebraically. Renormalisation is therefore a guard, not a fix.
        """
        matrix = scoreline_matrix(LH, LA)
        for rho in (-0.15, -0.05, 0.05, 0.15):
            adjusted = [list(row) for row in matrix.grid]
            for home, away in LOW_SCORES:
                adjusted[home][away] *= tau(home, away, LH, LA, rho)
            assert sum(sum(r) for r in adjusted) == pytest.approx(
                matrix.total(), abs=1e-12
            ), rho

    def test_the_corrected_matrix_sums_to_one(self) -> None:
        for rho in (-0.2, -0.05, 0.05, 0.2):
            corrected = apply_correction(scoreline_matrix(LH, LA), rho)
            assert corrected.total() == pytest.approx(1.0, abs=TOL)

    def test_only_the_four_cells_move_before_normalisation(self) -> None:
        """Normalisation then scales everything by ONE common factor."""
        plain = scoreline_matrix(LH, LA)
        corrected = apply_correction(plain, -0.1)
        ratios = [
            corrected.grid[h][a] / plain.grid[h][a]
            for h in range(plain.max_goals + 1)
            for a in range(plain.max_goals + 1)
            if (h, a) not in LOW_SCORES and plain.grid[h][a] > 0.0
        ]
        assert max(ratios) - min(ratios) < 1e-12

    def test_a_negative_rho_raises_the_goalless_draw(self) -> None:
        plain = scoreline_matrix(LH, LA)
        corrected = apply_correction(plain, -0.1)
        assert corrected.probability(0, 0) > plain.probability(0, 0)
        assert corrected.probability(1, 1) > plain.probability(1, 1)
        assert corrected.probability(1, 0) < plain.probability(1, 0)

    def test_every_corrected_probability_stays_valid(self) -> None:
        for rho in (-0.25, -0.1, 0.1, 0.25):
            corrected = apply_correction(scoreline_matrix(LH, LA), rho)
            for row in corrected.grid:
                for p in row:
                    assert math.isfinite(p) and 0.0 <= p <= 1.0

    def test_markets_remain_valid_after_correction(self) -> None:
        markets = derive_markets(apply_correction(scoreline_matrix(LH, LA), -0.12))
        assert sum(markets.one_x_two) == pytest.approx(1.0, abs=TOL)
        assert markets.btts_yes + markets.btts_no == pytest.approx(1.0, abs=TOL)
        for line in (0.5, 1.5, 2.5, 3.5):
            assert markets.over[line] + markets.under[line] == pytest.approx(
                1.0, abs=TOL
            )
        assert all(0.0 <= p <= 1.0 for p in markets.one_x_two)

    def test_a_negative_rho_raises_the_draw_probability(self) -> None:
        plain = derive_markets(scoreline_matrix(LH, LA))
        corrected = derive_markets(apply_correction(scoreline_matrix(LH, LA), -0.1))
        assert corrected.draw > plain.draw

    def test_an_extreme_rho_is_clamped_rather_than_producing_a_negative(self) -> None:
        corrected = apply_correction(scoreline_matrix(0.4, 0.3), -50.0)
        assert corrected.total() == pytest.approx(1.0, abs=TOL)
        assert all(p >= 0.0 for row in corrected.grid for p in row)


class TestRhoEstimation:
    def test_it_is_deterministic(self) -> None:
        rates = [(1.5, 1.2)] * 40
        outcomes = [(0, 0)] * 10 + [(1, 1)] * 10 + [(2, 1)] * 20
        weights = [1.0] * 40
        first = estimate_rho(rates, outcomes, weights)
        second = estimate_rho(rates, outcomes, weights)
        assert first.rho == second.rho

    def test_an_excess_of_goalless_draws_pulls_rho_negative(self) -> None:
        """More 0-0 and 1-1 than independence expects is the classic case."""
        rates = [(1.5, 1.2)] * 60
        outcomes = [(0, 0)] * 20 + [(1, 1)] * 20 + [(2, 1)] * 20
        estimate = estimate_rho(rates, outcomes, [1.0] * 60)
        assert estimate.rho < 0.0
        assert estimate.informative_matches == 40

    def test_an_excess_of_one_nils_pushes_rho_positive(self) -> None:
        rates = [(1.5, 1.2)] * 60
        outcomes = [(1, 0)] * 20 + [(0, 1)] * 20 + [(2, 1)] * 20
        assert estimate_rho(rates, outcomes, [1.0] * 60).rho > 0.0

    def test_no_low_score_matches_means_no_correction(self) -> None:
        """Nothing in the window speaks to low-score dependence."""
        rates = [(1.5, 1.2)] * 10
        estimate = estimate_rho(rates, [(3, 2)] * 10, [1.0] * 10)
        assert estimate.rho == 0.0
        assert estimate.informative_matches == 0

    def test_the_estimate_stays_inside_the_feasible_interval(self) -> None:
        rates = [(1.5, 1.2)] * 30 + [(0.5, 0.4)] * 10
        outcomes = [(0, 0)] * 20 + [(2, 1)] * 20
        estimate = estimate_rho(rates, outcomes, [1.0] * 40)
        assert estimate.lower_bound <= estimate.rho <= estimate.upper_bound
        for lam_h, lam_a in rates:
            for home, away in LOW_SCORES:
                assert tau(home, away, lam_h, lam_a, estimate.rho) > 0.0

    def test_the_fitted_rho_beats_zero_on_the_training_likelihood(self) -> None:
        rates = [(1.5, 1.2)] * 60
        outcomes = [(0, 0)] * 20 + [(1, 1)] * 20 + [(2, 1)] * 20
        assert estimate_rho(rates, outcomes, [1.0] * 60).log_likelihood_gain > 0.0

    def test_weights_change_the_estimate(self) -> None:
        """Recency weighting must reach rho too, not just attack and defence."""
        rates = [(1.5, 1.2)] * 40
        outcomes = [(0, 0)] * 20 + [(1, 0)] * 20
        flat = estimate_rho(rates, outcomes, [1.0] * 40)
        tilted = estimate_rho(rates, outcomes, [0.01] * 20 + [1.0] * 20)
        assert flat.rho != tilted.rho
