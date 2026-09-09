"""Postgres implementations of the P0-10 ports (PHASE-0-SPEC.md §16.6).

EXPLICIT psycopg SQL, NO ORM. Drizzle owns the schema and Python never
migrates, so every statement here is written against the 16-migration schema
exactly as it stands. No table, column or constraint is added.

Nothing in `engine.providers` may import this module: adapters return DTOs and
never touch a connection. That boundary is asserted by a test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from engine.ingestion.archive import ArchiveRecord
from engine.ingestion.dto import (
    CanonicalFixture,
    CanonicalOdds,
    CanonicalResult,
    CanonicalStats,
    PayloadRef,
    ProviderRef,
)
from engine.ingestion.identity import (
    Ambiguous,
    EntityKind,
    Resolution,
    Resolved,
    Unknown,
)
from engine.ingestion.runs import RunIdentity, RunStatus
from engine.ingestion.signatures import body_hash
from engine.ingestion.stats import IngestionStats
from engine.model.artifact import PERSISTED_LINES, PredictionArtifact

_BODY_UPSERT = """
INSERT INTO raw_payload_bodies (hash_algo, body_hash, body, byte_size)
VALUES ('sha256', %s, %s, %s)
ON CONFLICT (hash_algo, body_hash) DO NOTHING
"""

_BODY_SELECT = """
SELECT id FROM raw_payload_bodies
 WHERE hash_algo = 'sha256' AND body_hash = %s
