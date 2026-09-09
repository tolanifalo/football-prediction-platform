"""The football-data.org adapter (UPCOMING-FIXTURES.md §3).

Imports no psycopg, no schema module and no repository: it is handed an
`HttpTransport` and a `RawArchive` and depends on nothing else. Same P0-10
contract the historical adapter implements, same envelope, same error
taxonomy, same archive - a second provider was supposed to need no new
machinery, and it did not.

CREDENTIALS NEVER REACH THIS FILE. The token is injected by the transport from
the environment (`HttpxTransport.from_env`), so it cannot appear in a request
signature, in `raw_payloads.request_params`, or in this module's source.
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
from engine.providers.football_data_org.catalog import (
    COMPETITIONS,
    MATCHES_ENDPOINT,
)
from engine.providers.football_data_org.parser import ParsedFixtures, parse_matches

PROVIDER_SLUG = "football-data-org"
ADAPTER_VERSION = "football-data-org@1.0.0"


@dataclass
class FootballDataOrgAdapter:
    """Upcoming fixtures for one competition."""

    transport: HttpTransport
    archive: RawArchive
    job_run_id: int
    provider_slug: str = PROVIDER_SLUG
    adapter_version: str = ADAPTER_VERSION
    #: Populated by `fetch` so the caller can persist without re-parsing.
    last_parsed: ParsedFixtures | None = field(default=None, repr=False)
    #: The transport's answer, so the caller can record the HTTP counters.
    last_response: HttpResponse | None = field(default=None, repr=False)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            domains=frozenset({Domain.FIXTURES}),
            markets=frozenset(),
            # One competition is one response. The API does paginate large
            # ranges by date filter, but a season of one league is not large
            # and a fabricated cursor would be a lie about the envelope.
            supports_pagination=False,
            rate_limit_per_min=10,
            historical_from=date(1993, 8, 1),
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        code = request.scope.get("code", "")
        if code not in COMPETITIONS:
            return FetchResult(
                complete=False,
                problems=[
                    Problem(
                        ProblemKind.UNSUPPORTED_FEATURE,
                        f"competition {code!r} is not in the project catalogue",
                    )
                ],
            )

        endpoint = MATCHES_ENDPOINT.format(code=code)
        params = {
            key: value
            for key, value in request.scope.items()
            if key in {"status", "dateFrom", "dateTo", "season"}
        }
        self.last_response = None
        self.last_parsed = None
        try:
            response = self.transport.send(
                HttpRequest(endpoint=endpoint, params=params)
            )
        except TransportError as exc:
            # No response content means no evidence to archive, and a failed
            # fetch must never read as a completed sync.
            return FetchResult(complete=False, problems=[exc.problem])

        self.last_response = response
        fetched_at = datetime.now(UTC)
        # Bytes are evidence whether or not they are useful, so a 403 is
        # archived exactly like a 200.
        payload = self.archive.store(
            ArchiveRecord(
                source_slug=self.provider_slug,
                job_run_id=self.job_run_id,
                endpoint=endpoint,
                content=response.content,
                params=response.safe_params,
                response_headers=response.headers,
                http_status=response.status,
                fetched_at=fetched_at,
            )
        )

        if not response.ok:
            kind = {
                400: ProblemKind.AUTHENTICATION,
                401: ProblemKind.AUTHENTICATION,
                403: ProblemKind.AUTHORIZATION,
                404: ProblemKind.NOT_FOUND,
                429: ProblemKind.RATE_LIMIT,
            }.get(response.status, ProblemKind.PROVIDER_SERVER)
            return FetchResult(
                provenance=[payload],
                complete=False,
                problems=[Problem(kind, f"{endpoint} -> HTTP {response.status}")],
            )

        parsed = parse_matches(
            response.content, code=code, payload=payload, known_at=fetched_at
        )
        self.last_parsed = parsed
        records: list[CanonicalRecord] = list(parsed.fixtures)
        return FetchResult(
            records=records,
            provenance=[payload],
            next_cursor=None,
            complete=True,
            problems=parsed.problems,
        )
