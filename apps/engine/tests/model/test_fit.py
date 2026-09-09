"""The estimator (MODEL-BASELINE.md §2).

No database. Every observation is constructed here, which is what makes the
cutoff claims checkable: if a match is not in the list it cannot have moved a
parameter, and the list is visible in the test.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from engine.model.fit import (
    FitConfig,
    InsufficientHistory,
    MatchObservation,
    fit_poisson,
    observations_before,
)
from engine.model.predict import expected_goals

SEASON_START = datetime(2024, 8, 1, 15, 0, tzinfo=UTC)
CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

TEAMS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def match(
    home: str, away: str, hg: int, ag: int, day: int
) -> MatchObservation:
    return MatchObservation(
        home_team=home,
        away_team=away,
        home_goals=hg,
        away_goals=ag,
        kickoff=SEASON_START + timedelta(days=day),
    )


def double_round_robin(
    strengths: dict[str, float], *, home_edge: float = 0.3, seed: int = 7
) -> list[MatchObservation]:
    """A full season generated from KNOWN parameters, deterministically.

    Poisson draws come from a seeded `random.Random` via Knuth's method, so the
    dataset is fixed forever and the fit can be checked for recovering the
    strengths that produced it.
    """
    rng = random.Random(seed)

    def draw(lam: float) -> int:
        target = math.exp(-lam)
        product = 1.0
        k = 0
        while True:
            product *= rng.random()
            if product <= target:
                return k
            k += 1

    out: list[MatchObservation] = []
    day = 0
    names = sorted(strengths)
    for home in names:
        for away in names:
            if home == away:
                continue
            lam_h = math.exp(0.1 + home_edge + strengths[home] - strengths[away])
            lam_a = math.exp(0.1 + strengths[away] - strengths[home])
            out.append(match(home, away, draw(lam_h), draw(lam_a), day))
            day += 1
    return out


BALANCED = [
    match("alpha", "bravo", 2, 1, 1),
    match("charlie", "delta", 1, 1, 2),
    match("bravo", "alpha", 0, 2, 3),
    match("delta", "charlie", 2, 0, 4),
    match("alpha", "charlie", 3, 0, 5),
    match("bravo", "delta", 1, 2, 6),
    match("charlie", "alpha", 1, 2, 7),
    match("delta", "bravo", 1, 0, 8),
]


class TestDeterminism:
    def test_the_same_input_gives_bit_identical_parameters(self) -> None:
        a = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        b = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        assert a.mu == b.mu
        assert a.home_advantage == b.home_advantage
        for team in a.ratings:
            assert a.ratings[team].attack == b.ratings[team].attack
            assert a.ratings[team].defence == b.ratings[team].defence

    def test_row_order_does_not_change_the_fit(self) -> None:
        """A fit that depended on arrival order would not be reproducible."""
        shuffled = list(reversed(BALANCED))
        a = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        b = fit_poisson(shuffled, data_cutoff=CUTOFF, as_of=AS_OF)
        assert a.mu == pytest.approx(b.mu, abs=1e-12)
        for team in a.ratings:
            assert a.ratings[team].attack == pytest.approx(
                b.ratings[team].attack, abs=1e-12
            )

    def test_metadata_is_serialisable_and_reproduces_the_fit(self) -> None:
        model = fit_poisson(
            BALANCED, data_cutoff=CUTOFF, as_of=AS_OF, scope="test:2024/25"
        )
        meta = model.as_metadata()
        assert meta["model_version"] == "poisson-independent@1.0.0"
        assert meta["scope"] == "test:2024/25"
        assert meta["data_cutoff"] == CUTOFF.isoformat()
        assert meta["as_of"] == AS_OF.isoformat()
        assert meta["training_matches"] == len(BALANCED)
        assert isinstance(meta["config"], dict)
        assert isinstance(meta["ratings"], dict)


class TestIdentifiability:
    def test_attack_and_defence_are_centred_on_zero(self) -> None:
        """The two invariances are pinned, so the parameters are unique."""
        model = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        attacks = [r.attack for r in model.ratings.values()]
        defences = [r.defence for r in model.ratings.values()]
        assert sum(attacks) == pytest.approx(0.0, abs=1e-12)
        assert sum(defences) == pytest.approx(0.0, abs=1e-12)

    def test_centring_leaves_every_lambda_unchanged(self) -> None:
        """Absorbing the shift into mu is a reparameterisation, not a change."""
        model = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        lam_h, lam_a = expected_goals(model, "alpha", "bravo")
        # Rebuilt by hand from the published parameters.
        home = model.ratings["alpha"]
        away = model.ratings["bravo"]
        assert lam_h == pytest.approx(
            math.exp(model.mu + model.home_advantage + home.attack - away.defence),
            abs=1e-12,
        )
        assert lam_a == pytest.approx(
            math.exp(model.mu + away.attack - home.defence), abs=1e-12
        )

    def test_the_unpenalised_fit_satisfies_its_first_order_conditions(self) -> None:
        """At the MLE, each team's goals scored equals its expected goals.

        This is an exact property of the Poisson likelihood, so it checks the
        optimiser against mathematics rather than against its own output.
        """
        model = fit_poisson(
            BALANCED,
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(ridge=0.0, min_matches=0, tolerance=1e-13),
        )
        for team in {o.home_team for o in BALANCED}:
            scored = 0
            expected = 0.0
            for o in BALANCED:
                lam_h, lam_a = expected_goals(model, o.home_team, o.away_team)
                if o.home_team == team:
                    scored += o.home_goals
                    expected += lam_h
                elif o.away_team == team:
                    scored += o.away_goals
                    expected += lam_a
            assert expected == pytest.approx(scored, rel=1e-6)


class TestParameterMeaning:
    def test_a_prolific_team_gets_a_higher_attack_rating(self) -> None:
        model = fit_poisson(
            double_round_robin({"alpha": 0.6, "bravo": 0.0, "charlie": -0.6}),
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(min_matches=0),
        )
        assert model.ratings["alpha"].attack > model.ratings["charlie"].attack

    def test_defence_is_positive_for_a_team_that_concedes_few(self) -> None:
        """Defence enters lambda with a MINUS sign: higher means meaner."""
        stingy = [
            match("wall", "leaky", 1, 0, 1),
            match("leaky", "wall", 3, 0, 2),
            match("wall", "mid", 1, 0, 3),
            match("mid", "wall", 2, 0, 4),
            match("leaky", "mid", 1, 3, 5),
            match("mid", "leaky", 3, 1, 6),
        ]
        model = fit_poisson(
            stingy,
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(min_matches=0),
        )
        assert model.ratings["wall"].defence > model.ratings["leaky"].defence

    def test_home_advantage_equals_the_log_goal_ratio_exactly(self) -> None:
        """An identity, not a tolerance, for a complete double round robin.

        Every team hosts and visits every other, so the attack and defence
        terms appear identically on both sides of the ledger and cancel. The
        first-order condition then forces exp(gamma) = home goals / away
        goals. It holds to machine precision regardless of sample size, which
        makes it a far better check than "gamma is roughly what generated the
        data" - 30 matches can and does draw a 55-24 split from a fair 0.4.
        """
        league = [f"team{i:02d}" for i in range(14)]
        data = double_round_robin(dict.fromkeys(league, 0.0), home_edge=0.4, seed=11)
        model = fit_poisson(
            data,
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(ridge=0.0, min_matches=0),
        )
        home_goals = sum(o.home_goals for o in data)
        away_goals = sum(o.away_goals for o in data)
        assert model.home_advantage == pytest.approx(
            math.log(home_goals / away_goals), abs=1e-9
        )
        # And with 182 matches the estimate is near the truth that made it.
        assert 0.4 < model.home_advantage < 0.9

    def test_home_advantage_is_positive_when_hosts_score_more(self) -> None:
        model = fit_poisson(
            double_round_robin(dict.fromkeys(TEAMS, 0.0), home_edge=0.4),
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(ridge=0.0, min_matches=0),
        )
        assert model.home_advantage > 0.0

    def test_the_fit_recovers_the_strengths_that_generated_the_data(self) -> None:
        truth = {"alpha": 0.5, "bravo": 0.25, "charlie": 0.0,
                 "delta": -0.25, "echo": -0.5, "foxtrot": -0.6}
        model = fit_poisson(
            double_round_robin(truth),
            data_cutoff=CUTOFF,
            as_of=AS_OF,
            config=FitConfig(ridge=0.01, min_matches=0),
        )
        ranked = sorted(
            model.ratings, key=lambda t: model.ratings[t].attack, reverse=True
        )
        # Ordering, not exact values: 30 matches is a small sample and the
        # point is that the estimator is not scrambling the signal.
        assert ranked[0] in {"alpha", "bravo"}
        assert ranked[-1] in {"echo", "foxtrot"}


class TestRegularisation:
    def test_more_ridge_shrinks_ratings_toward_league_average(self) -> None:
        data = double_round_robin({"alpha": 0.8, "bravo": 0.0, "charlie": -0.8})
        loose = fit_poisson(
            data, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(ridge=0.001, min_matches=0),
        )
        tight = fit_poisson(
            data, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(ridge=5.0, min_matches=0),
        )
        spread_loose = max(r.attack for r in loose.ratings.values()) - min(
            r.attack for r in loose.ratings.values()
        )
        spread_tight = max(r.attack for r in tight.ratings.values()) - min(
            r.attack for r in tight.ratings.values()
        )
        assert spread_tight < spread_loose

    def test_regularisation_is_deterministic(self) -> None:
        data = double_round_robin({"alpha": 0.4, "bravo": 0.0, "charlie": -0.4})
        config = FitConfig(ridge=0.3, min_matches=0)
        a = fit_poisson(data, data_cutoff=CUTOFF, as_of=AS_OF, config=config)
        b = fit_poisson(data, data_cutoff=CUTOFF, as_of=AS_OF, config=config)
        assert a.ratings["alpha"].attack == b.ratings["alpha"].attack

    def test_a_goalless_team_still_produces_a_finite_fit(self) -> None:
        """Unpenalised, this team's attack maximum is minus infinity."""
        data = [
            match("blank", "other", 0, 2, 1),
            match("other", "blank", 3, 0, 2),
            match("blank", "third", 0, 1, 3),
            match("third", "blank", 2, 0, 4),
            match("other", "third", 1, 1, 5),
            match("third", "other", 2, 2, 6),
        ]
        model = fit_poisson(
            data, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(ridge=0.05, min_matches=0),
        )
        assert math.isfinite(model.ratings["blank"].attack)
        lam_h, lam_a = expected_goals(model, "blank", "other")
        assert math.isfinite(lam_h) and lam_h > 0.0


