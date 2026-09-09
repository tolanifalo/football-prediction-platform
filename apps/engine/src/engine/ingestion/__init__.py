"""Provider ingestion foundation (P0-10).

The boundary this package exists to hold:

    provider -> HttpTransport -> ProviderAdapter -> RawArchive
             -> canonical DTOs -> identity resolution -> repositories

An adapter depends on two ports and nothing else. It must not import psycopg,
any schema module, or any repository - which is what makes providers
replaceable and adapters unit-testable with no database.

P0-10 implements NO provider. That is P0-11.
See docs/PHASE-0-SPEC.md section 15.
"""

from engine.ingestion.archive import ArchiveRecord, InMemoryRawArchive, RawArchive
from engine.ingestion.contracts import (
    Capabilities,
    Domain,
    FetchRequest,
    FetchResult,
    ProviderAdapter,
)
from engine.ingestion.dto import (
    BookmakerRef,
    CanonicalCompetition,
    CanonicalFixture,
    CanonicalOdds,
    CanonicalRecord,
    CanonicalResult,
    CanonicalSeason,
    CanonicalStats,
    CanonicalTeam,
    CompetitionRef,
    FixtureRef,
    PayloadRef,
    ProviderRef,
    SeasonRef,
    TeamRef,
)
from engine.ingestion.errors import IngestionError, Problem, ProblemKind, is_transient
from engine.ingestion.identity import (
    Ambiguous,
    EntityKind,
    IdentityResolver,
    Resolution,
    Resolved,
    Unknown,
)
from engine.ingestion.redaction import (
    normalise_headers,
    persistable_headers,
    scrub_params,
)
from engine.ingestion.runs import (
    CanonicalWriter,
    JobRunStore,
    RunIdentity,
    RunStatus,
    classify,
)
from engine.ingestion.signatures import body_hash, request_signature
from engine.ingestion.stats import IngestionStats
from engine.ingestion.transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    HttpxTransport,
    RetryPolicy,
    TransportError,
    TransportTimeouts,
)

__all__ = [
    "Ambiguous",
    "ArchiveRecord",
    "BookmakerRef",
    "CanonicalCompetition",
    "CanonicalFixture",
    "CanonicalOdds",
    "CanonicalRecord",
    "CanonicalResult",
    "CanonicalSeason",
    "CanonicalStats",
    "CanonicalTeam",
    "CanonicalWriter",
    "Capabilities",
    "CompetitionRef",
    "Domain",
    "EntityKind",
    "FetchRequest",
    "FetchResult",
    "FixtureRef",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "HttpxTransport",
    "IdentityResolver",
    "InMemoryRawArchive",
    "IngestionError",
    "IngestionStats",
    "JobRunStore",
    "PayloadRef",
    "Problem",
    "ProblemKind",
    "ProviderAdapter",
    "ProviderRef",
    "RawArchive",
    "Resolution",
    "Resolved",
    "RetryPolicy",
    "RunIdentity",
    "RunStatus",
    "SeasonRef",
    "TeamRef",
    "TransportError",
    "TransportTimeouts",
    "Unknown",
    "body_hash",
    "classify",
    "is_transient",
    "normalise_headers",
    "persistable_headers",
    "request_signature",
    "scrub_params",
]
