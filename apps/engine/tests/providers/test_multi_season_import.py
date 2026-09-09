"""Several seasons through the same pipeline, twice (MODEL-BASELINE.md §5).

The single-season case is covered by `test_import_integration.py`. What is new
here is everything that only shows up once a SECOND season exists: one
competition rather than two, distinct seasons under it, a team registry shared
across seasons rather than duplicated, and idempotency that has to hold for
each scope independently.

Marked `db`, and it reuses P0-11's `clean_db`, which EMPTIES the canonical
tables at setup and teardown. Running this against the development database
destroys an imported corpus; re-run the import jobs afterwards.
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import psycopg
import pytest

from engine.ingestion.runs import RunStatus
from engine.ingestion.transport import HttpxTransport
from engine.jobs.import_football_data import run_import
from engine.jobs.resolve_identities import run_resolution
from engine.providers.football_data_couk.seed import E0_TEAMS
from providers.test_import_integration import clean_db

__all__ = ["clean_db"]

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
#: Two real seasons whose files differ in column count and bookmaker set.
SEASONS = ("2223", "2425")


def sample(season: str) -> bytes:
    return (FIXTURES / f"E0_{season}_sample.csv").read_bytes()


def transport_for(body: bytes) -> HttpxTransport:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body)),
        base_url="https://football-data.co.uk",
    )
    return HttpxTransport("https://football-data.co.uk", client=client)


def import_all(conn: psycopg.Connection[Any]) -> list[Any]:
    return [
        run_import(
            conn, division="E0", season=season,
            transport=transport_for(sample(season)),
        )
        for season in SEASONS
    ]


def count(conn: psycopg.Connection[Any], sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return int(row[0]) if row else 0


@pytest.mark.db
class TestMultiSeasonCorpus:
    def test_both_seasons_import(self, clean_db: psycopg.Connection[Any]) -> None:
        reports = import_all(clean_db)
        assert all(r.status is RunStatus.OK for r in reports)
        assert count(clean_db, "SELECT count(*) FROM fixtures") == sum(
            r.fixtures for r in reports
        )

    def test_one_competition_and_two_distinct_seasons(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        """A season is not a competition. Two files must not fork the league."""
        import_all(clean_db)
        assert count(clean_db, "SELECT count(*) FROM competitions") == 1
        assert count(clean_db, "SELECT count(*) FROM seasons") == len(SEASONS)
        with clean_db.cursor() as cur:
            cur.execute("SELECT count(DISTINCT competition_id) FROM seasons")
            row = cur.fetchone()
        assert row is not None and row[0] == 1

    def test_the_team_registry_is_shared_not_duplicated(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        """`teams` is a registry (§10.5): a club spanning seasons has one row."""
        import_all(clean_db)
        assert count(clean_db, "SELECT count(*) FROM teams") == len(E0_TEAMS)
        assert count(clean_db, "SELECT count(*) FROM team_aliases") == len(E0_TEAMS)

    def test_each_fixture_belongs_to_exactly_one_season(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        import_all(clean_db)
        assert (
            count(
                clean_db,
                """SELECT count(*) FROM (
                     SELECT season_id, home_team_id, away_team_id FROM fixtures
                      GROUP BY 1,2,3 HAVING count(*) > 1) d""",
            )
            == 0
        )


@pytest.mark.db
class TestMultiSeasonIdempotency:
    def test_a_second_pass_over_every_season_changes_nothing(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        import_all(clean_db)
        tables = (
            "fixtures", "fixture_schedule", "match_results", "match_stats",
            "odds_series", "odds_ticks", "teams", "team_aliases", "seasons",
            "competitions", "bookmakers", "raw_payload_bodies",
        )
        before = {t: count(clean_db, f"SELECT count(*) FROM {t}") for t in tables}
        payloads_before = count(clean_db, "SELECT count(*) FROM raw_payloads")

        second = import_all(clean_db)

        after = {t: count(clean_db, f"SELECT count(*) FROM {t}") for t in tables}
        assert after == before
        for report in second:
            assert report.schedules_written == 0
            assert report.results_written == 0
            assert report.stats_written == 0
            assert report.ticks_written == 0
        # Evidence grows by one observation per season; bodies do not.
        assert count(clean_db, "SELECT count(*) FROM raw_payloads") == (
            payloads_before + len(SEASONS)
        )

    def test_identity_resolution_is_idempotent_across_seasons(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        import_all(clean_db)
        first = [
            run_resolution(clean_db, division="E0", season=s) for s in SEASONS
        ]
        second = [
            run_resolution(clean_db, division="E0", season=s) for s in SEASONS
        ]
        # One competition mapping shared, one season mapping each, teams once.
        assert first[0].created == len(E0_TEAMS) + 2
        assert first[1].created == 1
        for report in second:
            assert report.created == 0
            assert report.conflicts == 0
            assert report.review_items == 0
        assert count(clean_db, "SELECT count(*) FROM entity_review_queue") == 0
        assert count(clean_db, "SELECT count(*) FROM external_ids") == (
            len(E0_TEAMS) + 1 + len(SEASONS)
        )

    def test_no_duplicate_current_facts_after_two_passes(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        import_all(clean_db)
        import_all(clean_db)
        for table, key in (
            ("fixture_schedule", "fixture_id"),
            ("match_results", "fixture_id, source_id"),
            ("match_stats", "fixture_id, source_id"),
        ):
            assert (
                count(
                    clean_db,
                    f"""SELECT count(*) FROM (
                          SELECT {key} FROM {table} WHERE superseded_at IS NULL
                           GROUP BY {key} HAVING count(*) > 1) d""",
                )
                == 0
            ), table
