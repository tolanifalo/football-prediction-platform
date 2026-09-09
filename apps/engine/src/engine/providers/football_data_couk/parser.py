"""CSV row -> canonical DTOs for football-data.co.uk (PHASE-0-SPEC.md §16.3).

Column-NAME driven, never positional: the E0 file has grown from 28 columns in
1993/94 to 132 in 2025/26, and column counts differ between divisions within
one season. An unknown column is ignored here and preserved in raw evidence -
the provider adding a column must never fail an import.

Decoded with `utf-8-sig`, because the 2025/26 file carries a UTF-8 BOM that
would otherwise appear as a `﻿Div` key.
"""

from __future__ import annotations

import csv
import io
import zoneinfo
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from engine.ingestion.dto import (
    BookmakerRef,
    CanonicalFixture,
    CanonicalOdds,
    CanonicalResult,
    CanonicalStats,
    CompetitionRef,
    FixtureRef,
    PayloadRef,
    SeasonRef,
    TeamRef,
)
from engine.ingestion.errors import Problem, ProblemKind
from engine.providers.football_data_couk.catalog import (
    BOOKMAKERS,
    DIVISIONS,
    OVER_UNDER_LINE,
    STAT_COLUMNS,
    season_label,
)

#: Why `observed_at` is the kickoff instant, recorded on every closing tick.
#: The provider declares these columns closing but supplies NO timestamp, so a
#: convention is mandatory (§14.4) and must be stated rather than implied.
CLOSING_CONVENTION = (
    "football-data.co.uk declares the C-suffixed columns as closing odds but "
    "supplies no timestamp; observed_at is set to the fixture kickoff instant"
)


@dataclass
class ParsedFile:
    """Everything one CSV yielded, plus everything that went wrong."""

    fixtures: list[CanonicalFixture] = field(default_factory=list)
    results: list[CanonicalResult] = field(default_factory=list)
    stats: list[CanonicalStats] = field(default_factory=list)
    odds: list[CanonicalOdds] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    team_names: set[str] = field(default_factory=set)
    rows_seen: int = 0


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _as_int(row: Mapping[str, str], key: str) -> int | None:
    """Blank stays NULL. A blank is 'not provided', never zero (§6 rule 12)."""
    raw = _clean(row.get(key))
    if raw is None:
        return None
    return int(raw)


def _as_decimal(row: Mapping[str, str], key: str) -> Decimal | None:
    raw = _clean(row.get(key))
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_kickoff(day: str, clock: str | None, tz_name: str) -> tuple[datetime, date]:
    """Provider date + UK-local time -> (kickoff_utc, local_date).

    Both published date formats are accepted: `dd/mm/yy` up to ~2017/18 and
    `dd/mm/yyyy` since. A two-digit year below 93 is a 2000s season, because
    the archive starts in 1993/94.
    """
    text = day.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        break
    else:
        raise ValueError(f"unrecognised date {day!r}")

    hour, minute = 0, 0
    if clock:
        pieces = clock.strip().split(":")
        hour, minute = int(pieces[0]), int(pieces[1])

    tz = zoneinfo.ZoneInfo(tz_name)
    # The provider's clock is LOCAL. Localising it is the whole point: storing
    # it as UTC unchanged would move every kickoff by the UK offset.
    local = parsed.replace(hour=hour, minute=minute, tzinfo=tz)
    return local.astimezone(UTC), local.date()


def _closing(
    fixture: FixtureRef,
    payload: PayloadRef,
    known_at: datetime,
    observed_at: datetime,
    book: BookmakerRef,
    market_type: str,
    selection: str,
    price: Decimal,
    line: Decimal | None = None,
) -> CanonicalOdds:
    """One closing observation. Explicit arguments, so mypy checks every field."""
    return CanonicalOdds(
        payload_ref=payload,
        known_at=known_at,
        fixture=fixture,
        bookmaker=book,
        period="ft",
        market_type=market_type,  # type: ignore[arg-type]
        line=line,
        selection=selection,
        side="back",
        price=price,
        is_available=True,
        price_kind="provider_closing",
        observed_at=observed_at,
        provider_at=None,
        observed_at_convention=CLOSING_CONVENTION,
    )


