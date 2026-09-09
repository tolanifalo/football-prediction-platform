"""Walk-forward backtest and scoring rules (MODEL-BASELINE.md §6).

Deterministic fixtures, no database. The claims that matter are structural -
chronological order, each prediction seeing only its own past, and the target
never appearing in its own training set - so they are asserted directly rather
than inferred from a plausible-looking metric.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from engine.model.backtest import BacktestConfig, walk_forward
from engine.model.fit import (
    FitConfig,
    InsufficientHistory,
    MatchObservation,
    fit_poisson,
    observations_before,
)
from engine.model.metrics import (
    UNIFORM_BRIER,
    UNIFORM_LOG_LOSS,
    brier_score,
    calibration,
    log_loss,
    outcome_of,
)

START = datetime(2024, 8, 10, 15, 0, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def season(rounds: int = 3) -> list[MatchObservation]:
    """A repeating round robin, so every team has plenty of history."""
    rows: list[MatchObservation] = []
    day = 0
    for _ in range(rounds):
        for home in TEAMS:
            for away in TEAMS:
                if home == away:
                    continue
                day += 1
                rows.append(
                    MatchObservation(
                        home_team=home,
                        away_team=away,
                        home_goals=(day % 3),
                        away_goals=((day + 1) % 3),
                        kickoff=START + timedelta(days=day),
                    )
                )
    return rows


CONFIG = BacktestConfig(
    min_training_matches=30, fit=FitConfig(ridge=0.05, min_matches=0)
)


class TestMetrics:
    def test_uniform_forecasts_score_the_textbook_values(self) -> None:
        third = (1 / 3, 1 / 3, 1 / 3)
        outcomes = ["home", "draw", "away"]
        assert log_loss([third] * 3, outcomes) == pytest.approx(UNIFORM_LOG_LOSS)
        assert brier_score([third] * 3, outcomes) == pytest.approx(UNIFORM_BRIER)

    def test_a_perfect_forecast_scores_zero(self) -> None:
        assert log_loss([(1.0, 0.0, 0.0)], ["home"]) == pytest.approx(0.0, abs=1e-12)
        assert brier_score([(1.0, 0.0, 0.0)], ["home"]) == pytest.approx(0.0)

    def test_a_confident_miss_is_punished_but_stays_finite(self) -> None:
        """No infinity, even when the model said a thing could not happen."""
        score = log_loss([(0.0, 0.0, 1.0)], ["home"])
        assert math.isfinite(score) and score > 30.0

    def test_brier_is_bounded_by_two(self) -> None:
        assert brier_score([(0.0, 0.0, 1.0)], ["home"]) == pytest.approx(2.0)

    def test_confidence_in_the_right_answer_beats_hedging(self) -> None:
        outcomes = ["home", "home", "home"]
        sharp = [(0.8, 0.1, 0.1)] * 3
        vague = [(0.4, 0.3, 0.3)] * 3
        assert log_loss(sharp, outcomes) < log_loss(vague, outcomes)
        assert brier_score(sharp, outcomes) < brier_score(vague, outcomes)

    def test_outcome_classification(self) -> None:
        assert outcome_of(2, 1) == "home"
        assert outcome_of(1, 1) == "draw"
        assert outcome_of(0, 3) == "away"

    def test_calibration_compares_mean_forecast_to_realised_frequency(self) -> None:
        rows = calibration([(0.5, 0.25, 0.25)] * 4, ["home", "home", "draw", "away"])
        home = next(r for r in rows if r.outcome == "home")
        assert home.mean_predicted == pytest.approx(0.5)
        assert home.observed_frequency == pytest.approx(0.5)
        assert home.bias == pytest.approx(0.0)

    def test_mismatched_lengths_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            log_loss([(0.5, 0.3, 0.2)], ["home", "draw"])

    def test_scoring_nothing_is_an_error_not_a_zero(self) -> None:
        with pytest.raises(ValueError):
            log_loss([], [])


class TestWalkForward:
    def test_predictions_are_in_chronological_order(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        kickoffs = [s.kickoff for s in report.scored]
        assert kickoffs == sorted(kickoffs)
        assert report.first_prediction == kickoffs[0]
        assert report.last_prediction == kickoffs[-1]

    def test_input_order_does_not_change_the_result(self) -> None:
        forwards = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        backwards = walk_forward(
            list(reversed(season())), as_of=AS_OF, config=CONFIG
        )
        assert forwards.log_loss == pytest.approx(backwards.log_loss, abs=1e-12)
        assert forwards.predictions == backwards.predictions

    def test_each_prediction_only_saw_earlier_matches(self) -> None:
        """The structural claim, checked against the data rather than assumed."""
        rows = season()
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        for scored in report.scored:
            eligible = sum(1 for o in rows if o.kickoff < scored.kickoff)
            assert scored.training_matches == eligible

    def test_no_fixture_is_in_its_own_training_set(self) -> None:
        rows = season()
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        for scored in report.scored:
            # Its own kickoff is excluded, so the count must be strictly below
            # the number of matches up to and including it.
            up_to_and_including = sum(
                1 for o in rows if o.kickoff <= scored.kickoff
            )
            assert scored.training_matches < up_to_and_including

    def test_early_fixtures_are_skipped_not_predicted_on_thin_air(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        assert report.skipped == CONFIG.min_training_matches
        assert report.predictions + report.skipped == len(season())

    def test_fits_are_cached_per_distinct_cutoff(self) -> None:
        """One fit per kickoff instant, not one per fixture."""
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        distinct = len({s.kickoff for s in report.scored})
        assert report.fits == distinct

    def test_every_fit_converges(self) -> None:
        """A model reported without convergence is a number, not a fit."""
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        assert report.predictions > 0
        # Convergence is asserted through the estimator directly, since the
        # report carries fits only implicitly.
        rows = season()
        for scored in report.scored[:10]:
            model = fit_poisson(
                observations_before(rows, scored.kickoff),
                data_cutoff=scored.kickoff,
                as_of=AS_OF,
                config=CONFIG.fit,
            )
            assert model.converged, f"fit at {scored.kickoff} hit the sweep cap"
            assert model.sweeps < CONFIG.fit.max_sweeps

    def test_a_dataset_with_no_usable_history_refuses(self) -> None:
        with pytest.raises(InsufficientHistory):
            walk_forward(
                season()[:5],
                as_of=AS_OF,
                config=BacktestConfig(min_training_matches=30),
            )

    def test_metrics_are_computed_over_the_scored_predictions(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        recomputed = log_loss(
            [s.probabilities for s in report.scored],
            [s.outcome for s in report.scored],
        )
        assert report.log_loss == pytest.approx(recomputed, abs=1e-12)
        assert report.predictions == len(report.scored)

    def test_actual_goal_means_match_the_data(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        n = report.predictions
        assert report.mean_actual_home_goals == pytest.approx(
            sum(s.actual_home for s in report.scored) / n
        )

    def test_every_probability_is_finite_and_normalised(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        for scored in report.scored:
            assert all(math.isfinite(p) and p >= 0.0 for p in scored.probabilities)
            assert sum(scored.probabilities) == pytest.approx(1.0, abs=1e-9)
            assert scored.lambda_home > 0.0 and scored.lambda_away > 0.0

    def test_baselines_are_scored_on_the_same_fixtures(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=CONFIG)
        assert math.isfinite(report.baseline_frequency_log_loss)
        assert math.isfinite(report.baseline_league_log_loss)
        # The league-average baseline knows nothing about individual teams, so
        # within one cutoff every fixture gets the identical triple. Across
        # cutoffs it can repeat by coincidence, hence <= rather than ==.
        distinct = {s.baseline_league for s in report.scored}
        assert len(distinct) <= report.fits
        by_cutoff = {s.kickoff: s.baseline_league for s in report.scored}
        for scored in report.scored:
            assert scored.baseline_league == by_cutoff[scored.kickoff]

    def test_cold_start_fixtures_can_be_excluded(self) -> None:
        rows = season()
        newcomer = MatchObservation(
            home_team="newcomer",
            away_team="alpha",
            home_goals=1,
            away_goals=1,
            kickoff=START + timedelta(days=500),
        )
        config = BacktestConfig(
            min_training_matches=30,
            fit=FitConfig(ridge=0.05, min_matches=4),
            skip_cold_start=True,
        )
        strict = walk_forward([*rows, newcomer], as_of=AS_OF, config=config)
        assert all(not s.cold_started for s in strict.scored)
