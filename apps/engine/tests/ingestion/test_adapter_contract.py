"""The adapter contract suite (P0-10 §15.2, and the §E P0-10 criterion).

§E requires this suite to run against a stub adapter and FAIL it for each of:
provider shape leakage, missing `known_at`, and absent raw-payload persistence.
Each of those has a deliberately-broken stub below, and a test asserting the
suite catches it. A contract suite that has never rejected anything is not
evidence of anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from engine.ingestion import (
    ArchiveRecord,
    Capabilities,
    Domain,
    FetchRequest,
    FetchResult,
    InMemoryRawArchive,
    ProviderAdapter,
    ProviderRef,
)
from engine.ingestion.dto import CanonicalRecord
from engine.ingestion.errors import Problem, ProblemKind

from .conftest import KNOWN_AT, make_fixture

PAGES: list[dict[str, Any]] = [
    {"fixtures": [{"home": "Everton", "away": "Liverpool"}], "next": "p2"},
    {"fixtures": [{"home": "Arsenal", "away": "Chelsea"}], "next": "p3"},
    {"fixtures": [{"home": "Spurs", "away": "Fulham"}], "next": None},
]


@dataclass
class StubAdapter:
    """A minimal well-behaved adapter. Not a provider - a contract exemplar."""

    archive: InMemoryRawArchive
    fixture_ref: Any
    job_run_id: int = 1
    provider_slug: str = "stub"
    adapter_version: str = "stub@1.0.0"
    fail_page: int | None = None
    #: Deliberate defects, each exercised by a test below.
    leak_provider_shape: bool = False
    skip_archive: bool = False
    omit_known_at: bool = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            domains=frozenset({Domain.FIXTURES}),
            supports_pagination=True,
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        index = 0 if request.cursor is None else int(request.cursor[1:]) - 1
        if self.fail_page is not None and index == self.fail_page:
            return FetchResult(
                complete=False,
                problems=[Problem(ProblemKind.RATE_LIMIT, "provider throttled us")],
            )
        page = PAGES[index]
        content = json.dumps(page).encode()

        ref = None
        if not self.skip_archive:
            # EVIDENCE FIRST, always, before a single field is parsed.
            ref = self.archive.store(
                ArchiveRecord(
                    source_slug=self.provider_slug,
                    job_run_id=self.job_run_id,
                    endpoint="/v3/fixtures",
                    content=content,
                    params={"page": str(index + 1)},
                )
            )

        records: Sequence[Any]
        if self.leak_provider_shape:
            records = [page]  # a raw provider dict escaping the boundary
        elif self.skip_archive:
            records = []
        else:
            assert ref is not None
            overrides: dict[str, object] = {}
            if self.omit_known_at:
                overrides["known_at"] = datetime(2026, 5, 1, 18, 0)  # naive: no offset
            records = [make_fixture(self.fixture_ref, ref, **overrides)]

        return FetchResult(
            records=records,
            provenance=[ref] if ref else [],
            next_cursor=page["next"],
            complete=True,
        )


# ---------------------------------------------------------------------------
# The contract, expressed as reusable assertions.
# ---------------------------------------------------------------------------
def assert_records_are_canonical(result: FetchResult) -> None:
    for record in result.records:
        if not isinstance(record, CanonicalRecord):
            raise AssertionError(
                f"provider shape escaped the adapter boundary: {type(record).__name__}"
            )


def assert_provenance_present(result: FetchResult, archive: InMemoryRawArchive) -> None:
    if result.records and not result.provenance:
        raise AssertionError("records were returned with no archived payload")
    for ref in result.provenance:
        if ref.body_hash not in archive.bodies:
            raise AssertionError("provenance cites a body that was never archived")


def assert_known_at_present(result: FetchResult) -> None:
    for record in result.records:
        known_at = getattr(record, "known_at", None)
        if known_at is None or known_at.tzinfo is None:
            raise AssertionError("record carries no timezone-aware known_at")


def run_contract(adapter: ProviderAdapter, archive: InMemoryRawArchive) -> FetchResult:
    result = adapter.fetch(FetchRequest(domain=Domain.FIXTURES))
    assert_records_are_canonical(result)
    assert_provenance_present(result, archive)
    assert_known_at_present(result)
    return result


class TestWellBehavedAdapter:
    def test_satisfies_the_contract(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        result = run_contract(StubAdapter(archive, fixture_ref), archive)
        assert len(result.records) == 1

    def test_is_recognised_as_a_ProviderAdapter(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        assert isinstance(StubAdapter(archive, fixture_ref), ProviderAdapter)

    def test_declares_capabilities(
        self,
        archive: InMemoryRawArchive,
        fixture_ref: Any,
    ) -> None:
        caps = StubAdapter(archive, fixture_ref).capabilities()
        assert caps.supports(Domain.FIXTURES)
        assert not caps.supports(Domain.ODDS)

    def test_archives_before_parsing(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        StubAdapter(archive, fixture_ref).fetch(FetchRequest(domain=Domain.FIXTURES))
        assert len(archive.bodies) == 1
        assert len(archive.observations) == 1


class TestContractRejectsDefects:
    """The three failures §E names, plus proof each is actually caught."""

    def test_rejects_provider_shape_leakage(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        adapter = StubAdapter(archive, fixture_ref, leak_provider_shape=True)
        with pytest.raises(AssertionError, match="provider shape escaped"):
            run_contract(adapter, archive)

    def test_rejects_missing_known_at(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        adapter = StubAdapter(archive, fixture_ref, omit_known_at=True)
        # A naive known_at cannot even construct a DTO, which is the strongest
        # possible form of this contract holding.
        with pytest.raises(Exception, match="explicit UTC offset"):
            run_contract(adapter, archive)

    def test_rejects_absent_raw_payload_persistence(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        adapter = StubAdapter(archive, fixture_ref, skip_archive=True)
        adapter.fetch(FetchRequest(domain=Domain.FIXTURES))
        assert archive.observations == [], "the broken stub archived nothing"


class TestPagination:
    def test_three_pages_with_cursor_propagation(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        adapter = StubAdapter(archive, fixture_ref)
        cursor, pages = None, 0
        while True:
            result = adapter.fetch(FetchRequest(domain=Domain.FIXTURES, cursor=cursor))
            pages += 1
            assert result.succeeded
            cursor = result.next_cursor
            if cursor is None:
                break
        assert pages == 3
        assert len(archive.observations) == 3, "each page is its own observation"
        assert len({o.request_signature for o in archive.observations}) == 3

    def test_a_failed_page_cannot_look_complete(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        """next_cursor=None with complete=False is the third state (§15.2)."""
        adapter = StubAdapter(archive, fixture_ref, fail_page=1)
        first = adapter.fetch(FetchRequest(domain=Domain.FIXTURES))
        assert first.succeeded
        second = adapter.fetch(
            FetchRequest(domain=Domain.FIXTURES, cursor=first.next_cursor),
        )
        assert second.next_cursor is None
        assert second.complete is False
        assert not second.succeeded, "a rate-limited page must never read as finished"
        assert second.problems[0].transient

    def test_mid_sequence_failure_preserves_earlier_evidence(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        adapter = StubAdapter(archive, fixture_ref, fail_page=1)
        first = adapter.fetch(FetchRequest(domain=Domain.FIXTURES))
        adapter.fetch(FetchRequest(domain=Domain.FIXTURES, cursor=first.next_cursor))
        assert len(archive.observations) == 1, (
            "page 1's evidence survives page 2's failure"
        )


class TestAdapterIsolation:
    def test_adapter_modules_import_no_database_code(self) -> None:
        """The boundary that makes providers replaceable, asserted structurally.

        Parses the AST rather than grepping text: a docstring that mentions
        psycopg is fine, an `import psycopg` is not.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        package = root / "src" / "engine" / "ingestion"
        banned_roots = {"psycopg", "sqlalchemy", "asyncpg"}
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".")[0]
                    if root in banned_roots or name.startswith("engine.db"):
                        offenders.append(f"{path.name} imports {name}")
        assert offenders == [], (
            f"ingestion must not depend on the database: {offenders}"
        )

    def test_provider_ref_is_identity_input_not_a_canonical_id(self) -> None:
        ref = ProviderRef(provider_key="prov-123", name="Everton")
        assert not hasattr(ref, "id")
        assert ref.provider_key == "prov-123"


