"""Six seasons of real E0 files through the one adapter (MODEL-BASELINE.md §5).

PARAMETERISED, NOT DUPLICATED. The contract is identical for every season, so
it is asserted once and run six times; only the genuine per-season variations
get their own test.

Every fixture is BYTE-EXACT provider output - header plus five real rows, kept
verbatim including CRLF and any UTF-8 BOM. The point of this suite is that the
column-name-driven parser survives the schema drift these files actually
contain, so synthesising them would test nothing.

Variations these files carry, verified against the live source:

  2021/22 and 2024/25   UTF-8 BOM before `Div`
  2024/25               120 columns rather than 106: Interwetten and BetVictor
                        gone, 1XBet and Betfair (including the exchange)
                        arrived. Only four of the six catalogued bookmakers
                        still publish closing 1X2 prices.
  all six               CRLF, dd/mm/yyyy dates, `Time` present on every row,
                        all twelve statistic columns populated.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from engine.ingestion.dto import PayloadRef
from engine.providers.football_data_couk.catalog import BOOKMAKERS, season_label
from engine.providers.football_data_couk.parser import ParsedFile, parse_csv

KNOWN_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
PAYLOAD = PayloadRef(body_hash="d" * 64, request_signature="sig-historical")

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEASONS = ("1920", "2021", "2122", "2223", "2324", "2425")
#: Seasons whose file begins with a UTF-8 byte-order mark.
BOM_SEASONS = frozenset({"2122", "2425"})
#: Interwetten and BetVictor stopped appearing in 2024/25.
REDUCED_BOOK_SEASONS = frozenset({"2425"})


def sample(season: str) -> bytes:
    return (FIXTURES / f"E0_{season}_sample.csv").read_bytes()


def parsed(season: str) -> ParsedFile:
    return parse_csv(
        sample(season),
        division="E0",
        season_folder=season,
        payload=PAYLOAD,
        known_at=KNOWN_AT,
    )


@pytest.mark.parametrize("season", SEASONS)
class TestEverySeasonParses:
    def test_it_parses_without_problems(self, season: str) -> None:
        out = parsed(season)
        assert out.problems == [], f"{season}: {out.problems}"
        assert out.rows_seen == len(out.fixtures) > 0

    def test_dates_and_kickoff_times_are_present(self, season: str) -> None:
        """Every one of these seasons supplies `Time`; none falls back."""
        for fixture in parsed(season).fixtures:
            assert fixture.kickoff_utc.tzinfo is not None
            assert fixture.local_tz == "Europe/London"
            assert fixture.status == "ft"

    def test_the_kickoff_lands_in_the_right_season(self, season: str) -> None:
        label, start_year = season_label(season)
        for fixture in parsed(season).fixtures:
            assert fixture.kickoff_utc.year in (start_year, start_year + 1)
            assert fixture.ref.season.label == label

    def test_results_are_complete(self, season: str) -> None:
        out = parsed(season)
        assert len(out.results) == len(out.fixtures)
        for result in out.results:
            assert result.result_source == "played"
            assert result.is_trainable is True
            assert result.ft_home is not None and result.ft_away is not None
            assert result.ht_home is not None and result.ht_away is not None

    def test_all_twelve_statistics_are_populated(self, season: str) -> None:
        out = parsed(season)
        assert len(out.stats) == len(out.fixtures)
        for stats in out.stats:
            for field in (
                "home_shots", "away_shots",
                "home_shots_on_target", "away_shots_on_target",
                "home_corners", "away_corners",
                "home_fouls", "away_fouls",
                "home_yellow_cards", "away_yellow_cards",
                "home_red_cards", "away_red_cards",
            ):
                assert getattr(stats, field) is not None, f"{season}: {field}"

    def test_the_provider_supplies_no_possession_or_xg(self, season: str) -> None:
        """Absent in every season, so the canonical columns stay NULL."""
        for stats in parsed(season).stats:
            assert not hasattr(stats, "home_possession")
            assert not hasattr(stats, "home_xg")

    def test_only_closing_odds_are_emitted(self, season: str) -> None:
        out = parsed(season)
        assert out.odds, f"{season} produced no odds"
        for odds in out.odds:
            assert odds.price_kind == "provider_closing"
            assert odds.price is not None and odds.price > 1
            assert odds.provider_at is None
            assert odds.bookmaker.kind == "bookmaker"

    def test_no_aggregate_pseudo_bookmaker_appears(self, season: str) -> None:
        """Max and Avg are cross-book aggregates with no bookmaker (§14.2)."""
        catalogued = {b.slug for b in BOOKMAKERS}
        for odds in parsed(season).odds:
            assert odds.bookmaker.provider_key in catalogued
            assert odds.bookmaker.provider_key not in {"max", "avg", "bb"}

    def test_every_ordered_pairing_is_unique(self, season: str) -> None:
        """The meeting hook's precondition, checked per season."""
        pairs = [
            (f.ref.home_team.name, f.ref.away_team.name)
            for f in parsed(season).fixtures
        ]
        assert len(pairs) == len(set(pairs))


