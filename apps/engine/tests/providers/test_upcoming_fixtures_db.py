"""Upcoming-fixture ingestion, and the chain through to a prediction.

ISOLATED BY WIPE, NOT BY ROLLBACK, and that is forced rather than chosen:
`run_import` COMMITS - evidence has to be durable before anything is parsed -
so a rolled-back transaction cannot contain it. These tests therefore use
P0-11's `clean_db`, which empties the canonical tables before AND after each
one. No fabricated fixture can survive into the authoritative corpus, which
is the requirement; the corpus is re-imported afterwards.

The chain being demonstrated:

    provider response -> canonical fixture -> fixture_schedule
                      -> prediction eligibility -> production prediction

No live credentials: every response comes from a checked-in payload through
`httpx.MockTransport`.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest

from engine.ingestion.runs import RunStatus
from engine.ingestion.transport import HttpxTransport
from engine.jobs.generate_predictions import generate
from engine.jobs.import_upcoming_fixtures import run_import
from engine.model.repository import load_eligible_fixtures
from engine.providers.football_data_org.catalog import BASE_URL
from providers.test_import_integration import clean_db

__all__ = ["clean_db"]

PAYLOADS = (
    pathlib.Path(__file__).parent / "fixtures" / "football_data_org"
)
#: The canonical clubs the payloads reference, by our slug and display name.
CLUBS: tuple[tuple[str, str], ...] = (
    ("arsenal", "Arsenal"),
    ("chelsea", "Chelsea"),
    ("liverpool", "Liverpool"),
    ("manchester-city", "Manchester City"),
    ("everton", "Everton"),
    ("fulham", "Fulham"),
)
HISTORY_SEASON = "2025/26"
HISTORY_START = datetime(2025, 8, 9, 14, 0, tzinfo=UTC)
#: The payloads all sit in the 2026/27 season.
UPCOMING_SEASON = "2026/27"


def body(name: str) -> bytes:
    return (PAYLOADS / f"{name}.json").read_bytes()


def transport_for(name: str) -> HttpxTransport:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200, content=body(name),
                headers={"content-type": "application/json"},
            )
        ),
        base_url=BASE_URL,
    )
    return HttpxTransport(BASE_URL, client=client)


@pytest.fixture
def conn(clean_db: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """An emptied canonical database, restored to empty afterwards."""
    yield clean_db


class World:
    """A canonical world with the clubs the payloads name, plus history.

    The history matters: the prediction job needs at least 30 played matches
    before it will fit, so the chain cannot be demonstrated on fixtures alone.
    """

    def __init__(self, conn: psycopg.Connection[Any], *, history: bool = True) -> None:
        self.conn = conn
        self.tag = uuid.uuid4().hex[:10]
        self.team_ids: dict[str, UUID] = {}
        self._build(history)

    def _one(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _build(self, history: bool) -> None:
        self.source_id = UUID(str(self._one(
            """INSERT INTO data_sources (slug, display_name, kinds, base_url)
               VALUES (%s,'Probe','{fixtures}','https://example.test')
               RETURNING id""",
            (f"hist-{self.tag}",),
        )))
        self.body_id = int(self._one(
            """INSERT INTO raw_payload_bodies (hash_algo, body_hash, body, byte_size)
               VALUES ('sha256', %s, %s, 5) RETURNING id""",
            (uuid.uuid4().hex + uuid.uuid4().hex, b"probe"),
        ))
        self.country_id = UUID(str(self._one(
            "INSERT INTO countries (slug,name) VALUES (%s,'England') RETURNING id",
            ("england",),
        ))) if not self._exists("countries", "england") else self._id(
            "countries", "england"
        )
        self.competition_id = (
            self._id("competitions", "england-premier-league")
            if self._exists("competitions", "england-premier-league")
            else UUID(str(self._one(
                """INSERT INTO competitions (slug,country_id,type,tier,gender)
                   VALUES ('england-premier-league',%s,'league',1,'men')
                   RETURNING id""",
                (self.country_id,),
            )))
        )
        for slug, name in CLUBS:
            if self._exists("teams", slug):
                self.team_ids[slug] = self._id("teams", slug)
                continue
            team = UUID(str(self._one(
                "INSERT INTO teams (slug,country_id,gender) VALUES (%s,%s,'men') "
                "RETURNING id",
                (slug, self.country_id),
            )))
            with self.conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO team_names (team_id,name,name_type,valid_from)
                       VALUES (%s,%s,'official',%s)""",
                    (team, name, date(1900, 1, 1)),
                )
            self.team_ids[slug] = team
        if history:
            self._seed_history()

    def _exists(self, table: str, slug: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE slug=%s", (slug,))
            return cur.fetchone() is not None

    def _id(self, table: str, slug: str) -> UUID:
        return UUID(str(self._one(f"SELECT id FROM {table} WHERE slug=%s", (slug,))))

    def _seed_history(self) -> None:
        """A played season, so the model has something legal to train on."""
        season = UUID(str(self._one(
            """INSERT INTO seasons (slug,competition_id,label,start_year,
                                    start_date,end_date)
               VALUES (%s,%s,%s,2025,%s,%s) RETURNING id""",
            (f"pl-hist-{self.tag}", self.competition_id, HISTORY_SEASON,
             HISTORY_START.date(), HISTORY_START.date() + timedelta(days=250)),
        )))
        slugs = [s for s, _ in CLUBS]
        day = 0
        for home in slugs:
            for away in slugs:
                if home == away:
                    continue
                day += 1
                kickoff = HISTORY_START + timedelta(days=day * 3)
                fixture = UUID(str(self._one(
                    """INSERT INTO fixtures (season_id,stage,leg,replay_number,
                                             home_team_id,away_team_id)
                       VALUES (%s,'regular',1,0,%s,%s) RETURNING id""",
                    (season, self.team_ids[home], self.team_ids[away]),
                )))
                with self.conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO fixture_schedule
                             (fixture_id,kickoff_utc,local_date,local_tz,status,
                              source_id,raw_payload_body_id,known_at)
                           VALUES (%s,%s,%s,'Europe/London','ft',%s,%s,%s)""",
                        (fixture, kickoff, kickoff.date(), self.source_id,
                         self.body_id, HISTORY_START),
                    )
                    cur.execute(
                        """INSERT INTO match_results
                             (fixture_id,result_source,is_trainable,ft_home,
                              ft_away,source_id,raw_payload_body_id,known_at)
                           VALUES (%s,'played',true,%s,%s,%s,%s,%s)""",
                        (fixture, day % 3, (day + 1) % 3, self.source_id,
                         self.body_id, HISTORY_START),
                    )

    def ingest(self, payload: str = "upcoming") -> Any:
        return run_import(
            self.conn, code="PL", transport=transport_for(payload)
        )

    def upcoming(self) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT hn.name, an.name, s.kickoff_utc, s.status, s.local_date
                     FROM fixtures f
                     JOIN seasons se ON se.id=f.season_id
                     JOIN fixture_schedule s ON s.fixture_id=f.id
                      AND s.superseded_at IS NULL
                     JOIN team_names hn ON hn.team_id=f.home_team_id
                      AND hn.valid_to IS NULL
                     JOIN team_names an ON an.team_id=f.away_team_id
                      AND an.valid_to IS NULL
                    WHERE se.label=%s ORDER BY s.kickoff_utc""",
                (UPCOMING_SEASON,),
            )
            return list(cur.fetchall())

    def count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0]) if row else 0


