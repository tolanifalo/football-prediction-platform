"""Parser and adapter tests for football-data.co.uk (P0-11).

NO LIVE PROVIDER CALLS. Every byte comes from a checked-in fixture derived from
the real E0 2023/24 file; every HTTP interaction goes through
`httpx.MockTransport`.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from engine.ingestion import (
    ArchiveRecord,
    Domain,
    FetchRequest,
    InMemoryRawArchive,
    PayloadRef,
)
from engine.ingestion.errors import ProblemKind
from engine.ingestion.transport import HttpxTransport, RetryPolicy
from engine.providers.football_data_couk import (
    CLOSING_CONVENTION,
    FootballDataCoUkAdapter,
    parse_csv,
)
from engine.providers.football_data_couk.catalog import season_label
from engine.providers.football_data_couk.parser import parse_kickoff

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "E0_2324_sample.csv"
BOM_SAMPLE = FIXTURES / "E0_bom_legacy_sample.csv"
KNOWN_AT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
PAYLOAD = PayloadRef(body_hash="c" * 64, request_signature="sig-p11")


def parse(path: pathlib.Path, season: str = "2324"):  # type: ignore[no-untyped-def]
    return parse_csv(
        path.read_bytes(), division="E0", season_folder=season,
        payload=PAYLOAD, known_at=KNOWN_AT,
    )


class TestRealSample:
    def test_parses_every_row(self) -> None:
        out = parse(SAMPLE)
        assert out.rows_seen == 12
        assert len(out.fixtures) == 12
        assert out.problems == []

    def test_crlf_and_column_names_not_positions(self) -> None:
        assert b"\r\n" in SAMPLE.read_bytes()
        out = parse(SAMPLE)
        first = out.fixtures[0]
        assert first.ref.home_team.name == "Burnley"
        assert first.ref.away_team.name == "Man City"

    def test_uk_local_time_becomes_utc(self) -> None:
        """11/08/2023 20:00 UK is BST, so 19:00Z - not 20:00Z."""
        out = parse(SAMPLE)
        first = out.fixtures[0]
        assert first.kickoff_utc == datetime(2023, 8, 11, 19, 0, tzinfo=UTC)
        assert first.local_tz == "Europe/London"
        assert str(first.local_date) == "2023-08-11"

    def test_fixture_identity_has_no_kickoff(self) -> None:
        ref = parse(SAMPLE).fixtures[0].ref
        assert (ref.stage, ref.leg, ref.replay_number) == ("regular", 1, 0)
        assert not hasattr(ref, "kickoff_utc")

    def test_status_is_always_ft(self) -> None:
        """The archive contains only played matches (§16.1)."""
        assert {f.status for f in parse(SAMPLE).fixtures} == {"ft"}

    def test_result_mapping(self) -> None:
        result = parse(SAMPLE).results[0]
        assert (result.ft_home, result.ft_away) == (0, 3)
        assert (result.ht_home, result.ht_away) == (0, 2)
        assert result.result_source == "played"
        assert result.is_trainable is True

    def test_stats_mapping_with_possession_and_xg_absent(self) -> None:
        stats = parse(SAMPLE).stats[0]
        assert stats.home_shots == 6 and stats.away_shots == 17
        assert stats.home_shots_on_target == 1 and stats.away_shots_on_target == 8
        assert stats.home_corners == 6 and stats.away_corners == 5
        assert stats.home_fouls == 11 and stats.away_fouls == 8
        assert stats.home_red_cards == 1 and stats.away_red_cards == 0
        # This provider supplies neither, so the DTO has no field for them.
        assert not hasattr(stats, "home_possession")
        assert not hasattr(stats, "home_xg")

    def test_only_closing_odds_are_emitted(self) -> None:
        for odds in parse(SAMPLE).odds:
            assert odds.price_kind == "provider_closing"
            assert odds.observed_at_convention == CLOSING_CONVENTION
            assert odds.side == "back"
            assert odds.bookmaker.kind == "bookmaker"
            assert odds.provider_at is None

    def test_closing_price_matches_the_C_column_not_the_pre_closing_one(self) -> None:
        """Burnley v Man City: B365H=8 pre-closing, B365CH=9 closing."""
        odds = parse(SAMPLE).odds
        b365_home = [
            o for o in odds
            if o.bookmaker.provider_key == "bet365"
            and o.market_type == "1x2" and o.selection == "home"
            and o.fixture.home_team.name == "Burnley"
        ]
        assert len(b365_home) == 1
        assert b365_home[0].price == Decimal("9")

    def test_observed_at_is_the_kickoff_instant(self) -> None:
        out = parse(SAMPLE)
        kickoffs = {(f.ref.home_team.name, f.kickoff_utc) for f in out.fixtures}
        for o in out.odds:
            assert (o.fixture.home_team.name, o.observed_at) in kickoffs

    def test_asian_handicap_uses_the_home_perspective_line(self) -> None:
        ah = [o for o in parse(SAMPLE).odds if o.market_type == "asian_handicap"]
        assert ah, "the sample carries closing Asian handicap prices"
        for o in ah:
            assert o.selection in {"home", "away"}
            assert o.line is not None
        # Both selections of one FIXTURE share ONE line (G8). The key must be
        # the ordered pairing: a club hosts several matches in one sample.
        by_fixture: dict[tuple[str, str], set[Decimal]] = {}
        for o in ah:
            key = (o.fixture.home_team.name, o.fixture.away_team.name)
            by_fixture.setdefault(key, set()).add(o.line)  # type: ignore[arg-type]
        assert all(len(lines) == 1 for lines in by_fixture.values())
        # ...and each fixture has both sides of that one market.
        sides = {
            (
                o.fixture.home_team.name,
                o.fixture.away_team.name,
                o.bookmaker.provider_key,
            ):
            {x.selection for x in ah
             if (x.fixture.home_team.name, x.fixture.away_team.name,
                 x.bookmaker.provider_key)
             == (o.fixture.home_team.name, o.fixture.away_team.name,
                 o.bookmaker.provider_key)}
            for o in ah
        }
        assert all(s == {"home", "away"} for s in sides.values())

    def test_over_under_line_is_2_50(self) -> None:
        ou = [o for o in parse(SAMPLE).odds if o.market_type == "over_under"]
        assert ou and all(o.line == Decimal("2.50") for o in ou)

    def test_no_aggregate_columns_are_ingested(self) -> None:
        """Max/Avg are cross-bookmaker aggregates with no bookmaker (§14.2)."""
        slugs = {o.bookmaker.provider_key for o in parse(SAMPLE).odds}
        assert slugs <= {"bet365", "bwin", "interwetten", "pinnacle",
                         "william-hill", "betvictor"}
        assert not any(s in {"max", "avg", "market"} for s in slugs if s)

    def test_empty_odds_cell_produces_no_tick(self) -> None:
        """The last sample row has empty MaxCAHH/AvgCAHH cells upstream."""
        out = parse(SAMPLE)
        assert all(o.price is not None for o in out.odds)
        assert all(o.is_available for o in out.odds)

    def test_every_ordered_pairing_is_unique(self) -> None:
        pairs = [
            (f.ref.home_team.name, f.ref.away_team.name)
            for f in parse(SAMPLE).fixtures
        ]
        assert len(pairs) == len(set(pairs))

    def test_team_names_are_collected_verbatim(self) -> None:
        names = parse(SAMPLE).team_names
        assert "Nott'm Forest" in names, "the provider's spelling is preserved"
        assert "Man City" in names


class TestBomAndLegacySchema:
    def test_bom_is_stripped_so_div_is_a_normal_key(self) -> None:
        assert BOM_SAMPLE.read_bytes().startswith(b"\xef\xbb\xbf")
        out = parse(BOM_SAMPLE, season="9394")
        assert out.problems == []
        assert len(out.fixtures) == 2

    def test_two_digit_date_is_parsed_as_1993(self) -> None:
        out = parse(BOM_SAMPLE, season="9394")
        assert out.fixtures[0].kickoff_utc.year == 1993

    def test_unknown_columns_are_tolerated(self) -> None:
        """A future provider column must never fail an import."""
        assert "FutureUnknownColumn" in BOM_SAMPLE.read_text(encoding="utf-8-sig")
        assert parse(BOM_SAMPLE, season="9394").problems == []

    def test_missing_time_defaults_to_midnight_local(self) -> None:
        out = parse(BOM_SAMPLE, season="9394")
        # 00:00 BST on 14/08/93 is 23:00Z on the 13th.
        assert out.fixtures[0].kickoff_utc.hour == 23

    def test_blank_stats_stay_null_and_never_zero(self) -> None:
        out = parse(BOM_SAMPLE, season="9394")
        # The second row has every stat blank, so no stats record is emitted.
        assert len(out.stats) == 1
        assert out.stats[0].home_shots == 14

    def test_blank_odds_row_yields_no_ticks_for_that_fixture(self) -> None:
        out = parse(BOM_SAMPLE, season="9394")
        everton = [o for o in out.odds if o.fixture.home_team.name == "Everton"]
        assert everton == []


class TestMalformedRows:
    def test_a_row_without_a_score_is_a_problem_not_a_crash(self) -> None:
        content = (
            b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\r\n"
            b"E0,11/08/2023,Burnley,Man City,,\r\n"
            b"E0,12/08/2023,Arsenal,Chelsea,2,1\r\n"
        )
        out = parse_csv(content, division="E0", season_folder="2324",
                        payload=PAYLOAD, known_at=KNOWN_AT)
        assert len(out.fixtures) == 1, "the good row still parses"
        assert len(out.problems) == 1
        assert out.problems[0].kind is ProblemKind.SCHEMA_VALIDATION

    def test_an_unparseable_date_is_a_problem(self) -> None:
        content = (
            b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\r\n"
            b"E0,2023-08-11,Burnley,Man City,0,3\r\n"
        )
        out = parse_csv(content, division="E0", season_folder="2324",
                        payload=PAYLOAD, known_at=KNOWN_AT)
        assert out.fixtures == [] and len(out.problems) == 1

    def test_a_trailing_blank_line_is_not_a_defect(self) -> None:
        content = (
            b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\r\n"
            b"E0,11/08/2023,Burnley,Man City,0,3\r\n"
            b",,,,,\r\n"
        )
        out = parse_csv(content, division="E0", season_folder="2324",
                        payload=PAYLOAD, known_at=KNOWN_AT)
        assert len(out.fixtures) == 1 and out.problems == []

    def test_a_row_where_teams_are_identical_is_rejected(self) -> None:
        content = (
            b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\r\n"
            b"E0,11/08/2023,Burnley,Burnley,0,3\r\n"
        )
        out = parse_csv(content, division="E0", season_folder="2324",
                        payload=PAYLOAD, known_at=KNOWN_AT)
        assert out.fixtures == [] and len(out.problems) == 1

    def test_an_unknown_division_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not in the project catalogue"):
            parse_csv(b"Div\r\n", division="ZZ9", season_folder="2324",
                      payload=PAYLOAD, known_at=KNOWN_AT)


class TestHelpers:
    @pytest.mark.parametrize(
        ("folder", "label", "year"),
        [(
            "2324",
            "2023/24",
            2023), ("9394",
            "1993/94",
            1993), ("0001",
            "2000/01",
            2000,
        )],
    )
    def test_season_label(self, folder: str, label: str, year: int) -> None:
        assert season_label(folder) == (label, year)

    def test_both_published_date_formats(self) -> None:
        long_form, _ = parse_kickoff("11/08/2023", "20:00", "Europe/London")
        short_form, _ = parse_kickoff("11/08/23", "20:00", "Europe/London")
        assert long_form == short_form

    def test_winter_kickoff_has_no_bst_offset(self) -> None:
        kickoff, day = parse_kickoff("13/01/2024", "15:00", "Europe/London")
        assert kickoff == datetime(2024, 1, 13, 15, 0, tzinfo=UTC)
        assert str(day) == "2024-01-13"


def _transport(handler, **kw):  # type: ignore[no-untyped-def]
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://football-data.co.uk"
    )
    return HttpxTransport(
        "https://football-data.co.uk", client=client,
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.01),
        sleep=kw.get("sleeps", []).append if "sleeps" in kw else (lambda _s: None),
    )


class TestAdapter:
    def test_fetch_archives_before_parsing_and_completes(self) -> None:
        body = SAMPLE.read_bytes()
        archive = InMemoryRawArchive()
        adapter = FootballDataCoUkAdapter(
            transport=_transport(lambda _r: httpx.Response(200, content=body)),
            archive=archive, job_run_id=1,
        )
        result = adapter.fetch(
            FetchRequest(
                domain=Domain.FIXTURES,
                scope={"division": "E0", "season": "2324"},
            )
        )
        assert result.complete and result.next_cursor is None
        assert len(archive.bodies) == 1 and len(archive.observations) == 1
        # The archived bytes are the EXACT response, BOM and CRLF intact.
        assert next(iter(archive.bodies.values())) == body
        assert result.records and result.provenance

    def test_file_provider_declares_no_pagination(self) -> None:
        adapter = FootballDataCoUkAdapter(
            transport=_transport(lambda _r: httpx.Response(200, content=b"")),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        assert adapter.capabilities().supports_pagination is False

    def test_404_archives_the_bytes_and_does_not_complete(self) -> None:
        archive = InMemoryRawArchive()
        adapter = FootballDataCoUkAdapter(
            transport=_transport(lambda _r: httpx.Response(404, content=b"nope")),
            archive=archive, job_run_id=1,
        )
        result = adapter.fetch(
            FetchRequest(
                domain=Domain.FIXTURES,
                scope={"division": "E0", "season": "9999"},
            )
        )
        assert result.complete is False
        assert result.problems[0].kind is ProblemKind.NOT_FOUND
        # A permanent HTTP failure is still a request the run must count.
        assert adapter.last_response is not None
        assert adapter.last_response.status == 404
        assert (
            len(archive.observations) == 1
        ), "a permanent failure still produced evidence"

    def test_a_download_failure_never_reads_as_complete(self) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        adapter = FootballDataCoUkAdapter(
            transport=_transport(boom), archive=InMemoryRawArchive(), job_run_id=1
        )
        result = adapter.fetch(
            FetchRequest(
                domain=Domain.FIXTURES,
                scope={"division": "E0", "season": "2324"},
            )
        )
        assert result.complete is False and result.succeeded is False
        assert result.records == ()
        # No response at all, so the caller must count the problem, not a
        # status code. This is the branch that makes a timeout visible.
        assert adapter.last_response is None

    def test_an_uncatalogued_division_is_unsupported(self) -> None:
        adapter = FootballDataCoUkAdapter(
            transport=_transport(lambda _r: httpx.Response(200, content=b"")),
            archive=InMemoryRawArchive(), job_run_id=1,
        )
        result = adapter.fetch(
            FetchRequest(
                domain=Domain.FIXTURES,
                scope={"division": "ZZ9", "season": "2324"},
            )
        )
        assert result.problems[0].kind is ProblemKind.UNSUPPORTED_FEATURE
        assert result.complete is False

    def test_refetching_reuses_the_body_and_adds_an_observation(self) -> None:
        body = SAMPLE.read_bytes()
        archive = InMemoryRawArchive()
        for _ in range(2):
            archive.store(
                ArchiveRecord(source_slug="football-data-couk", job_run_id=1,
                              endpoint="/mmz4281/2324/E0.csv", content=body)
            )
        assert len(archive.bodies) == 1
        assert len(archive.observations) == 2

    def test_adapter_imports_no_database_code(self) -> None:
        import ast

        root = pathlib.Path(__file__).resolve().parents[2]
        package = root / "src" / "engine" / "providers"
        banned = {"psycopg", "sqlalchemy", "asyncpg"}
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root_pkg = name.split(".")[0]
                    persists = name.startswith("engine.ingestion.postgres")
                    if root_pkg in banned or persists:
                        offenders.append(f"{path.name} imports {name}")
        assert offenders == [], f"a provider must not touch the database: {offenders}"
