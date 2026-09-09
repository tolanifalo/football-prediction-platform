"""The P0-12 job: record which provider key means which canonical entity.

    uv run python -m engine.jobs.resolve_identities --division E0 --season 2324

READS THE DATABASE, DOWNLOADS NOTHING. Every provider string it needs is
already there: P0-11 wrote the provider's team spellings into `team_aliases`,
and the division/season codes come from the project catalogue. Re-fetching the
CSV would prove nothing this cannot.

Two kinds of identity, and they are established differently:

  * COMPETITION and SEASON are DECLARED. The catalogue states that `E0` means
    the English Premier League - the CSV says only `Div=E0`. The job looks the
    canonical row up by the slug the catalogue declares and maps the provider's
    code to it. If the canonical row is absent it files a review item; it
    never creates one.

  * TEAMS are RESOLVED, through `PostgresIdentityResolver`, by exact alias
    match and nothing else. Ambiguous or unknown goes to the review queue and
    no mapping is written.

Neither path can remap. `ExternalIdStore.ensure` writes a mapping only where
none exists; a provider key that already points somewhere else is a conflict
for a human to settle (§6 rule 9).
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
from engine.ingestion.dto import ProviderRef
from engine.ingestion.identity import (
    Ambiguous,
    EntityKind,
    Resolved,
    Unknown,
)
from engine.ingestion.postgres import (
    ExternalIdStore,
    MappingOutcome,
    PostgresIdentityResolver,
    PostgresJobRunStore,
)
from engine.ingestion.runs import RunIdentity, RunStatus
from engine.ingestion.stats import IngestionStats
from engine.providers.football_data_couk.adapter import ADAPTER_VERSION, PROVIDER_SLUG
from engine.providers.football_data_couk.catalog import DIVISIONS, season_label

JOB_NAME = "resolve_identities_football_data_couk"


def season_external_id(division: str, season: str) -> str:
    """The provider's own key for one season OF ONE DIVISION.

    `2324` alone would be a lie: the provider uses that folder for every
    division's file that season, so `2324` names twenty leagues, not one. The
    provider addresses this resource at `/mmz4281/2324/E0.csv`, and the pair
    from that path is the smallest thing that actually identifies it.
    """
    return f"{season}/{division}"


@dataclass
class ResolutionReport:
    job_run_id: int
    status: RunStatus
    created: int = 0
    unchanged: int = 0
    conflicts: int = 0
    ambiguous: int = 0
    unknown: int = 0
    review_items: int = 0
    resolved_teams: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def mapped(self) -> int:
        return self.created + self.unchanged


def _scalar(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def run_resolution(
    conn: psycopg.Connection[Any],
    *,
    division: str,
    season: str,
    as_of: datetime | None = None,
) -> ResolutionReport:
    meta = DIVISIONS[division]
    label, _start_year = season_label(season)
    moment = as_of or datetime.now(UTC)
    scope_key = f"{division}:{season}"

    source_id = _scalar(
        conn, "SELECT id FROM data_sources WHERE slug = %s", (PROVIDER_SLUG,)
    )
    if source_id is None:
        raise SystemExit(
            f"data source {PROVIDER_SLUG!r} is not registered; run the import first"
        )
    source_id = UUID(str(source_id))

    runs = PostgresJobRunStore(conn)
    identity = RunIdentity(
        job_name=JOB_NAME,
        scope_key=scope_key,
        run_date=moment.date(),
        attempt=runs.next_attempt(RunIdentity(JOB_NAME, scope_key, moment.date())),
    )
    job_run_id = runs.open(
        identity, source_id=source_id, adapter_version=ADAPTER_VERSION,
        params={"division": division, "season": season},
    )
    conn.commit()

    stats = IngestionStats()
    report = ResolutionReport(job_run_id, RunStatus.RUNNING)
    resolver = PostgresIdentityResolver(conn, source_id=source_id, as_of=moment)
    store = ExternalIdStore(conn, source_id=source_id)

    def record(outcome: MappingOutcome, kind: EntityKind, key: str) -> None:
        if outcome is MappingOutcome.CREATED:
            report.created += 1
        elif outcome is MappingOutcome.UNCHANGED:
            report.unchanged += 1
        else:
            report.conflicts += 1
            report.problems.append(f"{kind}:{key} already maps elsewhere")
            if store.file_for_review(
                kind, key, "conflicting: provider key already maps elsewhere"
            ):
                report.review_items += 1

    # -- declared identities: competition, then the season inside it --------
    competition_id = _scalar(
        conn, "SELECT id FROM competitions WHERE slug = %s", (meta.competition_slug,)
    )
    if competition_id is None:
        report.unknown += 1
        report.problems.append(f"competition {meta.competition_slug} not in database")
        if store.file_for_review(
            EntityKind.COMPETITION, division, "unknown: no canonical competition"
        ):
            report.review_items += 1
    else:
        competition_id = UUID(str(competition_id))
        record(
            store.ensure(
                EntityKind.COMPETITION, division, competition_id, known_at=moment
            ),
            EntityKind.COMPETITION,
            division,
        )
        season_key = season_external_id(division, season)
        season_id = _scalar(
            conn,
            "SELECT id FROM seasons WHERE competition_id = %s AND label = %s",
            (competition_id, label),
        )
        if season_id is None:
            report.unknown += 1
            report.problems.append(f"season {label} not in database")
            if store.file_for_review(
                EntityKind.SEASON, season_key, "unknown: no canonical season"
            ):
                report.review_items += 1
        else:
            record(
                store.ensure(
                    EntityKind.SEASON, season_key, UUID(str(season_id)),
                    known_at=moment,
                ),
                EntityKind.SEASON,
                season_key,
            )

    # -- resolved identities: the provider's team strings -------------------
    # The strings come from `team_aliases`, which is where P0-11 recorded what
    # this provider actually calls each club. No file is read.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT alias FROM team_aliases WHERE source_id = %s ORDER BY alias",
            (source_id,),
        )
        provider_teams = [str(r[0]) for r in cur.fetchall()]

    for name in provider_teams:
        outcome = resolver.resolve(EntityKind.TEAM, ProviderRef(name=name))
        match outcome:
            case Resolved(internal_id=team_id):
                report.resolved_teams += 1
                stats.identity_resolved += 1
                record(
                    store.ensure(EntityKind.TEAM, name, team_id, known_at=moment),
                    EntityKind.TEAM,
                    name,
                )
            case Ambiguous(candidates=candidates, reason=reason):
                report.ambiguous += 1
                stats.identity_ambiguous += 1
                report.problems.append(f"team:{name} ambiguous ({len(candidates)})")
                # No candidate is named: choosing one to record would be the
                # guess this whole phase exists to prevent.
                if store.file_for_review(
                    EntityKind.TEAM, name, f"ambiguous: {reason}"
                ):
                    report.review_items += 1
            case Unknown(reason=reason):
                report.unknown += 1
                stats.identity_unknown += 1
                report.problems.append(f"team:{name} unknown")
                if store.file_for_review(EntityKind.TEAM, name, f"unknown: {reason}"):
                    report.review_items += 1

    report.status = (
        RunStatus.OK
        if not (report.conflicts or report.ambiguous or report.unknown)
        else RunStatus.PARTIAL
    )
    runs.close(
        job_run_id,
        report.status,
        stats,
        error="; ".join(report.problems[:10]) or None,
    )
    conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Map football-data.co.uk provider keys to canonical entities."
    )
    parser.add_argument("--division", default="E0")
    parser.add_argument("--season", default="2324")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    with psycopg.connect(args.database_url or database_url()) as conn:
        report = run_resolution(
            conn, division=args.division, season=args.season
        )
    print(f"job_run {report.job_run_id}: {report.status}")
    print(f"  mappings created    {report.created}")
    print(f"  mappings unchanged  {report.unchanged}")
    print(f"  teams resolved      {report.resolved_teams}")
    print(f"  conflicts           {report.conflicts}")
    print(f"  ambiguous           {report.ambiguous}")
    print(f"  unknown             {report.unknown}")
    print(f"  review items filed  {report.review_items}")
    for problem in report.problems[:10]:
        print(f"  ! {problem}")
    return 0 if report.status is RunStatus.OK else 1


if __name__ == "__main__":
    sys.exit(main())
