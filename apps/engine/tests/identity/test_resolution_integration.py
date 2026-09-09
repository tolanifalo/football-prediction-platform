"""The P0-12 acceptance test: import, then resolve, then resolve again.

This one CANNOT use the rollback fixture the rest of the identity suite uses:
`run_resolution` commits its own `job_runs` row before doing any work, exactly
as the import job does, so its effects have to be real.

It therefore reuses P0-11's `clean_db`, and that fixture EMPTIES THE CANONICAL
TABLES at setup and teardown. Running this suite against the development
database destroys an import; re-run the import job afterwards. That behaviour
is inherited, not introduced here, and it is why every other identity test
rolls back instead.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from providers.test_import_integration import SAMPLE, clean_db, transport_for

from engine.ingestion.dto import ProviderRef
from engine.ingestion.identity import EntityKind, Resolved
from engine.ingestion.postgres import PostgresIdentityResolver
from engine.ingestion.runs import RunStatus
from engine.jobs.import_football_data import run_import
from engine.jobs.resolve_identities import run_resolution, season_external_id

# Re-exported so pytest can see the fixture in this module's namespace.
__all__ = ["clean_db"]

EXPECTED_TEAMS = 20
#: 20 teams + the E0 competition + the 2023/24 season.
EXPECTED_MAPPINGS = EXPECTED_TEAMS + 2


def imported(conn: psycopg.Connection[Any]) -> None:
    run_import(
        conn,
        division="E0",
        season="2324",
        transport=transport_for(SAMPLE.read_bytes()),
    )


def counts(conn: psycopg.Connection[Any], sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return int(row[0]) if row else 0


@pytest.mark.db
class TestFullResolution:
    def test_every_e0_identity_resolves_deterministically(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        imported(clean_db)
        assert counts(clean_db, "SELECT count(*) FROM external_ids") == 0

        report = run_resolution(clean_db, division="E0", season="2324")

        assert report.status is RunStatus.OK
        assert report.resolved_teams == EXPECTED_TEAMS
        assert report.created == EXPECTED_MAPPINGS
        assert report.unchanged == 0
        # The three that must be zero, or the run guessed something.
        assert report.ambiguous == 0
        assert report.unknown == 0
        assert report.conflicts == 0
        assert report.review_items == 0
        assert report.problems == []

    def test_the_mappings_are_the_ones_the_provider_actually_supplies(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        imported(clean_db)
        run_resolution(clean_db, division="E0", season="2324")

        with clean_db.cursor() as cur:
            cur.execute(
                """SELECT entity_type, count(*) FROM external_ids
                    WHERE superseded_at IS NULL GROUP BY 1 ORDER BY 1"""
            )
            by_type = {str(t): int(n) for t, n in cur.fetchall()}
        assert by_type == {"competition": 1, "season": 1, "team": EXPECTED_TEAMS}

        with clean_db.cursor() as cur:
            cur.execute(
                "SELECT external_id FROM external_ids WHERE entity_type='season'"
            )
            row = cur.fetchone()
        # Not the bare folder name: `2324` names every division that season.
        assert row is not None and row[0] == season_external_id("E0", "2324")
        assert row[0] == "2324/E0"

        # No country mapping: the CSV carries no country token at all, and
        # inventing one would be an external ID the provider never supplied.
        assert (
            counts(
                clean_db,
                "SELECT count(*) FROM external_ids WHERE entity_type='country'",
            )
            == 0
        )

    def test_a_mapped_key_then_resolves_through_external_ids(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        """After the run the mapping, not the alias, is what answers."""
        imported(clean_db)
        run_resolution(clean_db, division="E0", season="2324")

        with clean_db.cursor() as cur:
            cur.execute("SELECT id FROM data_sources WHERE slug='football-data-couk'")
            row = cur.fetchone()
        assert row is not None
        resolver = PostgresIdentityResolver(clean_db, source_id=row[0])

        team = resolver.resolve(EntityKind.TEAM, ProviderRef(name="Man City"))
        assert isinstance(team, Resolved)
        # The competition has no alias table, so resolving it at all proves
        # the answer came from external_ids.
        competition = resolver.resolve(
            EntityKind.COMPETITION, ProviderRef(provider_key="E0", name="E0")
        )
        assert isinstance(competition, Resolved)


@pytest.mark.db
class TestIdempotency:
    def test_a_second_run_changes_nothing(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        imported(clean_db)
        first = run_resolution(clean_db, division="E0", season="2324")
        second = run_resolution(clean_db, division="E0", season="2324")

        assert second.status is RunStatus.OK
        assert second.created == 0, "a rerun must write no new mapping"
        assert second.unchanged == EXPECTED_MAPPINGS
        assert second.conflicts == 0
        assert second.review_items == 0
        assert second.mapped == first.mapped

        assert (
            counts(clean_db, "SELECT count(*) FROM external_ids")
            == EXPECTED_MAPPINGS
        )
        assert counts(clean_db, "SELECT count(*) FROM entity_review_queue") == 0
        # Nothing was superseded, because nothing disagreed.
        assert (
            counts(
                clean_db,
                "SELECT count(*) FROM external_ids WHERE superseded_at IS NOT NULL",
            )
            == 0
        )

    def test_no_duplicate_current_mapping_survives_two_runs(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        imported(clean_db)
        run_resolution(clean_db, division="E0", season="2324")
        run_resolution(clean_db, division="E0", season="2324")

        assert (
            counts(
                clean_db,
                """SELECT count(*) FROM (
                     SELECT source_id, entity_type, external_id FROM external_ids
                      WHERE superseded_at IS NULL
                      GROUP BY 1,2,3 HAVING count(*) > 1) d""",
            )
            == 0
        )

    def test_resolution_leaves_the_imported_facts_untouched(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        """Identity resolution reads facts; it must never rewrite one."""
        imported(clean_db)
        tables = (
            "fixtures", "fixture_schedule", "match_results", "match_stats",
            "odds_series", "odds_ticks", "raw_payloads", "raw_payload_bodies",
            "teams", "team_aliases",
        )
        before = {t: counts(clean_db, f"SELECT count(*) FROM {t}") for t in tables}

        run_resolution(clean_db, division="E0", season="2324")
        run_resolution(clean_db, division="E0", season="2324")

        after = {t: counts(clean_db, f"SELECT count(*) FROM {t}") for t in tables}
        assert after == before
        assert (
            counts(
                clean_db,
                """SELECT count(*) FROM fixture_schedule
                    WHERE superseded_at IS NOT NULL""",
            )
            == 0
        )
