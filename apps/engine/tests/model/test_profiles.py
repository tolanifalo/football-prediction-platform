"""The adopted production configuration (MODEL-BASELINE.md §2).

These tests exist to stop the production model changing by accident. The
180-day half-life was selected by a predeclared rule and confirmed once
against held-out seasons; if someone edits it, that evidence no longer
describes what runs, and these assertions are what says so.

They also pin the CONTROL. It is the benchmark every future model has to
beat, so "unweighted, no correction" has to keep meaning exactly that.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from engine.model.decay import DecayConfig
from engine.model.fit import (
    MODEL_FAMILY,
    MODEL_VERSION,
    FitConfig,
    MatchObservation,
    fit_poisson,
)
from engine.model.predict import predict_fixture
from engine.model.profiles import (
    CONTROL,
    DEFAULT_PROFILE,
    EXPERIMENTAL,
    PRODUCTION,
    PRODUCTION_HALF_LIFE_DAYS,
    PROFILES,
    profile,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
START = datetime(2024, 8, 1, 15, 0, tzinfo=UTC)
CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)
TEAMS = ["alpha", "bravo", "charlie", "delta"]


def season() -> list[MatchObservation]:
    rows: list[MatchObservation] = []
    day = 0
    for home in TEAMS:
        for away in TEAMS:
            if home == away:
                continue
            day += 1
            rows.append(
                MatchObservation(
                    home_team=home, away_team=away,
                    home_goals=(day % 3), away_goals=((day + 1) % 3),
                    kickoff=START + timedelta(days=day * 5),
                )
            )
    return rows


class TestProductionDefault:
    def test_the_default_profile_is_production(self) -> None:
        assert DEFAULT_PROFILE is PRODUCTION
        assert PRODUCTION.name == "production"

    def test_production_uses_a_180_day_half_life(self) -> None:
        """THE adopted value. Changing it is a new evaluation, not an edit."""
        assert PRODUCTION_HALF_LIFE_DAYS == 180.0
        assert PRODUCTION.fit.decay.enabled is True
        assert PRODUCTION.fit.decay.half_life_days == 180.0

    def test_production_does_not_enable_dixon_coles(self) -> None:
        """It was evaluated and NOT adopted; nothing may switch it on here."""
        assert PRODUCTION.fit.dixon_coles is False

    def test_production_keeps_the_shared_cold_start_policy(self) -> None:
        assert PRODUCTION.fit.min_matches == CONTROL.fit.min_matches == 4

    def test_production_keeps_the_shared_ridge(self) -> None:
        assert PRODUCTION.fit.ridge == CONTROL.fit.ridge

    def test_production_actually_weights_its_training_data(self) -> None:
        rows = season()
        produced = fit_poisson(
            rows, data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit
        )
        controlled = fit_poisson(
            rows, data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit
        )
        assert produced.mu != controlled.mu, "production must not equal control"


class TestControlPreserved:
    def test_control_is_unweighted(self) -> None:
        assert CONTROL.fit.decay.enabled is False
        assert CONTROL.fit.decay.half_life_days is None

    def test_control_applies_no_correction(self) -> None:
        assert CONTROL.fit.dixon_coles is False
        model = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit
        )
        assert model.rho is None

    def test_control_matches_the_bare_estimator_defaults(self) -> None:
        """The frozen mathematics, unchanged by the adoption."""
        rows = season()
        bare = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF)
        controlled = fit_poisson(
            rows, data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit
        )
        assert bare.mu == controlled.mu
        assert bare.home_advantage == controlled.home_advantage
        assert bare.log_likelihood == controlled.log_likelihood
        for team in bare.ratings:
            assert bare.ratings[team].attack == controlled.ratings[team].attack
            assert bare.ratings[team].defence == controlled.ratings[team].defence

    def test_control_is_reachable_by_name(self) -> None:
        assert profile("control") is CONTROL

    def test_control_output_is_reproducible(self) -> None:
        rows = season()
        a = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit)
        b = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit)
        assert a.mu == b.mu and a.log_likelihood == b.log_likelihood


class TestExperimentalStillAvailable:
    @pytest.mark.parametrize(
        "name",
        ["dixon-coles", "decay-30d", "decay-90d", "decay-365d",
         "decay-365d-dixon-coles"],
    )
    def test_each_experimental_profile_is_reachable(self, name: str) -> None:
        assert profile(name) is EXPERIMENTAL[name]

    def test_dixon_coles_is_available_but_never_the_default(self) -> None:
        assert EXPERIMENTAL["dixon-coles"].fit.dixon_coles is True
        assert DEFAULT_PROFILE.fit.dixon_coles is False

    def test_no_shipped_profile_other_than_the_experimental_ones_uses_dc(
        self,
    ) -> None:
        for name, entry in PROFILES.items():
            if entry.fit.dixon_coles:
                assert name in EXPERIMENTAL, f"{name} switched on Dixon-Coles"

    def test_an_unknown_profile_is_refused_rather_than_defaulted(self) -> None:
        with pytest.raises(KeyError, match="unknown model profile"):
            profile("no-such-model")

    def test_every_profile_carries_its_own_name(self) -> None:
        for name, entry in PROFILES.items():
            assert entry.name == name
            assert entry.fit.profile == name


class TestMetadata:
    def test_a_production_fit_records_the_half_life(self) -> None:
        meta = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=AS_OF,
            scope="england-premier-league:2024/25", config=PRODUCTION.fit,
        ).as_metadata()
        assert meta["model_family"] == MODEL_FAMILY
        assert meta["model_version"] == MODEL_VERSION
        assert meta["profile"] == "production"
        assert meta["decay_enabled"] is True
        assert meta["decay_half_life_days"] == 180.0
        assert meta["dixon_coles_enabled"] is False
        assert meta["cold_start_min_matches"] == 4
        assert meta["scope"] == "england-premier-league:2024/25"
        assert meta["data_cutoff"] == CUTOFF.isoformat()
        assert meta["training_matches"] == len(season())

    def test_a_control_fit_records_that_decay_is_off(self) -> None:
        meta = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=AS_OF, config=CONTROL.fit
        ).as_metadata()
        assert meta["profile"] == "control"
        assert meta["decay_enabled"] is False
        assert meta["decay_half_life_days"] is None

    def test_the_profile_itself_serialises(self) -> None:
        meta = PRODUCTION.as_metadata()
        assert meta["profile"] == "production"
        assert meta["model_family"] == MODEL_FAMILY
        assert meta["decay"] == {"half_life_days": 180.0, "label": "180d"}

    def test_the_model_version_did_not_move(self) -> None:
        """Adoption changed a configuration, not the estimator."""
        assert MODEL_VERSION == "poisson-independent@1.0.0"


class TestNoOddsDependency:
    def test_no_model_module_imports_anything_odds_related(self) -> None:
        """Non-negotiable rule 1, asserted over the whole model package."""
        package = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "engine" / "model"
        )
        banned = {"odds", "bookmaker", "price", "value"}
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(token in name.lower() for token in banned):
                        offenders.append(f"{path.name} imports {name}")
        assert offenders == []

    def test_the_repository_query_selects_no_price_column(self) -> None:
        source = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "engine" / "model" / "repository.py"
        ).read_text(encoding="utf-8")
        lowered = source.lower()
        for token in ("odds_ticks", "odds_series", "bookmaker", "price"):
            assert token not in lowered, token

    def test_a_prediction_exposes_no_price(self) -> None:
        model = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit
        )
        meta = predict_fixture(model, "alpha", "bravo").as_metadata()
        assert not any(
            token in key.lower()
            for key in meta
            for token in ("odds", "price", "bookmaker")
        )


class TestDeterminism:
    def test_the_production_configuration_is_deterministic(self) -> None:
        rows = season()
        a = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit)
        b = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit)
        assert a.mu == b.mu
        assert a.home_advantage == b.home_advantage
        assert a.log_likelihood == b.log_likelihood
        for team in a.ratings:
            assert a.ratings[team].attack == b.ratings[team].attack

    def test_production_predictions_are_deterministic(self) -> None:
        rows = season()
        model = fit_poisson(
            rows, data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit
        )
        first = predict_fixture(model, "alpha", "bravo")
        second = predict_fixture(model, "alpha", "bravo")
        assert first.markets.one_x_two == second.markets.one_x_two
        assert first.lambda_home == second.lambda_home

    def test_an_explicit_180_day_config_equals_the_production_profile(
        self,
    ) -> None:
        rows = season()
        explicit = FitConfig(decay=DecayConfig(180.0), profile="production")
        a = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=explicit)
        b = fit_poisson(rows, data_cutoff=CUTOFF, as_of=AS_OF, config=PRODUCTION.fit)
        assert a.mu == b.mu
        assert a.log_likelihood == b.log_likelihood
