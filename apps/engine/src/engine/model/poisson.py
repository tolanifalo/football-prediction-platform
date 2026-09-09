"""The scoreline distribution, and every market derived from it (MODEL-BASELINE.md §3).

PURE MATHEMATICS. No database, no provider, no odds - importable and testable
on its own, which is the point: the numbers here are the ones every later
model has to beat, so they must be checkable without infrastructure.

THE MATRIX IS THE SOURCE OF TRUTH. 1X2, totals and both-teams-to-score are all
read off the same joint distribution; none of them is fitted separately. Two
models of the same match that disagree about P(over 2.5) and P(home win) are
two models, and arbitrage against yourself is the least of the problems.

Goals are treated as INDEPENDENT Poisson draws. That is a known simplification
- real scorelines are correlated, which is what the Dixon-Coles low-score
correction addresses - and it is deliberate here (MODEL-BASELINE.md §7).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Goals per team the matrix covers, 0..MAX_GOALS inclusive.
#:
#: MEASURED, not assumed. Tail mass beyond the cap, by lambda:
#:
#:     cap   lambda 1.4    lambda 3.0    worst for lambda <= 4
#:      10      2.8e-07       2.9e-04             2.8e-03
#:      12      1.4e-08       1.6e-05             2.7e-04
#:      15      3.0e-11       1.2e-07             4.9e-06
#:
#: 15 is the default because 10 leaves nearly three parts in a thousand
#: unmodelled at the high end, which is larger than it looks next to a draw
#: probability of 0.25. The matrix is (cap+1)^2 floats - 256 against 121 - so
#: the accuracy is effectively free. `truncated_mass` reports the exact figure
#: per fixture, so the approximation is never silent whatever the cap.
MAX_GOALS: Final[int] = 15

#: Lambda is clamped into this range before any distribution is built.
#:
#: The floor keeps log-likelihood finite for a team the fit drove to zero; the
#: ceiling stops a pathological fit producing a distribution that is entirely
#: truncation error. Neither bound binds on real league data - they exist so a
#: bad input fails loudly at a boundary rather than quietly as NaN.
MIN_LAMBDA: Final[float] = 1e-6
MAX_LAMBDA: Final[float] = 15.0

#: Matrix rows must sum to 1 within this before renormalisation is applied.
NORMALISATION_TOLERANCE: Final[float] = 1e-9

OVER_UNDER_LINES: Final[tuple[float, ...]] = (0.5, 1.5, 2.5, 3.5)


def validate_lambda(value: float, *, label: str) -> float:
    """Reject what cannot be a rate, and clamp what is merely extreme."""
    if math.isnan(value):
        raise ValueError(f"{label} is NaN")
    if math.isinf(value):
        raise ValueError(f"{label} is infinite")
    if value < 0.0:
        raise ValueError(f"{label} is negative ({value})")
    return min(max(value, MIN_LAMBDA), MAX_LAMBDA)


def poisson_pmf(lam: float, max_goals: int = MAX_GOALS) -> tuple[float, ...]:
    """P(X = k) for k in 0..max_goals, by recurrence.

    p(0) = exp(-lambda), then p(k) = p(k-1) * lambda / k.

    NO FACTORIAL IS EVER COMPUTED. The textbook form lambda^k * e^-lambda / k!
    overflows k! at k=171 in double precision and loses precision long before
    that; the recurrence is exact to rounding and cannot overflow for any
    lambda this function accepts.
    """
    if max_goals < 0:
        raise ValueError("max_goals must not be negative")
    lam = validate_lambda(lam, label="lambda")
    out = [math.exp(-lam)]
    for k in range(1, max_goals + 1):
        out.append(out[-1] * lam / k)
    return tuple(out)


@dataclass(frozen=True)
class ScorelineMatrix:
    """The joint distribution over (home goals, away goals).

    `truncated_mass` is the probability that fell outside 0..max_goals for
    either side BEFORE renormalisation. It is kept rather than discarded so a
    caller can see exactly how much of the answer is approximation.
    """

    lambda_home: float
    lambda_away: float
    max_goals: int
    #: rows are home goals, columns away goals; renormalised to sum to 1.
    grid: tuple[tuple[float, ...], ...]
    truncated_mass: float

    def probability(self, home_goals: int, away_goals: int) -> float:
        if not (0 <= home_goals <= self.max_goals):
            return 0.0
        if not (0 <= away_goals <= self.max_goals):
            return 0.0
        return self.grid[home_goals][away_goals]

    def total(self) -> float:
        return sum(sum(row) for row in self.grid)


def scoreline_matrix(
    lambda_home: float, lambda_away: float, max_goals: int = MAX_GOALS
) -> ScorelineMatrix:
    """Build the joint distribution, renormalising the truncated tail.

    Independence makes the joint the outer product of two marginals. The
    truncated mass is redistributed proportionally rather than dropped, so the
    matrix sums to exactly 1 and no market silently loses probability.
    """
    home = poisson_pmf(lambda_home, max_goals)
    away = poisson_pmf(lambda_away, max_goals)
    raw_mass = sum(home) * sum(away)
    if raw_mass <= 0.0:
        raise ValueError("scoreline matrix has no mass")

    grid = tuple(tuple(h * a / raw_mass for a in away) for h in home)
    return ScorelineMatrix(
        lambda_home=validate_lambda(lambda_home, label="lambda_home"),
        lambda_away=validate_lambda(lambda_away, label="lambda_away"),
        max_goals=max_goals,
        grid=grid,
        truncated_mass=1.0 - raw_mass,
    )


@dataclass(frozen=True)
class MarketProbabilities:
    """Every market this phase derives, all from one matrix."""

    home_win: float
    draw: float
    away_win: float
    over: dict[float, float]
    under: dict[float, float]
    btts_yes: float
    btts_no: float
    expected_home_goals: float
    expected_away_goals: float

    @property
    def one_x_two(self) -> tuple[float, float, float]:
        return (self.home_win, self.draw, self.away_win)


def derive_markets(
    matrix: ScorelineMatrix, lines: Sequence[float] = OVER_UNDER_LINES
) -> MarketProbabilities:
    """Read the markets off the matrix. Nothing here is fitted."""
    home_win = draw = away_win = 0.0
    btts_yes = 0.0
    expected_home = expected_away = 0.0
    total_goal_mass: dict[int, float] = {}

    for home_goals, row in enumerate(matrix.grid):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
            if home_goals > 0 and away_goals > 0:
                btts_yes += probability
            expected_home += home_goals * probability
            expected_away += away_goals * probability
            total = home_goals + away_goals
            total_goal_mass[total] = total_goal_mass.get(total, 0.0) + probability

    over: dict[float, float] = {}
    under: dict[float, float] = {}
    for line in lines:
        # A half-goal line cannot push, so under is exactly the complement.
        under_line = sum(p for t, p in total_goal_mass.items() if t < line)
        under[line] = under_line
        over[line] = 1.0 - under_line

    return MarketProbabilities(
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over=over,
        under=under,
        btts_yes=btts_yes,
        btts_no=1.0 - btts_yes,
        expected_home_goals=expected_home,
        expected_away_goals=expected_away,
    )
