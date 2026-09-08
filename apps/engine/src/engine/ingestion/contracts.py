"""The adapter contract (PHASE-0-SPEC.md §15.2).

ONE `fetch` METHOD, NOT FIVE. ARCHITECTURE.md §4A originally specified
`list_competitions()`, `list_fixtures()` and three siblings, each returning a
bare list. That shape cannot express pagination state, partial failure,
rate-limit state or the raw-payload linkage, so it forced every adapter either
to write to the database itself - coupling provider parsing to Postgres - or to
drop the evidence. A single method carrying a `domain` discriminator expresses
those concerns once.

The underlying rules from §4A are unchanged and are what this shape protects:
provider-specific types never escape an adapter, every response body is
archived before parsing, and adding a provider is one new module plus one
`data_sources` row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from engine.ingestion.dto import CanonicalRecord, PayloadRef
from engine.ingestion.errors import Problem


class Domain(StrEnum):
    """A data domain an adapter may support. Not a table name."""

    COMPETITIONS = "competitions"
    SEASONS = "seasons"
    TEAMS = "teams"
    FIXTURES = "fixtures"
    RESULTS = "results"
    ODDS = "odds"


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually supply.

    Small on purpose: no plugin discovery, no registry. Adding a provider is a
    module and a `data_sources` row.
    """

    domains: frozenset[Domain]
    markets: frozenset[str] = frozenset()
    supports_pagination: bool = False
    rate_limit_per_min: int | None = None
    historical_from: date | None = None

    def supports(self, domain: Domain) -> bool:
        return domain in self.domains


@dataclass(frozen=True)
class FetchRequest:
    """What to fetch. `cursor` is opaque and only the adapter interprets it."""

    domain: Domain
    scope: Mapping[str, str] = field(default_factory=dict)
    cursor: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """One page of one domain.

    `next_cursor` and `complete` are SEPARATE and both are load-bearing:

        next_cursor=None, complete=True   -> finished, nothing more to fetch
        next_cursor="abc", complete=True  -> more pages follow
        next_cursor=None, complete=False  -> WE STOPPED AND DID NOT FINISH

    The third case is why a rate limit or an exhausted retry budget can never
    be mistaken for a completed sync.
    """

    records: Sequence[CanonicalRecord] = ()
    provenance: Sequence[PayloadRef] = ()
    next_cursor: str | None = None
    complete: bool = True
    problems: Sequence[Problem] = ()

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def succeeded(self) -> bool:
        """A page is a success only if it finished AND nothing went wrong."""
        return self.complete and not self.problems


@runtime_checkable
class ProviderAdapter(Protocol):
    """Turns one provider's responses into canonical DTOs.

    An implementation must not import psycopg, any schema module, or any
    repository. It receives an `HttpTransport` and a `RawArchive` and depends on
    nothing else, which is what makes it unit-testable with no database.
    """

    #: Matches `data_sources.slug`. The only identity an adapter owns.
    provider_slug: str
    #: Written to `job_runs.adapter_version` - a parsing bug is a provenance
    #: question, and you must be able to find every row a broken adapter made.
    adapter_version: str

    def capabilities(self) -> Capabilities: ...

    def fetch(self, request: FetchRequest) -> FetchResult: ...
