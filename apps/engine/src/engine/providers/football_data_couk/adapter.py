"""The football-data.co.uk adapter (PHASE-0-SPEC.md §16.2).

FILE-BASED, MODELLED HONESTLY. This provider publishes one CSV per season and
division, not a paginated API, so one `FetchRequest` maps to one file and one
`FetchResult` with `next_cursor=None, complete=True`. `supports_pagination` is
False rather than a fabricated single-page cursor - the P0-10 envelope already
expresses "one page, done" without distortion.

Imports no psycopg, no schema module and no repository: it is handed an
`HttpTransport` and a `RawArchive` and depends on nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from engine.ingestion.archive import ArchiveRecord, RawArchive
from engine.ingestion.contracts import Capabilities, Domain, FetchRequest, FetchResult
from engine.ingestion.dto import CanonicalRecord
from engine.ingestion.errors import Problem, ProblemKind
from engine.ingestion.transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
)
from engine.providers.football_data_couk.catalog import DIVISIONS
from engine.providers.football_data_couk.parser import ParsedFile, parse_csv

PROVIDER_SLUG = "football-data-couk"
ADAPTER_VERSION = "football-data-couk@1.0.0"
BASE_URL = "https://football-data.co.uk"
#: The apex host. `www.football-data.co.uk` returns HTTP 503 (verified
#: 2026-09-08, and recorded in DECISIONS-01 during the original research).
ENDPOINT_TEMPLATE = "/mmz4281/{season}/{division}.csv"


@dataclass
class FootballDataCoUkAdapter:
    """One provider, one file per (season, division)."""

    transport: HttpTransport
    archive: RawArchive
    job_run_id: int
    provider_slug: str = PROVIDER_SLUG
    adapter_version: str = ADAPTER_VERSION
    #: Populated by `fetch` so the caller can persist without re-parsing.
    last_parsed: ParsedFile | None = field(default=None, repr=False)
    #: The transport's answer, kept so the caller can record the HTTP counters
    #: on the job run. The adapter deliberately owns no `IngestionStats`: the
    #: run's counters belong to the run, not to one adapter (P0-10 §15).
    last_response: HttpResponse | None = field(default=None, repr=False)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            domains=frozenset(
                {
                    Domain.COMPETITIONS,
                    Domain.SEASONS,
                    Domain.TEAMS,
                    Domain.FIXTURES,
                    Domain.RESULTS,
                    Domain.ODDS,
                }
            ),
            markets=frozenset({"1x2", "over_under", "asian_handicap"}),
            # One file is the whole scope. There is nothing to page through.
            supports_pagination=False,
            rate_limit_per_min=None,
            historical_from=date(1993, 8, 1),
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        division = request.scope.get("division", "")
        season = request.scope.get("season", "")
        if division not in DIVISIONS:
            return FetchResult(
                complete=False,
                problems=[
                    Problem(
                        ProblemKind.UNSUPPORTED_FEATURE,
                        f"division {division!r} is not in the project catalogue",
                    )
                ],
            )

        endpoint = ENDPOINT_TEMPLATE.format(season=season, division=division)
        params = {"division": division, "season": season}
        self.last_response = None
        try:
            response = self.transport.send(HttpRequest(endpoint=endpoint))
        except TransportError as exc:
            # No response content means no evidence to archive, and a failed
            # download must never read as a completed sync.
            return FetchResult(complete=False, problems=[exc.problem])

        self.last_response = response
        if not response.ok:
            # A permanent failure still produced bytes, and bytes are evidence.
            self.archive.store(
                ArchiveRecord(
                    source_slug=self.provider_slug,
                    job_run_id=self.job_run_id,
                    endpoint=endpoint,
                    content=response.content,
                    params=params,
                    response_headers=response.headers,
                    http_status=response.status,
                    fetched_at=datetime.now(UTC),
                )
            )
            kind = (
                ProblemKind.NOT_FOUND
                if response.status == 404
                else ProblemKind.PROVIDER_SERVER
            )
            return FetchResult(
                complete=False,
                problems=[Problem(kind, f"{endpoint} -> HTTP {response.status}")],
            )

        # ARCHIVE BEFORE PARSING. The exact response bytes, BOM and CRLF
        # intact - they are what the provider actually sent (§9.4).
        known_at = datetime.now(UTC)
        payload = self.archive.store(
            ArchiveRecord(
                source_slug=self.provider_slug,
                job_run_id=self.job_run_id,
                endpoint=endpoint,
                content=response.content,
                params=params,
                response_headers=response.headers,
                http_status=response.status,
                fetched_at=known_at,
            )
        )

        try:
            parsed = parse_csv(
                response.content,
                division=division,
                season_folder=season,
                payload=payload,
                known_at=known_at,
            )
        except Exception as exc:  # noqa: BLE001 - a whole-file failure, evidence already safe
            return FetchResult(
                provenance=[payload],
                complete=False,
                problems=[Problem(ProblemKind.MALFORMED_PAYLOAD, f"{endpoint}: {exc}")],
            )

        self.last_parsed = parsed
        records: list[CanonicalRecord] = [
            *parsed.fixtures, *parsed.results, *parsed.stats, *parsed.odds
        ]
        return FetchResult(
            records=records,
            provenance=[payload],
            next_cursor=None,
            complete=True,
            problems=parsed.problems,
        )
