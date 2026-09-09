"""The baseline prediction model: independent Poisson (MODEL-BASELINE.md).

Deliberately the simplest thing that can be called a football model, so every
later improvement has something honest to beat. No Dixon-Coles, no Elo, no xG,
no machine learning, and NO BOOKMAKER ODDS ANYWHERE - the model/odds wall is
non-negotiable rule 1, and a baseline that peeked at prices would make value
detection circular before it was even built.
"""

from engine.model.backtest import (
    BacktestConfig,
    BacktestReport,
    ScoredPrediction,
    walk_forward,
)
from engine.model.decay import DecayConfig, weights_for
from engine.model.dixon_coles import (
    InvalidRho,
    apply_correction,
    estimate_rho,
    feasible_rho,
    tau,
)
from engine.model.fit import (
    MODEL_VERSION,
    FitConfig,
    FittedModel,
    InsufficientHistory,
    MatchObservation,
    TeamRating,
    fit_poisson,
    observations_before,
)
from engine.model.metrics import (
    OUTCOMES,
    CalibrationRow,
    brier_score,
    calibration,
    log_loss,
    outcome_of,
)
from engine.model.poisson import (
    MAX_GOALS,
    MarketProbabilities,
    ScorelineMatrix,
    derive_markets,
    poisson_pmf,
    scoreline_matrix,
)
from engine.model.predict import Prediction, expected_goals, predict_fixture

__all__ = [
    "BacktestConfig",
    "BacktestReport",
    "CalibrationRow",
    "DecayConfig",
    "FitConfig",
    "FittedModel",
    "InsufficientHistory",
    "InvalidRho",
    "MAX_GOALS",
    "MODEL_VERSION",
    "MarketProbabilities",
    "MatchObservation",
    "OUTCOMES",
    "Prediction",
    "ScoredPrediction",
    "ScorelineMatrix",
    "TeamRating",
    "apply_correction",
    "brier_score",
    "calibration",
    "derive_markets",
    "estimate_rho",
    "expected_goals",
    "feasible_rho",
    "fit_poisson",
    "log_loss",
    "observations_before",
    "outcome_of",
    "poisson_pmf",
    "predict_fixture",
    "scoreline_matrix",
    "tau",
    "walk_forward",
    "weights_for",
]