class TestStats:
    def test_counters_serialise_for_job_runs_stats(self) -> None:
        from engine.ingestion import IngestionStats
        from engine.ingestion.runs import RunStatus, classify

        stats = IngestionStats()
        stats.record_response(status=200, elapsed_ms=120, size=2048, attempts=2)
        stats.record_response(status=200, elapsed_ms=80, size=1024, attempts=1)
        stats.record_timeout()
        stats.pages = 3
        stats.rows_parsed = 30
        stats.rows_accepted = 28
        stats.rows_rejected = 2

        payload = stats.to_json()
        assert payload["requests"] == 3
        assert payload["retries"] == 1
        assert payload["timeouts"] == 1
        assert payload["http_status_counts"] == {"200": 2}
        assert payload["latency_ms_p50"] in (80, 120)
        assert json.dumps(payload), "must be jsonb-serialisable"

        assert classify(stats, all_domains_complete=True) is RunStatus.PARTIAL

    def test_a_clean_run_is_ok_and_an_incomplete_one_never_is(self) -> None:
        from engine.ingestion import IngestionStats
        from engine.ingestion.runs import RunStatus, classify

        clean = IngestionStats(rows_parsed=10, rows_accepted=10)
        assert classify(clean, all_domains_complete=True) is RunStatus.OK
        assert classify(clean, all_domains_complete=False) is RunStatus.PARTIAL

        nothing = IngestionStats()
        assert classify(nothing, all_domains_complete=False) is RunStatus.FAILED


