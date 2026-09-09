"""The production prediction pipeline against PostgreSQL (PREDICTIONS.md).

HERMETIC AND ROLLED BACK. Each test seeds its own competition, season, teams,
fixtures and results inside a transaction that is discarded at teardown, so
the six-season development corpus is never touched and nothing here depends
on run order. The one exception is the historical-reproduction class, which
needs the real corpus and says so.

The claims that matter are the reproducibility ones: the same cutoff produces
the same probabilities, a later result cannot reach an earlier prediction, and
what is persisted equals what the model computed.
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
from engine.ingestion.postgres import PostgresPredictionStore, PredictionOutcome
from engine.ingestion.runs import RunStatus
from engine.jobs.generate_predictions import generate
from engine.model.artifact import artifact_from
from engine.model.fit import fit_poisson, observations_before
from engine.model.predict import predict_fixture
from engine.model.profiles import CONTROL, PRODUCTION
from engine.model.repository import load_corpus, load_eligible_fixtures

SEASON_LABEL = "2024/25"
SEASON_START = datetime(2024, 8, 10, 14, 0, tzinfo=UTC)
#: The seeded season is 90 fixtures at one every two days, so it spans 180
#: days. Day 110 leaves ~55 played (comfortably over the 30-match minimum)
#: and ~35 still to come.
CUTOFF = SEASON_START + timedelta(days=110)
KNOWN_AT = datetime(2026, 6, 1, tzinfo=UTC)
TEAMS = [f"Probe{i:02d}" for i in range(10)]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    connection = psycopg.connect(database_url())
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


class Seeded:
    """One synthetic season, built through the real schema."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self.tag = uuid.uuid4().hex[:10]
        self.competition_slug = f"probeland-league-{self.tag}"
        self.team_ids: dict[str, UUID] = {}
        self.fixture_ids: dict[tuple[str, str], UUID] = {}
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
               VALUES (%s, 'Probe', '{fixtures}', 'https://example.test')
               RETURNING id""",
            (f"src-{self.tag}",),
        )))
        self.body_id = int(self._one(
            """INSERT INTO raw_payload_bodies (hash_algo, body_hash, body, byte_size)
               VALUES ('sha256', %s, %s, 5) RETURNING id""",
            (uuid.uuid4().hex + uuid.uuid4().hex, b"probe"),
        ))
        country = UUID(str(self._one(
            "INSERT INTO countries (slug, name) VALUES (%s,'Probeland') RETURNING id",
            (f"probeland-{self.tag}",),
        )))
        competition = UUID(str(self._one(
            """INSERT INTO competitions (slug, country_id, type, tier, gender)
               VALUES (%s,%s,'league',1,'men') RETURNING id""",
            (self.competition_slug, country),
        )))
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO competition_names
                     (competition_id, name, name_type, valid_from)
                   VALUES (%s,'Probe League','official',%s)""",
                (competition, date(2000, 1, 1)),
            )
        self.season_id = UUID(str(self._one(
            """INSERT INTO seasons
                 (slug, competition_id, label, start_year, start_date, end_date)
               VALUES (%s,%s,%s,2024,%s,%s) RETURNING id""",
            (f"season-{self.tag}", competition, SEASON_LABEL,
             SEASON_START.date(), SEASON_START.date() + timedelta(days=300)),
        )))
        for name in TEAMS:
            team = UUID(str(self._one(
                """INSERT INTO teams (slug, country_id, gender)
                   VALUES (%s,%s,'men') RETURNING id""",
                (f"{name.lower()}-{self.tag}", country),
            )))
            with self.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO team_names (team_id, name, name_type, valid_from)
                       VALUES (%s,%s,'official',%s)""",
                    (team, name, date(1900, 1, 1)),
                )
            self.team_ids[name] = team

        day = 0
        for home in TEAMS:
            for away in TEAMS:
                if home == away:
                    continue
                day += 1
                self.add(home, away, day % 3, (day + 1) % 3, day * 2)

    def add(self, home: str, away: str, hg: int, ag: int, day: int) -> UUID:
        kickoff = SEASON_START + timedelta(days=day)
        fixture = UUID(str(self._one(
            """INSERT INTO fixtures
                 (season_id, stage, leg, replay_number, home_team_id, away_team_id)
               VALUES (%s,'regular',1,0,%s,%s) RETURNING id""",
            (self.season_id, self.team_ids[home], self.team_ids[away]),
        )))
        self.fixture_ids[(home, away)] = fixture
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fixture_schedule
                     (fixture_id, kickoff_utc, local_date, local_tz, status,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s,%s,%s,'UTC','ft',%s,%s,%s)""",
                (fixture, kickoff, kickoff.date(), self.source_id,
                 self.body_id, SEASON_START),
            )
            cur.execute(
                """INSERT INTO match_results
                     (fixture_id, result_source, is_trainable, ft_home, ft_away,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s,'played',true,%s,%s,%s,%s,%s)""",
                (fixture, hg, ag, self.source_id, self.body_id, SEASON_START),
            )
        return fixture

    def run(self, cutoff: datetime = CUTOFF, **kwargs: Any) -> Any:
        return generate(
            self.conn,
            data_cutoff=cutoff,
            known_at=kwargs.pop("known_at", KNOWN_AT),
            competition_slug=self.competition_slug,
            seasons=(SEASON_LABEL,),
            **kwargs,
        )

    def stored(self) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT p.fixture_id, p.p_home, p.p_draw, p.p_away,
                          p.lambda_home, p.lambda_away, p.scoreline,
                          p.is_cold_start, p.data_cutoff, p.profile
                     FROM predictions p
                     JOIN fixtures f ON f.id = p.fixture_id
                    WHERE f.season_id = %s AND p.superseded_at IS NULL
                    ORDER BY p.fixture_id""",
                (self.season_id,),
            )
            return list(cur.fetchall())

    def count(self, sql: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, (self.season_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0


@pytest.mark.db
class TestGeneration:
    def test_it_predicts_every_eligible_fixture(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        report = seeded.run()
        assert report.status is RunStatus.OK
        assert report.eligible > 0
        assert report.created == report.eligible
        assert len(seeded.stored()) == report.eligible

    def test_only_fixtures_at_or_after_the_cutoff_are_eligible(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        eligible = load_eligible_fixtures(
            conn,
            competition_slug=seeded.competition_slug,
            data_cutoff=CUTOFF,
            as_of=KNOWN_AT,
            season_labels=[SEASON_LABEL],
        )
        assert eligible
        assert all(f.kickoff >= CUTOFF for f in eligible)

    def test_eligibility_never_consults_the_result(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """Every seeded fixture has a score; eligibility must ignore that."""
        seeded = Seeded(conn)
        eligible = load_eligible_fixtures(
            conn,
            competition_slug=seeded.competition_slug,
            data_cutoff=CUTOFF,
            as_of=KNOWN_AT,
            season_labels=[SEASON_LABEL],
        )
        played_after_cutoff = seeded.count(
            """SELECT count(*) FROM fixtures f
                 JOIN fixture_schedule s ON s.fixture_id=f.id
                  AND s.superseded_at IS NULL
                WHERE f.season_id = %s AND s.kickoff_utc >= '"""
            + CUTOFF.isoformat()
            + "'::timestamptz"
        )
        assert len(eligible) == played_after_cutoff

    def test_the_persisted_artifact_equals_the_direct_model_output(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The whole point of persisting: no refit needed, and no drift."""
        seeded = Seeded(conn)
        seeded.run()

        corpus = load_corpus(
            conn, competition_slug=seeded.competition_slug,
            season_labels=[SEASON_LABEL], as_of=KNOWN_AT,
        )
        model = fit_poisson(
            observations_before(corpus, CUTOFF),
            data_cutoff=CUTOFF, as_of=KNOWN_AT, config=PRODUCTION.fit,
        )
        by_fixture = {str(row[0]): row for row in seeded.stored()}
        checked = 0
        for (home, away), fixture_id in seeded.fixture_ids.items():
            row = by_fixture.get(str(fixture_id))
            if row is None:
                continue
            direct = predict_fixture(model, home, away)
            assert row[1] == direct.markets.home_win
            assert row[2] == direct.markets.draw
            assert row[3] == direct.markets.away_win
            assert row[4] == direct.lambda_home
            assert row[5] == direct.lambda_away
            assert list(row[6]) == [
                v for r in direct.matrix.grid for v in r
            ]
            checked += 1
        assert checked > 0

    def test_the_run_records_its_job(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        report = seeded.run()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_name, status FROM job_runs WHERE id = %s",
                (report.job_run_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "generate_predictions" and row[1] == "ok"
        assert seeded.count(
            """SELECT count(*) FROM predictions p JOIN fixtures f
                 ON f.id=p.fixture_id WHERE f.season_id=%s
                AND p.job_run_id IS NOT NULL"""
        ) > 0

    def test_a_cutoff_after_known_at_is_refused(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        with pytest.raises(ValueError, match="after known_at"):
            seeded.run(cutoff=KNOWN_AT + timedelta(days=1))

    def test_too_little_history_fails_rather_than_guessing(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        report = seeded.run(cutoff=SEASON_START + timedelta(days=2))
        assert report.status is RunStatus.FAILED
        assert "training matches" in report.problems[0]
        assert seeded.stored() == []


@pytest.mark.db
class TestIdempotency:
    def test_a_rerun_writes_nothing(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        first = seeded.run()
        before = seeded.stored()

        second = seeded.run(known_at=KNOWN_AT + timedelta(days=7))

        assert second.created == 0
        assert second.revised == 0
        assert second.unchanged == first.created
        assert seeded.stored() == before

    def test_a_rerun_produces_identical_probabilities(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        seeded.run()
        first = [(str(r[0]), r[1], r[2], r[3]) for r in seeded.stored()]
        seeded.run(known_at=KNOWN_AT + timedelta(days=7))
        second = [(str(r[0]), r[1], r[2], r[3]) for r in seeded.stored()]
        assert first == second

    def test_no_duplicate_current_artifact_survives_two_runs(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        seeded.run()
        seeded.run()
        assert seeded.count(
            """SELECT count(*) FROM (
                 SELECT p.fixture_id, p.model_version, p.profile, p.data_cutoff
                   FROM predictions p JOIN fixtures f ON f.id=p.fixture_id
                  WHERE f.season_id=%s AND p.superseded_at IS NULL
                  GROUP BY 1,2,3,4 HAVING count(*) > 1) d"""
        ) == 0

    def test_a_different_cutoff_is_a_different_generation(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """History is kept: an earlier prediction is not overwritten."""
        seeded = Seeded(conn)
        seeded.run(cutoff=CUTOFF)
        seeded.run(cutoff=CUTOFF + timedelta(days=20))
        cutoffs = seeded.count(
            """SELECT count(DISTINCT p.data_cutoff) FROM predictions p
                 JOIN fixtures f ON f.id=p.fixture_id
                WHERE f.season_id=%s AND p.superseded_at IS NULL"""
        )
        assert cutoffs == 2

    def test_a_different_profile_is_a_different_generation(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        seeded.run(profile_name=PRODUCTION.name)
        seeded.run(profile_name=CONTROL.name)
        profiles = seeded.count(
            """SELECT count(DISTINCT p.profile) FROM predictions p
                 JOIN fixtures f ON f.id=p.fixture_id
                WHERE f.season_id=%s AND p.superseded_at IS NULL"""
        )
        assert profiles == 2

    def test_changed_numbers_supersede_rather_than_overwrite(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The only way an artifact changes: close the old, open a new one."""
        seeded = Seeded(conn)
        seeded.run()
        row = seeded.stored()[0]
        fixture_id = row[0]

        corpus = load_corpus(
            conn, competition_slug=seeded.competition_slug,
            season_labels=[SEASON_LABEL], as_of=KNOWN_AT,
        )
        model = fit_poisson(
            observations_before(corpus, CUTOFF),
            data_cutoff=CUTOFF, as_of=KNOWN_AT, config=PRODUCTION.fit,
        )
        prediction = predict_fixture(model, TEAMS[0], TEAMS[1])
        artifact = artifact_from(
            prediction, model, fixture_id=str(fixture_id),
            known_at=KNOWN_AT + timedelta(days=1),
        )
        from dataclasses import replace

        nudged = replace(artifact, lambda_home=artifact.lambda_home * 1.01)
        outcome = PostgresPredictionStore(conn).store(nudged)

        assert outcome is PredictionOutcome.REVISED
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FILTER (WHERE superseded_at IS NULL),
                          count(*) FILTER (WHERE superseded_at IS NOT NULL)
                     FROM predictions WHERE fixture_id = %s""",
                (fixture_id,),
            )
            current, superseded = cur.fetchone()  # type: ignore[misc]
        assert current == 1 and superseded == 1


@pytest.mark.db
class TestNoLeakageAcrossTheCutoff:
    def test_a_later_result_cannot_change_an_earlier_prediction(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The pipeline's version of the mandatory leakage test."""
        seeded = Seeded(conn)
        seeded.run()
        before = [(str(r[0]), r[1], r[2], r[3], r[4]) for r in seeded.stored()]

        # Rewrite a result AFTER the cutoff with a 9-0 rout, through the
        # append-only path, then regenerate at the same cutoff.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.id, r.fixture_id FROM match_results r
                     JOIN fixtures f ON f.id=r.fixture_id
                     JOIN fixture_schedule s ON s.fixture_id=f.id
                      AND s.superseded_at IS NULL
                    WHERE f.season_id=%s AND r.superseded_at IS NULL
                      AND s.kickoff_utc > %s
                    ORDER BY s.kickoff_utc DESC LIMIT 1""",
                (seeded.season_id, CUTOFF),
            )
            row = cur.fetchone()
            assert row is not None
            moment = datetime.now(UTC)
            cur.execute(
                "UPDATE match_results SET superseded_at=%s WHERE id=%s",
                (moment, row[0]),
            )
            cur.execute(
                """INSERT INTO match_results
                     (fixture_id, result_source, is_trainable, ft_home, ft_away,
                      source_id, raw_payload_body_id, known_at)
                   VALUES (%s,'played',true,9,0,%s,%s,%s)""",
                (row[1], seeded.source_id, seeded.body_id, moment),
            )

        seeded.run(known_at=datetime.now(UTC))
        after = [(str(r[0]), r[1], r[2], r[3], r[4]) for r in seeded.stored()]
        assert after == before, "a post-cutoff result reached the prediction"

    def test_the_training_set_is_strictly_before_the_cutoff(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        corpus = load_corpus(
            conn, competition_slug=seeded.competition_slug,
            season_labels=[SEASON_LABEL], as_of=KNOWN_AT,
        )
        training = observations_before(corpus, CUTOFF)
        assert training
        assert all(o.kickoff < CUTOFF for o in training)
        assert len(training) < len(corpus)

    def test_an_eligible_fixture_is_never_in_its_own_training_set(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        seeded = Seeded(conn)
        corpus = load_corpus(
            conn, competition_slug=seeded.competition_slug,
            season_labels=[SEASON_LABEL], as_of=KNOWN_AT,
        )
        training_keys = {
            (o.home_team, o.away_team) for o in observations_before(corpus, CUTOFF)
        }
        eligible = load_eligible_fixtures(
            conn, competition_slug=seeded.competition_slug,
            data_cutoff=CUTOFF, as_of=KNOWN_AT, season_labels=[SEASON_LABEL],
        )
        for fixture in eligible:
            assert (fixture.home_team, fixture.away_team) not in training_keys


@pytest.mark.db
class TestHistoricalReproductionOnTheRealCorpus:
    """Uses the imported Premier League corpus; skips if it is absent.

    The db suite's other classes empty the canonical tables, so this asserts
    what it needs rather than assuming it - and reports a skip instead of a
    misleading failure when the corpus has been wiped.
    """

    BENCHMARK_CUTOFF = datetime(2024, 1, 15, tzinfo=UTC)

    def corpus_present(self, conn: psycopg.Connection[Any]) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM fixtures f JOIN seasons se
                     ON se.id=f.season_id JOIN competitions c
                     ON c.id=se.competition_id
                    WHERE c.slug='england-premier-league'"""
            )
            row = cur.fetchone()
        return bool(row and row[0] >= 2280)

    def test_the_benchmark_cutoff_reproduces_identically(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        if not self.corpus_present(conn):
            pytest.skip("six-season corpus not present in this database")
        known = datetime.now(UTC)
        first = generate(conn, data_cutoff=self.BENCHMARK_CUTOFF, known_at=known)
        assert first.status is RunStatus.OK
        assert first.eligible > 0

        with conn.cursor() as cur:
            cur.execute(
                """SELECT fixture_id, p_home, p_draw, p_away, lambda_home
                     FROM predictions WHERE data_cutoff=%s
                      AND superseded_at IS NULL ORDER BY fixture_id""",
                (self.BENCHMARK_CUTOFF,),
            )
            before = list(cur.fetchall())

        second = generate(
            conn,
            data_cutoff=self.BENCHMARK_CUTOFF,
            known_at=known + timedelta(hours=1),
        )
        assert second.created == 0 and second.revised == 0
        assert second.unchanged == first.created + first.unchanged

        with conn.cursor() as cur:
            cur.execute(
                """SELECT fixture_id, p_home, p_draw, p_away, lambda_home
                     FROM predictions WHERE data_cutoff=%s
                      AND superseded_at IS NULL ORDER BY fixture_id""",
                (self.BENCHMARK_CUTOFF,),
            )
            after = list(cur.fetchall())
        assert after == before
