"""HTTP transport (PHASE-0-SPEC.md §15.4).

Retries, backoff, timeouts and credential injection live HERE, once. An adapter
that had to implement its own retry loop is a loop every future provider would
re-implement wrongly.

CREDENTIALS NEVER LEAVE THIS MODULE. They are read from the environment, added
to the outbound request, and never appear in the returned `HttpResponse`, in
`request_params`, in a signature, or in a log line. The adapter above never
sees one and therefore cannot leak one.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from engine.ingestion.errors import Problem, ProblemKind, problem_kind_for_status
from engine.ingestion.redaction import persistable_headers, scrub_params

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpRequest:
    """A request in provider-neutral terms.

    `endpoint` is the TEMPLATE ("/v3/fixtures"), never an interpolated URL, so
    a signature can never contain a query string with a key in it (§9.1).
    """

    endpoint: str
    params: Mapping[str, str] = field(default_factory=dict)
    method: str = "GET"


@dataclass(frozen=True)
class HttpResponse:
    """What the transport hands back. Contains no credential, by construction.

    `headers` is already lowercased and whitelisted, so it is safe to persist
    into `raw_payloads.response_headers` as-is.
    """

    status: int
    content: bytes
    headers: Mapping[str, str]
    elapsed_ms: int
    #: Params with credentials replaced by a marker - safe for `request_params`.
    safe_params: Mapping[str, object]
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter: float = 0.25

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Backoff for `attempt` (1-based). `Retry-After` always wins."""
        if retry_after is not None:
            return min(retry_after, self.max_delay_seconds)
        exponential = self.base_delay_seconds * (2 ** (attempt - 1))
        raw: float = min(exponential, self.max_delay_seconds)
        # Jitter spreads retries so concurrent runs do not resonate.
        spread: float = random.uniform(-self.jitter, self.jitter)
        return raw * (1.0 + spread)


@dataclass(frozen=True)
class TransportTimeouts:
    """Explicit, all four. A default of None is how an ingest job hangs."""

    connect: float = 5.0
    read: float = 30.0
    write: float = 10.0
    pool: float = 5.0

    def to_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect, read=self.read, write=self.write, pool=self.pool
        )


class TransportError(Exception):
    """A request that produced no usable response. Carries a `Problem`."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(f"{problem.kind}: {problem.message}")
        self.problem = problem


class HttpTransport(Protocol):
    """The port an adapter depends on. Never httpx directly."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds form only. An HTTP-date form is ignored rather than guessed."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class HttpxTransport:
    """The production `HttpTransport`.

    Synchronous on purpose: Phase 0 invokes jobs by hand and has no scheduler,
    so async would add concurrency complexity for scalability nothing needs.

    Testable with `httpx.MockTransport` - pass a pre-built `httpx.Client`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        credential_params: Mapping[str, str] | None = None,
        credential_headers: Mapping[str, str] | None = None,
        timeouts: TransportTimeouts | None = None,
        retry: RetryPolicy | None = None,
        sleep: object = time.sleep,
    ) -> None:
        self._timeouts = timeouts or TransportTimeouts()
        self._retry = retry or RetryPolicy()
        # Held privately and never surfaced on a response.
        self._credential_params = dict(credential_params or {})
        self._credential_headers = dict(credential_headers or {})
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=base_url, timeout=self._timeouts.to_httpx()
        )

    @classmethod
    def from_env(
        cls,
        base_url: str,
        *,
        api_key_env: str | None = None,
        api_key_param: str | None = None,
        api_key_header: str | None = None,
        **kwargs: object,
    ) -> HttpxTransport:
        """Build a transport with credentials read from the environment.

        Secrets live in the environment and nowhere else - not in a config
        file, not in the database, not in a `data_sources` row (§9.1: the
        provider registry "holds no credentials").
        """
        params: dict[str, str] = {}
        headers: dict[str, str] = {}
        if api_key_env:
            secret = os.environ.get(api_key_env)
            if not secret:
                raise TransportError(
                    Problem(
                        kind=ProblemKind.AUTHENTICATION,
                        message=f"environment variable {api_key_env} is not set",
                    )
                )
            if api_key_param:
                params[api_key_param] = secret
            elif api_key_header:
                headers[api_key_header] = secret
            else:
                raise ValueError("api_key_env requires api_key_param or api_key_header")
        return cls(
            base_url,
            credential_params=params,
            credential_headers=headers,
            **kwargs,  # type: ignore[arg-type]
        )

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send with bounded retries. Raises `TransportError` when unusable."""
        # The caller's params are never mutated; credentials are merged into a
        # copy for the wire only.
        wire_params = {**dict(request.params), **self._credential_params}
        safe_params = scrub_params(request.params)
        last: Problem | None = None

        for attempt in range(1, self._retry.max_attempts + 1):
            started = time.perf_counter()
            try:
                response = self._client.request(
                    request.method,
                    request.endpoint,
                    params=wire_params,
                    headers=self._credential_headers or None,
                )
            except httpx.TimeoutException as exc:
                last = Problem(
                    kind=ProblemKind.TIMEOUT,
                    message=f"{request.method} {request.endpoint} timed out: {exc}",
                    context={"attempt": attempt},
                )
            except httpx.HTTPError as exc:
                last = Problem(
                    kind=ProblemKind.NETWORK,
                    message=f"{request.method} {request.endpoint} failed: {exc}",
                    context={"attempt": attempt},
                )
            else:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                kind = problem_kind_for_status(response.status_code)
                headers = persistable_headers(response.headers.items())
                if kind is None:
                    return HttpResponse(
                        status=response.status_code,
                        content=response.content,
                        headers=headers,
                        elapsed_ms=elapsed_ms,
                        safe_params=safe_params,
                        attempts=attempt,
                    )
                retry_after = _parse_retry_after(headers.get("retry-after"))
                last = Problem(
                    kind=kind,
                    message=(
                        f"{request.method} {request.endpoint} "
                        f"-> HTTP {response.status_code}"
                    ),
                    context={"attempt": attempt, "status": response.status_code},
                    retry_after_seconds=retry_after,
                )
                # A permanent failure still produced BYTES, and those bytes are
                # evidence. Hand them back so the caller can archive them.
                if not last.transient:
                    return HttpResponse(
                        status=response.status_code,
                        content=response.content,
                        headers=headers,
                        elapsed_ms=elapsed_ms,
                        safe_params=safe_params,
                        attempts=attempt,
                    )

            if not last.transient or attempt == self._retry.max_attempts:
                break
            delay = self._retry.delay_for(attempt, last.retry_after_seconds)
            logger.warning(
                "ingestion retry",
                extra={"endpoint": request.endpoint, "attempt": attempt},
            )
            self._sleep(delay)  # type: ignore[operator]

        assert last is not None
        raise TransportError(last)

    def close(self) -> None:
        self._client.close()