class TestCutoffEnforcement:
    def test_an_observation_at_the_cutoff_is_rejected(self) -> None:
        """Strictly before. A match kicking off AT the cutoff has not finished."""
        cutoff = SEASON_START + timedelta(days=5)
        data = [*BALANCED[:3], match("alpha", "delta", 9, 0, 5)]
        with pytest.raises(ValueError, match="strictly before"):
            fit_poisson(data, data_cutoff=cutoff, as_of=AS_OF)

    def test_an_observation_after_the_cutoff_is_rejected(self) -> None:
        cutoff = SEASON_START + timedelta(days=4)
        with pytest.raises(ValueError, match="strictly before"):
            fit_poisson(BALANCED, data_cutoff=cutoff, as_of=AS_OF)

    def test_observations_before_keeps_only_earlier_kickoffs(self) -> None:
        cutoff = SEASON_START + timedelta(days=5)
        kept = observations_before(BALANCED, cutoff)
        assert len(kept) == 4
        assert all(o.kickoff < cutoff for o in kept)

    def test_observations_before_returns_chronological_order(self) -> None:
        kept = observations_before(list(reversed(BALANCED)), CUTOFF)
        assert [o.kickoff for o in kept] == sorted(o.kickoff for o in kept)

    def test_an_empty_training_window_refuses_rather_than_guesses(self) -> None:
        with pytest.raises(InsufficientHistory):
            fit_poisson([], data_cutoff=CUTOFF, as_of=AS_OF)