"""

_OBSERVATION_INSERT = """
INSERT INTO raw_payloads
  (source_id, job_run_id, body_id, endpoint, request_signature,
   request_params, response_headers, http_status, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PostgresRawArchive:
    """Persists evidence, and commits it before anything is parsed.

    Bodies deduplicate globally on (hash_algo, body_hash); observations never
    deduplicate, because a re-fetch and a retry are each a new observation.
    """

    def __init__(self, conn: psycopg.Connection[Any], *, source_id: UUID) -> None:
        self._conn = conn
        self._source_id = source_id

    def store(self, record: ArchiveRecord) -> PayloadRef:
        ref = record.payload_ref()
        digest = body_hash(record.content)
        with self._conn.cursor() as cur:
            cur.execute(_BODY_UPSERT, (digest, record.content, len(record.content)))
            cur.execute(_BODY_SELECT, (digest,))
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                _OBSERVATION_INSERT,
                (
                    self._source_id,
                    record.job_run_id,
                    row[0],
                    record.endpoint,
                    ref.request_signature,
                    Jsonb(dict(record.params)),
                    Jsonb(dict(record.response_headers)),
                    record.http_status,
                    record.fetched_at or datetime.now(UTC),
                ),
            )
        return ref

    def body_id_for(self, digest: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(_BODY_SELECT, (digest,))
            row = cur.fetchone()
            assert row is not None, f"body {digest} was never archived"
            return int(row[0])


_RUN_INSERT = """
INSERT INTO job_runs
  (job_name, scope_key, run_date, attempt, source_id, adapter_version,
   params, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, 'running')
RETURNING id
"""

_RUN_NEXT_ATTEMPT = """
SELECT coalesce(max(attempt), 0) + 1 FROM job_runs
 WHERE job_name = %s AND scope_key = %s AND run_date = %s
"""

_RUN_CLOSE = """
UPDATE job_runs
   SET status = %s, finished_at = now(), stats = %s, error = %s
 WHERE id = %s
"""


class PostgresJobRunStore:
    """Opens and closes one `job_runs` row per invocation (§9.2)."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def open(
        self,
        identity: RunIdentity,
        *,
        source_id: UUID,
        adapter_version: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                _RUN_INSERT,
                (
                    identity.job_name,
                    identity.scope_key,
                    identity.run_date,
                    identity.attempt,
                    source_id,
                    adapter_version,
                    Jsonb(params or {}),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def next_attempt(self, identity: RunIdentity) -> int:
        """A duplicate run is a 23505 on the unique key; pick the next attempt."""
        with self._conn.cursor() as cur:
            cur.execute(
                _RUN_NEXT_ATTEMPT,
                (identity.job_name, identity.scope_key, identity.run_date),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 1

    def close(
        self,
        job_run_id: int,
        status: RunStatus,
        stats: IngestionStats,
        error: str | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                _RUN_CLOSE,
                (status.value, Jsonb(stats.to_json()), error, job_run_id),
            )


class ReferenceSeeder:
    """Upserts the canonical reference rows an import needs (§16.4).

    Deterministic and idempotent: every insert is keyed on a natural key, so a
    rerun changes nothing. This is NOT identity resolution - it seeds exactly
    the entities the operator declared, and resolves provider strings only by
    EXACT alias match. Fuzzy matching is P0-12.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _scalar(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None

    def country(self, slug: str, name: str) -> UUID:
        found = self._scalar("SELECT id FROM countries WHERE slug=%s", (slug,))
        if found:
            return UUID(str(found))
        sql = "INSERT INTO countries (slug, name) VALUES (%s, %s) RETURNING id"
        return UUID(str(self._scalar(sql, (slug, name))))

    def competition(
        self,
        slug: str,
        name: str,
        country_id: UUID,
        *,
        comp_type: str,
        tier: int | None,
        gender: str,
    ) -> UUID:
        found = self._scalar("SELECT id FROM competitions WHERE slug=%s", (slug,))
        if found:
            return UUID(str(found))
        insert = """
            INSERT INTO competitions (slug, country_id, type, tier, gender)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """
        comp_id = UUID(
            str(self._scalar(insert, (slug, country_id, comp_type, tier, gender)))
        )
        name_sql = """
            INSERT INTO competition_names
              (competition_id, name, name_type, valid_from)
            VALUES (%s, %s, 'official', %s)
        """
        with self._conn.cursor() as cur:
            cur.execute(name_sql, (comp_id, name, date(1992, 8, 1)))
        return comp_id

    def season(
        self,
        slug: str,
        competition_id: UUID,
        label: str,
        start_year: int,
        start_date: date | None,
        end_date: date | None,
    ) -> UUID:
        found = self._scalar(
            "SELECT id FROM seasons WHERE competition_id=%s AND label=%s",
            (competition_id, label),
        )
        if found:
            return UUID(str(found))
        insert = """
            INSERT INTO seasons
              (slug, competition_id, label, start_year, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """
        params = (slug, competition_id, label, start_year, start_date, end_date)
        return UUID(str(self._scalar(insert, params)))

    def team(
        self,
        slug: str,
        name: str,
        country_id: UUID,
        aliases: Sequence[str],
        source_id: UUID,
    ) -> UUID:
        found = self._scalar("SELECT id FROM teams WHERE slug=%s", (slug,))
        if found:
            team_id = UUID(str(found))
        else:
            insert = """
                INSERT INTO teams (slug, country_id, gender)
                VALUES (%s, %s, 'men') RETURNING id
            """
            team_id = UUID(str(self._scalar(insert, (slug, country_id))))
            name_sql = """
                INSERT INTO team_names (team_id, name, name_type, valid_from)
                VALUES (%s, %s, 'official', %s)
            """
            with self._conn.cursor() as cur:
                cur.execute(name_sql, (team_id, name, date(1888, 1, 1)))
        # Aliases are the provider's spellings: matching strings, never
        # display truth (§10.3).
        alias_sql = """
            INSERT INTO team_aliases
              (team_id, alias, normalized_alias, source_id)
            SELECT %s, %s, %s, %s
             WHERE NOT EXISTS (
               SELECT 1 FROM team_aliases
                WHERE team_id = %s AND alias = %s
                  AND source_id IS NOT DISTINCT FROM %s)
        """
        with self._conn.cursor() as cur:
            for alias in aliases:
                lowered = alias.strip().lower()
                cur.execute(
                    alias_sql,
                    (team_id, alias, lowered, source_id, team_id, alias, source_id),
                )
        return team_id

    def bookmaker(self, slug: str, name: str) -> UUID:
        found = self._scalar("SELECT id FROM bookmakers WHERE slug=%s", (slug,))
        if found:
            return UUID(str(found))
        insert = """
            INSERT INTO bookmakers (slug, name, kind, commission_rate)
            VALUES (%s, %s, 'bookmaker', NULL) RETURNING id
        """
        return UUID(str(self._scalar(insert, (slug, name))))

    def data_source(self, slug: str, name: str, kinds: Sequence[str]) -> UUID:
        found = self._scalar("SELECT id FROM data_sources WHERE slug=%s", (slug,))
        if found:
            return UUID(str(found))
        insert = """
            INSERT INTO data_sources (slug, display_name, kinds, base_url)
            VALUES (%s, %s, %s, 'https://football-data.co.uk') RETURNING id
        """
        return UUID(str(self._scalar(insert, (slug, name, list(kinds)))))

    def alias_index(self, source_id: UUID) -> dict[str, UUID]:
        """Exact provider string -> canonical team. No fuzzy matching (P0-12)."""
        sql = "SELECT alias, team_id FROM team_aliases WHERE source_id = %s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (source_id,))
            return {str(a): UUID(str(t)) for a, t in cur.fetchall()}


_FIXTURE_SELECT = """
SELECT id FROM fixtures
 WHERE season_id=%s AND stage=%s AND leg=%s AND replay_number=%s
   AND home_team_id=%s AND away_team_id=%s
"""

_FIXTURE_INSERT = """
INSERT INTO fixtures
  (season_id, stage, leg, replay_number, home_team_id, away_team_id)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
"""

_SCHEDULE_CURRENT = """
SELECT kickoff_utc, local_date, local_tz, status, is_neutral_venue
  FROM fixture_schedule
 WHERE fixture_id=%s AND superseded_at IS NULL
"""

_SCHEDULE_CLOSE = """
UPDATE fixture_schedule SET superseded_at = now()
 WHERE fixture_id=%s AND superseded_at IS NULL
"""

_SCHEDULE_INSERT = """
INSERT INTO fixture_schedule
  (fixture_id, kickoff_utc, local_date, local_tz, venue_id, status,
   is_neutral_venue, source_id, raw_payload_body_id, known_at)
VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s)
"""

_RESULT_CURRENT = """
SELECT result_source, is_trainable, ft_home, ft_away, ht_home, ht_away
  FROM match_results
 WHERE fixture_id=%s AND source_id=%s AND superseded_at IS NULL
"""

_RESULT_CLOSE = """
UPDATE match_results SET superseded_at = now()
 WHERE fixture_id=%s AND source_id=%s AND superseded_at IS NULL
"""

_RESULT_INSERT = """
INSERT INTO match_results
  (fixture_id, result_source, is_trainable, ft_home, ft_away,
   ht_home, ht_away, source_id, raw_payload_body_id, known_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_STATS_CLOSE = """
UPDATE match_stats SET superseded_at = now()
 WHERE fixture_id=%s AND source_id=%s AND superseded_at IS NULL
"""

_SERIES_SELECT = """
SELECT id FROM odds_series
 WHERE fixture_id=%s AND bookmaker_id=%s AND period=%s AND market_type=%s
   AND line IS NOT DISTINCT FROM %s AND selection=%s AND side=%s
"""

_SERIES_INSERT = """
INSERT INTO odds_series
  (fixture_id, bookmaker_id, period, market_type, line, selection, side)
VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
"""

_TICK_CURRENT = """
SELECT price, is_available FROM odds_ticks
 WHERE series_id=%s AND source_id=%s AND observed_at=%s AND price_kind=%s
   AND superseded_at IS NULL
"""

_TICK_CLOSE = """
UPDATE odds_ticks SET superseded_at = now()
 WHERE series_id=%s AND source_id=%s AND observed_at=%s AND price_kind=%s
   AND superseded_at IS NULL
"""

_TICK_INSERT = """
INSERT INTO odds_ticks
  (series_id, source_id, raw_payload_body_id, price, is_available,
   price_kind, observed_at, provider_at, known_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s)
"""

STAT_FIELDS = (
    "home_shots",
    "away_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_corners",
    "away_corners",
    "home_fouls",
    "away_fouls",
    "home_yellow_cards",
    "away_yellow_cards",
    "home_red_cards",
    "away_red_cards",
)


class PostgresCanonicalWriter:
    """Writes canonical facts, idempotently on each table's business key.

    Every `upsert_*` returns True only when it actually wrote a row, so a
    rerun over identical data reports zero writes rather than silently
    creating revisions.
    """

    def __init__(
        self, conn: psycopg.Connection[Any], *, source_id: UUID, body_id: int
    ) -> None:
        self._conn = conn
        self._source_id = source_id
        self._body_id = body_id

    def upsert_fixture(
        self,
        season_id: UUID,
        home_id: UUID,
        away_id: UUID,
        fixture: CanonicalFixture,
    ) -> UUID:
        ref = fixture.ref
        key = (season_id, ref.stage, ref.leg, ref.replay_number, home_id, away_id)
        with self._conn.cursor() as cur:
            cur.execute(_FIXTURE_SELECT, key)
            row = cur.fetchone()
            if row:
                return UUID(str(row[0]))
            cur.execute(_FIXTURE_INSERT, key)
            created = cur.fetchone()
            assert created is not None
            return UUID(str(created[0]))

    def upsert_schedule(self, fixture_id: UUID, fixture: CanonicalFixture) -> bool:
        """A new revision ONLY when the content differs (§12.4)."""
        incoming = (
            fixture.kickoff_utc,
            fixture.local_date,
            fixture.local_tz,
            fixture.status,
            fixture.is_neutral_venue,
        )
        with self._conn.cursor() as cur:
            cur.execute(_SCHEDULE_CURRENT, (fixture_id,))
            current = cur.fetchone()
            if current is not None and tuple(current) == incoming:
                return False
            if current is not None:
                # Close then open, two ordered statements: the partial unique
                # index is not deferrable and a one-shot flip is
                # order-dependent (§10.4).
                cur.execute(_SCHEDULE_CLOSE, (fixture_id,))
            cur.execute(
                _SCHEDULE_INSERT,
                (
                    fixture_id,
                    *incoming,
                    self._source_id,
                    self._body_id,
                    fixture.known_at,
                ),
            )
            return True

    def upsert_result(self, fixture_id: UUID, result: CanonicalResult) -> bool:
        incoming = (
            result.result_source,
            result.is_trainable,
            result.ft_home,
            result.ft_away,
            result.ht_home,
            result.ht_away,
        )
        with self._conn.cursor() as cur:
            cur.execute(_RESULT_CURRENT, (fixture_id, self._source_id))
            current = cur.fetchone()
            if current is not None and tuple(current) == incoming:
                return False
            if current is not None:
                cur.execute(_RESULT_CLOSE, (fixture_id, self._source_id))
            cur.execute(
                _RESULT_INSERT,
                (
                    fixture_id,
                    *incoming,
                    self._source_id,
                    self._body_id,
                    result.known_at,
                ),
            )
            return True

    def upsert_stats(self, fixture_id: UUID, stats: CanonicalStats) -> bool:
        """Possession and xG stay NULL - this provider supplies neither."""
        incoming = tuple(getattr(stats, name) for name in STAT_FIELDS)
        columns = ", ".join(STAT_FIELDS)
        select = (
            f"SELECT {columns} FROM match_stats"
            " WHERE fixture_id=%s AND source_id=%s AND superseded_at IS NULL"
        )
        placeholders = ", ".join(["%s"] * len(STAT_FIELDS))
        insert = (
            "INSERT INTO match_stats"
            f" (fixture_id, source_id, raw_payload_body_id, known_at, {columns})"
            f" VALUES (%s, %s, %s, %s, {placeholders})"
        )
        with self._conn.cursor() as cur:
            cur.execute(select, (fixture_id, self._source_id))
            current = cur.fetchone()
            if current is not None and tuple(current) == incoming:
                return False
            if current is not None:
                cur.execute(_STATS_CLOSE, (fixture_id, self._source_id))
            cur.execute(
                insert,
                (
                    fixture_id,
                    self._source_id,
                    self._body_id,
                    stats.known_at,
                    *incoming,
                ),
            )
            return True

    def upsert_series(
        self, fixture_id: UUID, bookmaker_id: UUID, odds: CanonicalOdds
    ) -> int:
        key = (
            fixture_id,
            bookmaker_id,
            odds.period,
            odds.market_type,
            odds.line,
            odds.selection,
            odds.side,
        )
        with self._conn.cursor() as cur:
            cur.execute(_SERIES_SELECT, key)
            row = cur.fetchone()
            if row:
                return int(row[0])
            cur.execute(_SERIES_INSERT, key)
            created = cur.fetchone()
            assert created is not None
            return int(created[0])

    def append_tick(self, series_id: int, odds: CanonicalOdds) -> bool:
        """Change-only (§6 rule 15): an identical current observation is skipped."""
        key = (series_id, self._source_id, odds.observed_at, odds.price_kind)
        with self._conn.cursor() as cur:
            cur.execute(_TICK_CURRENT, key)
            current = cur.fetchone()
            if current is not None:
                if (current[0], current[1]) == (odds.price, odds.is_available):
                    return False
                cur.execute(_TICK_CLOSE, key)
            cur.execute(
                _TICK_INSERT,
                (
                    series_id,
                    self._source_id,
                    self._body_id,
                    odds.price,
                    odds.is_available,
                    odds.price_kind,
                    odds.observed_at,
                    odds.known_at,
                ),
            )
            return True


# ===========================================================================
# P0-12: identity resolution (PHASE-0-SPEC.md §17)
# ===========================================================================
# Reads go through `external_ids_as_of`, the P0-06 wrapper, so the visibility
# predicate is never restated here (§11.1). Writes hold to the P0-06 grants:
# SELECT and INSERT, plus UPDATE of `superseded_at` alone. `internal_id` is
# never rewritten - the trigger would reject it, and rule 5 of §11.3 says a
# remap supersedes and inserts.

#: Resolution through the P0-06 as-of wrapper. DISTINCT because two rows
#: mapping one provider key to the SAME canonical entity are redundant, not
#: ambiguous; two rows disagreeing about the entity genuinely are.
_MAPPING_AS_OF = """
SELECT DISTINCT internal_id FROM external_ids_as_of(%s)
 WHERE source_id = %s AND entity_type = %s AND external_id = %s
"""

#: The alias fallback, for a provider whose key IS a human-readable string.
#: `created_at` is passed through fn_visible_at with a NULL supersede time
#: because an alias is never superseded - delegating rather than writing
#: `created_at <= cutoff` by hand keeps the predicate in one place (§11.1).
_ALIAS_LOOKUP = """
SELECT DISTINCT team_id FROM team_aliases
 WHERE normalized_alias = %s
   AND (source_id = %s OR source_id IS NULL)
   AND fn_visible_at(created_at, NULL::timestamptz, %s)
"""

_MAPPING_CURRENT = """
SELECT id, internal_id FROM external_ids
 WHERE source_id = %s AND entity_type = %s AND external_id = %s
   AND superseded_at IS NULL
"""

_MAPPING_INSERT = """
INSERT INTO external_ids
  (source_id, entity_type, external_id, internal_id, confidence, known_at)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
"""

_MAPPING_SUPERSEDE = """
UPDATE external_ids SET superseded_at = %s
 WHERE id = %s AND superseded_at IS NULL
"""

#: Idempotent by construction: a second run finds the open item and adds none.
_REVIEW_INSERT = """
INSERT INTO entity_review_queue
  (entity_type, source_id, external_id, candidate_internal_id, reason)
SELECT %s, %s, %s, %s, %s
 WHERE NOT EXISTS (
   SELECT 1 FROM entity_review_queue
    WHERE entity_type = %s AND source_id = %s AND external_id = %s
      AND status = 'open')
RETURNING id
"""


class PostgresIdentityResolver:
    """The P0-12 resolver: exact matching only, and it refuses to guess.

    Implements the P0-10 `IdentityResolver` Protocol. Two deterministic
    lookups, in order:

      1. an existing `external_ids` mapping, read as of `as_of`;
      2. for teams only, an EXACT normalised alias match.

    There is no third step. No edit distance, no token overlap, no score, no
    "closest" anything. A provider string matching nothing is `Unknown`; one
    matching two canonical entities is `Ambiguous`. Neither is ever narrowed
    to a single candidate (§10.3, §6 rule 9).

    Resolution NEVER creates a canonical entity and never writes a mapping.
    Persisting a decision belongs to `ExternalIdStore`, so a read cannot have
    a write as a side effect.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        source_id: UUID,
        as_of: datetime | None = None,
    ) -> None:
        self._conn = conn
        self._source_id = source_id
        self._as_of = as_of or datetime.now(UTC)

    @property
    def as_of(self) -> datetime:
        return self._as_of

    def _ids(self, sql: str, params: tuple[Any, ...]) -> list[UUID]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [UUID(str(r[0])) for r in cur.fetchall()]

    def resolve(self, kind: EntityKind, ref: ProviderRef) -> Resolution:
        key = ref.provider_key or ref.name
        mapped = self._ids(
            _MAPPING_AS_OF, (self._as_of, self._source_id, str(kind), key)
        )
        if len(mapped) == 1:
            return Resolved(entity_kind=kind, internal_id=mapped[0])
        if len(mapped) > 1:
            # Overlapping revisions for one key at one instant. The partial
            # unique index forbids this at the CURRENT time, so arriving here
            # means a historical cutoff sees two revisions at once - a
            # supersede written with an overlapping known_at.
            return Ambiguous(
                entity_kind=kind,
                candidates=tuple(sorted(mapped, key=str)),
                reason="overlapping external_ids revisions at this cutoff",
            )

        if kind is not EntityKind.TEAM:
            # Only teams have an alias table. For every other registry an
            # absent mapping is simply unknown; there is nothing else to try.
            return Unknown(entity_kind=kind, reason="no external_ids mapping")

        matches = self._ids(
            _ALIAS_LOOKUP, (key.strip().lower(), self._source_id, self._as_of)
        )
        if len(matches) == 1:
            return Resolved(entity_kind=kind, internal_id=matches[0])
        if len(matches) > 1:
            return Ambiguous(
                entity_kind=kind,
                candidates=tuple(sorted(matches, key=str)),
                reason=f"alias matches {len(matches)} teams",
            )
        return Unknown(entity_kind=kind, reason="no mapping and no exact alias")


class MappingOutcome(StrEnum):
    """What a call to `ensure` actually did."""

    CREATED = "created"
    UNCHANGED = "unchanged"
    #: A current mapping points elsewhere. NEVER auto-remapped.
    CONFLICT = "conflict"


class ExternalIdStore:
    """Writes identity decisions, and refuses to make one silently.

    `ensure` is the automatic path and is deliberately incapable of changing
    an existing mapping: a disagreement is recorded for a human (§6 rule 9,
    "Never auto-remap"). `supersede_and_remap` is the deliberate path, used
    once a person has decided - it closes the old revision and opens a new one
    so history stays readable at its own cutoff (§11.3 rule 5).
    """

    def __init__(self, conn: psycopg.Connection[Any], *, source_id: UUID) -> None:
        self._conn = conn
        self._source_id = source_id

    def _current(self, entity_type: str, external_id: str) -> tuple[int, UUID] | None:
        with self._conn.cursor() as cur:
            cur.execute(_MAPPING_CURRENT, (self._source_id, entity_type, external_id))
            row = cur.fetchone()
            return (int(row[0]), UUID(str(row[1]))) if row else None

    def ensure(
        self,
        kind: EntityKind,
        external_id: str,
        internal_id: UUID,
        *,
        known_at: datetime | None = None,
        confidence: int | None = None,
    ) -> MappingOutcome:
        """Create the mapping if absent; never overwrite a disagreeing one."""
        current = self._current(str(kind), external_id)
        if current is not None:
            if current[1] == internal_id:
                return MappingOutcome.UNCHANGED
            return MappingOutcome.CONFLICT
        with self._conn.cursor() as cur:
            cur.execute(
                _MAPPING_INSERT,
                (
                    self._source_id,
                    str(kind),
                    external_id,
                    internal_id,
                    confidence,
                    known_at or datetime.now(UTC),
                ),
            )
        return MappingOutcome.CREATED

    def supersede_and_remap(
        self,
        kind: EntityKind,
        external_id: str,
        internal_id: UUID,
        *,
        at: datetime | None = None,
    ) -> bool:
        """Close the current revision and open a new one, at one instant.

        The two timestamps are the SAME value on purpose: the old row's
        `superseded_at` equals the new row's `known_at`, so exactly one
        revision is visible at every cutoff. Overlapping them would make the
        resolver answer `Ambiguous`, which is the correct response to a
        history that genuinely says two things at once.
        """
        moment = at or datetime.now(UTC)
        current = self._current(str(kind), external_id)
        if current is not None and current[1] == internal_id:
            return False
        with self._conn.cursor() as cur:
            if current is not None:
                cur.execute(_MAPPING_SUPERSEDE, (moment, current[0]))
            cur.execute(
                _MAPPING_INSERT,
                (self._source_id, str(kind), external_id, internal_id, None, moment),
            )
        return True

    def file_for_review(
        self,
        kind: EntityKind,
        external_id: str,
        reason: str,
        *,
        candidate: UUID | None = None,
    ) -> bool:
        """Record that a human must decide. Idempotent per open item."""
        with self._conn.cursor() as cur:
            cur.execute(
                _REVIEW_INSERT,
                (
                    str(kind), self._source_id, external_id, candidate, reason,
                    str(kind), self._source_id, external_id,
                ),
            )
            return cur.fetchone() is not None


# ===========================================================================
# P1-01: prediction persistence (PREDICTIONS.md)
# ===========================================================================
# The same shape as every other revisioned writer here: compare content first,
# write a revision ONLY when something actually differs. A rerun of the same
# (fixture, model, cutoff) with the same underlying data writes nothing at all.

#: The columns that decide whether a rerun changed anything. `known_at` is
#: excluded on purpose - regenerating the same prediction tomorrow is the same
#: prediction, and including the timestamp would make every rerun a revision.
_PREDICTION_CURRENT = """
SELECT id, lambda_home, lambda_away, max_goals, scoreline, truncated_mass,
       p_home, p_draw, p_away, p_over_0_5, p_over_1_5, p_over_2_5, p_over_3_5,
       p_btts_yes, is_cold_start, cold_started_teams, training_matches
  FROM predictions
 WHERE fixture_id = %s AND model_version = %s AND profile = %s
   AND data_cutoff = %s AND superseded_at IS NULL
"""

_PREDICTION_SUPERSEDE = """
UPDATE predictions SET superseded_at = %s
 WHERE id = %s AND superseded_at IS NULL
"""

_PREDICTION_INSERT = """
INSERT INTO predictions
  (fixture_id, model_family, model_version, profile, data_cutoff, known_at,
   lambda_home, lambda_away, max_goals, scoreline, truncated_mass,
   p_home, p_draw, p_away, p_over_0_5, p_over_1_5, p_over_2_5, p_over_3_5,
   p_btts_yes, is_cold_start, cold_started_teams, training_matches,
   fit_metadata, job_run_id)
VALUES (%s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s)
RETURNING id
"""


class PredictionOutcome(StrEnum):
    """What a call to `store` actually did."""

    CREATED = "created"
    #: The same prediction already existed. Nothing was written.
    UNCHANGED = "unchanged"
    #: Same identity, different numbers - the old row was superseded first.
    REVISED = "revised"


class PostgresPredictionStore:
    """Persists prediction artifacts, and refuses to overwrite one.

    There is no "latest" column and no in-place update of a probability. A
    regenerated prediction whose numbers differ - because a result underneath
    its cutoff was revised - closes the old revision and opens a new one, so
    what we predicted at the time stays readable at its own cutoff forever.
    """

    def __init__(
        self, conn: psycopg.Connection[Any], *, job_run_id: int | None = None
    ) -> None:
        self._conn = conn
        self._job_run_id = job_run_id

    def _comparable(self, artifact: PredictionArtifact) -> tuple[Any, ...]:
        return (
            artifact.lambda_home,
            artifact.lambda_away,
            artifact.max_goals,
            list(artifact.scoreline),
            artifact.truncated_mass,
            artifact.p_home,
            artifact.p_draw,
            artifact.p_away,
            *(artifact.p_over[line] for line in PERSISTED_LINES),
            artifact.p_btts_yes,
            artifact.is_cold_start,
            list(artifact.cold_started_teams),
            artifact.training_matches,
        )

    def store(self, artifact: PredictionArtifact) -> PredictionOutcome:
        """Persist one prediction. Validates before it touches the database."""
        artifact.validate()
        fixture_id, model_version, profile, data_cutoff = artifact.identity

        with self._conn.cursor() as cur:
            cur.execute(
                _PREDICTION_CURRENT,
                (fixture_id, model_version, profile, data_cutoff),
            )
            current = cur.fetchone()
            outcome = PredictionOutcome.CREATED
            if current is not None:
                if tuple(current[1:]) == self._comparable(artifact):
                    return PredictionOutcome.UNCHANGED
                cur.execute(
                    _PREDICTION_SUPERSEDE, (artifact.known_at, current[0])
                )
                outcome = PredictionOutcome.REVISED

            cur.execute(
                _PREDICTION_INSERT,
                (
                    fixture_id,
                    artifact.model_family,
                    model_version,
                    profile,
                    data_cutoff,
                    artifact.known_at,
                    artifact.lambda_home,
                    artifact.lambda_away,
                    artifact.max_goals,
                    list(artifact.scoreline),
                    artifact.truncated_mass,
                    artifact.p_home,
                    artifact.p_draw,
                    artifact.p_away,
                    *(artifact.p_over[line] for line in PERSISTED_LINES),
                    artifact.p_btts_yes,
                    artifact.is_cold_start,
                    list(artifact.cold_started_teams),
                    artifact.training_matches,
                    Jsonb(artifact.fit_metadata),
                    self._job_run_id,
                ),
            )
        return outcome
