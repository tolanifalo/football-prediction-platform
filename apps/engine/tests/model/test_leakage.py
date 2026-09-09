"""THE MANDATORY LEAKAGE TEST (MODEL-BASELINE.md §1).

The construction is adversarial on purpose. A team plays an ordinary first
half-season, then wins 9-0, 9-0, 9-0. Those three results move its attack
rating enormously - the test asserts that they do, because a leakage test that
passes because the future data was harmless proves nothing at all.

Then the same earlier fixture is predicted twice: once against a database that
stops before the blowouts, once against a database that contains them. The two
predictions must be IDENTICAL TO THE BIT.

This is the test that would fail if someone ever fits on the full season and
scores it, which is the single most common way a football model is accidentally
made to look brilliant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.model.fit import (
    FitConfig,
    FittedModel,
    MatchObservation,
    fit_poisson,
    observations_before,
)
from engine.model.predict import predict_fixture

START = datetime(2024, 8, 10, 15, 0, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta", "echo"]


def at(day: int) -> datetime:
    return START + timedelta(days=day)


def obs(home: str, away: str, hg: int, ag: int, day: int) -> MatchObservation:
    return MatchObservation(
        home_team=home, away_team=away, home_goals=hg, away_goals=ag, kickoff=at(day)
    )


def past() -> list[MatchObservation]:
    """An unremarkable early season: everybody near the league average."""
    rows: list[MatchObservation] = []
    day = 0
    for home in TEAMS:
        for away in TEAMS:
            if home == away:
                continue
            day += 1
            rows.append(obs(home, away, 1 + (day % 2), 1, day))
    return rows


#: The fixture under prediction, after the ordinary run and before the shocks.
TARGET_DAY = 100
FUTURE_SHOCKS = [
    obs("alpha", "bravo", 9, 0, 200),
    obs("alpha", "charlie", 9, 0, 201),
    obs("alpha", "delta", 9, 0, 202),
]

CONFIG = FitConfig(ridge=0.05, min_matches=0)


def fit_to(rows: list[MatchObservation], cutoff: datetime) -> FittedModel:
    return fit_poisson(
        observations_before(rows, cutoff),
        data_cutoff=cutoff,
        as_of=AS_OF,
        config=CONFIG,
    )


class TestFutureResultsCannotReachThePast:
    def test_the_future_results_really_are_dramatic(self) -> None:
        """Guard the guard: a harmless future would prove nothing.

        Measured on this dataset, the three blowouts lift alpha's attack
        rating by 0.67 on the log scale - which is what matters, a 95% rise in
        its expected goals. The assertion is on that ratio rather than on the
        raw parameter, because "dramatic" is a claim about predictions.
        """
        without = fit_poisson(
            past(), data_cutoff=at(150), as_of=AS_OF, config=CONFIG
        )
        with_shocks = fit_poisson(
            past() + FUTURE_SHOCKS, data_cutoff=at(300), as_of=AS_OF, config=CONFIG
        )
        quiet = predict_fixture(without, "alpha", "echo").lambda_home
        loud = predict_fixture(with_shocks, "alpha", "echo").lambda_home
        assert loud > 1.7 * quiet, f"future data only moved lambda {quiet}->{loud}"

    def test_an_earlier_prediction_is_bit_identical_with_and_without(self) -> None:
        """THE test. Adding the future must change nothing before it."""
        cutoff = at(TARGET_DAY)
        before = fit_to(past(), cutoff)
        after = fit_to(past() + FUTURE_SHOCKS, cutoff)

        prediction_before = predict_fixture(before, "alpha", "echo")
        prediction_after = predict_fixture(after, "alpha", "echo")

        assert prediction_before.lambda_home == prediction_after.lambda_home
        assert prediction_before.lambda_away == prediction_after.lambda_away
        assert prediction_before.markets.one_x_two == prediction_after.markets.one_x_two
        assert prediction_before.markets.btts_yes == prediction_after.markets.btts_yes
        assert (
            prediction_before.markets.over[2.5] == prediction_after.markets.over[2.5]
        )

    def test_every_fitted_parameter_is_bit_identical_too(self) -> None:
        cutoff = at(TARGET_DAY)
        before = fit_to(past(), cutoff)
        after = fit_to(past() + FUTURE_SHOCKS, cutoff)
        assert before.mu == after.mu
        assert before.home_advantage == after.home_advantage
        assert before.training_matches == after.training_matches
        for team in TEAMS:
            a = before.ratings[team]
            b = after.ratings[team]
            assert a.attack == b.attack and a.defence == b.defence

    def test_the_target_fixture_is_never_in_its_own_training_set(self) -> None:
        """A fixture at the cutoff is excluded, so it cannot train on itself."""
        target = obs("alpha", "echo", 4, 0, TARGET_DAY)
        rows = [*past(), target]
        training = observations_before(rows, target.kickoff)
        assert target not in training
        assert all(o.kickoff < target.kickoff for o in training)

    def test_the_estimator_refuses_a_training_set_containing_the_future(self) -> None:
        """Belt as well as braces: the fit rejects what the filter would drop."""
        cutoff = at(TARGET_DAY)
        with pytest.raises(ValueError, match="strictly before"):
            fit_poisson(
                past() + FUTURE_SHOCKS,
                data_cutoff=cutoff,
                as_of=AS_OF,
                config=CONFIG,
            )

    def test_a_later_prediction_does_change_once_the_shocks_are_past(self) -> None:
        """The mirror image: information legitimately available must be used.

        Without this, a model that ignored all data would pass every test
        above.
        """
        early = fit_to(past() + FUTURE_SHOCKS, at(TARGET_DAY))
        late = fit_to(past() + FUTURE_SHOCKS, at(300))
        early_prediction = predict_fixture(early, "alpha", "echo")
        late_prediction = predict_fixture(late, "alpha", "echo")
        assert late_prediction.lambda_home > early_prediction.lambda_home * 1.5
