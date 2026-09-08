"""Shared fixtures for the P0-10 ingestion suite.

NO LIVE PROVIDER CALLS ANYWHERE. Every HTTP interaction goes through
`httpx.MockTransport`; every archive interaction through `InMemoryRawArchive`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from engine.ingestion import (
    ArchiveRecord,
    CanonicalFixture,
    CanonicalOdds,
    CanonicalResult,
    CompetitionRef,
    FixtureRef,
    InMemoryRawArchive,
    PayloadRef,
    SeasonRef,
    TeamRef,
)
from engine.ingestion.dto import BookmakerRef
from engine.ingestion.transport import HttpxTransport, RetryPolicy

KNOWN_AT = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)


@pytest.fixture
def archive() -> InMemoryRawArchive:
    return InMemoryRawArchive()


@pytest.fixture
def payload_ref() -> PayloadRef:
    return PayloadRef(body_hash="a" * 64, request_signature="sig-test")


@pytest.fixture
def fixture_ref() -> FixtureRef:
    return FixtureRef(
        season=SeasonRef(
            competition=CompetitionRef(name="Premier League"),
            label="2025/26",
        ),
        stage="regular_r1",
        home_team=TeamRef(name="Everton"),
        away_team=TeamRef(name="Liverpool"),
    )


@pytest.fixture
def sportsbook() -> BookmakerRef:
    return BookmakerRef(name="Bet365", kind="bookmaker")


@pytest.fixture
def exchange() -> BookmakerRef:
    return BookmakerRef(
        name="Betfair",
        kind="exchange",
        commission_rate=Decimal("0.0500"),
    )


def make_fixture(
    ref: FixtureRef,
    payload: PayloadRef,
    **overrides: object,
) -> CanonicalFixture:
    values: dict[str, object] = {
        "payload_ref": payload,
        "known_at": KNOWN_AT,
        "ref": ref,
        "kickoff_utc": datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        "local_date": datetime(2026, 5, 1, 14, 0, tzinfo=UTC).date(),
        "local_tz": "Europe/London",
        "status": "scheduled",
    }
    values.update(overrides)
    return CanonicalFixture(**values)  # type: ignore[arg-type]


def make_result(
    ref: FixtureRef,
    payload: PayloadRef,
    **overrides: object,
) -> CanonicalResult:
    values: dict[str, object] = {
        "payload_ref": payload,
        "known_at": KNOWN_AT,
        "fixture": ref,
        "result_source": "played",
        "is_trainable": True,
        "ft_home": 2,
        "ft_away": 1,
    }
    values.update(overrides)
    return CanonicalResult(**values)  # type: ignore[arg-type]


def make_odds(
    ref: FixtureRef, payload: PayloadRef, book: BookmakerRef, **overrides: object
) -> CanonicalOdds:
    values: dict[str, object] = {
        "payload_ref": payload,
        "known_at": KNOWN_AT,
        "fixture": ref,
        "bookmaker": book,
        "market_type": "1x2",
        "selection": "home",
        "price": Decimal("2.1000"),
        "observed_at": datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return CanonicalOdds(**values)  # type: ignore[arg-type]


def mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    retry: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> HttpxTransport:
    """A transport whose network is a function. Sleeps are recorded, not taken."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://provider.test"
    )
    recorded = sleeps if sleeps is not None else []
    return HttpxTransport(
        "https://provider.test",
        client=client,
        retry=retry or RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
        sleep=recorded.append,
    )


def sequence_handler(
    responses: Sequence[httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Replay responses in order; the last one repeats if asked again."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[index]

    return handler


def archived(
    archive: InMemoryRawArchive,
    content: bytes,
    **kwargs: object,
) -> PayloadRef:
    record = ArchiveRecord(
        source_slug="probe",
        job_run_id=1,
        endpoint=str(kwargs.pop("endpoint", "/v3/fixtures")),
        content=content,
        **kwargs,  # type: ignore[arg-type]
    )
    return archive.store(record)
