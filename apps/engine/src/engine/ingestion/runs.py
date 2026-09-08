"""Job-run semantics and the canonical write ports (PHASE-0-SPEC.md §15.9).

ONE `job_runs` ROW = ONE INVOCATION OF ONE INGEST JOB AGAINST ONE SOURCE
(§9.2). Not one HTTP request, and not one fixture. Multiple requests and pages
within a run share its `job_run_id`; each is a separate `raw_payloads` row.

P0-10 adds NO table and NO column. `job_runs` already carries `params`,
`stats`, `error`, `adapter_version`, `attempt`, a `(job_name, scope_key,
run_date, attempt)` unique key, and a `status` domain that already includes
`partial`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol

from engine.ingestion.dto import (
    CanonicalCompetition,
    CanonicalFixture,
    CanonicalOdds,
    CanonicalResult,
    CanonicalSeason,
    CanonicalTeam,
)
from engine.ingestion.stats import IngestionStats


class RunStatus(StrEnum):
    """Exactly the four values `job_runs_status_check` permits."""

    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class RunIdentity:
    """The natural key `job_runs` already enforces as UNIQUE.

    A second concurrent run with the same identity is rejected by the database
    with 23505 (probed), which is why no advisory lock is needed.
    """

    job_name: str
    scope_key: str
    run_date: date
    attempt: int = 1


def classify(stats: IngestionStats, *, all_domains_complete: bool) -> RunStatus:
    """Decide a run's terminal status.

    A run is `ok` only when every domain reported completion AND nothing was
    rejected or failed. A failed page can never produce `ok` - that is the
    whole reason `FetchResult.complete` is separate from `next_cursor`.
    """
    if not all_domains_complete:
        return RunStatus.PARTIAL if stats.rows_accepted else RunStatus.FAILED
    if stats.pages_failed or stats.rows_rejected:
        return RunStatus.PARTIAL
    return RunStatus.OK


class JobRunStore(Protocol):
    """Opens, updates and closes a `job_runs` row. Implemented in P0-11."""

    def open(
        self, identity: RunIdentity, *, source_slug: str, adapter_version: str
    ) -> int: ...

    def close(self, job_run_id: int, status: RunStatus, stats: IngestionStats,
              error: str | None = None) -> None: ...


# ---------------------------------------------------------------------------
# Canonical write ports.
#
# ADAPTERS DO NOT WRITE. They return DTOs; these ports persist them. That
# separation is what makes providers replaceable, adapters testable with no
# database, and provider parsing free of psycopg and Drizzle.
#
# P0-10 defines the interfaces only. P0-11 implements them as explicit psycopg
# SQL - no ORM, per the standing rule that Drizzle owns the schema and Python
# never migrates.
# ---------------------------------------------------------------------------
class CanonicalWriter(Protocol):
    """Persists canonical DTOs whose identities have already been resolved.

    Every method is idempotent by the business key of the table it writes, so
    re-running an import leaves canonical row counts unchanged while
    `raw_payloads` grows (§E, P0-11).
    """

    def upsert_competitions(self, records: Sequence[CanonicalCompetition]) -> int: ...

    def upsert_seasons(self, records: Sequence[CanonicalSeason]) -> int: ...

    def upsert_teams(self, records: Sequence[CanonicalTeam]) -> int: ...

    def upsert_fixtures(self, records: Sequence[CanonicalFixture]) -> int: ...

    def append_results(self, records: Sequence[CanonicalResult]) -> int: ...

    def append_odds(self, records: Sequence[CanonicalOdds]) -> int: ...
