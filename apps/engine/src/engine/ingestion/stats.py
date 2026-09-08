"""Ingestion counters (PHASE-0-SPEC.md §15.10).

NO NEW TABLE. These serialise into `job_runs.stats`, which is jsonb and already
writable by `engine_rw` (`GRANT UPDATE (status, finished_at, stats, error)`).

Everything else is structured stdout logging. No metrics backend, no dashboard,
no monitoring stack: §G bans it and nothing consumes it yet. Per-request status
and latency are already reconstructible from `raw_payloads`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestionStats:
    """Mutable counters for one run. Serialised once, when the run closes."""

    requests: int = 0
    retries: int = 0
    timeouts: int = 0
    bytes_fetched: int = 0
    http_status_counts: dict[str, int] = field(default_factory=dict)
    latency_ms: list[int] = field(default_factory=list)

    pages: int = 0
    pages_failed: int = 0

    rows_parsed: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0

    identity_resolved: int = 0
    identity_ambiguous: int = 0
    identity_unknown: int = 0

    def record_response(
        self, *, status: int, elapsed_ms: int, size: int, attempts: int
    ) -> None:
        self.requests += 1
        self.retries += max(0, attempts - 1)
        self.bytes_fetched += size
        self.latency_ms.append(elapsed_ms)
        key = str(status)
        self.http_status_counts[key] = self.http_status_counts.get(key, 0) + 1

    def record_timeout(self) -> None:
        """A timeout produced no response content, so it is NOT a raw_payloads
        row (`body_id` is NOT NULL). It is counted here or it is invisible."""
        self.requests += 1
        self.timeouts += 1

    def _percentile(self, fraction: float) -> int | None:
        if not self.latency_ms:
            return None
        ordered = sorted(self.latency_ms)
        index = min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))
        return ordered[index]

    def to_json(self) -> dict[str, Any]:
        """The shape written to `job_runs.stats`."""
        payload: dict[str, Any] = {
            "requests": self.requests,
            "retries": self.retries,
            "timeouts": self.timeouts,
            "bytes": self.bytes_fetched,
            "http_status_counts": dict(self.http_status_counts),
            "pages": self.pages,
            "pages_failed": self.pages_failed,
            "rows_parsed": self.rows_parsed,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "identity_resolved": self.identity_resolved,
            "identity_ambiguous": self.identity_ambiguous,
            "identity_unknown": self.identity_unknown,
        }
        p50 = self._percentile(0.5)
        if p50 is not None:
            payload["latency_ms_p50"] = p50
            payload["latency_ms_max"] = max(self.latency_ms)
        return payload
