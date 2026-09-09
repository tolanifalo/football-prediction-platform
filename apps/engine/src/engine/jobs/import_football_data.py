"""The ingest job: football-data.co.uk -> canonical Postgres (§16.6).

Invoked by hand. Phase 0 has no scheduler (§G).

    uv run python -m engine.jobs.import_football_data --division E0 --season 2324

Transaction shape, deliberately:

  1. the job_run row, committed
  2. raw evidence, committed BEFORE a single row is parsed
  3. reference data, committed
  4. ONE TRANSACTION PER FIXTURE - a bad row costs one match, not 380
  5. the job_run closed with its counters

Raw evidence never rolls back because a downstream parse failed.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any

import psycopg

from engine.db import database_url
from engine.ingestion.contracts import Domain, FetchRequest
from engine.ingestion.dto import (
    CanonicalFixture,
    CanonicalOdds,
    CanonicalResult,
    CanonicalStats,
    FixtureRef,
)
from engine.ingestion.errors import ProblemKind
from engine.ingestion.postgres import (
    PostgresCanonicalWriter,
    PostgresJobRunStore,
    PostgresRawArchive,
    ReferenceSeeder,
)
from engine.ingestion.runs import RunIdentity, RunStatus, classify
from engine.ingestion.stats import IngestionStats
from engine.ingestion.transport import HttpxTransport
from engine.providers.football_data_couk.adapter import (
    ADAPTER_VERSION,
    BASE_URL,
    PROVIDER_SLUG,
    FootballDataCoUkAdapter,
)
from engine.providers.football_data_couk.catalog import (
    BOOKMAKERS,
    DIVISIONS,
    season_label,
)
from engine.providers.football_data_couk.seed import E0_2324_TEAMS

JOB_NAME = "ingest_football_data_couk"


@dataclass
class FixtureBundle:
    """Everything one provider row produced, written in one transaction."""

    fixture: CanonicalFixture
    result: CanonicalResult | None = None
    stats: CanonicalStats | None = None
    odds: list[CanonicalOdds] = dc_field(default_factory=list)


@dataclass
class ImportReport:
    job_run_id: int
    status: RunStatus
    stats: IngestionStats
    fixtures: int = 0
    schedules_written: int = 0
    results_written: int = 0
    stats_written: int = 0
    series: int = 0
    ticks_written: int = 0
    unresolved_teams: list[str] | None = None


def _verify_unique_pairings(fixtures: list[CanonicalFixture]) -> list[str]:
    """Every ordered pairing must occur exactly once (§16.5).

    If it does not, STOP and report the offenders rather than inventing meeting
    ordinals - the generalised regular_m1/m2 rule is a broader identity problem
    and belongs with P0-12.
    """
    seen: dict[tuple[str, str], int] = {}
    for f in fixtures:
        key = (f.ref.home_team.name, f.ref.away_team.name)
        seen[key] = seen.get(key, 0) + 1
    return [f"{h} v {a} x{n}" for (h, a), n in sorted(seen.items()) if n > 1]


def run_import(
    conn: psycopg.Connection[Any],
    *,
    division: str,
    season: str,
    transport: HttpxTransport,
) -> ImportReport:
    meta = DIVISIONS[division]
    label, start_year = season_label(season)
    scope_key = f"{division}:{season}"
    stats = IngestionStats()

    seeder = ReferenceSeeder(conn)
    source_id = seeder.data_source(
        PROVIDER_SLUG,
        "football-data.co.uk",
        ["fixtures", "odds"],
    )
    runs = PostgresJobRunStore(conn)
    identity = RunIdentity(
        job_name=JOB_NAME,
        scope_key=scope_key,
        run_date=datetime.now(UTC).date(),
        attempt=runs.next_attempt(
            RunIdentity(JOB_NAME, scope_key, datetime.now(UTC).date())
        ),
    )
    job_run_id = runs.open(
        identity, source_id=source_id, adapter_version=ADAPTER_VERSION,
        params={"division": division, "season": season},
    )
    conn.commit()

    archive = PostgresRawArchive(conn, source_id=source_id)
    adapter = FootballDataCoUkAdapter(
        transport=transport, archive=archive, job_run_id=job_run_id
    )
    result = adapter.fetch(
        FetchRequest(
            domain=Domain.FIXTURES,
            scope={"division": division, "season": season},
        )
    )
    conn.commit()  # evidence is durable before anything is parsed downstream

    # The HTTP counters, recorded on every path. A timeout produces no
    # response and therefore no `raw_payloads` row, so if it is not counted
    # here it is invisible afterwards.
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
        runs.close(job_run_id, RunStatus.FAILED, stats,
                   error="; ".join(
                       p.message for p in result.problems) or "download failed",
                   )
        conn.commit()
        return ImportReport(job_run_id, RunStatus.FAILED, stats)

    parsed = adapter.last_parsed
    stats.pages = 1
    stats.rows_parsed = parsed.rows_seen
    stats.rows_rejected = len(parsed.problems)

    duplicates = _verify_unique_pairings(parsed.fixtures)
    if duplicates:
        runs.close(job_run_id, RunStatus.FAILED, stats,
                   error=f"duplicate ordered pairings: {', '.join(duplicates[:10])}")
        conn.commit()
        raise SystemExit(
            f"duplicate ordered pairings, refusing to import: {duplicates[:10]}",
        )

    # -- reference data, committed once -----------------------------------
    country_id = seeder.country(meta.country_slug, meta.country_name)
    competition_id = seeder.competition(
        meta.competition_slug, meta.competition_name, country_id,
        comp_type=meta.competition_type, tier=meta.tier, gender=meta.gender,
    )
    kickoffs = sorted(f.kickoff_utc for f in parsed.fixtures)
    season_id = seeder.season(
        f"{meta.competition_slug}-{label.replace('/', '-')}", competition_id, label,
        start_year, kickoffs[0].date() if kickoffs else None,
        kickoffs[-1].date() if kickoffs else None,
    )
    for team_slug, team_name, team_aliases in E0_2324_TEAMS:
        seeder.team(team_slug, team_name, country_id, team_aliases, source_id)
    bookmaker_ids = {b.slug: seeder.bookmaker(b.slug, b.name) for b in BOOKMAKERS}
    alias_to_team = seeder.alias_index(source_id)
    conn.commit()

    # An unmapped provider string is NEVER guessed and NEVER auto-created.
    unresolved = sorted(n for n in parsed.team_names if n not in alias_to_team)
    if unresolved:
        stats.identity_unknown = len(unresolved)
        runs.close(job_run_id, RunStatus.PARTIAL, stats,
                   error=f"unresolved provider teams: {', '.join(unresolved)}")
        conn.commit()
        return ImportReport(
            job_run_id,
            RunStatus.PARTIAL,
            stats,
            unresolved_teams=unresolved,
        )

    # -- group the parsed records by fixture -------------------------------
    payload = result.provenance[0]
    body_id = archive.body_id_for(payload.body_hash)
    report = ImportReport(job_run_id, RunStatus.RUNNING, stats)

    def key_of(ref: FixtureRef) -> tuple[str, str]:
        return (ref.home_team.name, ref.away_team.name)

    grouped: dict[tuple[str, str], FixtureBundle] = {
        key_of(f.ref): FixtureBundle(fixture=f) for f in parsed.fixtures
    }
    for r in parsed.results:
        grouped[key_of(r.fixture)].result = r
    for st in parsed.stats:
        grouped[key_of(st.fixture)].stats = st
    for od in parsed.odds:
        grouped[key_of(od.fixture)].odds.append(od)

    # -- one transaction per fixture ---------------------------------------
    writer = PostgresCanonicalWriter(conn, source_id=source_id, body_id=body_id)
    for (home_name, away_name), bundle in grouped.items():
        try:
            fixture_id = writer.upsert_fixture(
                season_id,
                alias_to_team[home_name],
                alias_to_team[away_name],
                bundle.fixture,
            )
            report.fixtures += 1
            if writer.upsert_schedule(fixture_id, bundle.fixture):
                report.schedules_written += 1
            if bundle.result and writer.upsert_result(fixture_id, bundle.result):
                report.results_written += 1
            if bundle.stats and writer.upsert_stats(fixture_id, bundle.stats):
                report.stats_written += 1
            for od in bundle.odds:
                series_id = writer.upsert_series(
                    fixture_id, bookmaker_ids[od.bookmaker.provider_key or ""], od
                )
                report.series += 1
                if writer.append_tick(series_id, od):
                    report.ticks_written += 1
            conn.commit()
            stats.rows_accepted += 1
            stats.identity_resolved += 2
        except Exception as exc:  # noqa: BLE001 - one fixture, never the file
            conn.rollback()
            stats.rows_rejected += 1
            print(
                f"  fixture failed: {home_name} v {away_name}: {exc}",
                file=sys.stderr,
            )

    status = classify(stats, all_domains_complete=True)
    runs.close(job_run_id, status, stats)
    conn.commit()
    report.status = status
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import one football-data.co.uk file.")
    parser.add_argument("--division", default="E0")
    parser.add_argument("--season", default="2324")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    transport = HttpxTransport(BASE_URL)
    with psycopg.connect(args.database_url or database_url()) as conn:
        report = run_import(
            conn, division=args.division, season=args.season, transport=transport
        )

    print(f"job_run {report.job_run_id}: {report.status}")
    print(f"  fixtures        {report.fixtures}")
    print(f"  schedules       {report.schedules_written}")
    print(f"  results         {report.results_written}")
    print(f"  match_stats     {report.stats_written}")
    print(f"  odds_series     {report.series}")
    print(f"  odds_ticks      {report.ticks_written}")
    if report.unresolved_teams:
        print(f"  UNRESOLVED      {report.unresolved_teams}")
    return 0 if report.status is RunStatus.OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