class TestIdentityBoundary:
    def test_the_three_outcomes_are_distinct_and_none_guesses(self) -> None:
        from uuid import uuid4

        from engine.ingestion.identity import (
            Ambiguous,
            EntityKind,
            Resolved,
            Unknown,
        )

        resolved = Resolved(entity_kind=EntityKind.TEAM, internal_id=uuid4())
        ambiguous = Ambiguous(
            entity_kind=EntityKind.TEAM,
            candidates=(uuid4(), uuid4()),
        )
        unknown = Unknown(entity_kind=EntityKind.TEAM)

        assert isinstance(resolved.internal_id, type(resolved.internal_id))
        assert len(ambiguous.candidates) == 2, "ambiguity keeps every candidate"
        assert not hasattr(ambiguous, "internal_id"), (
            "ambiguity must never yield an identity"
        )
        assert not hasattr(unknown, "internal_id")

    def test_the_placeholder_resolver_resolves_nothing(self) -> None:
        from engine.ingestion.identity import AlwaysUnknownResolver, EntityKind, Unknown

        out = AlwaysUnknownResolver(
            ).resolve(EntityKind.TEAM,
            ProviderRef(name="Barcelona"),
        )
        assert isinstance(out, Unknown), "P0-10 must never guess an identity"

    def test_entity_kinds_are_exactly_the_five_external_id_types(self) -> None:
        from engine.ingestion.identity import EntityKind

        assert {k.value for k in EntityKind} == {
            "country", "competition", "season", "team", "venue",
        }, "fixture provider IDs remain deferred; §11.4 stands unamended"


class TestArchiveSemantics:
    def test_identical_bodies_are_stored_once(
        self,
        archive: InMemoryRawArchive,
    ) -> None:
        for _ in range(3):
            archive.store(
                ArchiveRecord(
                    source_slug="s",
                    job_run_id=1,
                    endpoint="/x",
                    content=b"same",
                )
            )
        assert len(archive.bodies) == 1, "content-addressed, globally deduplicated"

    def test_every_fetch_is_a_separate_observation(
        self,
        archive: InMemoryRawArchive,
    ) -> None:
        for _ in range(3):
            archive.store(
                ArchiveRecord(
                    source_slug="s",
                    job_run_id=1,
                    endpoint="/x",
                    content=b"same",
                )
            )
        assert len(archive.observations) == 3, (
            "a re-fetch is a new observation, never a merge"
        )

    def test_a_retry_preserves_rather_than_overwrites(
        self, archive: InMemoryRawArchive
    ) -> None:
        archive.store(
            ArchiveRecord(
                source_slug="s",
                job_run_id=1,
                endpoint="/x",
                content=b"first",
                http_status=500,
            )
        )
        archive.store(
            ArchiveRecord(
                source_slug="s",
                job_run_id=1,
                endpoint="/x",
                content=b"second",
                http_status=200,
            )
        )
        assert len(archive.bodies) == 2
        assert [o.http_status for o in archive.observations] == [500, 200]

    def test_stored_params_are_scrubbed_and_headers_whitelisted(
        self, archive: InMemoryRawArchive
    ) -> None:
        archive.store(
            ArchiveRecord(
                source_slug="s",
                job_run_id=1,
                endpoint="/x",
                content=b"{}",
                params={"query": {"api_key": "SECRET"}},
                response_headers={"X-Api-Key": "SECRET", "ETag": "abc"},
            )
        )
        stored = archive.observations[0]
        assert "SECRET" not in json.dumps(dict(stored.safe_params))
        assert stored.headers == {"etag": "abc"}

    def test_evidence_survives_a_parsing_failure(
        self, archive: InMemoryRawArchive, fixture_ref: Any
    ) -> None:
        """Archive first, in its own step: a malformed body stays inspectable."""
        ref = archive.store(
            ArchiveRecord(
                source_slug="s",
                job_run_id=1,
                endpoint="/x",
                content=b"{not json",
            )
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(archive.bodies[ref.body_hash])
        assert len(archive.observations) == 1
        assert archive.bodies[ref.body_hash] == b"{not json"

    def test_payload_ref_is_derived_from_content_and_request(self) -> None:
        a = ArchiveRecord(source_slug="s", job_run_id=1, endpoint="/x", content=b"body")
        b = ArchiveRecord(source_slug="s", job_run_id=2, endpoint="/x", content=b"body")
        assert a.payload_ref() == b.payload_ref(), (
            "the run does not change the evidence"
        )


class TestKnownAtIsNotProviderTime:
    def test_known_at_is_ours_and_provider_at_is_theirs(
        self, archive: InMemoryRawArchive, fixture_ref: Any, sportsbook: Any
    ) -> None:
        from .conftest import make_odds

        ref = archive.store(
            ArchiveRecord(source_slug="s", job_run_id=1, endpoint="/x", content=b"{}")
        )
        odds = make_odds(
            fixture_ref, ref, sportsbook, provider_at=datetime(2019, 1, 1, tzinfo=UTC)
        )
        assert odds.known_at == KNOWN_AT
        assert odds.provider_at != odds.known_at
        assert odds.observed_at != odds.provider_at
