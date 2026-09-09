"""Turning a fit into a prediction for one fixture (MODEL-BASELINE.md §4).

The whole leakage rule shows up here as a signature: a prediction is a function
of a fitted model and two teams, and the fitted model carries the cutoff it was
entitled to see. There is no way to reach a result from here.

COLD START IS NEVER SILENT. If either side was set to league average, the
prediction says so in `cold_started` and `is_cold_start`. A prediction that
looks ordinary while hiding that one team was a placeholder is worse than no
prediction, because it is indistinguishable from a real one downstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from engine.model.fit import FittedModel
from engine.model.poisson import (
    MAX_GOALS,
    MarketProbabilities,
    ScorelineMatrix,
    derive_markets,
    scoreline_matrix,
    validate_lambda,
)


@dataclass(frozen=True)
class Prediction:
    home_team: str
    away_team: str
    lambda_home: float
    lambda_away: float
    matrix: ScorelineMatrix
    markets: MarketProbabilities
    model_version: str
    data_cutoff: datetime
    training_matches: int
    #: Teams that fell back to league average, if any.
    cold_started: tuple[str, ...]

    @property
    def is_cold_start(self) -> bool:
        return bool(self.cold_started)

    def as_metadata(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "data_cutoff": self.data_cutoff.isoformat(),
            "training_matches": self.training_matches,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
            "cold_started": list(self.cold_started),
            "is_cold_start": self.is_cold_start,
            "truncated_mass": self.matrix.truncated_mass,
            "p_home": self.markets.home_win,
            "p_draw": self.markets.draw,
            "p_away": self.markets.away_win,
            "p_btts_yes": self.markets.btts_yes,
            "p_over_2_5": self.markets.over[2.5],
        }


def expected_goals(
    model: FittedModel, home_team: str, away_team: str
) -> tuple[float, float]:
    """The two rates, straight from the parameterisation.

        log(lambda_home) = mu + home_advantage + attack[home] - defence[away]
        log(lambda_away) = mu + attack[away] - defence[home]
    """
    home = model.rating_for(home_team)
    away = model.rating_for(away_team)
    lambda_home = math.exp(
        model.mu + model.home_advantage + home.attack - away.defence
    )
    lambda_away = math.exp(model.mu + away.attack - home.defence)
    return (
        validate_lambda(lambda_home, label="lambda_home"),
        validate_lambda(lambda_away, label="lambda_away"),
    )


def predict_fixture(
    model: FittedModel,
    home_team: str,
    away_team: str,
    *,
    max_goals: int = MAX_GOALS,
) -> Prediction:
    """One fixture, one matrix, every market derived from it."""
    if home_team == away_team:
        raise ValueError("a team cannot play itself")
    lambda_home, lambda_away = expected_goals(model, home_team, away_team)
    matrix = scoreline_matrix(lambda_home, lambda_away, max_goals)
    cold = tuple(
        team
        for team in (home_team, away_team)
        if model.rating_for(team).cold_start
    )
    return Prediction(
        home_team=home_team,
        away_team=away_team,
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        matrix=matrix,
        markets=derive_markets(matrix),
        model_version=model.model_version,
        data_cutoff=model.data_cutoff,
        training_matches=model.training_matches,
        cold_started=cold,
    )
