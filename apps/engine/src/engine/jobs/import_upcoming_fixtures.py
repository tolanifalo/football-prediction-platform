"""Ingest upcoming fixtures from football-data.org (UPCOMING-FIXTURES.md).

    FOOTBALL_DATA_ORG_TOKEN=... uv run python -m engine.jobs.import_upcoming_fixtures

Transaction shape, deliberately, and the same as the historical importer:

  1. the job_run row, committed
  2. raw evidence, committed BEFORE a single row is parsed downstream
  3. reference data - country, competition, season - committed
  4. ONE TRANSACTION PER FIXTURE - a bad row costs one match, not the file
  5. the job_run closed with its counters

DECLARED VERSUS RESOLVED, the split P0-12 established and this reuses:

  COMPETITION and SEASON are DECLARED. The catalogue states that `PL` means
  the English Premier League, and the provider supplies the season's dates;
  the canonical rows are created if absent, because a season is a registry
  entity we derive, not a claim to adjudicate.

  TEAMS are RESOLVED, through the exact-alias resolver and nothing else. A
  club we do not recognise is NEVER created: the fixture is refused and the
  name goes to `entity_review_queue`.

NO FUZZY MATCHING ANYWHERE, and no future result is consulted: a fixture is
ingested because it is scheduled, not because anyone knows how it ended.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from engine.db import database_url
from engine.ingestion.contracts import Domain, FetchRequest
from engine.ingestion.dto import CanonicalFixture, ProviderRef
from engine.ingestion.errors import ProblemKind
from engine.ingestion.identity import Ambiguous, EntityKind, Resolved, Unknown
from engine.ingestion.postgres import (
    ExternalIdStore,
    PostgresCanonicalWriter,
    PostgresIdentityResolver,
    PostgresJobRunStore,
    PostgresRawArchive,
    ReferenceSeeder,
)
from engine.ingestion.runs import RunIdentity, RunStatus, classify
from engine.ingestion.stats import IngestionStats
from engine.ingestion.transport import HttpxTransport, TransportError
from engine.providers.football_data_org.adapter import (
    ADAPTER_VERSION,
    PROVIDER_SLUG,
    FootballDataOrgAdapter,
)
from engine.providers.football_data_org.catalog import (
    API_KEY_ENV,
    API_KEY_HEADER,
    BASE_URL,
    COMPETITIONS,
)
from engine.providers.football_data_org.seed import PL_TEAM_ALIASES

JOB_NAME = "import_upcoming_fixtures"


@dataclass
class UpcomingReport:
    job_run_id: int
    status: RunStatus
    parsed: int = 0
    fixtures: int = 0
    schedules_written: int = 0
    unchanged: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    review_items: int = 0
    team_mappings: int = 0
    aliases_seeded: int = 0
    problems: list[str] = field(default_factory=list)


def run_import(
    conn: psycopg.Connection[Any],
    *,
    code: str,
    transport: HttpxTransport,
    scope: dict[str, str] | None = None,
) -> UpcomingReport:
    meta = COMPETITIONS[code]

    seeder = ReferenceSeeder(conn)
    source_id = seeder.data_source(
        PROVIDER_SLUG, "football-data.org", ["fixtures"], BASE_URL
    )
    runs = PostgresJobRunStore(conn)
    scope_key = f"{code}:upcoming"
    now = datetime.now(UTC)
    identity = RunIdentity(
        job_name=JOB_NAME,
        scope_key=scope_key,
        run_date=now.date(),
        attempt=runs.next_attempt(RunIdentity(JOB_NAME, scope_key, now.date())),
    )
    job_run_id = runs.open(
        identity, source_id=source_id, adapter_version=ADAPTER_VERSION,
        params={"code": code, **(scope or {})},
    )
    conn.commit()

    stats = IngestionStats()
    report = UpcomingReport(job_run_id, RunStatus.RUNNING)

    archive = PostgresRawArchive(conn, source_id=source_id)
    adapter = FootballDataOrgAdapter(
        transport=transport, archive=archive, job_run_id=job_run_id
    )
    result = adapter.fetch(
        FetchRequest(domain=Domain.FIXTURES, scope={"code": code, **(scope or {})})
    )
    conn.commit()  # evidence is durable before anything downstream

    if adapter.last_response is not None:
        stats.record_response(
            status=adapter.last_response.status,
            elapsed_ms=adapter.last_response.elapsed_ms,
            size=len(adapter.last_response.content),
            attempts=adapter.last_response.attempts,
        )
    elif any(p.kind is ProblemKind.TIMEOUT for p in result.problems):
        stats.record_timeout()

    if not result.complete or adapter.last_parsed is None:
        report.status = RunStatus.FAILED
        report.problems = [p.message for p in result.problems] or ["fetch failed"]
        runs.close(
            job_run_id, report.status, stats,
            error="; ".join(report.problems[:5]),
        )
        conn.commit()
        return report

    parsed = adapter.last_parsed
    stats.pages = 1
    stats.rows_parsed = parsed.rows_seen
    stats.rows_rejected = len(parsed.problems)
    report.parsed = parsed.rows_seen
    report.problems = [p.message for p in parsed.problems]

    # -- reference data: DECLARED, committed once --------------------------
    country_id = seeder.country(meta.country_slug, "England")
    competition_id = seeder.competition(
        meta.competition_slug, "Premier League", country_id,
        comp_type="league", tier=1, gender="men",
    )
    season_ids: dict[str, UUID] = {}
    for fixture in parsed.fixtures:
        label = fixture.ref.season.label
        if label in season_ids:
            continue
        kickoffs = [
            f.kickoff_utc for f in parsed.fixtures if f.ref.season.label == label
        ]
        season_ids[label] = seeder.season(
            f"{meta.competition_slug}-{label.replace('/', '-')}",
            competition_id, label, int(label.split("/")[0]),
            min(kickoffs).date(), max(kickoffs).date(),
        )

    # Provider spellings, attached to clubs that ALREADY exist. Nothing is
    # created: an unrecognised club must reach the review queue, not the
    # registry.
    for team_slug, aliases in PL_TEAM_ALIASES:
        for alias in aliases:
            if seeder.alias_for_existing_team(team_slug, alias, source_id):
                report.aliases_seeded += 1
    conn.commit()

    # `as_of` is TRANSACTION time - what we believe right now - and it must be
    # taken AFTER the aliases are written, not at job start. Taken earlier it
    # sits before their `created_at`, `fn_visible_at` hides the rows the job
    # has just inserted, and every club resolves as unknown. That is not a
    # hypothetical: it is what the first run of this job did.
    resolved_at = datetime.now(UTC)
    resolver = PostgresIdentityResolver(
        conn, source_id=source_id, as_of=resolved_at
    )
    store = ExternalIdStore(conn, source_id=source_id)
    payload = result.provenance[0]
    body_id = archive.body_id_for(payload.body_hash)
    writer = PostgresCanonicalWriter(conn, source_id=source_id, body_id=body_id)

    for fixture in parsed.fixtures:
        try:
            teams = _resolve_sides(fixture, resolver, store, report)
            if teams is None:
                conn.commit()  # keep any review item that was filed
                continue
            home_id, away_id = teams
            fixture_id = writer.upsert_fixture(
                season_ids[fixture.ref.season.label], home_id, away_id, fixture
            )
            report.fixtures += 1
            if writer.upsert_schedule(fixture_id, fixture):
                report.schedules_written += 1
            else:
                report.unchanged += 1
            conn.commit()
            stats.rows_accepted += 1
            stats.identity_resolved += 2
        except Exception as exc:  # noqa: BLE001 - one fixture, never the file
            conn.rollback()
            stats.rows_rejected += 1
            report.problems.append(
                f"{fixture.ref.home_team.name} v {fixture.ref.away_team.name}: {exc}"
            )

    report.status = classify(stats, all_domains_complete=True)
    if report.unresolved or report.ambiguous:
        report.status = RunStatus.PARTIAL
    runs.close(
        job_run_id, report.status, stats,
        error="; ".join(report.problems[:10]) or None,
    )
    conn.commit()
    return report


def _resolve_sides(
    fixture: CanonicalFixture,
    resolver: PostgresIdentityResolver,
    store: ExternalIdStore,
    report: UpcomingReport,
) -> tuple[UUID, UUID] | None:
    """Both clubs, or None and a review item. Never a guess."""
    resolved: list[UUID] = []
    for side in (fixture.ref.home_team, fixture.ref.away_team):
        outcome = resolver.resolve(EntityKind.TEAM, ProviderRef(name=side.name))
        match outcome:
            case Resolved(internal_id=team_id):
                resolved.append(team_id)
                # The provider's numeric team id IS a provider primary key,
                # which is what external_ids is for (§11.4). Recorded only
                # once the name has resolved deterministically.
                if side.provider_key and side.provider_key != "None":
                    store.ensure(EntityKind.TEAM, side.provider_key, team_id)
                    report.team_mappings += 1
            case Ambiguous(reason=reason):
                report.ambiguous += 1
                report.problems.append(f"team {side.name!r} ambiguous: {reason}")
                if store.file_for_review(
                    EntityKind.TEAM, side.name, f"ambiguous: {reason}"
                ):
                    report.review_items += 1
                return None
            case Unknown(reason=reason):
                report.unresolved += 1
                report.problems.append(f"team {side.name!r} unknown: {reason}")
                if store.file_for_review(
                    EntityKind.TEAM, side.name, f"unknown: {reason}"
                ):
                    report.review_items += 1
                return None
    return (resolved[0], resolved[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import upcoming fixtures from football-data.org."
    )
    parser.add_argument("--code", default="PL", choices=sorted(COMPETITIONS))
    parser.add_argument(
        "--status",
        default="SCHEDULED",
        help="Provider status filter, e.g. SCHEDULED or TIMED. "
             "Passed through to the API and recorded in request_params.",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    try:
        transport = HttpxTransport.from_env(
            BASE_URL, api_key_env=API_KEY_ENV, api_key_header=API_KEY_HEADER
        )
    except TransportError as exc:
        print(f"{exc.problem.message}", file=sys.stderr)
        print(
            f"Set {API_KEY_ENV} to a football-data.org token. The adapter and "
            "its whole test path run without one; only a live fetch needs it.",
            file=sys.stderr,
        )
        return 1

    with psycopg.connect(args.database_url or database_url()) as conn:
        report = run_import(
            conn, code=args.code, transport=transport,
            scope={"status": args.status},
        )

    print(f"job_run {report.job_run_id}: {report.status}")
    print(f"  parsed             {report.parsed}")
    print(f"  fixtures           {report.fixtures}")
    print(f"  schedules written  {report.schedules_written}")
    print(f"  unchanged          {report.unchanged}")
    print(f"  unresolved teams   {report.unresolved}")
    print(f"  ambiguous teams    {report.ambiguous}")
    print(f"  review items       {report.review_items}")
    print(f"  team id mappings   {report.team_mappings}")
    for problem in report.problems[:10]:
        print(f"  ! {problem}")
    return 0 if report.status is RunStatus.OK else 1


if __name__ == "__main__":
    sys.exit(main())
