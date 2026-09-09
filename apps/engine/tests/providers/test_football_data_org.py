"""The football-data.org adapter and parser (UPCOMING-FIXTURES.md).

NO LIVE CREDENTIALS ANYWHERE. Every response comes from a checked-in payload
through `httpx.MockTransport`, so the whole phase is testable without a token -
which is the point: the adapter boundary exists so that the absence of a
credential blocks a live fetch and nothing else.

The payloads are realistic rather than minimal: the envelope, the competition
and season objects, the odds placeholder the free tier returns, and the team
shape are all as the documented API publishes them.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import httpx
import pytest

from engine.ingestion.archive import InMemoryRawArchive
from engine.ingestion.contracts import Domain, FetchRequest
from engine.ingestion.dto import PayloadRef
from engine.ingestion.errors import ProblemKind
from engine.ingestion.transport import HttpxTransport
from engine.providers.football_data_org.adapter import (
    ADAPTER_VERSION,
    PROVIDER_SLUG,
    FootballDataOrgAdapter,
)
from engine.providers.football_data_org.catalog import (
    API_KEY_ENV,
    API_KEY_HEADER,
    BASE_URL,
    COMPETITIONS,
    STATUS_MAP,
    season_label,
)
from engine.providers.football_data_org.parser import parse_matches

PAYLOADS = pathlib.Path(__file__).parent / "fixtures" / "football_data_org"
KNOWN_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
PAYLOAD = PayloadRef(body_hash="e" * 64, request_signature="sig-fdo")


def body(name: str) -> bytes:
    return (PAYLOADS / f"{name}.json").read_bytes()


def parsed(name: str, code: str = "PL"):  # type: ignore[no-untyped-def]
    return parse_matches(body(name), code=code, payload=PAYLOAD, known_at=KNOWN_AT)


def transport_for(
    handler: object, *, headers: dict[str, str] | None = None
) -> HttpxTransport:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        base_url=BASE_URL,
    )
    return HttpxTransport(
        BASE_URL, client=client, credential_headers=headers or {}
    )


def responder(name: str, status: int = 200, extra: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body(name),
            headers={"content-type": "application/json", **(extra or {})},
        )

    return handler


class TestCatalogue:
    def test_the_competition_metadata_matches_the_live_api(self) -> None:
        """Verified against /v4/competitions, which needs no credentials."""
        meta = COMPETITIONS["PL"]
        assert meta.provider_id == 2021
        assert meta.code == "PL"
        assert meta.competition_slug == "england-premier-league"
        assert meta.local_tz == "Europe/London"

    def test_the_season_label_matches_the_historical_convention(self) -> None:
        from datetime import date

        assert season_label(date(2026, 8, 21)) == ("2026/27", 2026)
        assert season_label(date(1999, 8, 7)) == ("1999/00", 1999)

    def test_awarded_has_no_canonical_status(self) -> None:
        """Mapping it to `ft` would claim ninety minutes were played."""
        assert "AWARDED" not in STATUS_MAP

    def test_every_mapped_status_is_a_canonical_one(self) -> None:
        canonical = {
            "scheduled", "live", "suspended", "ft",
            "postponed", "abandoned", "cancelled",
        }
        assert set(STATUS_MAP.values()) <= canonical


class TestParsingNormalFixtures:
    def test_it_parses_every_upcoming_match(self) -> None:
        out = parsed("upcoming")
        assert out.rows_seen == 3
        assert len(out.fixtures) == 3
        assert out.problems == []

    def test_kickoff_is_utc_and_the_local_date_follows_it(self) -> None:
        first = parsed("upcoming").fixtures[0]
        assert first.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
        assert first.local_tz == "Europe/London"
        # 15:00 BST on the 12th.
        assert str(first.local_date) == "2026-09-12"

    def test_teams_and_season_come_through_canonically(self) -> None:
        first = parsed("upcoming").fixtures[0]
        assert first.ref.home_team.name == "Arsenal FC"
        assert first.ref.away_team.name == "Chelsea FC"
        assert first.ref.season.label == "2026/27"

    def test_the_canonical_meeting_identity_is_the_regular_one(self) -> None:
        for fixture in parsed("upcoming").fixtures:
            assert fixture.ref.stage == "regular"
            assert fixture.ref.leg == 1
            assert fixture.ref.replay_number == 0

    def test_status_maps_to_scheduled(self) -> None:
        assert all(f.status == "scheduled" for f in parsed("upcoming").fixtures)

    def test_provider_ids_are_kept_separate_from_canonical_fields(self) -> None:
        """A provider key is identity INPUT and never a canonical value."""
        out = parsed("upcoming")
        assert set(out.provider_ids) == {"500001", "500002", "500003"}
        for fixture in out.fixtures:
            # The provider's numeric id lives on the ref as `provider_key`...
            assert fixture.ref.home_team.provider_key is not None
            # ...and nowhere in a canonical field.
            assert not fixture.ref.stage.isdigit()
            assert "500001" not in fixture.ref.home_team.name

    def test_the_provider_team_ids_are_captured_for_the_identity_layer(
        self,
    ) -> None:
        out = parsed("upcoming")
        assert out.team_ids["57"] == "Arsenal FC"
        assert out.team_ids["65"] == "Manchester City FC"

    def test_no_result_is_required_or_read(self) -> None:
        """Eligibility is about scheduling, never about knowing a score."""
        raw = json.loads(body("upcoming"))
        for match in raw["matches"]:
            assert match["score"]["fullTime"]["home"] is None
        assert len(parsed("upcoming").fixtures) == 3


class TestPostponementAndRescheduling:
    def test_a_postponed_match_maps_to_the_canonical_status(self) -> None:
        out = parsed("postponed")
        arsenal = next(
            f for f in out.fixtures if f.ref.home_team.name == "Arsenal FC"
        )
        assert arsenal.status == "postponed"
        assert len(out.fixtures) == 3

    def test_a_rescheduled_match_keeps_its_identity_and_moves_its_kickoff(
        self,
    ) -> None:
        """Kickoff is NOT identity: the same pairing, a different instant."""
        before = next(
            f for f in parsed("upcoming").fixtures
            if f.ref.home_team.name == "Arsenal FC"
        )
        after = next(
            f for f in parsed("rescheduled").fixtures
            if f.ref.home_team.name == "Arsenal FC"
        )
        assert after.kickoff_utc != before.kickoff_utc
        assert after.ref.stage == before.ref.stage
        assert after.ref.leg == before.ref.leg
        assert after.ref.replay_number == before.ref.replay_number
        assert str(after.local_date) == "2026-11-04"


class TestFailsClosed:
    def test_an_unknown_club_still_parses_and_is_left_to_the_resolver(
        self,
    ) -> None:
        """The parser does not know our registry; the resolver refuses."""
        out = parsed("unknown_team")
        names = {f.ref.away_team.name for f in out.fixtures}
        assert "Barnstoneworth United FC" in names

    def test_a_repeated_ordered_pairing_is_refused(self) -> None:
        """The P0-12 meeting hook, unchanged: no ordinal is manufactured."""
        out = parsed("repeated_pairing")
        assert any(
            p.kind is ProblemKind.IDENTITY_AMBIGUOUS for p in out.problems
        )
        pairings = {(f.ref.home_team.name, f.ref.away_team.name) for f in out.fixtures}
        assert ("Arsenal FC", "Chelsea FC") not in pairings
        # The unaffected match still comes through.
        assert ("Liverpool FC", "Manchester City FC") in pairings

    def test_the_refusal_mentions_the_missing_ordinal(self) -> None:
        problem = next(
            p for p in parsed("repeated_pairing").problems
            if p.kind is ProblemKind.IDENTITY_AMBIGUOUS
        )
        assert "meeting ordinal" in problem.message

    def test_matchday_is_available_but_deliberately_unused(self) -> None:
        """It would distinguish the repeat, and using it would fork identity."""
        raw = json.loads(body("repeated_pairing"))
        matchdays = {m["matchday"] for m in raw["matches"]}
        assert len(matchdays) > 1, "the payload does carry distinct matchdays"
        assert any(
            p.kind is ProblemKind.IDENTITY_AMBIGUOUS
            for p in parsed("repeated_pairing").problems
        )

    def test_an_unsupported_stage_is_refused(self) -> None:
        out = parsed("unsupported")
        assert any(
            p.kind is ProblemKind.UNSUPPORTED_FEATURE and "stage" in p.message
            for p in out.problems
        )

    def test_an_awarded_match_is_refused_rather_than_mapped(self) -> None:
        out = parsed("unsupported")
        assert any(
            "AWARDED" in p.message for p in out.problems
        )
        assert len(out.fixtures) == 1

    def test_malformed_rows_never_lose_the_good_one(self) -> None:
        out = parsed("malformed")
        assert out.rows_seen == 4
        assert len(out.problems) == 3
        assert len(out.fixtures) == 1
        assert out.fixtures[0].ref.home_team.name == "Everton FC"

    def test_a_non_json_body_is_a_problem_not_an_exception(self) -> None:
        out = parse_matches(
            b"<html>maintenance</html>", code="PL", payload=PAYLOAD,
            known_at=KNOWN_AT,
        )
        assert out.problems[0].kind is ProblemKind.MALFORMED_PAYLOAD
        assert out.fixtures == []

    def test_a_response_without_a_matches_array_is_a_problem(self) -> None:
        out = parse_matches(
            b'{"errorCode":403}', code="PL", payload=PAYLOAD, known_at=KNOWN_AT
        )
        assert out.problems[0].kind is ProblemKind.MALFORMED_PAYLOAD

    def test_an_uncatalogued_competition_is_refused(self) -> None:
        with pytest.raises(ValueError, match="catalogue"):
            parse_matches(
                body("upcoming"), code="XX", payload=PAYLOAD, known_at=KNOWN_AT
            )


class TestAdapter:
    def request(self) -> FetchRequest:
        return FetchRequest(domain=Domain.FIXTURES, scope={"code": "PL"})

    def test_a_successful_fetch_archives_and_completes(self) -> None:
        archive = InMemoryRawArchive()
        adapter = FootballDataOrgAdapter(
            transport=transport_for(responder("upcoming")),
            archive=archive, job_run_id=1,
        )
        result = adapter.fetch(self.request())
        assert result.complete is True and result.succeeded is True
        assert len(result.records) == 3
        assert len(archive.observations) == 1

    def test_the_archived_bytes_are_exactly_what_the_provider_sent(self) -> None:
        archive = InMemoryRawArchive()
        adapter = FootballDataOrgAdapter(
            transport=transport_for(responder("upcoming")),
            archive=archive, job_run_id=1,
        )
        ref = adapter.fetch(self.request()).provenance[0]
        assert archive.bodies[ref.body_hash] == body("upcoming")

    def test_it_reports_one_page_and_no_cursor(self) -> None:
        adapter = FootballDataOrgAdapter(
            transport=transport_for(responder("upcoming")),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        result = adapter.fetch(self.request())
        assert result.next_cursor is None and result.has_more is False
        assert adapter.capabilities().supports_pagination is False

    def test_a_403_archives_the_bytes_and_does_not_complete(self) -> None:
        """The real unauthenticated response. Bytes are evidence regardless."""
        archive = InMemoryRawArchive()
        adapter = FootballDataOrgAdapter(
            transport=transport_for(
                lambda _r: httpx.Response(
                    403, content=b'{"message":"restricted","errorCode":403}'
                )
            ),
            archive=archive, job_run_id=1,
        )
        result = adapter.fetch(self.request())
        assert result.complete is False
        assert result.problems[0].kind is ProblemKind.AUTHORIZATION
        assert len(archive.observations) == 1

    def test_a_400_invalid_token_is_an_authentication_problem(self) -> None:
        adapter = FootballDataOrgAdapter(
            transport=transport_for(
                lambda _r: httpx.Response(
                    400,
                    content=b'{"message":"Your API token is invalid.",'
                            b'"errorCode":400}',
                )
            ),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        result = adapter.fetch(self.request())
        assert result.complete is False
        assert result.problems[0].kind is ProblemKind.AUTHENTICATION

    def test_an_uncatalogued_competition_never_reaches_the_network(self) -> None:
        called: list[int] = []

        def handler(_r: httpx.Request) -> httpx.Response:
            called.append(1)
            return httpx.Response(200, content=b"{}")

        adapter = FootballDataOrgAdapter(
            transport=transport_for(handler),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        result = adapter.fetch(
            FetchRequest(domain=Domain.FIXTURES, scope={"code": "XX"})
        )
        assert result.complete is False and called == []

    def test_the_adapter_declares_only_the_fixtures_domain(self) -> None:
        adapter = FootballDataOrgAdapter(
            transport=transport_for(responder("upcoming")),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        capabilities = adapter.capabilities()
        assert capabilities.domains == frozenset({Domain.FIXTURES})
        assert capabilities.markets == frozenset()


class TestSecretsAndEvidence:
    def test_private_response_headers_are_never_archived(self) -> None:
        """Only the approved whitelist reaches `raw_payloads`."""
        archive = InMemoryRawArchive()
        adapter = FootballDataOrgAdapter(
            transport=transport_for(
                responder(
                    "upcoming",
                    extra={
                        "set-cookie": "session=abcdef; HttpOnly",
                        "x-api-key": "super-secret",
                        "authorization": "Bearer nope",
                        "x-requests-available-minute": "9",
                    },
                )
            ),
            archive=archive, job_run_id=1,
        )
        adapter.fetch(FetchRequest(domain=Domain.FIXTURES, scope={"code": "PL"}))
        stored = {k.lower() for k in archive.observations[0].headers}
        for banned in ("set-cookie", "x-api-key", "authorization"):
            assert banned not in stored, banned

    def test_the_token_never_reaches_the_request_signature_or_params(
        self,
    ) -> None:
        archive = InMemoryRawArchive()
        adapter = FootballDataOrgAdapter(
            transport=transport_for(
                responder("upcoming"), headers={API_KEY_HEADER: "SECRET-TOKEN"}
            ),
            archive=archive, job_run_id=1,
        )
        adapter.fetch(
            FetchRequest(
                domain=Domain.FIXTURES, scope={"code": "PL", "status": "SCHEDULED"}
            )
        )
        record = archive.observations[0]
        assert "SECRET-TOKEN" not in json.dumps(dict(record.safe_params))
        assert "SECRET-TOKEN" not in str(record.endpoint)
        assert "SECRET-TOKEN" not in record.request_signature
        assert all("SECRET-TOKEN" != v for v in record.headers.values())

    def test_the_signature_is_deterministic_for_one_request(self) -> None:
        signatures = []
        for _ in range(2):
            archive = InMemoryRawArchive()
            adapter = FootballDataOrgAdapter(
                transport=transport_for(responder("upcoming")),
                archive=archive, job_run_id=1,
            )
            result = adapter.fetch(
                FetchRequest(
                    domain=Domain.FIXTURES,
                    scope={"code": "PL", "status": "SCHEDULED"},
                )
            )
            signatures.append(result.provenance[0].request_signature)
        assert signatures[0] == signatures[1]

    def test_a_different_scope_changes_the_signature(self) -> None:
        def signature_for(scope: dict[str, str]) -> str:
            adapter = FootballDataOrgAdapter(
                transport=transport_for(responder("upcoming")),
                archive=InMemoryRawArchive(), job_run_id=1,
            )
            return adapter.fetch(
                FetchRequest(domain=Domain.FIXTURES, scope=scope)
            ).provenance[0].request_signature

        assert signature_for({"code": "PL", "status": "SCHEDULED"}) != signature_for(
            {"code": "PL", "status": "FINISHED"}
        )

    def test_the_endpoint_template_carries_no_query_string(self) -> None:
        from engine.providers.football_data_org.catalog import MATCHES_ENDPOINT

        assert "?" not in MATCHES_ENDPOINT
        assert MATCHES_ENDPOINT.format(code="PL") == "/v4/competitions/PL/matches"

    def test_the_credential_boundary_is_environment_only(self) -> None:
        """The token is named, not embedded, anywhere in the package."""
        package = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "engine" / "providers" / "football_data_org"
        )
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "X-Auth-Token: " not in source
            if path.name != "catalog.py":
                assert API_KEY_ENV not in source or "import" in source
        assert API_KEY_ENV == "FOOTBALL_DATA_ORG_TOKEN"


class TestNoDatabaseOrModelLeakage:
    def test_the_provider_package_imports_no_database_code(self) -> None:
        import ast

        package = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "engine" / "providers" / "football_data_org"
        )
        banned = {"psycopg", "sqlalchemy", "asyncpg"}
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".")[0]
                    if root in banned or name.startswith("engine.ingestion.postgres"):
                        offenders.append(f"{path.name} imports {name}")
        assert offenders == []

    def test_the_provider_package_imports_no_model_code(self) -> None:
        package = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src" / "engine" / "providers" / "football_data_org"
        )
        for path in package.glob("*.py"):
            assert "engine.model" not in path.read_text(encoding="utf-8")

    def test_the_adapter_version_and_slug_are_stable(self) -> None:
        assert PROVIDER_SLUG == "football-data-org"
        assert ADAPTER_VERSION == "football-data-org@1.0.0"
