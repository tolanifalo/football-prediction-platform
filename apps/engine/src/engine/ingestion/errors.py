"""Canonical ingestion error taxonomy (PHASE-0-SPEC.md §15.6).

One small closed vocabulary, shared by the transport, the adapters and the
ingestion service. Provider-specific exception types never escape an adapter:
they are translated into a `Problem` here.

Retryability is a property of the KIND, declared once, so no caller has to
remember that a 401 must not be retried while a 429 must.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProblemKind(StrEnum):
    """Why an ingestion step did not produce a usable record."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PROVIDER_SERVER = "provider_server"
    NOT_FOUND = "not_found"
    MALFORMED_PAYLOAD = "malformed_payload"
    SCHEMA_VALIDATION = "schema_validation"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    UNSUPPORTED_FEATURE = "unsupported_feature"


#: Kinds worth retrying. Everything else is permanent for this request.
#:
#: Authentication and authorization are deliberately absent: retrying a bad key
#: burns quota and can earn a ban. Malformed and schema-invalid payloads are
#: absent because the provider will send the same bytes again.
TRANSIENT_KINDS: frozenset[ProblemKind] = frozenset(
    {
        ProblemKind.RATE_LIMIT,
        ProblemKind.TIMEOUT,
        ProblemKind.NETWORK,
        ProblemKind.PROVIDER_SERVER,
    }
)


def is_transient(kind: ProblemKind) -> bool:
    """True if retrying the same request could plausibly succeed."""
    return kind in TRANSIENT_KINDS


@dataclass(frozen=True)
class Problem:
    """One thing that went wrong, carried rather than raised.

    Ingestion is expected to be partially successful: a page of fifty fixtures
    where three fail validation yields forty-seven records and three problems.
    Raising would discard the forty-seven.
    """

    kind: ProblemKind
    message: str
    #: Which record or page, when that is meaningful. Never a provider object.
    context: dict[str, Any] = field(default_factory=dict)
    #: Populated for RATE_LIMIT when the provider sent Retry-After.
    retry_after_seconds: float | None = None

    @property
    def transient(self) -> bool:
        return is_transient(self.kind)


class IngestionError(Exception):
    """Raised only when a run cannot continue at all.

    Per-record and per-page failures are `Problem` values, not exceptions.
    """

    def __init__(self, problem: Problem) -> None:
        super().__init__(f"{problem.kind}: {problem.message}")
        self.problem = problem


def problem_kind_for_status(status: int) -> ProblemKind | None:
    """Map an HTTP status to a problem kind, or None when it is a success."""
    if 200 <= status < 300:
        return None
    if status in (401, 407):
        return ProblemKind.AUTHENTICATION
    if status == 403:
        return ProblemKind.AUTHORIZATION
    if status == 404:
        return ProblemKind.NOT_FOUND
    if status == 429:
        return ProblemKind.RATE_LIMIT
    if status >= 500:
        return ProblemKind.PROVIDER_SERVER
    # Any other 4xx is the provider telling us the request itself is wrong.
    return ProblemKind.MALFORMED_PAYLOAD
