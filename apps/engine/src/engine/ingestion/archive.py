"""The raw archive port (PHASE-0-SPEC.md §15.5, §9.1, §9.3).

EVIDENCE IS ARCHIVED BEFORE PARSING, AND IN ITS OWN TRANSACTION. A malformed
payload must still be inspectable afterwards; raw evidence must never disappear
because downstream parsing failed.

Two tables, two different jobs (§9.1):

  raw_payload_bodies  content-addressed, globally deduplicated on
                      (hash_algo, body_hash). The same bytes are stored once.
  raw_payloads        ONE ROW PER FETCH. Never deduplicated - a re-fetch is a
                      new observation, and a retry is a new observation. That
                      is the point.

The adapter depends only on the Protocol; tests use the in-memory fake.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from engine.ingestion.dto import PayloadRef
from engine.ingestion.redaction import persistable_headers, scrub_params
from engine.ingestion.signatures import body_hash, request_signature


@dataclass(frozen=True)
class ArchiveRecord:
    """One fetch, ready to be archived.

    `content` is the EXACT decompressed response bytes: after transport decoding
    and before any parsing (§9.4).
    """

    source_slug: str
    job_run_id: int
    endpoint: str
    content: bytes
    params: Mapping[str, Any] = field(default_factory=dict)
    response_headers: Mapping[str, str] = field(default_factory=dict)
    http_status: int | None = None
    fetched_at: datetime | None = None

    def payload_ref(self) -> PayloadRef:
        return PayloadRef(
            body_hash=body_hash(self.content),
            request_signature=request_signature(self.endpoint, self.params),
        )


class RawArchive(Protocol):
    """Persists evidence. The only database-shaped thing an adapter touches."""

    def store(self, record: ArchiveRecord) -> PayloadRef: ...


@dataclass
class _StoredObservation:
    body_hash: str
    request_signature: str
    endpoint: str
    job_run_id: int
    http_status: int | None
    safe_params: Mapping[str, Any]
    headers: Mapping[str, str]
    fetched_at: datetime


class InMemoryRawArchive:
    """Test double with the real deduplication semantics.

    Bodies deduplicate by hash; observations never do. Tests assert exactly the
    property the production table enforces, without needing Postgres.
    """

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.observations: list[_StoredObservation] = []

    def store(self, record: ArchiveRecord) -> PayloadRef:
        ref = record.payload_ref()
        # Global dedup on content, exactly like UNIQUE (hash_algo, body_hash).
        self.bodies.setdefault(ref.body_hash, record.content)
        # Never deduplicated: one row per fetch, retries included.
        self.observations.append(
            _StoredObservation(
                body_hash=ref.body_hash,
                request_signature=ref.request_signature,
                endpoint=record.endpoint,
                job_run_id=record.job_run_id,
                http_status=record.http_status,
                safe_params=scrub_params(record.params),
                headers=persistable_headers(record.response_headers),
                fetched_at=record.fetched_at or datetime.now(UTC),
            )
        )
        return ref

    def observations_for(self, body_hash_value: str) -> list[_StoredObservation]:
        return [o for o in self.observations if o.body_hash == body_hash_value]


#: The SQL a production RawArchive issues. Kept here as the single reference
#: for P0-11's psycopg implementation, so the boundary is documented without
#: P0-10 opening a database connection.
#:
#: Body first, ON CONFLICT DO NOTHING so a concurrent identical fetch is
#: idempotent (probed); then the observation, which always inserts.
BODY_UPSERT_SQL = """
INSERT INTO raw_payload_bodies (hash_algo, body_hash, body, byte_size)
VALUES ('sha256', %(body_hash)s, %(body)s, %(byte_size)s)
ON CONFLICT (hash_algo, body_hash) DO NOTHING
"""

BODY_SELECT_SQL = """
SELECT id FROM raw_payload_bodies
 WHERE hash_algo = 'sha256' AND body_hash = %(body_hash)s
"""

OBSERVATION_INSERT_SQL = """
INSERT INTO raw_payloads
  (source_id, job_run_id, body_id, endpoint, request_signature,
   request_params, response_headers, http_status, fetched_at)
VALUES
  (%(source_id)s, %(job_run_id)s, %(body_id)s, %(endpoint)s, %(request_signature)s,
   %(request_params)s, %(response_headers)s, %(http_status)s, %(fetched_at)s)
"""
