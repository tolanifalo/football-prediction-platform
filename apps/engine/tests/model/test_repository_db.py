"""The database boundary and a leakage proof (MODEL-BASELINE.md §5).

HERMETIC AND ROLLED BACK. Each test seeds its own competition, season, teams,
fixtures and results inside a transaction that is discarded at teardown.

That is not fussiness. These tests originally read the imported Premier League
data and passed in isolation, then failed in the full suite: the P0-11 and
P0-12 integration suites use `clean_db`, which EMPTIES the canonical tables,
and they run first alphabetically. A test that depends on ambient database
state is a test that depends on run order. Seeding its own world costs a few
dozen lines and removes the whole class of problem - and it leaves the
development data untouched, which `clean_db` would not.

Real-data validation lives in the walk-forward backtest job, not here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from engine.db import database_url
from engine.model.fit import FitConfig, fit_poisson, observations_before
from engine.model.predict import predict_fixture
from engine.model.repository import load_observations

CONFIG = FitConfig(ridge=0.05, min_matches=0)
SEASON_LABEL = "2023/24"
SEASON_START = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
#: Mid-season: plenty of history before it, plenty of matches after.
CUTOFF = SEASON_START + timedelta(days=15)

TEAMS = ["Ashford", "Barrow", "Colne", "Dunmow", "Elmswell", "Fenton"]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    connection = psycopg.connect(database_url())
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


class Seeded:
    """A complete synthetic season, built through the real schema."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self.tag = uuid.uuid4().hex[:10]
        self.competition_slug = f"testland-league-{self.tag}"
        self.team_ids: dict[str, UUID] = {}
        self.expected = 0
        self._build()

    def _one(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _build(self) -> None:
        self.source_id = UUID(str(self._one(
            """INSERT INTO data_sources (slug, display_name, kinds, base_url)
               VALUES (%s, 'Test', '{fixtures}', 'https://example.test')
               RETURNING id""",
            (f"src-{self.tag}",),
        )))
        self.body_id = int(self._one(
            """INSERT INTO raw_payload_bodies (hash_algo, body_hash, body, byte_size)
               VALUES ('sha256', %s, %s, 4) RETURNING id""",
            (uuid.uuid4().hex + uuid.uuid4().hex, b"seed"),
        ))
        country_id = UUID(str(self._one(
            "INSERT INTO countries (slug, name) VALUES (%s, 'Testland') RETURNING id",
            (f"testland-{self.tag}",),
        )))
        competition_id = UUID(str(self._one(
            """INSERT INTO competitions (slug, country_id, type, tier, gender)
               VALUES (%s, %s, 'league', 1, 'men') RETURNING id""",
            (self.competition_slug, country_id),
        )))
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO competition_names
                     (competition_id, name, name_type, valid_from)
                   VALUES (%s, 'Test League', 'official', %s)""",
                (competition_id, date(2000, 1, 1)),
            )
        self.season_id = UUID(str(self._one(
            """INSERT INTO seasons
                 (slug, competition_id, label, start_year, start_date, end_date)
               VALUES (%s, %s, %s, 2023, %s, %s) RETURNING id""",
            (
                f"season-{self.tag}", competition_id, SEASON_LABEL,
                SEASON_START.date(), SEASON_START.date() + timedelta(days=280),
            ),
        )))
        for name in TEAMS:
            team_id = UUID(str(self._one(
                """INSERT INTO teams (slug, country_id, gender)
                   VALUES (%s, %s, 'men') RETURNING id""",
                (f"{name.lower()}-{self.tag}", country_id),
            )))
            with self.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO team_names (team_id, name, name_type, valid_from)
                       VALUES (%s, %s, 'official', %s)""",
                    (team_id, name, date(1900, 1, 1)),
                )
            self.team_ids[name] = team_id

        # A double round robin, one match per day, deterministic scores.
        day = 0
        for home in TEAMS:
            for away in TEAMS:
                if home == away:
                    continue
                day += 1
                self.add_match(home, away, day % 3, (day + 1) % 3, day)
                self.expected += 1

    def add_match(self, home: str, away: str, hg: int, ag: int, day: int) -> UUID:
        kickoff = SEASON_START + timedelta(days=day)
        fixture_id = UUID(str(self._one(
            """INSERT INTO fixtures
                 (season_id, stage, leg, replay_number, home_team_id, away_team_id)
               VALUES (%s, 'regular', 1, 0, %s, %s) RETURNING id""",
            (self.season_id, self.team_ids[home], self.team_ids[away]),
        )))
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fixture_schedule
                     (fixture_id, kickoff_utc, local_date, local_tz, status,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s, %s, %s, 'UTC', 'ft', %s, %s, %s)""",
                (fixture_id, kickoff, kickoff.date(), self.source_id,
                 self.body_id, SEASON_START),
            )
            cur.execute(
                """INSERT INTO match_results
                     (fixture_id, result_source, is_trainable, ft_home, ft_away,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s, 'played', true, %s, %s, %s, %s, %s)""",
                (fixture_id, hg, ag, self.source_id, self.body_id, SEASON_START),
            )
        return fixture_id

    def load(self) -> list[Any]:
        return load_observations(
            self.conn,
            competition_slug=self.competition_slug,
            season_label=SEASON_LABEL,
            as_of=datetime.now(UTC),
        )


@pytest.mark.db
class TestRepository:
    def test_it_loads_the_seeded_season(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        observations = seeded.load()
        assert len(observations) == seeded.expected
        assert {o.home_team for o in observations} == set(TEAMS)

    def test_every_observation_carries_a_kickoff(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """`occurred_at` is NULL here, as on the real import; the schedule wins."""
        for observation in Seeded(conn).load():
            assert observation.kickoff is not None
            assert observation.kickoff.tzinfo is not None

    def test_results_arrive_in_chronological_order(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        kickoffs = [o.kickoff for o in Seeded(conn).load()]
        assert kickoffs == sorted(kickoffs)

    def test_an_untrainable_result_is_excluded(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """P0-08's flag: an abandoned match must never train a scoring model."""
        seeded = Seeded(conn)
        assert len(seeded.load()) > 0
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE match_results SET is_trainable = false
                    WHERE source_id = %s AND superseded_at IS NULL""",
                (seeded.source_id,),
            )
        assert seeded.load() == []

    def test_a_superseded_result_is_invisible(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE match_results SET superseded_at = now()
                    WHERE source_id = %s AND superseded_at IS NULL""",
                (seeded.source_id,),
            )
        assert seeded.load() == []

    def test_an_unknown_scope_returns_nothing_rather_than_everything(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        assert (
            load_observations(
                conn,
                competition_slug="no-such-league",
                season_label=SEASON_LABEL,
                as_of=datetime.now(UTC),
            )
            == []
        )

    def test_the_cutoff_selects_a_strict_prefix(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        observations = Seeded(conn).load()
        training = observations_before(observations, CUTOFF)
        assert 0 < len(training) < len(observations)
        assert all(o.kickoff < CUTOFF for o in training)


@pytest.mark.db
class TestLeakageAgainstTheDatabase:
    def test_a_later_result_added_to_the_database_changes_nothing_before_it(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """THE database-level leakage proof (MODEL-BASELINE.md §1).

        A late-season result is superseded with a 9-0 rout - a genuine
        revision through the P0-08 append-only path, not a doctored row - and
        the mid-season prediction is recomputed. It must be bit-identical.
        """
        seeded = Seeded(conn)
        before = seeded.load()
        model_before = fit_poisson(
            observations_before(before, CUTOFF),
            data_cutoff=CUTOFF,
            as_of=datetime.now(UTC),
            config=CONFIG,
        )
        prediction_before = predict_fixture(model_before, "Ashford", "Barrow")

        # -- rewrite the future --------------------------------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.fixture_id, s.kickoff_utc
                  FROM match_results r
                  JOIN fixture_schedule s ON s.fixture_id = r.fixture_id
                   AND s.superseded_at IS NULL
                 WHERE r.superseded_at IS NULL AND r.source_id = %s
                   AND s.kickoff_utc > %s
                 ORDER BY s.kickoff_utc DESC LIMIT 1
                """,
                (seeded.source_id, CUTOFF),
            )
            row = cur.fetchone()
            assert row is not None, "no late-season result to rewrite"
            result_id, fixture_id, kickoff = row
            assert kickoff > CUTOFF

            # NOT in the future. A `known_at` later than the loader's `as_of`
            # is correctly invisible - the transaction-time axis is real, and
            # stamping this a second ahead hid the revision entirely on the
            # first attempt at this test.
            moment = datetime.now(UTC)
            cur.execute(
                "UPDATE match_results SET superseded_at = %s WHERE id = %s",
                (moment, result_id),
            )
            cur.execute(
                """INSERT INTO match_results
                     (fixture_id, result_source, is_trainable, ft_home, ft_away,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s, 'played', true, 9, 0, %s, %s, %s)""",
                (fixture_id, seeded.source_id, seeded.body_id, moment),
            )

        after = seeded.load()
        assert len(after) == len(before), "the revision replaced, not appended"
        assert any(
            o.home_goals == 9 and o.away_goals == 0 for o in after
        ), "the rewritten future is genuinely visible to the loader"

        model_after = fit_poisson(
            observations_before(after, CUTOFF),
            data_cutoff=CUTOFF,
            as_of=datetime.now(UTC),
            config=CONFIG,
        )
        prediction_after = predict_fixture(model_after, "Ashford", "Barrow")

        assert prediction_after.lambda_home == prediction_before.lambda_home
        assert prediction_after.lambda_away == prediction_before.lambda_away
        assert (
            prediction_after.markets.one_x_two == prediction_before.markets.one_x_two
        )
        assert model_after.mu == model_before.mu
        assert model_after.home_advantage == model_before.home_advantage
        assert model_after.training_matches == model_before.training_matches

    def test_the_same_rewrite_does_reach_a_prediction_after_it(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The control. If nothing ever moved, the test above would be empty."""
        seeded = Seeded(conn)
        observations = seeded.load()
        late = max(o.kickoff for o in observations) + timedelta(days=1)
        full = fit_poisson(
            observations_before(observations, late),
            data_cutoff=late,
            as_of=datetime.now(UTC),
            config=CONFIG,
        )
        early = fit_poisson(
            observations_before(observations, CUTOFF),
            data_cutoff=CUTOFF,
            as_of=datetime.now(UTC),
            config=CONFIG,
        )
        assert full.training_matches > early.training_matches
        assert full.mu != early.mu
