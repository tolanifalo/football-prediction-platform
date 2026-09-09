"""Recency weighting (MODEL-EXPERIMENTS.md §2).

The claim that matters most is negative: with decay off, the weighted
estimator must reproduce the frozen baseline BIT FOR BIT, not approximately.
If it did not, every experimental comparison in this phase would be measuring
two changes at once.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from engine.model.decay import DecayConfig, age_in_days, weights_for
from engine.model.fit import FitConfig, MatchObservation, fit_poisson

START = datetime(2024, 8, 1, 15, 0, tzinfo=UTC)
CUTOFF = datetime(2025, 2, 1, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta"]


def obs(home: str, away: str, hg: int, ag: int, day: int) -> MatchObservation:
    return MatchObservation(
        home_team=home, away_team=away, home_goals=hg, away_goals=ag,
        kickoff=START + timedelta(days=day),
    )


SEASON = [
    obs("alpha", "bravo", 2, 1, 1),
    obs("charlie", "delta", 1, 1, 2),
    obs("bravo", "alpha", 0, 2, 20),
    obs("delta", "charlie", 2, 0, 21),
    obs("alpha", "charlie", 3, 0, 60),
    obs("bravo", "delta", 1, 2, 61),
    obs("charlie", "alpha", 1, 2, 120),
    obs("delta", "bravo", 1, 0, 121),
]


class TestWeightFunction:
    def test_a_match_at_the_cutoff_weighs_one(self) -> None:
        assert DecayConfig(half_life_days=90).weight_for_age(0.0) == 1.0

    def test_the_half_life_halves_the_weight(self) -> None:
        """The whole reason for choosing this parameterisation."""
        config = DecayConfig(half_life_days=90)
        assert config.weight_for_age(90.0) == pytest.approx(0.5, abs=1e-15)
        assert config.weight_for_age(180.0) == pytest.approx(0.25, abs=1e-15)
        assert config.weight_for_age(270.0) == pytest.approx(0.125, abs=1e-15)

    def test_weight_declines_monotonically_with_age(self) -> None:
        config = DecayConfig(half_life_days=90)
        weights = [config.weight_for_age(float(d)) for d in range(0, 400, 10)]
        assert weights == sorted(weights, reverse=True)
        assert all(0.0 < w <= 1.0 for w in weights)

    def test_a_shorter_half_life_forgets_faster(self) -> None:
        age = 100.0
        assert DecayConfig(30).weight_for_age(age) < DecayConfig(90).weight_for_age(age)
        assert (
            DecayConfig(90).weight_for_age(age)
            < DecayConfig(365).weight_for_age(age)
        )

    def test_disabled_decay_returns_exactly_one(self) -> None:
        """Exactly 1.0, not approximately - the baseline depends on it."""
        config = DecayConfig()
        assert config.enabled is False
        for age in (0.0, 1.0, 500.0, 10_000.0):
            assert config.weight_for_age(age) == 1.0

    def test_a_negative_age_is_refused(self) -> None:
        """A match after the cutoff would be up-weighted, not down-weighted."""
        with pytest.raises(ValueError, match="future"):
            DecayConfig(90).weight_for_age(-1.0)

    def test_a_non_positive_half_life_is_refused(self) -> None:
        for bad in (0.0, -30.0):
            with pytest.raises(ValueError, match="positive"):
                DecayConfig(half_life_days=bad)

    def test_weights_are_deterministic(self) -> None:
        first = weights_for(SEASON, CUTOFF, DecayConfig(90))
        second = weights_for(SEASON, CUTOFF, DecayConfig(90))
        assert first == second

    def test_age_is_measured_from_the_cutoff(self) -> None:
        assert age_in_days(START, START + timedelta(days=7)) == pytest.approx(7.0)
        early = weights_for(SEASON, CUTOFF, DecayConfig(90))
        later = weights_for(SEASON, CUTOFF + timedelta(days=90), DecayConfig(90))
        # The same match is older, so lighter, seen from a later cutoff.
        assert all(b < a for a, b in zip(early, later, strict=True))


class TestWeightedFitMatchesTheBaseline:
    def test_no_decay_is_bit_identical_to_the_frozen_estimator(self) -> None:
        """THE regression guard for the whole experiment phase."""
        default = fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF)
        explicit = fit_poisson(
            SEASON, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(decay=DecayConfig(half_life_days=None)),
        )
        assert default.mu == explicit.mu
        assert default.home_advantage == explicit.home_advantage
        assert default.log_likelihood == explicit.log_likelihood
        assert default.sweeps == explicit.sweeps
        for team in default.ratings:
            assert default.ratings[team].attack == explicit.ratings[team].attack
            assert default.ratings[team].defence == explicit.ratings[team].defence

    def test_the_default_config_estimates_no_rho(self) -> None:
        assert fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF).rho is None


class TestWeightedFit:
    def test_decay_changes_the_fit(self) -> None:
        """Guard the guard: if weighting did nothing there is no experiment."""
        plain = fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF)
        decayed = fit_poisson(
            SEASON, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(decay=DecayConfig(30)),
        )
        assert decayed.mu != plain.mu

    def test_a_weighted_fit_is_deterministic(self) -> None:
        config = FitConfig(decay=DecayConfig(45))
        a = fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF, config=config)
        b = fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF, config=config)
        assert a.mu == b.mu and a.log_likelihood == b.log_likelihood
        for team in a.ratings:
            assert a.ratings[team].attack == b.ratings[team].attack

    def test_row_order_does_not_change_a_weighted_fit(self) -> None:
        config = FitConfig(decay=DecayConfig(45))
        a = fit_poisson(SEASON, data_cutoff=CUTOFF, as_of=AS_OF, config=config)
        b = fit_poisson(
            list(reversed(SEASON)), data_cutoff=CUTOFF, as_of=AS_OF, config=config
        )
        assert a.mu == pytest.approx(b.mu, abs=1e-12)

    def test_the_weighted_fit_still_centres_its_parameters(self) -> None:
        model = fit_poisson(
            SEASON, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(decay=DecayConfig(30), min_matches=0),
        )
        assert sum(r.attack for r in model.ratings.values()) == pytest.approx(
            0.0, abs=1e-12
        )
        assert sum(r.defence for r in model.ratings.values()) == pytest.approx(
            0.0, abs=1e-12
        )

    def test_decay_still_refuses_an_observation_at_the_cutoff(self) -> None:
        with pytest.raises(ValueError, match="strictly before"):
            fit_poisson(
                SEASON,
                data_cutoff=SEASON[0].kickoff,
                as_of=AS_OF,
                config=FitConfig(decay=DecayConfig(30)),
            )

    def test_the_decay_configuration_is_in_the_metadata(self) -> None:
        model = fit_poisson(
            SEASON, data_cutoff=CUTOFF, as_of=AS_OF,
            config=FitConfig(decay=DecayConfig(90)),
        )
        config = model.as_metadata()["config"]
        assert isinstance(config, dict)
        assert config["decay"] == {"half_life_days": 90, "label": "90d"}

    def test_recent_form_dominates_under_aggressive_decay(self) -> None:
        """A team that was poor and became good should rate better with decay."""
        rows = [
            obs("riser", "other", 0, 3, 1),
            obs("other", "riser", 3, 0, 2),
            obs("riser", "third", 0, 2, 3),
            obs("third", "riser", 2, 0, 4),
            obs("riser", "other", 4, 0, 150),
            obs("other", "riser", 0, 4, 151),
            obs("riser", "third", 4, 0, 152),
            obs("third", "riser", 0, 4, 153),
            obs("other", "third", 1, 1, 80),
            obs("third", "other", 1, 1, 81),
        ]
        cutoff = START + timedelta(days=200)
        config = FitConfig(min_matches=0)
        plain = fit_poisson(rows, data_cutoff=cutoff, as_of=AS_OF, config=config)
        decayed = fit_poisson(
            rows, data_cutoff=cutoff, as_of=AS_OF,
            config=FitConfig(min_matches=0, decay=DecayConfig(30)),
        )
        assert decayed.ratings["riser"].attack > plain.ratings["riser"].attack


class TestWeightedLeakage:
    def test_a_future_result_cannot_reach_an_earlier_weighted_fit(self) -> None:
        """The leakage rule, re-proved with weighting switched on."""
        cutoff = START + timedelta(days=100)
        future = [
            obs("alpha", "bravo", 9, 0, 300),
            obs("alpha", "charlie", 9, 0, 301),
        ]
        config = FitConfig(decay=DecayConfig(30), min_matches=0)
        before = fit_poisson(
            [o for o in SEASON if o.kickoff < cutoff],
            data_cutoff=cutoff, as_of=AS_OF, config=config,
        )
        after = fit_poisson(
            [o for o in SEASON + future if o.kickoff < cutoff],
            data_cutoff=cutoff, as_of=AS_OF, config=config,
        )
        assert before.mu == after.mu
        assert before.home_advantage == after.home_advantage
        assert before.training_matches == after.training_matches
        for team in before.ratings:
            assert before.ratings[team].attack == after.ratings[team].attack
            assert before.ratings[team].defence == after.ratings[team].defence

    def test_weights_never_reference_a_future_match(self) -> None:
        cutoff = START + timedelta(days=100)
        training = [o for o in SEASON if o.kickoff < cutoff]
        weights = weights_for(training, cutoff, DecayConfig(30))
        assert len(weights) == len(training)
        assert all(math.isfinite(w) and 0.0 < w <= 1.0 for w in weights)
