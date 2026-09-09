"""Recency weighting for the training likelihood (MODEL-EXPERIMENTS.md §2).

EXPERIMENTAL AND OFF BY DEFAULT. `DecayConfig()` with no half-life produces a
weight of exactly 1.0 for every observation, which is the frozen baseline's
unweighted likelihood - not an approximation of it.

    weight = 0.5 ** (age_days / half_life_days)

Half-life rather than a bare rate because it is the version a person can hold
in their head: at 90 days, a match three months before the cutoff counts half
as much as one played yesterday, and one from the start of the season counts
about an eighth.

AGE IS MEASURED FROM THE PREDICTION CUTOFF, not from "now". Every fit
therefore has its own weight vector, which is the point - a model predicting
in September must weight an August match the way September saw it, not the way
the following May would.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only - importing it at runtime would
    from engine.model.fit import MatchObservation  # close a cycle

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class DecayConfig:
    """How fast the past stops mattering. `None` means it never does."""

    half_life_days: float | None = None

    def __post_init__(self) -> None:
        if self.half_life_days is not None and self.half_life_days <= 0.0:
            raise ValueError("half_life_days must be positive")

    @property
    def enabled(self) -> bool:
        return self.half_life_days is not None

    @property
    def label(self) -> str:
        if self.half_life_days is None:
            return "none"
        return f"{self.half_life_days:g}d"

    def as_metadata(self) -> dict[str, object]:
        return {"half_life_days": self.half_life_days, "label": self.label}

    def weight_for_age(self, age_days: float) -> float:
        """The weight of a match `age_days` before the cutoff.

        Age is never negative in a legal training set - an observation at or
        after the cutoff is rejected by the estimator - but a negative age
        would produce a weight above 1, so it is refused here too rather than
        silently up-weighting the future.
        """
        if age_days < 0.0:
            raise ValueError("age cannot be negative; that observation is future")
        if self.half_life_days is None:
            return 1.0
        return math.pow(0.5, age_days / self.half_life_days)


def age_in_days(kickoff: datetime, cutoff: datetime) -> float:
    return (cutoff - kickoff).total_seconds() / SECONDS_PER_DAY


def weights_for(
    observations: Sequence[MatchObservation],
    cutoff: datetime,
    config: DecayConfig,
) -> list[float]:
    """One weight per observation, in the order given.

    Returns exactly 1.0 for every match when decay is off, so the weighted
    estimator reduces to the unweighted one bit for bit rather than
    approximately.
    """
    if not config.enabled:
        return [1.0] * len(observations)
    return [
        config.weight_for_age(age_in_days(o.kickoff, cutoff)) for o in observations
    ]