class TestSeasonSpecificVariation:
    @pytest.mark.parametrize("season", sorted(BOM_SEASONS))
    def test_a_byte_order_mark_is_stripped(self, season: str) -> None:
        raw = sample(season)
        assert raw[:3] == b"\xef\xbb\xbf", f"{season} fixture lost its BOM"
        # The first column must still be found by name despite the mark.
        assert parsed(season).fixtures

    @pytest.mark.parametrize("season", sorted(set(SEASONS) - BOM_SEASONS))
    def test_a_season_without_a_mark_parses_the_same_way(self, season: str) -> None:
        assert sample(season)[:3] != b"\xef\xbb\xbf"
        assert parsed(season).fixtures

    @pytest.mark.parametrize("season", SEASONS)
    def test_the_fixture_keeps_crlf_line_endings(self, season: str) -> None:
        assert b"\r\n" in sample(season)

    def test_2425_lost_two_bookmakers_and_ignores_the_new_ones(self) -> None:
        """Interwetten and BetVictor gone; 1XBet and Betfair unknown to us.

        An unknown column is ignored and survives only in raw evidence, so a
        provider adding a bookmaker never fails an import - and never silently
        becomes a canonical bookmaker either.
        """
        books_2324 = {o.bookmaker.provider_key for o in parsed("2324").odds}
        books_2425 = {o.bookmaker.provider_key for o in parsed("2425").odds}
        assert {"interwetten", "betvictor"} <= books_2324
        assert not ({"interwetten", "betvictor"} & books_2425)
        # The columns that arrived are not catalogued, so they yield no ticks.
        assert b"1XBCH" in sample("2425") and b"BFECH" in sample("2425")
        assert all(b in {b2.slug for b2 in BOOKMAKERS} for b in books_2425)

    def test_the_reduced_season_still_produces_every_market(self) -> None:
        markets = {o.market_type for o in parsed("2425").odds}
        assert markets == {"1x2", "over_under", "asian_handicap"}

    @pytest.mark.parametrize("season", SEASONS)
    def test_no_season_emits_an_exchange_or_a_lay_price(self, season: str) -> None:
        """Betfair's exchange columns exist in 2024/25 and must stay out."""
        for odds in parsed(season).odds:
            assert odds.bookmaker.kind == "bookmaker"
            assert odds.bookmaker.commission_rate is None


class TestFrozenSeasonRegression:
    def test_2324_still_parses_exactly_as_before(self) -> None:
        """The season P0-11 imported must not have moved."""
        out = parsed("2324")
        assert out.rows_seen == 12
        assert len(out.fixtures) == 12
        first = out.fixtures[0]
        assert first.ref.home_team.name == "Burnley"
        assert first.ref.away_team.name == "Man City"
        assert first.kickoff_utc.isoformat().startswith("2023-08-11T19:00")
