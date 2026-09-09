"""Scoring rules for 1X2 forecasts (MODEL-BASELINE.md §6).

Both are PROPER scoring rules: they are optimised by reporting your honest
belief, so a model cannot improve its score by shading probabilities. That is
the only reason to prefer them to accuracy, which a model can game by always
predicting the favourite.

  LOG LOSS   -mean(log p_outcome). Lower is better. Unbounded above: one
             confident miss costs more than many small ones.
  BRIER      mean over fixtures of sum_k (p_k - o_k)^2, the multiclass form.
             Range 0..2. Bounded, so it is the gentler of the two.

Uniform 1/3 across three outcomes scores log loss 1.0986 and Brier 0.6667.
Anything worse than that is worse than knowing nothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Probabilities are clamped into [EPS, 1-EPS] before the log is taken.
#:
#: A zero-probability outcome that happens gives an infinite log loss, which
#: would make one impossible event destroy the whole report. This model cannot
#: emit exactly zero for a 1X2 outcome, so the clamp never binds in practice -
#: it is there so a future model that can does not produce `inf`.
EPS: Final[float] = 1e-15

Outcome = str
HOME: Final[Outcome] = "home"
DRAW: Final[Outcome] = "draw"
AWAY: Final[Outcome] = "away"
OUTCOMES: Final[tuple[Outcome, ...]] = (HOME, DRAW, AWAY)

UNIFORM_LOG_LOSS: Final[float] = math.log(3.0)
UNIFORM_BRIER: Final[float] = 2.0 / 3.0


def outcome_of(home_goals: int, away_goals: int) -> Outcome:
    if home_goals > away_goals:
        return HOME
    if home_goals == away_goals:
        return DRAW
    return AWAY


def log_loss(probabilities: Sequence[tuple[float, float, float]],
             outcomes: Sequence[Outcome]) -> float:
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes differ in length")
    if not probabilities:
        raise ValueError("no predictions to score")
    total = 0.0
    for triple, outcome in zip(probabilities, outcomes, strict=True):
        p = triple[OUTCOMES.index(outcome)]
        total -= math.log(min(max(p, EPS), 1.0 - EPS))
    return total / len(probabilities)


def brier_score(probabilities: Sequence[tuple[float, float, float]],
                outcomes: Sequence[Outcome]) -> float:
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes differ in length")
    if not probabilities:
        raise ValueError("no predictions to score")
    total = 0.0
    for triple, outcome in zip(probabilities, outcomes, strict=True):
        for k, probability in enumerate(triple):
            actual = 1.0 if OUTCOMES[k] == outcome else 0.0
            total += (probability - actual) ** 2
    return total / len(probabilities)


@dataclass(frozen=True)
class CalibrationRow:
    """One outcome's mean forecast against how often it actually happened."""

    outcome: Outcome
    mean_predicted: float
    observed_frequency: float
    count: int

    @property
    def bias(self) -> float:
        """Positive means the model said it more often than it happened."""
        return self.mean_predicted - self.observed_frequency


def calibration(
    probabilities: Sequence[tuple[float, float, float]],
    outcomes: Sequence[Outcome],
) -> tuple[CalibrationRow, ...]:
    """Mean forecast vs realised frequency, per outcome.

    Deliberately not a reliability curve with bins: 380 fixtures is too few
    for bin frequencies to mean much, and a bucketed chart would invite
    conclusions the sample cannot support.
    """
    if not probabilities:
        raise ValueError("no predictions to score")
    n = len(probabilities)
    rows: list[CalibrationRow] = []
    for k, outcome in enumerate(OUTCOMES):
        mean_predicted = sum(t[k] for t in probabilities) / n
        observed = sum(1 for o in outcomes if o == outcome) / n
        rows.append(
            CalibrationRow(
                outcome=outcome,
                mean_predicted=mean_predicted,
                observed_frequency=observed,
                count=n,
            )
        )
    return tuple(rows)