@pytest.mark.db
class TestCanonicalMapping:
    def test_it_ingests_the_upcoming_fixtures(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        report = world.ingest()
        assert report.status is RunStatus.OK
        assert report.fixtures == 3
        assert report.unresolved == 0 and report.ambiguous == 0
        assert len(world.upcoming()) == 3

    def test_the_canonical_names_are_ours_not_the_providers(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The provider says "Arsenal FC"; the registry says "Arsenal"."""
        world = World(conn)
        world.ingest()
        homes = {row[0] for row in world.upcoming()}
        assert "Arsenal" in homes
        assert "Arsenal FC" not in homes

    def test_the_season_is_created_under_the_existing_competition(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        assert world.count(
            "SELECT count(*) FROM seasons WHERE competition_id=%s AND label=%s",
            (world.competition_id, UPCOMING_SEASON),
        ) == 1
        # One competition, not one per provider.
        assert world.count(
            "SELECT count(*) FROM competitions WHERE slug='england-premier-league'"
        ) == 1

    def test_the_fixture_keeps_the_canonical_identity(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        assert world.count(
            """SELECT count(*) FROM fixtures f JOIN seasons se ON se.id=f.season_id
                WHERE se.label=%s AND (f.stage,f.leg,f.replay_number)
                  <> ('regular',1,0)""",
            (UPCOMING_SEASON,),
        ) == 0

    def test_kickoff_and_local_date_are_stored(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        for _, _, kickoff, status, local_date in world.upcoming():
            assert kickoff.tzinfo is not None
            assert status == "scheduled"
            assert local_date is not None

    def test_the_provider_team_ids_are_mapped_through_external_ids(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """A provider primary key belongs in external_ids and nowhere else."""
        world = World(conn)
        world.ingest()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.external_id, tn.name FROM external_ids e
                     JOIN data_sources d ON d.id=e.source_id
                     JOIN team_names tn ON tn.team_id=e.internal_id
                      AND tn.valid_to IS NULL
                    WHERE d.slug='football-data-org' AND e.entity_type='team'
                      AND e.superseded_at IS NULL""",
            )
            mapped = dict(cur.fetchall())
        assert mapped.get("57") == "Arsenal"
        assert mapped.get("65") == "Manchester City"

    def test_no_fixture_external_id_is_invented(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """`entity_type` permits five registries; fixture is not one (§11.4)."""
        world = World(conn)
        world.ingest()
        assert world.count(
            "SELECT count(*) FROM external_ids WHERE entity_type='fixture'"
        ) == 0


@pytest.mark.db
class TestRawEvidence:
    def test_the_exact_bytes_are_archived_once_per_fetch(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT b.body, p.http_status, p.endpoint, p.request_signature
                     FROM raw_payloads p
                     JOIN raw_payload_bodies b ON b.id=p.body_id
                     JOIN data_sources d ON d.id=p.source_id
                    WHERE d.slug='football-data-org'"""
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert bytes(rows[0][0]) == body("upcoming")
        assert rows[0][1] == 200
        assert rows[0][2] == "/v4/competitions/PL/matches"
        assert rows[0][3]

    def test_a_refetch_adds_an_observation_but_not_a_body(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        bodies = world.count("SELECT count(*) FROM raw_payload_bodies")
        world.ingest()
        assert world.count(
            """SELECT count(*) FROM raw_payloads p JOIN data_sources d
                 ON d.id=p.source_id WHERE d.slug='football-data-org'"""
        ) == 2
        assert world.count("SELECT count(*) FROM raw_payload_bodies") == bodies

    def test_the_run_is_recorded_with_its_adapter_version(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        report = world.ingest()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_name, adapter_version, status FROM job_runs WHERE id=%s",
                (report.job_run_id,),
            )
            row = cur.fetchone()
        assert row == ("import_upcoming_fixtures", "football-data-org@1.0.0", "ok")


@pytest.mark.db
class TestScheduleRevisions:
    def test_a_postponement_supersedes_rather_than_overwrites(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest("upcoming")
        world.ingest("postponed")

        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.status, s.superseded_at IS NULL AS current
                     FROM fixture_schedule s
                     JOIN fixtures f ON f.id=s.fixture_id
                     JOIN seasons se ON se.id=f.season_id
                     JOIN team_names hn ON hn.team_id=f.home_team_id
                      AND hn.valid_to IS NULL
                    WHERE se.label=%s AND hn.name='Arsenal'
                    ORDER BY s.known_at""",
                (UPCOMING_SEASON,),
            )
            revisions = cur.fetchall()
        assert len(revisions) == 2, "the earlier fact must survive"
        assert revisions[0] == ("scheduled", False)
        assert revisions[1] == ("postponed", True)

    def test_a_reschedule_creates_a_new_revision_on_the_same_fixture(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest("upcoming")
        before = world.count(
            """SELECT count(*) FROM fixtures f JOIN seasons se
                 ON se.id=f.season_id WHERE se.label=%s""",
            (UPCOMING_SEASON,),
        )
        world.ingest("rescheduled")
        after = world.count(
            """SELECT count(*) FROM fixtures f JOIN seasons se
                 ON se.id=f.season_id WHERE se.label=%s""",
            (UPCOMING_SEASON,),
        )
        assert after == before, "kickoff is not identity; no new fixture"
        arsenal = next(r for r in world.upcoming() if r[0] == "Arsenal")
        assert arsenal[2] == datetime(2026, 11, 4, 19, 45, tzinfo=UTC)

    def test_exactly_one_current_revision_per_fixture(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest("upcoming")
        world.ingest("postponed")
        world.ingest("rescheduled")
        assert world.count(
            """SELECT count(*) FROM (
                 SELECT s.fixture_id FROM fixture_schedule s
                   JOIN fixtures f ON f.id=s.fixture_id
                   JOIN seasons se ON se.id=f.season_id
                  WHERE se.label=%s AND s.superseded_at IS NULL
                  GROUP BY 1 HAVING count(*)>1) d""",
            (UPCOMING_SEASON,),
        ) == 0

    def test_known_at_is_when_we_learned_not_what_the_provider_claims(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """`lastUpdated` in the payload must never become `known_at`."""
        world = World(conn)
        before = datetime.now(UTC)
        world.ingest()
        claimed = json.loads(body("upcoming"))["matches"][0]["lastUpdated"]
        assert claimed.startswith("2026-09-01")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT min(s.known_at) FROM fixture_schedule s
                     JOIN fixtures f ON f.id=s.fixture_id
                     JOIN seasons se ON se.id=f.season_id
                    WHERE se.label=%s""",
                (UPCOMING_SEASON,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] >= before

    def test_kickoff_is_never_the_current_time(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        now = datetime.now(UTC)
        for _, _, kickoff, _, _ in world.upcoming():
            assert abs((kickoff - now).total_seconds()) > 3600


@pytest.mark.db
class TestIdempotency:
    def test_a_rerun_writes_no_new_canonical_row(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        first = world.ingest()
        snapshot = world.upcoming()
        second = world.ingest()

        assert second.fixtures == first.fixtures
        assert second.schedules_written == 0
        assert second.unchanged == first.fixtures
        assert world.upcoming() == snapshot

    def test_no_duplicate_fixture_is_created(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        world.ingest()
        assert world.count(
            """SELECT count(*) FROM (
                 SELECT season_id,home_team_id,away_team_id FROM fixtures
                  GROUP BY 1,2,3 HAVING count(*)>1) d"""
        ) == 0

    def test_team_mappings_are_not_duplicated(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        world.ingest()
        world.ingest()
        assert world.count(
            """SELECT count(*) FROM (
                 SELECT source_id,entity_type,external_id FROM external_ids
                  WHERE superseded_at IS NULL GROUP BY 1,2,3 HAVING count(*)>1) d"""
        ) == 0


@pytest.mark.db
class TestIdentityFailsClosed:
    def test_an_unknown_club_is_refused_and_reviewed(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        report = world.ingest("unknown_team")

        assert report.status is RunStatus.PARTIAL
        assert report.unresolved == 1
        assert report.review_items == 1
        # The good fixture still landed; the bad one did not.
        assert report.fixtures == 1
        with conn.cursor() as cur:
            cur.execute(
                """SELECT external_id, reason, status FROM entity_review_queue
                     WHERE entity_type='team' ORDER BY id DESC LIMIT 1"""
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "Barnstoneworth United FC"
        assert row[1].startswith("unknown")
        assert row[2] == "open"

    def test_no_team_is_created_for_an_unknown_club(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        before = world.count("SELECT count(*) FROM teams")
        world.ingest("unknown_team")
        assert world.count("SELECT count(*) FROM teams") == before

    def test_nothing_resolves_by_near_miss(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """No fuzzy matching: "Barnstoneworth United FC" must not become a club."""
        world = World(conn)
        world.ingest("unknown_team")
        assert world.count(
            """SELECT count(*) FROM fixtures f JOIN seasons se
                 ON se.id=f.season_id WHERE se.label=%s""",
            (UPCOMING_SEASON,),
        ) == 1

    def test_a_repeated_pairing_is_refused_at_the_parser(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        world = World(conn)
        report = world.ingest("repeated_pairing")
        assert any("meeting ordinal" in p for p in report.problems)
        pairings = {(r[0], r[1]) for r in world.upcoming()}
        assert ("Arsenal", "Chelsea") not in pairings


@pytest.mark.db
class TestChainToPrediction:
    def test_an_ingested_fixture_becomes_predictable(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """provider -> fixture -> schedule -> eligibility -> prediction."""
        world = World(conn)
        world.ingest()

        # The PRODUCTION invocation: the cutoff is now, and the eligible
        # fixtures are the ones that have not kicked off. The payloads sit a
        # few days ahead of the clock, which is what makes them upcoming.
        known_at = datetime.now(UTC)
        cutoff = known_at

        eligible = load_eligible_fixtures(
            conn,
            competition_slug="england-premier-league",
            data_cutoff=cutoff,
            as_of=known_at,
            season_labels=[UPCOMING_SEASON],
        )
        assert len(eligible) == 3, "the upcoming fixtures are eligible"
        assert all(f.kickoff >= cutoff for f in eligible)

        report = generate(
            conn,
            data_cutoff=cutoff,
            known_at=known_at,
            competition_slug="england-premier-league",
            seasons=(HISTORY_SEASON, UPCOMING_SEASON),
        )
        assert report.status is RunStatus.OK
        assert report.eligible == 3
        assert report.created == 3
        assert report.training_matches >= 30

        with conn.cursor() as cur:
            cur.execute(
                """SELECT hn.name, an.name, p.p_home, p.p_draw, p.p_away,
                          p.lambda_home, p.is_cold_start
                     FROM predictions p
                     JOIN fixtures f ON f.id=p.fixture_id
                     JOIN seasons se ON se.id=f.season_id
                     JOIN team_names hn ON hn.team_id=f.home_team_id
                      AND hn.valid_to IS NULL
                     JOIN team_names an ON an.team_id=f.away_team_id
                      AND an.valid_to IS NULL
                    WHERE se.label=%s ORDER BY hn.name""",
                (UPCOMING_SEASON,),
            )
            rows = cur.fetchall()
        assert len(rows) == 3
        for _, _, p_home, p_draw, p_away, lam, _cold in rows:
            assert abs(p_home + p_draw + p_away - 1) < 1e-9
            assert lam > 0

    def test_no_result_is_needed_for_the_fixture_to_be_predicted(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The upcoming fixtures have no score, and that is the whole point."""
        world = World(conn)
        world.ingest()
        assert world.count(
            """SELECT count(*) FROM match_results r
                 JOIN fixtures f ON f.id=r.fixture_id
                 JOIN seasons se ON se.id=f.season_id
                WHERE se.label=%s""",
            (UPCOMING_SEASON,),
        ) == 0
        now = datetime.now(UTC)
        generate(
            conn,
            data_cutoff=now,
            known_at=now,
            competition_slug="england-premier-league",
            seasons=(HISTORY_SEASON, UPCOMING_SEASON),
        )
        assert world.count(
            """SELECT count(*) FROM predictions p JOIN fixtures f
                 ON f.id=p.fixture_id JOIN seasons se ON se.id=f.season_id
                WHERE se.label=%s""",
            (UPCOMING_SEASON,),
        ) == 3
