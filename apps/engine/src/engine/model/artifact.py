"""The prediction artifact: what gets persisted (PREDICTIONS.md §1).

PURE, AND NO DATABASE. This module turns a `Prediction` into a plain,
serialisable record and back again. It imports nothing from psycopg and
nothing from the schema, which is what lets the correctness properties -
probabilities bounded, 1X2 summing, complements exact, the matrix round-
tripping - be tested without PostgreSQL anywhere near them.

The persistence module maps this record onto columns. The split matters: the
rules about what a valid prediction IS live here, and the rules about how a
row is written live there.

WHAT IS DELIBERATELY ABSENT: bookmaker odds, implied probabilities, value,
recommendations. Rule 1 is the model/odds wall, and a price on this record
would put one a single attribute away from the estimator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from engine.model.fit import FittedModel
from engine.model.poisson import OVER_UNDER_LINES, ScorelineMatrix, derive_markets
from engine.model.predict import Prediction

#: The over/under lines the artifact persists, one column each.
PERSISTED_LINES: Final[tuple[float, ...]] = OVER_UNDER_LINES

#: Slack allowed when re-checking a stored triple. It is float64 summation
#: error over ~256 cells, not a rounding budget: nothing is ever rounded on
#: the way in.
PROBABILITY_TOLERANCE: Final[float] = 1e-9


class InvalidArtifact(ValueError):
    """The record is not a valid prediction. Never persist one of these."""


def flatten(matrix: ScorelineMatrix) -> tuple[float, ...]:
    """Row-major flattening: index `h * (max_goals+1) + a`.

    Flat rather than nested because the shape is then one cardinality check
    against `max_goals`, and no database driver has to agree with us about
    multidimensional array bounds.
    """
    return tuple(value for row in matrix.grid for value in row)


def unflatten(values: Sequence[float], max_goals: int) -> tuple[tuple[float, ...], ...]:
    """The inverse of `flatten`, for reading a stored artifact back."""
    side = max_goals + 1
    if len(values) != side * side:
        raise InvalidArtifact(
            f"scoreline has {len(values)} cells, expected {side * side}"
        )
    return tuple(
        tuple(values[h * side : (h + 1) * side]) for h in range(side)
    )


@dataclass(frozen=True)
class PredictionArtifact:
    """One persisted prediction, independent of how it is stored.

    Identity is `(fixture_id, model_version, profile, data_cutoff)`. `known_at`
    is when it was produced and is NOT part of that: regenerating the same
    prediction tomorrow is the same prediction.
    """

    fixture_id: str
    model_family: str
    model_version: str
    profile: str
    data_cutoff: datetime
    known_at: datetime

    lambda_home: float
    lambda_away: float
    max_goals: int
    scoreline: tuple[float, ...]
    truncated_mass: float

    p_home: float
    p_draw: float
    p_away: float
    #: Keyed by line; `under` is `1 - over` and is never stored.
    p_over: dict[float, float]
    #: `btts_no` is `1 - btts_yes` and is never stored.
    p_btts_yes: float

    is_cold_start: bool
    cold_started_teams: tuple[str, ...]
    training_matches: int
    fit_metadata: dict[str, object]

    @property
    def identity(self) -> tuple[str, str, str, datetime]:
        return (self.fixture_id, self.model_version, self.profile, self.data_cutoff)

    @property
    def p_btts_no(self) -> float:
        return 1.0 - self.p_btts_yes

    def p_under(self, line: float) -> float:
        return 1.0 - self.p_over[line]

    def matrix(self) -> tuple[tuple[float, ...], ...]:
        return unflatten(self.scoreline, self.max_goals)

    def probability(self, home_goals: int, away_goals: int) -> float:
        if not (0 <= home_goals <= self.max_goals):
            return 0.0
        if not (0 <= away_goals <= self.max_goals):
            return 0.0
        return self.scoreline[home_goals * (self.max_goals + 1) + away_goals]

    def validate(self) -> PredictionArtifact:
        """Refuse to be persisted unless every probability rule holds.

        Checked here as well as by the database CHECK constraints on purpose:
        the constraints are the last line, and a failure there is an opaque
        23514 on one row of a batch. This says which rule and by how much.
        """
        side = self.max_goals + 1
        if len(self.scoreline) != side * side:
            raise InvalidArtifact(
                f"scoreline has {len(self.scoreline)} cells, expected {side * side}"
            )
        for value in self.scoreline:
            if not (0.0 <= value <= 1.0):
                raise InvalidArtifact(f"scoreline cell out of range: {value}")
        mass = sum(self.scoreline)
        if abs(mass - 1.0) > PROBABILITY_TOLERANCE:
            raise InvalidArtifact(f"scoreline mass is {mass}, expected 1")

        named = {
            "p_home": self.p_home, "p_draw": self.p_draw, "p_away": self.p_away,
            "p_btts_yes": self.p_btts_yes,
            **{f"p_over_{line}": p for line, p in self.p_over.items()},
        }
        for name, value in named.items():
            if not (0.0 <= value <= 1.0):
                raise InvalidArtifact(f"{name} is {value}, outside [0, 1]")

        total = self.p_home + self.p_draw + self.p_away
        if abs(total - 1.0) > PROBABILITY_TOLERANCE:
            raise InvalidArtifact(f"1X2 sums to {total}, expected 1")

        if set(self.p_over) != set(PERSISTED_LINES):
            raise InvalidArtifact(
                f"over lines {sorted(self.p_over)} != {list(PERSISTED_LINES)}"
            )
        ordered = [self.p_over[line] for line in sorted(PERSISTED_LINES)]
        if ordered != sorted(ordered, reverse=True):
            raise InvalidArtifact(f"over probabilities are not monotonic: {ordered}")

        if self.lambda_home <= 0.0 or self.lambda_away <= 0.0:
            raise InvalidArtifact("expected goals must be positive")
        if self.is_cold_start != bool(self.cold_started_teams):
            raise InvalidArtifact(
                "is_cold_start disagrees with cold_started_teams"
            )
        if self.data_cutoff > self.known_at:
            raise InvalidArtifact(
                "data_cutoff is after known_at: the model would have needed "
                "knowledge from after the moment it was recorded"
            )
        return self


def artifact_from(
    prediction: Prediction,
    model: FittedModel,
    *,
    fixture_id: str,
    known_at: datetime,
) -> PredictionArtifact:
    """Build the persistable record from a live prediction.

    NOTHING IS ROUNDED. Every probability is carried at full float64 and
    stored in a float8 column, so the persisted artifact reads back
    bit-identical to what the model computed - which is what makes
    "the artifact equals the direct model output" an exact claim.
    """
    markets = prediction.markets
    return PredictionArtifact(
        fixture_id=fixture_id,
        model_family=str(model.as_metadata()["model_family"]),
        model_version=model.model_version,
        profile=model.config.profile,
        data_cutoff=prediction.data_cutoff,
        known_at=known_at,
        lambda_home=prediction.lambda_home,
        lambda_away=prediction.lambda_away,
        max_goals=prediction.matrix.max_goals,
        scoreline=flatten(prediction.matrix),
        truncated_mass=prediction.matrix.truncated_mass,
        p_home=markets.home_win,
        p_draw=markets.draw,
        p_away=markets.away_win,
        p_over={line: markets.over[line] for line in PERSISTED_LINES},
        p_btts_yes=markets.btts_yes,
        is_cold_start=prediction.is_cold_start,
        cold_started_teams=prediction.cold_started,
        training_matches=prediction.training_matches,
        fit_metadata=_reproducible_metadata(model),
    ).validate()


def _reproducible_metadata(model: FittedModel) -> dict[str, object]:
    """Everything needed to reproduce the fit, minus the ratings themselves.

    The per-team ratings are omitted deliberately: they are recomputable from
    the cutoff and the configuration, and storing 27 of them per fixture would
    write the same table 380 times per run.
    """
    full = model.as_metadata()
    return {
        key: full[key]
        for key in (
            "model_family", "model_version", "profile", "scope",
            "data_cutoff", "as_of", "training_matches", "teams",
            "sweeps", "converged", "log_likelihood", "mu",
            "home_advantage", "rho", "decay_enabled",
            "decay_half_life_days", "dixon_coles_enabled",
            "cold_start_min_matches", "config",
        )
        if key in full
    }


def derived_markets_match(artifact: PredictionArtifact) -> bool:
    """Re-derive the stored markets from the stored matrix and compare.

    The artifact stores both the matrix and the markets read off it. That is a
    deliberate redundancy - the web read path must not re-implement the
    derivation in another language - and this is the check that keeps the two
    honest.
    """
    matrix = ScorelineMatrix(
        lambda_home=artifact.lambda_home,
        lambda_away=artifact.lambda_away,
        max_goals=artifact.max_goals,
        grid=artifact.matrix(),
        truncated_mass=artifact.truncated_mass,
    )
    markets = derive_markets(matrix)
    if abs(markets.home_win - artifact.p_home) > PROBABILITY_TOLERANCE:
        return False
    if abs(markets.draw - artifact.p_draw) > PROBABILITY_TOLERANCE:
        return False
    if abs(markets.away_win - artifact.p_away) > PROBABILITY_TOLERANCE:
        return False
    if abs(markets.btts_yes - artifact.p_btts_yes) > PROBABILITY_TOLERANCE:
        return False
    return all(
        abs(markets.over[line] - artifact.p_over[line]) <= PROBABILITY_TOLERANCE
        for line in PERSISTED_LINES
    )
