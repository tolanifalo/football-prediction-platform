"""Multi-season walk-forward behaviour (MULTI-SEASON-EVALUATION.md).

Synthetic seasons, no database: the properties being asserted are about
chronology and cutoffs, and constructing the corpus here is what makes them
checkable. The real-corpus numbers are produced by the evaluation job.

The season boundary is the theme. It is a REPORTING boundary, not a modelling
one - training crosses it freely, subject only to the cutoff - and almost
every test here is a different way of stating that without letting anything
leak backwards across it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.jobs.multi_season_evaluation import (
    ALL_SEASONS,
    DEVELOPMENT,
    HELD_OUT,
    MIN_LOG_LOSS_GAIN,
    Slice,
    select,
    summarise,
)
from engine.model.backtest import BacktestConfig, walk_forward
from engine.model.decay import DecayConfig, weights_for
from engine.model.fit import (
    FitConfig,
    MatchObservation,
    fit_poisson,
    observations_before,
)
from engine.model.predict import predict_fixture

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
#: A club that only appears in the final season - the promotion case.
PROMOTED = "newcomer"
SEASON_LABELS = ("2020/21", "2021/22", "2022/23")
CONFIG = BacktestConfig(
    min_training_matches=30, fit=FitConfig(ridge=0.05, min_matches=4)
)


def corpus(promote: bool = False) -> list[MatchObservation]:
    """Three synthetic seasons, each a double round robin, a year apart."""
    rows: list[MatchObservation] = []
    for index, label in enumerate(SEASON_LABELS):
        start = datetime(2020 + index, 8, 10, 15, 0, tzinfo=UTC)
        teams = list(TEAMS)
        if promote and label == SEASON_LABELS[-1]:
            teams = [*TEAMS[:-1], PROMOTED]
        day = 0
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                day += 1
                rows.append(
                    MatchObservation(
                        home_team=home,
                        away_team=away,
                        home_goals=(day % 3),
                        away_goals=((day + 1) % 3),
                        kickoff=start + timedelta(days=day),
                        season=label,
                    )
                )
    return rows


class TestCrossSeasonTraining:
    def test_a_later_season_trains_on_earlier_ones(self) -> None:
        """The whole point of the expansion: history crosses the boundary."""
        rows = corpus()
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        second_season = [s for s in report.scored if s.season == SEASON_LABELS[1]]
        assert second_season
        # Its very first prediction already has a full prior season behind it.
        first = min(second_season, key=lambda s: s.kickoff)
        per_season = len(rows) // len(SEASON_LABELS)
        assert first.training_matches >= per_season

    def test_only_the_first_season_is_ever_starved(self) -> None:
        rows = corpus()
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        skipped_seasons = {
            o.season
            for o in rows
            if not any(
                s.kickoff == o.kickoff and s.home_team == o.home_team
                for s in report.scored
            )
        }
        assert skipped_seasons <= {SEASON_LABELS[0]}

    def test_training_counts_grow_monotonically_through_the_corpus(self) -> None:
        report = walk_forward(corpus(), as_of=AS_OF, config=CONFIG)
        counts = [s.training_matches for s in report.scored]
        assert counts == sorted(counts)

    def test_the_season_label_never_reaches_the_estimator(self) -> None:
        """A season boundary must not change a single fitted parameter."""
        rows = corpus()
        stripped = [
            MatchObservation(
                home_team=o.home_team, away_team=o.away_team,
                home_goals=o.home_goals, away_goals=o.away_goals,
                kickoff=o.kickoff,
            )
            for o in rows
        ]
        cutoff = max(o.kickoff for o in rows) + timedelta(days=1)
        with_label = fit_poisson(rows, data_cutoff=cutoff, as_of=AS_OF)
        without = fit_poisson(stripped, data_cutoff=cutoff, as_of=AS_OF)
        assert with_label.mu == without.mu
        assert with_label.home_advantage == without.home_advantage
        for team in with_label.ratings:
            assert with_label.ratings[team].attack == without.ratings[team].attack


class TestPrefixIsExact:
    def test_the_bisected_training_set_equals_the_filter(self) -> None:
        """`walk_forward` slices by bisection; it must match the filter exactly.

        This is the optimisation that made 1,463 fits per candidate feasible.
        If it ever diverged it would be a silent cutoff change, so the two
        are compared object for object at every cutoff in the corpus.
        """
        rows = sorted(
            corpus(), key=lambda o: (o.kickoff, o.home_team, o.away_team)
        )
        from bisect import bisect_left

        kickoffs = [o.kickoff for o in rows]
        for cutoff in sorted({o.kickoff for o in rows}):
            by_bisect = rows[: bisect_left(kickoffs, cutoff)]
            by_filter = observations_before(rows, cutoff)
            assert by_bisect == by_filter, cutoff


class TestPromotionColdStart:
    def test_a_newly_promoted_club_starts_cold(self) -> None:
        rows = corpus(promote=True)
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        cold = [s for s in report.scored if PROMOTED in s.cold_started]
        assert cold, "the promoted club should be cold-started at first"
        assert all(s.season == SEASON_LABELS[-1] for s in cold)

    def test_a_returning_club_is_not_cold(self) -> None:
        """Only genuinely new clubs are cold: history survives the boundary."""
        report = walk_forward(corpus(promote=True), as_of=AS_OF, config=CONFIG)
        final = [s for s in report.scored if s.season == SEASON_LABELS[-1]]
        cold_names = {t for s in final for t in s.cold_started}
        assert cold_names <= {PROMOTED}

    def test_the_promoted_club_stops_being_cold_once_it_has_history(self) -> None:
        rows = corpus(promote=True)
        report = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        involving = [
            s for s in report.scored
            if PROMOTED in (s.home_team, s.away_team)
        ]
        assert involving
        assert PROMOTED not in involving[-1].cold_started

    def test_its_future_results_never_reach_its_first_prediction(self) -> None:
        """The leakage rule for exactly the case promotion creates."""
        rows = corpus(promote=True)
        first = min(
            (o for o in rows if PROMOTED in (o.home_team, o.away_team)),
            key=lambda o: o.kickoff,
        )
        training = observations_before(rows, first.kickoff)
        assert all(
            PROMOTED not in (o.home_team, o.away_team) for o in training
        ), "the promoted club must have no history before its own first match"


class TestCrossSeasonLeakage:
    def test_a_final_season_result_cannot_move_an_earlier_seasons_fit(self) -> None:
        rows = corpus()
        boundary = min(
            o.kickoff for o in rows if o.season == SEASON_LABELS[1]
        )
        shocks = [
            MatchObservation(
                home_team="alpha", away_team="bravo", home_goals=9,
                away_goals=0, kickoff=boundary + timedelta(days=400),
                season=SEASON_LABELS[2],
            )
        ]
        config = FitConfig(ridge=0.05, min_matches=0)
        before = fit_poisson(
            observations_before(rows, boundary), data_cutoff=boundary,
            as_of=AS_OF, config=config,
        )
        after = fit_poisson(
            observations_before(rows + shocks, boundary), data_cutoff=boundary,
            as_of=AS_OF, config=config,
        )
        assert before.mu == after.mu
        assert before.training_matches == after.training_matches
        a = predict_fixture(before, "alpha", "charlie")
        b = predict_fixture(after, "alpha", "charlie")
        assert a.lambda_home == b.lambda_home
        assert a.markets.one_x_two == b.markets.one_x_two

    def test_decay_weights_never_see_across_the_cutoff(self) -> None:
        """Weights are a function of age from the cutoff, nothing else."""
        rows = corpus()
        cutoff = min(o.kickoff for o in rows if o.season == SEASON_LABELS[2])
        training = observations_before(rows, cutoff)
        weights = weights_for(training, cutoff, DecayConfig(180))
        assert len(weights) == len(training)
        assert all(0.0 < w <= 1.0 for w in weights)
        # An older season weighs less than a newer one, with no discontinuity
        # at the boundary beyond what age alone produces.
        by_season: dict[str, list[float]] = {}
        for o, w in zip(training, weights, strict=True):
            by_season.setdefault(o.season, []).append(w)
        means = [sum(v) / len(v) for _, v in sorted(by_season.items())]
        assert means == sorted(means), "older seasons must not weigh more"

    def test_rho_is_estimated_only_from_legal_history(self) -> None:
        rows = corpus()
        cutoff = min(o.kickoff for o in rows if o.season == SEASON_LABELS[2])
        config = FitConfig(ridge=0.05, min_matches=4, dixon_coles=True)
        a = fit_poisson(
            observations_before(rows, cutoff), data_cutoff=cutoff,
            as_of=AS_OF, config=config,
        )
        future = [
            MatchObservation(
                home_team="alpha", away_team="bravo", home_goals=0,
                away_goals=0, kickoff=cutoff + timedelta(days=30),
                season=SEASON_LABELS[2],
            )
        ] * 20
        b = fit_poisson(
            observations_before(rows + future, cutoff), data_cutoff=cutoff,
            as_of=AS_OF, config=config,
        )
        assert a.rho == b.rho


class TestDeterminism:
    def test_a_rerun_is_identical(self) -> None:
        rows = corpus()
        first = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        second = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        assert first.log_loss == second.log_loss
        assert first.brier == second.brier
        assert [s.probabilities for s in first.scored] == [
            s.probabilities for s in second.scored
        ]

    def test_input_order_does_not_matter(self) -> None:
        rows = corpus()
        forwards = walk_forward(rows, as_of=AS_OF, config=CONFIG)
        backwards = walk_forward(
            list(reversed(rows)), as_of=AS_OF, config=CONFIG
        )
        assert forwards.log_loss == pytest.approx(backwards.log_loss, abs=1e-12)


class TestEvaluationSplit:
    def test_the_split_covers_every_season_exactly_once(self) -> None:
        assert set(DEVELOPMENT) | set(HELD_OUT) == set(ALL_SEASONS)
        assert not (set(DEVELOPMENT) & set(HELD_OUT))
        assert len(ALL_SEASONS) == 6

    def test_development_precedes_the_held_out_seasons(self) -> None:
        assert max(DEVELOPMENT) < min(HELD_OUT)

    def test_selection_reads_development_only(self) -> None:
        """`select` is not given held-out numbers, so it cannot use them."""
        import inspect

        signature = inspect.signature(select)
        assert list(signature.parameters) == ["development", "baseline_label"]

    def test_a_tiny_gain_does_not_qualify(self) -> None:
        base = Slice("A. frozen Poisson", 100, 1.0000, 0.6000, 1.5, 1.2, 1.5, 1.2, 0)
        barely = Slice(
            "F. Dixon-Coles", 100, 1.0000 - MIN_LOG_LOSS_GAIN / 2, 0.5999,
            1.5, 1.2, 1.5, 1.2, 0,
        )
        development = {c.label: base for c in __import__(
            "engine.jobs.multi_season_evaluation", fromlist=["CANDIDATES"]
        ).CANDIDATES}
        development["F. Dixon-Coles"] = barely
        assert select(development, "A. frozen Poisson") is None

    def test_a_clear_gain_qualifies_and_the_simpler_model_wins(self) -> None:
        base = Slice("A. frozen Poisson", 100, 1.0000, 0.6000, 1.5, 1.2, 1.5, 1.2, 0)
        better = Slice("x", 100, 0.9800, 0.5900, 1.5, 1.2, 1.5, 1.2, 0)
        module = __import__(
            "engine.jobs.multi_season_evaluation", fromlist=["CANDIDATES"]
        )
        development = {c.label: base for c in module.CANDIDATES}
        development["E. decay 365d"] = better
        development["G. decay 365d + DC"] = better
        chosen = select(development, "A. frozen Poisson")
        assert chosen is not None
        assert chosen.label == "E. decay 365d", "the simpler of two equals"

    def test_brier_regression_disqualifies(self) -> None:
        base = Slice("A. frozen Poisson", 100, 1.0000, 0.6000, 1.5, 1.2, 1.5, 1.2, 0)
        lopsided = Slice("x", 100, 0.9800, 0.6100, 1.5, 1.2, 1.5, 1.2, 0)
        module = __import__(
            "engine.jobs.multi_season_evaluation", fromlist=["CANDIDATES"]
        )
        development = {c.label: base for c in module.CANDIDATES}
        development["F. Dixon-Coles"] = lopsided
        assert select(development, "A. frozen Poisson") is None

    def test_summarise_reports_the_slice_it_was_given(self) -> None:
        report = walk_forward(corpus(), as_of=AS_OF, config=CONFIG)
        one = [s for s in report.scored if s.season == SEASON_LABELS[1]]
        summary = summarise(SEASON_LABELS[1], one)
        assert summary.predictions == len(one)
        assert summary.actual_home == pytest.approx(
            sum(s.actual_home for s in one) / len(one)
        )

    def test_every_candidate_scores_identical_fixtures(self) -> None:
        module = __import__(
            "engine.jobs.multi_season_evaluation", fromlist=["CANDIDATES"]
        )
        rows = corpus()
        reference: list[tuple[datetime, str, str]] | None = None
        for candidate in module.CANDIDATES:
            report = walk_forward(rows, as_of=AS_OF, config=candidate.config())
            keys = [(s.kickoff, s.home_team, s.away_team) for s in report.scored]
            if reference is None:
                reference = keys
            assert keys == reference, candidate.label