class TestColdStart:
    def test_a_thin_team_is_set_to_league_average_and_flagged(self) -> None:
        data = [*BALANCED, match("newcomer", "alpha", 5, 0, 9)]
        model = fit_poisson(
            data, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(min_matches=4),
        )
        rating = model.ratings["newcomer"]
        assert rating.cold_start is True
        assert rating.attack == 0.0 and rating.defence == 0.0
        assert rating.matches == 1
        assert "newcomer" in model.cold_started

    def test_a_five_nil_win_does_not_make_a_thin_team_look_strong(self) -> None:
        """The whole point: one freak result must not become a rating."""
        data = [*BALANCED, match("newcomer", "alpha", 5, 0, 9)]
        model = fit_poisson(
            data, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(min_matches=4),
        )
        lam_newcomer, _ = expected_goals(model, "newcomer", "bravo")
        lam_average, _ = expected_goals(model, "unheard-of", "bravo")
        assert lam_newcomer == pytest.approx(lam_average, abs=1e-12)

    def test_a_team_never_seen_is_league_average_and_says_so(self) -> None:
        model = fit_poisson(BALANCED, data_cutoff=CUTOFF, as_of=AS_OF)
        rating = model.rating_for("who")
        assert rating.cold_start is True
        assert rating.matches == 0
        assert rating.attack == 0.0

    def test_a_well_observed_team_is_not_cold_started(self) -> None:
        model = fit_poisson(
            BALANCED, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(min_matches=4),
        )
        assert model.ratings["alpha"].cold_start is False
        assert "alpha" not in model.cold_started
