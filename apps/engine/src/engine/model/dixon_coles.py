"""Dixon-Coles low-score dependence correction (MODEL-EXPERIMENTS.md §3).

EXPERIMENTAL AND OFF BY DEFAULT. With `rho = 0` every tau is exactly 1.0 and
the corrected matrix is the independent-Poisson matrix, bit for bit.

Independent Poisson gets the low scores wrong in a specific, well-known way:
0-0 and 1-1 happen more often than independence predicts, and 1-0 and 0-1 less.
Dixon and Coles (1997) fix it with a multiplicative adjustment to exactly four
cells:

    tau(0,0) = 1 - lambda_h * lambda_a * rho
    tau(0,1) = 1 + lambda_h * rho
    tau(1,0) = 1 + lambda_a * rho
    tau(1,1) = 1 - rho
    tau(x,y) = 1                     everywhere else

A NEGATIVE rho is the empirically expected direction: it lifts 0-0 and 1-1 and
lowers 1-0 and 0-1. Dixon and Coles reported about -0.13.

THE ADJUSTMENT PRESERVES TOTAL PROBABILITY EXACTLY, which is not obvious and
is worth writing down. With K = exp(-lh) * exp(-la), the four independent cells
carry K * (1 + la + lh + lh*la), and after adjustment they carry

    K * [ (1 - lh*la*rho) + la*(1 + lh*rho) + lh*(1 + la*rho) + lh*la*(1 - rho) ]
  = K * [ 1 - lh*la*rho + la + lh*la*rho + lh + lh*la*rho + lh*la - lh*la*rho ]
  = K * (1 + la + lh + lh*la)

- the same quantity. Every rho term cancels. So the correction moves mass
between the four cells and never creates or destroys any. The matrix is still
renormalised afterwards, because the SCORELINE GRID IS TRUNCATED at
MAX_GOALS and that truncation is the one thing this identity does not cover.
The test suite asserts the pre-normalisation total is within 1e-12 of the
uncorrected total, so if the identity ever stops holding it is a failure, not
a silently absorbed renormalisation.

DEVIATION FROM THE PAPER, STATED PLAINLY. Dixon and Coles estimate rho jointly
with the attack, defence and home-advantage parameters. This implementation
estimates it in TWO STAGES: the independent-Poisson parameters are fitted
first, then rho is chosen to maximise the correction's contribution to the
same weighted likelihood, holding those parameters fixed. The profile
likelihood in rho is one-dimensional and smooth, so the second stage is exact
to floating point rather than approximate - but the joint optimum may differ
slightly from this conditional one. The simplification is deliberate: it keeps
the frozen baseline estimator untouched, which is what makes the experiment a
controlled comparison rather than two models that differ in two ways at once.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from engine.model.poisson import ScorelineMatrix

#: Keep every tau strictly positive: log(tau) appears in the likelihood, and a
#: rho sitting exactly on a boundary makes one cell's probability zero.
RHO_MARGIN: Final[float] = 1e-6

#: Golden-section iterations for the profile search. 200 shrinks any starting
#: bracket below double precision, so the result is deterministic and the
#: iteration count never has to be tuned.
GOLDEN_ITERATIONS: Final[int] = 200

_INV_PHI: Final[float] = (math.sqrt(5.0) - 1.0) / 2.0

LOW_SCORES: Final[tuple[tuple[int, int], ...]] = ((0, 0), (0, 1), (1, 0), (1, 1))


class InvalidRho(ValueError):
    """The dependence parameter would make a scoreline probability negative."""


def tau(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    """The multiplicative adjustment for one cell. 1.0 outside the four."""
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lambda_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lambda_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def feasible_rho(lambda_home: float, lambda_away: float) -> tuple[float, float]:
    """The open interval in which all four taus stay positive.

        max(-1/lh, -1/la)  <  rho  <  min(1/(lh*la), 1)
    """
    lower = max(-1.0 / lambda_home, -1.0 / lambda_away)
    upper = min(1.0 / (lambda_home * lambda_away), 1.0)
    return (lower, upper)


def feasible_rho_over(
    rates: Sequence[tuple[float, float]],
) -> tuple[float, float]:
    """The intersection over many fixtures - the range valid for all of them."""
    if not rates:
        raise InvalidRho("no rates to constrain rho")
    lowers, uppers = zip(
        *(feasible_rho(h, a) for h, a in rates), strict=True
    )
    return (max(lowers), min(uppers))


def validate_rho(rho: float, lambda_home: float, lambda_away: float) -> float:
    """Reject a rho that would make a probability negative."""
    if math.isnan(rho) or math.isinf(rho):
        raise InvalidRho(f"rho is not a finite number ({rho})")
    lower, upper = feasible_rho(lambda_home, lambda_away)
    if not (lower < rho < upper):
        raise InvalidRho(
            f"rho {rho} is outside ({lower}, {upper}) for lambdas "
            f"{lambda_home}, {lambda_away}"
        )
    return rho


def clamp_rho(rho: float, lambda_home: float, lambda_away: float) -> float:
    """Pull rho just inside the feasible interval for these rates.

    A rho fitted across a whole training set can be infeasible for one unusual
    fixture. Clamping keeps that fixture's distribution valid; the alternative
    is refusing to predict it, which throws away a fixture over a boundary
    condition that moves the answer by a rounding error.
    """
    lower, upper = feasible_rho(lambda_home, lambda_away)
    return min(max(rho, lower + RHO_MARGIN), upper - RHO_MARGIN)


def apply_correction(matrix: ScorelineMatrix, rho: float) -> ScorelineMatrix:
    """Adjust the four low-score cells, then renormalise the whole grid.

    Only those four cells change before normalisation. Normalisation then
    scales every cell by one common factor - which, by the identity in the
    module docstring, differs from 1 only by the truncated tail.
    """
    if rho == 0.0:
        return matrix
    safe = clamp_rho(rho, matrix.lambda_home, matrix.lambda_away)

    grid = [list(row) for row in matrix.grid]
    for home_goals, away_goals in LOW_SCORES:
        if home_goals > matrix.max_goals or away_goals > matrix.max_goals:
            continue
        adjustment = tau(
            home_goals, away_goals, matrix.lambda_home, matrix.lambda_away, safe
        )
        if adjustment <= 0.0:
            raise InvalidRho(
                f"rho {safe} gives a non-positive tau at "
                f"{home_goals}-{away_goals}"
            )
        grid[home_goals][away_goals] *= adjustment

    total = sum(sum(row) for row in grid)
    if total <= 0.0:
        raise InvalidRho("the corrected matrix has no mass")

    return ScorelineMatrix(
        lambda_home=matrix.lambda_home,
        lambda_away=matrix.lambda_away,
        max_goals=matrix.max_goals,
        grid=tuple(tuple(p / total for p in row) for row in grid),
        # The tail left outside the grid, plus whatever the correction moved
        # across the truncation boundary. Reported, never hidden.
        truncated_mass=matrix.truncated_mass,
    )


@dataclass(frozen=True)
class RhoEstimate:
    rho: float
    #: Training matches that actually constrained it - the low-score ones.
    informative_matches: int
    lower_bound: float
    upper_bound: float
    log_likelihood_gain: float


def estimate_rho(
    rates: Sequence[tuple[float, float]],
    outcomes: Sequence[tuple[int, int]],
    weights: Sequence[float],
) -> RhoEstimate:
    """Profile maximum likelihood for rho, holding the Poisson fit fixed.

    Only low-score observations contribute: tau is 1 elsewhere, so log(tau) is
    0 and those matches say nothing about rho. Because the adjustment
    preserves total probability, no normalising term enters the objective and
    the whole problem is

        maximise  sum_m weight_m * log tau(y_m | lambda_m, rho)

    over the interval where every tau stays positive. That objective is smooth
    and, in practice, unimodal, so golden-section search finds the maximum
    deterministically without derivatives or a starting guess.
    """
    lower, upper = feasible_rho_over(rates)
    lower += RHO_MARGIN
    upper -= RHO_MARGIN
    if lower >= upper:
        return RhoEstimate(0.0, 0, lower, upper, 0.0)

    informative = [
        (rates[m], outcomes[m], weights[m])
        for m in range(len(outcomes))
        if outcomes[m] in LOW_SCORES
    ]
    if not informative:
        # Nothing in the window says anything about low-score dependence.
        # Zero is the honest answer: it is the no-correction value.
        return RhoEstimate(0.0, 0, lower, upper, 0.0)

    def objective(candidate: float) -> float:
        total = 0.0
        for (lam_h, lam_a), (hg, ag), weight in informative:
            adjustment = tau(hg, ag, lam_h, lam_a, candidate)
            if adjustment <= 0.0:
                return -math.inf
            total += weight * math.log(adjustment)
        return total

    a, b = lower, upper
    c = b - _INV_PHI * (b - a)
    d = a + _INV_PHI * (b - a)
    fc, fd = objective(c), objective(d)
    for _ in range(GOLDEN_ITERATIONS):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - _INV_PHI * (b - a)
            fc = objective(c)
        else:
            a, c, fc = c, d, fd
            d = a + _INV_PHI * (b - a)
            fd = objective(d)
        if b - a < 1e-15:
            break

    best = (a + b) / 2.0
    return RhoEstimate(
        rho=best,
        informative_matches=len(informative),
        lower_bound=lower,
        upper_bound=upper,
        log_likelihood_gain=objective(best) - objective(0.0),
    )
