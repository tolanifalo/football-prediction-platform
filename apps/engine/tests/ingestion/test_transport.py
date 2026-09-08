"""HTTP transport behaviour (P0-10 §15.4). No network is ever touched."""

from __future__ import annotations

import httpx
import pytest

from engine.ingestion.errors import ProblemKind
from engine.ingestion.transport import (
    HttpRequest,
    HttpxTransport,
    RetryPolicy,
    TransportError,
    TransportTimeouts,
)

from .conftest import mock_transport, sequence_handler

OK = httpx.Response(
    200,
    content=b'{"ok":true}',
    headers={"Content-Type": "application/json"},
)


class TestSuccess:
    def test_returns_body_status_and_latency(self) -> None:
        transport = mock_transport(lambda _r: OK)
        got = transport.send(HttpRequest("/v3/fixtures", {"league": "E0"}))
        assert got.status == 200
        assert got.content == b'{"ok":true}'
        assert got.attempts == 1
        assert got.elapsed_ms >= 0

    def test_response_headers_are_lowercased_and_whitelisted(self) -> None:
        handler = lambda _r: httpx.Response(  # noqa: E731
            200, content=b"{}", headers={"ETag": "abc", "X-Api-Key": "SECRET"}
        )
        got = mock_transport(handler).send(HttpRequest("/v3/fixtures"))
        assert got.headers["etag"] == "abc"
        assert "x-api-key" not in got.headers, "a credential header must never persist"
        assert all(name == name.lower() for name in got.headers)

    def test_credentials_never_appear_on_the_response(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return OK

        client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://provider.test"
        )
        transport = HttpxTransport(
            "https://provider.test",
            client=client,
            credential_params={"api_key": "SECRET"},
        )
        got = transport.send(HttpRequest("/v3/fixtures", {"league": "E0"}))
        # The key reached the wire...
        assert seen["api_key"] == "SECRET"
        # ...and is redacted in the params destined for raw_payloads.
        assert got.safe_params == {"league": "E0"}
        assert "SECRET" not in str(got.safe_params)

    def test_caller_params_are_not_mutated(self) -> None:
        params = {"league": "E0"}
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: OK), base_url="https://provider.test"
        )
        HttpxTransport(
            "https://provider.test", client=client, credential_params={"api_key": "S"}
        ).send(HttpRequest("/v3/fixtures", params))
        assert params == {"league": "E0"}


class TestPermanentFailures:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, ProblemKind.AUTHENTICATION),
            (403, ProblemKind.AUTHORIZATION),
            (404, ProblemKind.NOT_FOUND),
            (400, ProblemKind.MALFORMED_PAYLOAD),
        ],
    )
    def test_permanent_status_is_returned_with_its_bytes(
        self, status: int, kind: ProblemKind
    ) -> None:
        """A permanent failure still produced bytes, and bytes are evidence."""
        sleeps: list[float] = []
        handler = sequence_handler([httpx.Response(status, content=b"denied")])
        got = mock_transport(handler, sleeps=sleeps).send(HttpRequest("/v3/fixtures"))
        assert got.status == status
        assert got.content == b"denied", "evidence must survive a permanent failure"
        assert sleeps == [], f"{kind} must not be retried"
        assert got.attempts == 1


class TestTransientFailures:
    def test_5xx_is_retried_then_succeeds(self) -> None:
        sleeps: list[float] = []
        handler = sequence_handler([httpx.Response(503, content=b""), OK])
        got = mock_transport(handler, sleeps=sleeps).send(HttpRequest("/v3/fixtures"))
        assert got.status == 200
        assert got.attempts == 2
        assert len(sleeps) == 1

    def test_retries_are_bounded_then_raise(self) -> None:
        sleeps: list[float] = []
        handler = sequence_handler([httpx.Response(500, content=b"")])
        transport = mock_transport(
            handler,
            retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
            sleeps=sleeps,
        )
        with pytest.raises(TransportError) as exc:
            transport.send(HttpRequest("/v3/fixtures"))
        assert exc.value.problem.kind is ProblemKind.PROVIDER_SERVER
        assert len(sleeps) == 2, "three attempts means two sleeps"

    def test_429_honours_retry_after(self) -> None:
        sleeps: list[float] = []
        handler = sequence_handler(
            [httpx.Response(429, content=b"", headers={"Retry-After": "7"}), OK]
        )
        got = mock_transport(handler, sleeps=sleeps).send(HttpRequest("/v3/fixtures"))
        assert got.status == 200
        assert sleeps == [7.0], "Retry-After must win over computed backoff"

    def test_timeout_is_retried_then_raises_timeout(self) -> None:
        sleeps: list[float] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        transport = mock_transport(
            handler,
            retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.01),
            sleeps=sleeps,
        )
        with pytest.raises(TransportError) as exc:
            transport.send(HttpRequest("/v3/fixtures"))
        assert exc.value.problem.kind is ProblemKind.TIMEOUT
        assert exc.value.problem.transient
        assert len(sleeps) == 1

    def test_network_error_is_classified_as_network(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        transport = mock_transport(handler, retry=RetryPolicy(max_attempts=1))
        with pytest.raises(TransportError) as exc:
            transport.send(HttpRequest("/v3/fixtures"))
        assert exc.value.problem.kind is ProblemKind.NETWORK


class TestPolicies:
    def test_backoff_grows_and_is_bounded(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=0.0)
        got = [policy.delay_for(n) for n in (1, 2, 3, 4, 5)]
        assert got == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_jitter_perturbs_within_bounds(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, jitter=0.25)
        for _ in range(50):
            assert 0.75 <= policy.delay_for(1) <= 1.25

    def test_retry_after_overrides_backoff_and_is_capped(self) -> None:
        policy = RetryPolicy(max_delay_seconds=30.0)
        assert policy.delay_for(1, retry_after=5.0) == 5.0
        assert policy.delay_for(1, retry_after=9999.0) == 30.0

    def test_all_four_timeouts_are_explicit(self) -> None:
        t = TransportTimeouts().to_httpx()
        assert t.connect and t.read and t.write and t.pool

    def test_missing_credential_env_fails_as_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FPP_TEST_KEY", raising=False)
        with pytest.raises(TransportError) as exc:
            HttpxTransport.from_env(
                "https://provider.test",
                api_key_env="FPP_TEST_KEY",
                api_key_param="api_key",
            )
        assert exc.value.problem.kind is ProblemKind.AUTHENTICATION
