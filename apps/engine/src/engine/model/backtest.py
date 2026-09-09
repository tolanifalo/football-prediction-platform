"""Walk-forward backtest (MODEL-BASELINE.md §6).

THE ONE RULE: a fixture is predicted by a model that has never seen it, and
has never seen anything that happened at or after its kickoff. There is no
single fit over the whole season anywhere in this file. Fitting once on
everything and scoring it is not a backtest - it is a report on how well the
model memorised the answers.

Each fixture's cutoff is ITS OWN KICKOFF INSTANT. Two matches on the same
afternoon with different kick-off times get different training sets, and the
later one is entitled to the earlier one's result because that result existed
before it started. Fits are cached by cutoff, so the 380 fixtures of a league
season need one fit per distinct kickoff instant - an exact saving, not an
approximation, because an identical cutoff yields an identical training set.

Two baselines are computed alongside, both walk-forward on the same cutoffs:

  CLASS FREQUENCY   the empirical home/draw/away rate in the training window.
                    Beating it is the minimum bar for the model knowing
                    anything about teams at all.
  LEAGUE AVERAGE    the same Poisson machinery with every team's attack and
                    defence forced to zero. It isolates what the team ratings
                    add over the league baseline plus home advantage.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from engine.model.fit import (
    FitConfig,
    FittedModel,
    InsufficientHistory,
    MatchObservation,
    fit_poisson,
    observations_before,
)
from engine.model.metrics import (
    OUTCOMES,
    CalibrationRow,
    Outcome,
    brier_score,
    calibration,
    log_loss,
    outcome_of,
)
from engine.model.poisson import derive_markets, scoreline_matrix
from engine.model.predict import predict_fixture

#: A fixture is skipped until the training window holds at least this many
#: matches. Below it the ratings are noise and the honest move is to decline.
DEFAULT_MIN_TRAINING_MATCHES: int = 30


@dataclass(frozen=True)
class BacktestConfig:
    min_training_matches: int = DEFAULT_MIN_TRAINING_MATCHES
    fit: FitConfig = field(default_factory=FitConfig)
    #: Skip a fixture where either side would be cold-started, rather than
    #: scoring a prediction the model itself flags as a placeholder.
    skip_cold_start: bool = False


@dataclass(frozen=True)
class ScoredPrediction:
    kickoff: datetime
    home_team: str
    away_team: str
    probabilities: tuple[float, float, float]
    lambda_home: float
    lambda_away: float
    actual_home: int
    actual_away: int
    outcome: Outcome
    training_matches: int
    cold_started: tuple[str, ...]
    baseline_frequency: tuple[float, float, float]
    baseline_league: tuple[float, float, float]
    #: P(0-0), P(0-1), P(1-0), P(1-1) - the cells Dixon-Coles adjusts, kept so
    #: the low-score diagnostic can compare candidates without refitting.
    low_score: tuple[float, float, float, float]


@dataclass(frozen=True)
class BacktestReport:
    predictions: int
    skipped: int
    first_prediction: datetime | None
    last_prediction: datetime | None
    log_loss: float
    brier: float
    mean_predicted_home_goals: float
    mean_predicted_away_goals: float
    mean_actual_home_goals: float
    mean_actual_away_goals: float
    calibration: tuple[CalibrationRow, ...]
    baseline_frequency_log_loss: float
    baseline_frequency_brier: float
    baseline_league_log_loss: float
    baseline_league_brier: float
    fits: int
    scored: tuple[ScoredPrediction, ...]

    def as_metadata(self) -> dict[str, object]:
        return {
            "predictions": self.predictions,
            "skipped": self.skipped,
            "first_prediction": (
                self.first_prediction.isoformat() if self.first_prediction else None
            ),
            "last_prediction": (
                self.last_prediction.isoformat() if self.last_prediction else None
            ),
            "log_loss": self.log_loss,
            "brier": self.brier,
            "baseline_frequency_log_loss": self.baseline_frequency_log_loss,
            "baseline_frequency_brier": self.baseline_frequency_brier,
            "baseline_league_log_loss": self.baseline_league_log_loss,
            "baseline_league_brier": self.baseline_league_brier,
            "mean_predicted_home_goals": self.mean_predicted_home_goals,
            "mean_predicted_away_goals": self.mean_predicted_away_goals,
            "mean_actual_home_goals": self.mean_actual_home_goals,
            "mean_actual_away_goals": self.mean_actual_away_goals,
            "fits": self.fits,
        }


def _class_frequency(
    training: Sequence[MatchObservation],
) -> tuple[float, float, float]:
    """Empirical 1X2 rate in the training window, Laplace-smoothed.

    The +1 per class keeps the log loss finite if a window happens to contain
    no draws; with a real window it shifts the estimate by well under a point.
    """
    counts = dict.fromkeys(OUTCOMES, 1.0)
    for o in training:
        counts[outcome_of(o.home_goals, o.away_goals)] += 1.0
    total = sum(counts.values())
    return (
        counts["home"] / total,
        counts["draw"] / total,
        counts["away"] / total,
    )


def _league_average_probabilities(model: FittedModel) -> tuple[float, float, float]:
    """The model with every team rating switched off: mu and home advantage."""
    lambda_home = math.exp(model.mu + model.home_advantage)
    lambda_away = math.exp(model.mu)
    markets = derive_markets(scoreline_matrix(lambda_home, lambda_away))
    return markets.one_x_two


def walk_forward(
    observations: Sequence[MatchObservation],
    *,
    as_of: datetime,
    scope: str = "",
    config: BacktestConfig | None = None,
) -> BacktestReport:
    """Predict every eligible fixture from its own past, then score."""
    settings = config or BacktestConfig()
    ordered = sorted(observations, key=lambda o: (o.kickoff, o.home_team, o.away_team))

    fit_cache: dict[datetime, FittedModel] = {}
    scored: list[ScoredPrediction] = []
    skipped = 0

    for target in ordered:
        cutoff = target.kickoff
        training = observations_before(ordered, cutoff)
        if len(training) < settings.min_training_matches:
            skipped += 1
            continue

        model = fit_cache.get(cutoff)
        if model is None:
            try:
                model = fit_poisson(
                    training,
                    data_cutoff=cutoff,
                    as_of=as_of,
                    scope=scope,
                    config=settings.fit,
                )
            except InsufficientHistory:
                skipped += 1
                continue
            fit_cache[cutoff] = model

        prediction = predict_fixture(model, target.home_team, target.away_team)
        if settings.skip_cold_start and prediction.is_cold_start:
            skipped += 1
            continue

        scored.append(
            ScoredPrediction(
                kickoff=target.kickoff,
                home_team=target.home_team,
                away_team=target.away_team,
                probabilities=prediction.markets.one_x_two,
                lambda_home=prediction.lambda_home,
                lambda_away=prediction.lambda_away,
                actual_home=target.home_goals,
                actual_away=target.away_goals,
                outcome=outcome_of(target.home_goals, target.away_goals),
                training_matches=model.training_matches,
                cold_started=prediction.cold_started,
                baseline_frequency=_class_frequency(training),
                baseline_league=_league_average_probabilities(model),
                low_score=(
                    prediction.matrix.probability(0, 0),
                    prediction.matrix.probability(0, 1),
                    prediction.matrix.probability(1, 0),
                    prediction.matrix.probability(1, 1),
                ),
            )
        )

    if not scored:
        raise InsufficientHistory("no fixture had enough history to predict")

    n = len(scored)
    probabilities = [s.probabilities for s in scored]
    outcomes = [s.outcome for s in scored]

    return BacktestReport(
        predictions=n,
        skipped=skipped,
        first_prediction=scored[0].kickoff,
        last_prediction=scored[-1].kickoff,
        log_loss=log_loss(probabilities, outcomes),
        brier=brier_score(probabilities, outcomes),
        mean_predicted_home_goals=sum(s.lambda_home for s in scored) / n,
        mean_predicted_away_goals=sum(s.lambda_away for s in scored) / n,
        mean_actual_home_goals=sum(s.actual_home for s in scored) / n,
        mean_actual_away_goals=sum(s.actual_away for s in scored) / n,
        calibration=calibration(probabilities, outcomes),
        baseline_frequency_log_loss=log_loss(
            [s.baseline_frequency for s in scored], outcomes
        ),
        baseline_frequency_brier=brier_score(
            [s.baseline_frequency for s in scored], outcomes
        ),
        baseline_league_log_loss=log_loss(
            [s.baseline_league for s in scored], outcomes
        ),
        baseline_league_brier=brier_score(
            [s.baseline_league for s in scored], outcomes
        ),
        fits=len(fit_cache),
        scored=tuple(scored),
    )
