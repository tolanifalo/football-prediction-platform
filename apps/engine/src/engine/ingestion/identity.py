"""The identity-resolution port (PHASE-0-SPEC.md §15.8).

P0-10 DEFINES THE INTERFACE AND IMPLEMENTS NONE OF IT. Matching, alias scoring,
`confidence` semantics and `fixture_match_candidates` all belong to P0-12.

The contract that matters is the outcome type: three cases, and only one of
them yields an identity. `Ambiguous` and `Unknown` are values a caller must
handle, not exceptions it can accidentally swallow into a guess.

  "Ambiguous resolution must fail rather than choose."          (§10.3)
  "Never auto-remap."                                           (§6 rule 9)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from engine.ingestion.dto import ProviderRef


class EntityKind(StrEnum):
    """The canonical registries a provider reference can resolve to.

    Exactly the five `external_ids.entity_type` values (§11.4). `fixture` is
    deliberately absent: fixture provider IDs are deferred and §11.4 stands
    unamended.
    """

    COUNTRY = "country"
    COMPETITION = "competition"
    SEASON = "season"
    TEAM = "team"
    VENUE = "venue"


@dataclass(frozen=True)
class Resolved:
    """Exactly one canonical entity matches."""

    entity_kind: EntityKind
    internal_id: UUID


@dataclass(frozen=True)
class Ambiguous:
    """Several canonical entities match. NEVER pick one.

    "Barcelona" matches FC Barcelona and Barcelona SC; "Arsenal" matches
    Arsenal FC, Arsenal Tula and Arsenal Sarandí (§10.3). The caller's only
    correct move is to file a review item and skip the record.
    """

    entity_kind: EntityKind
    candidates: tuple[UUID, ...]
    reason: str = "ambiguous"


@dataclass(frozen=True)
class Unknown:
    """Nothing matches. The common case at scale, not an error."""

    entity_kind: EntityKind
    reason: str = "unknown"


Resolution = Resolved | Ambiguous | Unknown


class IdentityResolver(Protocol):
    """Turns a provider reference into a canonical identity, or refuses to.

    Implementations must never create a canonical entity as a side effect of
    resolution, and must never rewrite an existing `external_ids.internal_id`:
    a remap supersedes and inserts (§11.3 rule 5).
    """

    def resolve(self, kind: EntityKind, ref: ProviderRef) -> Resolution: ...


class AlwaysUnknownResolver:
    """The P0-10 placeholder: resolves nothing, guesses nothing.

    Exists so the ingestion boundary is exercisable end to end before P0-12
    provides the real implementation. Wiring this into production would ingest
    zero canonical rows, which is the safe failure direction.
    """

    def resolve(self, kind: EntityKind, ref: ProviderRef) -> Resolution:
        return Unknown(
            entity_kind=kind, reason="no resolver configured (P0-12 owns matching)"
        )
