"""The scoreline distribution and its derived markets (MODEL-BASELINE.md §3).

No database and no fit: these are the numbers every later model must beat, so
they are checked against closed-form Poisson values rather than against
whatever the code happens to produce.
"""

from __future__ import annotations

import math

import pytest

from engine.model.poisson import (
    MAX_GOALS,
    MAX_LAMBDA,
    derive_markets,
    poisson_pmf,
    scoreline_matrix,
    validate_lambda,
)

TOL = 1e-12


def closed_form(lam: float, k: int) -> float:
    """The textbook formula, used only as an independent oracle."""
    return lam**k * math.exp(-lam) / math.factorial(k)


class TestPoissonPmf:
    @pytest.mark.parametrize("lam", [0.2, 1.0, 1.35, 2.5, 4.0])
    def test_matches_the_closed_form(self, lam: float) -> None:
        pmf = poisson_pmf(lam)
        for k, value in enumerate(pmf):
            assert value == pytest.approx(closed_form(lam, k), abs=1e-15)

    def test_sums_to_one_within_truncation(self) -> None:
        """The gap is the documented tail, not an error: ~3e-11 at 1.35."""
        assert sum(poisson_pmf(1.35)) == pytest.approx(1.0, abs=1e-9)

    def test_the_recurrence_never_computes_a_factorial(self) -> None:
        """171! overflows a double; the recurrence must not care."""
        pmf = poisson_pmf(3.0, max_goals=400)
        assert len(pmf) == 401
        assert all(math.isfinite(p) for p in pmf)
        assert sum(pmf) == pytest.approx(1.0, abs=1e-12)

    def test_mean_equals_lambda(self) -> None:
        pmf = poisson_pmf(2.2, max_goals=60)
        assert sum(k * p for k, p in enumerate(pmf)) == pytest.approx(2.2, abs=1e-9)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
    def test_a_rate_that_cannot_exist_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError):
            poisson_pmf(bad)

    def test_an_extreme_rate_is_clamped_not_accepted(self) -> None:
        assert validate_lambda(1e9, label="x") == MAX_LAMBDA


class TestScorelineMatrix:
    def test_the_matrix_sums_to_one(self) -> None:
        matrix = scoreline_matrix(1.6, 1.1)
        assert matrix.total() == pytest.approx(1.0, abs=TOL)

    def test_truncated_mass_is_reported_not_hidden(self) -> None:
        matrix = scoreline_matrix(1.4, 1.2)
        assert 0.0 < matrix.truncated_mass < 1e-9
        # Even after renormalisation the caller can see what was approximated.
        assert matrix.max_goals == MAX_GOALS

    def test_a_high_rate_truncates_more_and_says_so(self) -> None:
        low = scoreline_matrix(1.0, 1.0)
        high = scoreline_matrix(5.0, 5.0)
        assert high.truncated_mass > low.truncated_mass
        assert high.total() == pytest.approx(1.0, abs=TOL)

    def test_independence_makes_it_an_outer_product(self) -> None:
        matrix = scoreline_matrix(1.7, 0.9)
        home = poisson_pmf(1.7)
        away = poisson_pmf(0.9)
        mass = sum(home) * sum(away)
        assert matrix.probability(2, 1) == pytest.approx(
            home[2] * away[1] / mass, abs=TOL
        )

    def test_off_grid_scorelines_are_zero_not_an_error(self) -> None:
        matrix = scoreline_matrix(1.5, 1.5)
        assert matrix.probability(99, 0) == 0.0
        assert matrix.probability(-1, 0) == 0.0

    def test_no_probability_is_nan_or_negative(self) -> None:
        matrix = scoreline_matrix(0.05, 6.0)
        for row in matrix.grid:
            for p in row:
                assert math.isfinite(p) and p >= 0.0


class TestDerivedMarkets:
    def test_one_x_two_sums_to_one(self) -> None:
        markets = derive_markets(scoreline_matrix(1.8, 1.1))
        assert sum(markets.one_x_two) == pytest.approx(1.0, abs=TOL)

    def test_over_and_under_are_complements(self) -> None:
        markets = derive_markets(scoreline_matrix(1.45, 1.25))
        for line in (0.5, 1.5, 2.5, 3.5):
            assert markets.over[line] + markets.under[line] == pytest.approx(
                1.0, abs=TOL
            )

    def test_btts_yes_and_no_are_complements(self) -> None:
        markets = derive_markets(scoreline_matrix(1.6, 1.0))
        assert markets.btts_yes + markets.btts_no == pytest.approx(1.0, abs=TOL)

    def test_under_lines_are_monotonic(self) -> None:
        markets = derive_markets(scoreline_matrix(1.5, 1.3))
        unders = [markets.under[line] for line in (0.5, 1.5, 2.5, 3.5)]
        assert unders == sorted(unders)

    def test_under_0_5_is_the_nil_nil_probability(self) -> None:
        markets = derive_markets(scoreline_matrix(1.2, 0.9))
        matrix = scoreline_matrix(1.2, 0.9)
        assert markets.under[0.5] == pytest.approx(matrix.probability(0, 0), abs=TOL)

    def test_btts_yes_equals_one_minus_either_side_blanking(self) -> None:
        lam_h, lam_a = 1.7, 1.1
        markets = derive_markets(scoreline_matrix(lam_h, lam_a))
        # P(both score) = (1 - P(home 0)) * (1 - P(away 0)) under independence.
        expected = (1 - math.exp(-lam_h)) * (1 - math.exp(-lam_a))
        assert markets.btts_yes == pytest.approx(expected, abs=1e-6)

    def test_equal_rates_make_home_and_away_equally_likely(self) -> None:
        markets = derive_markets(scoreline_matrix(1.4, 1.4))
        assert markets.home_win == pytest.approx(markets.away_win, abs=TOL)

    def test_a_stronger_home_side_is_more_likely_to_win(self) -> None:
        weak = derive_markets(scoreline_matrix(1.2, 1.2))
        strong = derive_markets(scoreline_matrix(2.4, 1.2))
        assert strong.home_win > weak.home_win
        assert strong.away_win < weak.away_win

    def test_expected_goals_recover_the_rates(self) -> None:
        markets = derive_markets(scoreline_matrix(1.85, 1.15))
        assert markets.expected_home_goals == pytest.approx(1.85, abs=1e-6)
        assert markets.expected_away_goals == pytest.approx(1.15, abs=1e-6)

    def test_higher_rates_raise_the_over_line(self) -> None:
        low = derive_markets(scoreline_matrix(0.8, 0.7))
        high = derive_markets(scoreline_matrix(2.2, 1.9))
        assert high.over[2.5] > low.over[2.5]

    def test_realistic_rates_give_plausible_football_probabilities(self) -> None:
        """A sanity anchor: 1.55 v 1.15 is an ordinary Premier League match."""
        markets = derive_markets(scoreline_matrix(1.55, 1.15))
        assert 0.40 < markets.home_win < 0.50
        assert 0.20 < markets.draw < 0.30
        assert 0.24 < markets.away_win < 0.34
        assert 0.45 < markets.over[2.5] < 0.60
