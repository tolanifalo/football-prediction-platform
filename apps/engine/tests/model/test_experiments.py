"""Experimental discipline (MODEL-EXPERIMENTS.md §4).

A comparison is only a comparison if the candidates differ in exactly one
thing. These tests assert the parts that are supposed to be held constant
actually are - the eligible fixtures, their order, the target, and the frozen
baseline's own numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.jobs.model_experiments import HALF_LIVES, candidate, run
from engine.model.backtest import BacktestConfig, walk_forward
from engine.model.decay import DecayConfig
from engine.model.fit import FitConfig, MatchObservation
from engine.model.poisson import scoreline_matrix

START = datetime(2024, 8, 10, 15, 0, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def season(rounds: int = 3) -> list[MatchObservation]:
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
                        home_team=home, away_team=away,
                        home_goals=(day % 3), away_goals=((day + 1) % 3),
                        kickoff=START + timedelta(days=day),
                    )
                )
    return rows


ALL_CANDIDATES = [
    candidate("A. frozen", half_life=None, dixon=False),
    *[
        candidate(f"decay {hl:g}d", half_life=hl, dixon=False)
        for hl in HALF_LIVES
    ],
    candidate("F. DC", half_life=None, dixon=True),
    candidate("G. decay 365d + DC", half_life=365.0, dixon=True),
]


class TestFairComparison:
    def test_every_candidate_scores_the_same_fixtures(self) -> None:
        outcomes = run(season(), ALL_CANDIDATES, as_of=AS_OF, scope="test")
        reference = [
            (s.kickoff, s.home_team, s.away_team) for s in outcomes[0].report.scored
        ]
        for outcome in outcomes[1:]:
            actual = [
                (s.kickoff, s.home_team, s.away_team)
                for s in outcome.report.scored
            ]
            assert actual == reference, outcome.label

    def test_every_candidate_reports_the_same_counts(self) -> None:
        outcomes = run(season(), ALL_CANDIDATES, as_of=AS_OF, scope="test")
        first = outcomes[0].report
        for outcome in outcomes[1:]:
            assert outcome.report.predictions == first.predictions
            assert outcome.report.skipped == first.skipped
            assert outcome.report.fits == first.fits

    def test_the_target_is_identical_across_candidates(self) -> None:
        """Same actual results, or the metrics are not comparable."""
        outcomes = run(season(), ALL_CANDIDATES, as_of=AS_OF, scope="test")
        reference = [
            (s.actual_home, s.actual_away, s.outcome)
            for s in outcomes[0].report.scored
        ]
        for outcome in outcomes[1:]:
            assert [
                (s.actual_home, s.actual_away, s.outcome)
                for s in outcome.report.scored
            ] == reference

    def test_predictions_stay_chronological_under_every_configuration(self) -> None:
        for outcome in run(season(), ALL_CANDIDATES, as_of=AS_OF, scope="test"):
            kickoffs = [s.kickoff for s in outcome.report.scored]
            assert kickoffs == sorted(kickoffs), outcome.label

    def test_no_candidate_trains_on_its_own_target(self) -> None:
        rows = season()
        for outcome in run(rows, ALL_CANDIDATES, as_of=AS_OF, scope="test"):
            for scored in outcome.report.scored:
                eligible = sum(1 for o in rows if o.kickoff < scored.kickoff)
                assert scored.training_matches == eligible, outcome.label

    def test_the_cold_start_population_is_unchanged(self) -> None:
        outcomes = run(season(), ALL_CANDIDATES, as_of=AS_OF, scope="test")
        reference = [bool(s.cold_started) for s in outcomes[0].report.scored]
        for outcome in outcomes[1:]:
            assert [
                bool(s.cold_started) for s in outcome.report.scored
            ] == reference


class TestFrozenBaselineUnchanged:
    def test_the_default_configuration_is_the_frozen_model(self) -> None:
        """Defaults must not have drifted into an experimental setting."""
        config = FitConfig()
        assert config.decay.enabled is False
        assert config.dixon_coles is False
        assert BacktestConfig().fit.decay.half_life_days is None
        assert BacktestConfig().fit.dixon_coles is False

    def test_the_baseline_run_is_bit_identical_to_an_explicit_off_run(self) -> None:
        rows = season()
        default = walk_forward(rows, as_of=AS_OF, config=BacktestConfig(
            min_training_matches=30, fit=FitConfig(ridge=0.05, min_matches=4),
        ))
        explicit = walk_forward(rows, as_of=AS_OF, config=BacktestConfig(
            min_training_matches=30,
            fit=FitConfig(
                ridge=0.05, min_matches=4,
                decay=DecayConfig(half_life_days=None), dixon_coles=False,
            ),
        ))
        assert default.log_loss == explicit.log_loss
        assert default.brier == explicit.brier
        assert default.mean_predicted_home_goals == (
            explicit.mean_predicted_home_goals
        )

    def test_no_rho_is_applied_without_the_switch(self) -> None:
        report = walk_forward(season(), as_of=AS_OF, config=BacktestConfig(
            min_training_matches=30, fit=FitConfig(ridge=0.05, min_matches=4),
        ))
        assert report.predictions > 0
        # Low-score cells must equal the plain Poisson product, so the
        # correction demonstrably did not run.
        scored = report.scored[0]
        plain = scoreline_matrix(scored.lambda_home, scored.lambda_away)
        assert scored.low_score[0] == pytest.approx(plain.probability(0, 0), abs=1e-15)


class TestExperimentConfiguration:
    def test_each_candidate_records_its_configuration(self) -> None:
        for entry in ALL_CANDIDATES:
            meta = entry.fit_config.as_metadata()
            assert "decay" in meta and "dixon_coles" in meta

    def test_the_candidates_differ_only_in_model_configuration(self) -> None:
        """Everything held constant is held constant."""
        for entry in ALL_CANDIDATES:
            assert entry.config.min_training_matches == 30
            assert entry.config.skip_cold_start is False
            assert entry.fit_config.ridge == 0.05
            assert entry.fit_config.min_matches == 4

    def test_all_four_requested_half_lives_are_covered(self) -> None:
        assert HALF_LIVES == (30.0, 90.0, 180.0, 365.0)
