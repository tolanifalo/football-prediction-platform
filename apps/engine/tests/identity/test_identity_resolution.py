"""Resolver and bitemporal identity behaviour (PHASE-0-SPEC.md §17.2-§17.3).

EVERY TEST HERE RUNS INSIDE A TRANSACTION THAT IS ROLLED BACK. Nothing is
committed, so the imported E0 2023/24 development data is never touched - a
deliberate departure from P0-11's `clean_db` fixture, which empties the
canonical tables and would destroy it.

The bitemporal cases are the ones that matter. A mapping is not a fact about
now; it is a fact about what we believed at a moment, and the whole point of
`external_ids` being bitemporal is that correcting it must not rewrite the
past. These tests read through `external_ids_as_of`, the P0-06 wrapper, and
never restate its predicate (§11.1).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from engine.db import database_url
from engine.ingestion.dto import ProviderRef
from engine.ingestion.identity import Ambiguous, EntityKind, Resolved, Unknown
from engine.ingestion.postgres import (
    ExternalIdStore,
    MappingOutcome,
    PostgresIdentityResolver,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    """A connection whose every write is discarded.

    `autocommit` stays off and no test commits, so the ROLLBACK at teardown
    removes everything - including the rows the seed helper inserts.
    """
    connection = psycopg.connect(database_url())
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _tag() -> str:
    """A unique suffix, so a seeded slug can never collide with real data."""
    return uuid.uuid4().hex[:12]


def seed_source(conn: psycopg.Connection[Any], slug: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO data_sources (slug, display_name, kinds, base_url)
               VALUES (%s, %s, '{fixtures}', 'https://example.test')
               RETURNING id""",
            (slug, slug),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def seed_team(conn: psycopg.Connection[Any], slug: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM countries WHERE slug = 'england'",
        )
        found = cur.fetchone()
        if found is None:
            cur.execute(
                "INSERT INTO countries (slug, name) VALUES (%s,%s) RETURNING id",
                (f"country-{_tag()}", "Testland"),
            )
            found = cur.fetchone()
        assert found is not None
        cur.execute(
            """INSERT INTO teams (slug, country_id, gender)
               VALUES (%s, %s, 'men') RETURNING id""",
            (slug, found[0]),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def seed_alias(
    conn: psycopg.Connection[Any],
    team_id: UUID,
    alias: str,
    source_id: UUID | None,
    created_at: datetime = T0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO team_aliases
                 (team_id, alias, normalized_alias, source_id, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (team_id, alias, alias.strip().lower(), source_id, created_at),
        )


@pytest.mark.db
class TestExactResolution:
    def test_an_exact_alias_resolves_to_one_team(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        seed_alias(conn, team, "Nott'm Forest", source)

        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        outcome = resolver.resolve(EntityKind.TEAM, ProviderRef(name="Nott'm Forest"))

        assert outcome == Resolved(entity_kind=EntityKind.TEAM, internal_id=team)

    def test_alias_matching_is_case_insensitive_but_not_fuzzy(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        seed_alias(conn, team, "Man City", source)
        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)

        assert isinstance(
            resolver.resolve(EntityKind.TEAM, ProviderRef(name="  man city ")),
            Resolved,
        )
        # One character off is NOT a match. No edit distance, ever.
        assert isinstance(
            resolver.resolve(EntityKind.TEAM, ProviderRef(name="Man Cty")), Unknown
        )
        # Nor is a prefix, a substring or a superstring.
        for near_miss in ("Man", "Manchester City", "Man City FC"):
            assert isinstance(
                resolver.resolve(EntityKind.TEAM, ProviderRef(name=near_miss)),
                Unknown,
            ), near_miss

    def test_resolution_creates_nothing(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM teams")
            before = cur.fetchone()
        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        resolver.resolve(EntityKind.TEAM, ProviderRef(name="Never Heard Of It"))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM teams")
            after = cur.fetchone()
        assert before == after


@pytest.mark.db
class TestFailsClosed:
    def test_an_unknown_string_is_unknown(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        outcome = resolver.resolve(EntityKind.TEAM, ProviderRef(name="Ath Bilbao"))
        assert isinstance(outcome, Unknown)

    def test_an_alias_matching_two_teams_is_ambiguous(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """"Arsenal" is Arsenal FC, Arsenal Tula and Arsenal Sarandi (§10.3)."""
        source = seed_source(conn, f"src-{_tag()}")
        english = seed_team(conn, f"arsenal-{_tag()}")
        russian = seed_team(conn, f"arsenal-tula-{_tag()}")
        # One global alias and one provider-scoped alias, pointing at different
        # clubs. Both partial unique indexes permit this; the resolver must not.
        seed_alias(conn, english, f"Arsenal {_tag()}", None)
        ambiguous_string = f"Arsenal {_tag()}"
        seed_alias(conn, english, ambiguous_string, None)
        seed_alias(conn, russian, ambiguous_string, source)

        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        outcome = resolver.resolve(
            EntityKind.TEAM, ProviderRef(name=ambiguous_string)
        )

        assert isinstance(outcome, Ambiguous)
        assert set(outcome.candidates) == {english, russian}

    def test_a_non_team_entity_has_no_alias_fallback(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """Only teams have aliases. An unmapped competition is simply unknown."""
        source = seed_source(conn, f"src-{_tag()}")
        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        outcome = resolver.resolve(
            EntityKind.COMPETITION, ProviderRef(provider_key="E0", name="E0")
        )
        assert isinstance(outcome, Unknown)
        assert outcome.reason == "no external_ids mapping"

    def test_another_providers_alias_does_not_resolve_for_us(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        ours = seed_source(conn, f"ours-{_tag()}")
        theirs = seed_source(conn, f"theirs-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        their_string = f"Their Spelling {_tag()}"
        seed_alias(conn, team, their_string, theirs)

        resolver = PostgresIdentityResolver(conn, source_id=ours, as_of=T2)
        assert isinstance(
            resolver.resolve(EntityKind.TEAM, ProviderRef(name=their_string)),
            Unknown,
        )


@pytest.mark.db
class TestBitemporalVisibility:
    def test_a_current_mapping_resolves(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        assert (
            store.ensure(EntityKind.TEAM, "Burnley", team, known_at=T0)
            is MappingOutcome.CREATED
        )

        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T1)
        outcome = resolver.resolve(EntityKind.TEAM, ProviderRef(name="Burnley"))
        assert outcome == Resolved(entity_kind=EntityKind.TEAM, internal_id=team)

    def test_a_mapping_is_invisible_before_it_was_known(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The leakage rule: we cannot have known this yesterday."""
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        ExternalIdStore(conn, source_id=source).ensure(
            EntityKind.TEAM, "Burnley", team, known_at=T1
        )
        resolver = PostgresIdentityResolver(conn, source_id=source, as_of=T0)
        assert isinstance(
            resolver.resolve(EntityKind.TEAM, ProviderRef(name="Burnley")), Unknown
        )

    def test_a_superseded_mapping_stops_resolving_and_the_new_one_takes_over(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        wrong = seed_team(conn, f"wrong-{_tag()}")
        right = seed_team(conn, f"right-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        store.ensure(EntityKind.TEAM, "Sheffield United", wrong, known_at=T0)

        assert store.supersede_and_remap(
            EntityKind.TEAM, "Sheffield United", right, at=T1
        )

        after = PostgresIdentityResolver(conn, source_id=source, as_of=T2)
        assert after.resolve(
            EntityKind.TEAM, ProviderRef(name="Sheffield United")
        ) == Resolved(entity_kind=EntityKind.TEAM, internal_id=right)

    def test_a_historical_cutoff_still_sees_the_old_mapping(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """Correcting a mapping must not rewrite what we believed before."""
        source = seed_source(conn, f"src-{_tag()}")
        wrong = seed_team(conn, f"wrong-{_tag()}")
        right = seed_team(conn, f"right-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        store.ensure(EntityKind.TEAM, "Sheffield United", wrong, known_at=T0)
        store.supersede_and_remap(EntityKind.TEAM, "Sheffield United", right, at=T1)

        earlier = PostgresIdentityResolver(
            conn, source_id=source, as_of=T0 + timedelta(days=1)
        )
        assert earlier.resolve(
            EntityKind.TEAM, ProviderRef(name="Sheffield United")
        ) == Resolved(entity_kind=EntityKind.TEAM, internal_id=wrong)

    def test_exactly_one_revision_is_visible_at_every_cutoff(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The handover instant is shared, so the two revisions never overlap."""
        source = seed_source(conn, f"src-{_tag()}")
        wrong = seed_team(conn, f"wrong-{_tag()}")
        right = seed_team(conn, f"right-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        store.ensure(EntityKind.TEAM, "Luton", wrong, known_at=T0)
        store.supersede_and_remap(EntityKind.TEAM, "Luton", right, at=T1)

        expected = [
            (T0 - timedelta(seconds=1), None),
            (T0, wrong),
            (T1 - timedelta(seconds=1), wrong),
            (T1, right),
            (T2, right),
        ]
        for cutoff, want in expected:
            outcome = PostgresIdentityResolver(
                conn, source_id=source, as_of=cutoff
            ).resolve(EntityKind.TEAM, ProviderRef(name="Luton"))
            if want is None:
                assert isinstance(outcome, Unknown), cutoff
            else:
                assert outcome == Resolved(
                    entity_kind=EntityKind.TEAM, internal_id=want
                ), cutoff

    def test_overlapping_revisions_resolve_to_ambiguous_not_a_guess(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """A history that says two things at once must not be silently picked."""
        source = seed_source(conn, f"src-{_tag()}")
        first = seed_team(conn, f"first-{_tag()}")
        second = seed_team(conn, f"second-{_tag()}")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO external_ids
                     (source_id, entity_type, external_id, internal_id,
                      known_at, superseded_at)
                   VALUES (%s,'team','Wolves',%s,%s,%s)""",
                (source, first, T0, T2),
            )
            cur.execute(
                """INSERT INTO external_ids
                     (source_id, entity_type, external_id, internal_id, known_at)
                   VALUES (%s,'team','Wolves',%s,%s)""",
                (source, second, T1),
            )
        # T1..T2 is covered by BOTH revisions.
        outcome = PostgresIdentityResolver(
            conn, source_id=source, as_of=T1 + timedelta(days=1)
        ).resolve(EntityKind.TEAM, ProviderRef(name="Wolves"))
        assert isinstance(outcome, Ambiguous)
        assert set(outcome.candidates) == {first, second}

    def test_a_mapping_beats_an_alias_pointing_elsewhere(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """An explicit decision outranks a matching string."""
        source = seed_source(conn, f"src-{_tag()}")
        by_alias = seed_team(conn, f"alias-{_tag()}")
        by_mapping = seed_team(conn, f"mapped-{_tag()}")
        key = f"Ambiguous Name {_tag()}"
        seed_alias(conn, by_alias, key, source)
        ExternalIdStore(conn, source_id=source).ensure(
            EntityKind.TEAM, key, by_mapping, known_at=T0
        )
        outcome = PostgresIdentityResolver(
            conn, source_id=source, as_of=T1
        ).resolve(EntityKind.TEAM, ProviderRef(name=key))
        assert outcome == Resolved(
            entity_kind=EntityKind.TEAM, internal_id=by_mapping
        )


@pytest.mark.db
class TestNeverAutoRemap:
    def test_ensure_refuses_to_repoint_an_existing_mapping(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        first = seed_team(conn, f"first-{_tag()}")
        second = seed_team(conn, f"second-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        store.ensure(EntityKind.TEAM, "Brighton", first, known_at=T0)

        assert (
            store.ensure(EntityKind.TEAM, "Brighton", second, known_at=T1)
            is MappingOutcome.CONFLICT
        )
        # The original mapping is untouched, and no second row was written.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT internal_id FROM external_ids
                    WHERE source_id=%s AND external_id='Brighton'""",
                (source,),
            )
            rows = [UUID(str(r[0])) for r in cur.fetchall()]
        assert rows == [first]

    def test_ensure_is_idempotent_for_the_same_mapping(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        assert store.ensure(EntityKind.TEAM, "Fulham", team) is MappingOutcome.CREATED
        assert store.ensure(EntityKind.TEAM, "Fulham", team) is MappingOutcome.UNCHANGED
        assert store.ensure(EntityKind.TEAM, "Fulham", team) is MappingOutcome.UNCHANGED
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM external_ids WHERE source_id=%s", (source,)
            )
            row = cur.fetchone()
        assert row is not None and row[0] == 1

    def test_internal_id_cannot_be_rewritten_in_place(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """The P0-06 trigger, exercised from the P0-12 side (§11.3 rule 3)."""
        source = seed_source(conn, f"src-{_tag()}")
        first = seed_team(conn, f"first-{_tag()}")
        second = seed_team(conn, f"second-{_tag()}")
        ExternalIdStore(conn, source_id=source).ensure(
            EntityKind.TEAM, "Everton", first
        )
        with conn.cursor() as cur, pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute(
                """UPDATE external_ids SET internal_id=%s
                    WHERE source_id=%s AND external_id='Everton'""",
                (second, source),
            )

    def test_supersede_and_remap_is_a_no_op_when_nothing_changed(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        team = seed_team(conn, f"team-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        store.ensure(EntityKind.TEAM, "Chelsea", team, known_at=T0)

        # Remapping a key to where it already points writes NOTHING: no
        # supersede, no new revision, no spurious entry in the mapping's
        # history. Returning False says so.
        assert (
            store.supersede_and_remap(EntityKind.TEAM, "Chelsea", team, at=T1)
            is False
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM external_ids
                    WHERE source_id=%s AND superseded_at IS NOT NULL""",
                (source,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] == 0


@pytest.mark.db
class TestReviewQueue:
    def test_filing_the_same_open_item_twice_adds_one_row(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        source = seed_source(conn, f"src-{_tag()}")
        store = ExternalIdStore(conn, source_id=source)
        assert store.file_for_review(EntityKind.TEAM, "Ath Madrid", "unknown") is True
        assert store.file_for_review(EntityKind.TEAM, "Ath Madrid", "unknown") is False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM entity_review_queue WHERE source_id=%s",
                (source,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] == 1

    def test_an_ambiguous_item_names_no_candidate(
        self, conn: psycopg.Connection[Any]
    ) -> None:
        """Recording one of several candidates would be the guess itself."""
        source = seed_source(conn, f"src-{_tag()}")
        ExternalIdStore(conn, source_id=source).file_for_review(
            EntityKind.TEAM, "Arsenal", "ambiguous: alias matches 3 teams"
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT candidate_internal_id, status FROM entity_review_queue
                    WHERE source_id=%s""",
                (source,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] is None and row[1] == "open"