def _odds_for(
    row: Mapping[str, str],
    fixture: FixtureRef,
    payload: PayloadRef,
    known_at: datetime,
    observed_at: datetime,
) -> Iterator[CanonicalOdds]:
    """Only the documented CLOSING columns. Never Max/Avg, never pre-closing."""
    for book in BOOKMAKERS:
        ref = BookmakerRef(provider_key=book.slug, name=book.name, kind="bookmaker")

        if book.prefix_1x2:
            for suffix, selection in (("H", "home"), ("D", "draw"), ("A", "away")):
                price = _as_decimal(row, f"{book.prefix_1x2}C{suffix}")
                if price is not None:
                    yield _closing(
                        fixture, payload, known_at, observed_at, ref,
                        "1x2", selection, price,
                    )

        if book.prefix_ou:
            for column, selection in ((">2.5", "over"), ("<2.5", "under")):
                price = _as_decimal(row, f"{book.prefix_ou}C{column}")
                if price is not None:
                    yield _closing(
                        fixture, payload, known_at, observed_at, ref,
                        "over_under", selection, price,
                        line=Decimal(OVER_UNDER_LINE),
                    )

        if book.prefix_ah:
            # AHCh is the provider's closing handicap, stated from the HOME
            # team's perspective (notes.txt) - exactly G8's rule, so both
            # selections share one line and one market.
            line = _as_decimal(row, "AHCh")
            if line is not None:
                for suffix, selection in (("AHH", "home"), ("AHA", "away")):
                    price = _as_decimal(row, f"{book.prefix_ah}C{suffix}")
                    if price is not None:
                        yield _closing(
                            fixture, payload, known_at, observed_at, ref,
                            "asian_handicap", selection, price, line=line,
                        )


def parse_csv(
    content: bytes,
    *,
    division: str,
    season_folder: str,
    payload: PayloadRef,
    known_at: datetime,
) -> ParsedFile:
    """Decode and map one provider file. Never raises for a bad row."""
    meta = DIVISIONS.get(division)
    if meta is None:
        raise ValueError(f"division {division!r} is not in the project catalogue")

    label, start_year = season_label(season_folder)
    competition = CompetitionRef(provider_key=division, name=meta.competition_name,
                                 country_name=meta.country_name)
    season = SeasonRef(competition=competition, label=label, provider_key=season_folder)

    out = ParsedFile()
    # utf-8-sig strips a BOM if present; newline="" lets csv handle CRLF.
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))

    for number, row in enumerate(reader, start=2):
        home, away = _clean(row.get("HomeTeam")), _clean(row.get("AwayTeam"))
        if home is None and away is None:
            continue  # a trailing blank line, not a defect
        out.rows_seen += 1
        try:
            if home is None or away is None:
                raise ValueError("HomeTeam and AwayTeam are both required")
            ft_home, ft_away = _as_int(row, "FTHG"), _as_int(row, "FTAG")
            if ft_home is None or ft_away is None:
                raise ValueError("a row without a full-time score is not a result")
            day = _clean(row.get("Date"))
            if day is None:
                raise ValueError("Date is required")
            kickoff, local_day = parse_kickoff(
                day,
                _clean(row.get("Time")),
                meta.local_tz,
            )

            ref = FixtureRef(
                season=season,
                # The dataset has one occurrence of each ordered pairing, so a
                # single stage is sufficient and is verified before insertion.
                stage="regular",
                leg=1,
                replay_number=0,
                home_team=TeamRef(
                    provider_key=home,
                    name=home,
                    country_name=meta.country_name,
                ),
                away_team=TeamRef(
                    provider_key=away,
                    name=away,
                    country_name=meta.country_name,
                ),
            )
            out.team_names.update((home, away))

            out.fixtures.append(
                CanonicalFixture(
                    payload_ref=payload, known_at=known_at, ref=ref,
                    kickoff_utc=kickoff, local_date=local_day, local_tz=meta.local_tz,
                    # Only played matches appear in the archive; postponed and
                    # abandoned fixtures are simply absent (§16.1).
                    status="ft", venue_name=None, is_neutral_venue=False,
                )
            )
            out.results.append(
                CanonicalResult(
                    payload_ref=payload, known_at=known_at, fixture=ref,
                    result_source="played", is_trainable=True,
                    ft_home=ft_home, ft_away=ft_away,
                    ht_home=_as_int(row, "HTHG"), ht_away=_as_int(row, "HTAG"),
                )
            )

            values = {canon: _as_int(row, col) for col, canon in STAT_COLUMNS.items()}
            if any(v is not None for v in values.values()):
                out.stats.append(
                    CanonicalStats(
                        payload_ref=payload, known_at=known_at, fixture=ref, **values
                    )
                )

            out.odds.extend(_odds_for(row, ref, payload, known_at, kickoff))
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop 379 good ones
            out.problems.append(
                Problem(
                    kind=ProblemKind.SCHEMA_VALIDATION,
                    message=f"row {number}: {exc}",
                    context={"row": number, "home": home, "away": away},
                )
            )
    return out
