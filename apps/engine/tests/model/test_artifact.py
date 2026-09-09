"""Probability correctness of the persisted artifact (PREDICTIONS.md §1).

NO DATABASE. Everything here is a claim about what a valid prediction IS, so
it is checked on the pure record before PostgreSQL is anywhere near it. The
CHECK constraints assert the same rules again at the storage boundary; this
suite is what says which rule failed and by how much.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from engine.model.artifact import (
    PERSISTED_LINES,
    PROBABILITY_TOLERANCE,
    InvalidArtifact,
    PredictionArtifact,
    artifact_from,
    derived_markets_match,
    flatten,
    unflatten,
)
from engine.model.fit import FitConfig, MatchObservation, fit_poisson
from engine.model.poisson import MAX_GOALS, derive_markets, scoreline_matrix
from engine.model.predict import predict_fixture
from engine.model.profiles import CONTROL, PRODUCTION

CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)
KNOWN_AT = datetime(2026, 1, 1, tzinfo=UTC)
START = datetime(2024, 8, 1, 15, 0, tzinfo=UTC)
FIXTURE = "11111111-2222-3333-4444-555555555555"
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


def built(config: FitConfig | None = None, **overrides: object) -> PredictionArtifact:
    model = fit_poisson(
        season(), data_cutoff=CUTOFF, as_of=KNOWN_AT,
        config=config or PRODUCTION.fit,
    )
    prediction = predict_fixture(model, "alpha", "bravo")
    artifact = artifact_from(
        prediction, model, fixture_id=FIXTURE, known_at=KNOWN_AT
    )
    if overrides:
        from dataclasses import replace

        artifact = replace(artifact, **overrides)  # type: ignore[arg-type]
    return artifact


class TestProbabilityCorrectness:
    def test_every_scoreline_probability_is_in_range(self) -> None:
        for value in built().scoreline:
            assert math.isfinite(value) and 0.0 <= value <= 1.0

    def test_the_scoreline_mass_is_one(self) -> None:
        assert sum(built().scoreline) == pytest.approx(
            1.0, abs=PROBABILITY_TOLERANCE
        )

    def test_one_x_two_sums_to_one(self) -> None:
        a = built()
        assert a.p_home + a.p_draw + a.p_away == pytest.approx(
            1.0, abs=PROBABILITY_TOLERANCE
        )

    def test_over_and_under_are_exact_complements(self) -> None:
        a = built()
        for line in PERSISTED_LINES:
            assert a.p_over[line] + a.p_under(line) == 1.0

    def test_btts_yes_and_no_are_exact_complements(self) -> None:
        a = built()
        assert a.p_btts_yes + a.p_btts_no == 1.0

    def test_over_lines_are_monotonic(self) -> None:
        a = built()
        values = [a.p_over[line] for line in sorted(PERSISTED_LINES)]
        assert values == sorted(values, reverse=True)

    def test_the_matrix_has_the_declared_dimensions(self) -> None:
        a = built()
        side = a.max_goals + 1
        assert a.max_goals == MAX_GOALS
        assert len(a.scoreline) == side * side
        grid = a.matrix()
        assert len(grid) == side and all(len(row) == side for row in grid)

    def test_derived_markets_equal_the_prediction_code(self) -> None:
        """The stored markets must be what `derive_markets` produces."""
        assert derived_markets_match(built())

    def test_derived_markets_equal_a_fresh_derivation_cell_by_cell(self) -> None:
        model = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=KNOWN_AT, config=PRODUCTION.fit
        )
        prediction = predict_fixture(model, "alpha", "bravo")
        artifact = artifact_from(
            prediction, model, fixture_id=FIXTURE, known_at=KNOWN_AT
        )
        fresh = derive_markets(
            scoreline_matrix(artifact.lambda_home, artifact.lambda_away)
        )
        assert artifact.p_home == pytest.approx(fresh.home_win, abs=1e-12)
        assert artifact.p_draw == pytest.approx(fresh.draw, abs=1e-12)
        assert artifact.p_away == pytest.approx(fresh.away_win, abs=1e-12)
        assert artifact.p_btts_yes == pytest.approx(fresh.btts_yes, abs=1e-12)

    def test_nothing_is_rounded_on_the_way_in(self) -> None:
        """The artifact carries the model's float64, not a shortened copy."""
        model = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=KNOWN_AT, config=PRODUCTION.fit
        )
        prediction = predict_fixture(model, "alpha", "bravo")
        artifact = artifact_from(
            prediction, model, fixture_id=FIXTURE, known_at=KNOWN_AT
        )
        assert artifact.lambda_home == prediction.lambda_home
        assert artifact.p_home == prediction.markets.home_win
        assert artifact.scoreline[0] == prediction.matrix.probability(0, 0)


class TestFlattening:
    def test_row_major_round_trip(self) -> None:
        matrix = scoreline_matrix(1.6, 1.1)
        flat = flatten(matrix)
        assert unflatten(flat, matrix.max_goals) == matrix.grid

    def test_the_index_formula_is_row_major(self) -> None:
        matrix = scoreline_matrix(1.6, 1.1)
        flat = flatten(matrix)
        side = matrix.max_goals + 1
        for h in (0, 1, 3, 7):
            for a in (0, 2, 5):
                assert flat[h * side + a] == matrix.probability(h, a)

    def test_lookup_matches_the_matrix(self) -> None:
        artifact = built()
        grid = artifact.matrix()
        for h in (0, 1, 2, 5):
            for a in (0, 1, 4):
                assert artifact.probability(h, a) == grid[h][a]

    def test_off_grid_lookups_are_zero(self) -> None:
        artifact = built()
        assert artifact.probability(999, 0) == 0.0
        assert artifact.probability(-1, 0) == 0.0

    def test_a_wrong_cell_count_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="expected"):
            unflatten([0.5, 0.5], max_goals=5)


class TestColdStartIsExplicit:
    def test_a_cold_started_prediction_is_flagged_and_names_the_team(
        self,
    ) -> None:
        model = fit_poisson(
            season(), data_cutoff=CUTOFF, as_of=KNOWN_AT,
            config=FitConfig(min_matches=4, profile="production"),
        )
        prediction = predict_fixture(model, "alpha", "never-seen")
        artifact = artifact_from(
            prediction, model, fixture_id=FIXTURE, known_at=KNOWN_AT
        )
        assert artifact.is_cold_start is True
        assert "never-seen" in artifact.cold_started_teams

    def test_a_warm_prediction_is_not_flagged(self) -> None:
        artifact = built()
        assert artifact.is_cold_start is False
        assert artifact.cold_started_teams == ()

    def test_the_flag_and_the_list_must_agree(self) -> None:
        with pytest.raises(InvalidArtifact, match="cold_started_teams"):
            built(is_cold_start=True).validate()
        with pytest.raises(InvalidArtifact, match="cold_started_teams"):
            built(cold_started_teams=("alpha",)).validate()


class TestValidationRefusesBadArtifacts:
    def test_an_out_of_range_probability_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="outside"):
            built(p_home=1.5, p_draw=0.0, p_away=-0.5).validate()

    def test_a_one_x_two_triple_that_does_not_sum_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="sums to"):
            built(p_home=0.5, p_draw=0.2, p_away=0.2).validate()

    def test_a_non_monotonic_over_ladder_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="monotonic"):
            built(p_over={0.5: 0.1, 1.5: 0.9, 2.5: 0.5, 3.5: 0.2}).validate()

    def test_missing_lines_are_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="over lines"):
            built(p_over={0.5: 0.9}).validate()

    def test_a_matrix_whose_mass_is_wrong_is_refused(self) -> None:
        artifact = built()
        broken = tuple(v * 0.5 for v in artifact.scoreline)
        with pytest.raises(InvalidArtifact, match="mass"):
            built(scoreline=broken).validate()

    def test_a_matrix_of_the_wrong_size_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="expected"):
            built(scoreline=(1.0,)).validate()

    def test_a_non_positive_lambda_is_refused(self) -> None:
        with pytest.raises(InvalidArtifact, match="positive"):
            built(lambda_home=0.0).validate()

    def test_a_cutoff_after_the_generation_time_is_refused(self) -> None:
        """You cannot, now, have known something from later than now."""
        with pytest.raises(InvalidArtifact, match="after known_at"):
            built(data_cutoff=KNOWN_AT + timedelta(days=1)).validate()


class TestIdentityAndMetadata:
    def test_identity_is_fixture_model_profile_and_cutoff(self) -> None:
        artifact = built()
        assert artifact.identity == (
            FIXTURE, artifact.model_version, "production", CUTOFF
        )

    def test_known_at_is_not_part_of_the_identity(self) -> None:
        """Regenerating the same prediction later is the same prediction."""
        first = built()
        later = built(known_at=KNOWN_AT + timedelta(days=30))
        assert first.identity == later.identity

    def test_the_metadata_records_what_reproduces_the_fit(self) -> None:
        meta = built().fit_metadata
        for key in (
            "model_family", "model_version", "profile", "data_cutoff",
            "training_matches", "decay_enabled", "decay_half_life_days",
            "dixon_coles_enabled", "cold_start_min_matches", "converged",
        ):
            assert key in meta, key
        assert meta["decay_half_life_days"] == 180.0
        assert meta["dixon_coles_enabled"] is False

    def test_a_control_artifact_records_that_decay_is_off(self) -> None:
        artifact = built(config=CONTROL.fit)
        assert artifact.profile == "control"
        assert artifact.fit_metadata["decay_enabled"] is False

    def test_the_ratings_are_not_copied_into_every_prediction(self) -> None:
        """380 copies of the same rating table is a table, not metadata."""
        assert "ratings" not in built().fit_metadata


class TestNoOddsInTheArtifact:
    def test_no_field_mentions_a_price(self) -> None:
        from dataclasses import fields

        for field_ in fields(PredictionArtifact):
            assert not any(
                token in field_.name.lower()
                for token in ("odds", "price", "bookmaker", "value", "stake")
            ), field_.name

    def test_no_metadata_key_mentions_a_price(self) -> None:
        for key in built().fit_metadata:
            assert not any(
                token in key.lower()
                for token in ("odds", "price", "bookmaker")
            ), key
