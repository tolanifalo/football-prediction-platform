"""Project-maintained metadata for football-data.co.uk (PHASE-0-SPEC.md §16.2).

EVERYTHING IN THIS FILE IS OURS, NOT THE PROVIDER'S. The CSV says `Div = E0`
and nothing more: it carries no competition name, no country, no tier, no
season label and no bookmaker names. Mapping `E0` to the English Premier League
is a canonical decision we make and record here, so it is auditable in one
place rather than scattered through the parser.

Provider-derived, by contrast, is only: the division code, the season folder
name, team name strings, dates, times, scores, statistics and odds values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class DivisionMeta:
    """Our canonical reading of one provider division code."""

    division: str
    competition_slug: str
    competition_name: str
    country_slug: str
    country_name: str
    competition_type: str
    tier: int | None
    gender: str
    #: The provider publishes kickoff times in UK local time for every league
    #: (verified: Spanish slots appear one hour behind CET). For competitions
    #: outside the UK the local timezone is OURS, not the provider's - which is
    #: why it lives here rather than being inferred per row.
    local_tz: str


#: Deliberately one entry. P0-11 is a vertical slice, not a catalogue; adding a
#: division is one row here plus a `data_sources`-independent seed.
DIVISIONS: Final[dict[str, DivisionMeta]] = {
    "E0": DivisionMeta(
        division="E0",
        competition_slug="england-premier-league",
        competition_name="Premier League",
        country_slug="england",
        country_name="England",
        competition_type="league",
        tier=1,
        gender="men",
        local_tz="Europe/London",
    ),
}


@dataclass(frozen=True)
class BookmakerMeta:
    """One bookmaker, and the column prefixes the provider uses for it.

    The provider uses DIFFERENT prefixes for the same firm across markets -
    Pinnacle is `PS` for 1X2 but `P` for over/under and Asian handicap - so the
    prefixes are recorded per market rather than assumed uniform.
    """

    slug: str
    name: str
    #: Closing 1X2 columns are f"{prefix_1x2}C{H|D|A}".
    prefix_1x2: str | None = None
    #: Closing over/under columns are f"{prefix_ou}C>2.5" / f"{prefix_ou}C<2.5".
    prefix_ou: str | None = None
    #: Closing Asian handicap columns are f"{prefix_ah}CAHH" / f"{prefix_ah}CAHA".
    prefix_ah: str | None = None


#: Only the firms whose CLOSING columns actually appear in the first dataset.
#: Not every bookmaker football-data.co.uk has ever carried.
BOOKMAKERS: Final[tuple[BookmakerMeta, ...]] = (
    BookmakerMeta(
        "bet365",
        "Bet365",
        prefix_1x2="B365",
        prefix_ou="B365",
        prefix_ah="B365",
    ),
    BookmakerMeta("bwin", "bwin", prefix_1x2="BW"),
    BookmakerMeta("interwetten", "Interwetten", prefix_1x2="IW"),
    # `PS` for 1X2, `P` for the other two markets - the provider's own
    # inconsistency, recorded rather than worked around.
    BookmakerMeta(
        "pinnacle",
        "Pinnacle",
        prefix_1x2="PS",
        prefix_ou="P",
        prefix_ah="P",
    ),
    BookmakerMeta("william-hill", "William Hill", prefix_1x2="WH"),
    # notes.txt: "VC Bet ... (now BetVictor)". The column prefix stays VC.
    BookmakerMeta("betvictor", "BetVictor", prefix_1x2="VC"),
)

#: Column prefixes that are cross-bookmaker AGGREGATES, not a firm's price.
#: They have no `bookmaker_id`, and §14.2 says derived aggregates are not
#: stored - so they are excluded by name, loudly, rather than by omission.
EXCLUDED_AGGREGATE_PREFIXES: Final[frozenset[str]] = frozenset({"Max", "Avg", "Bb"})

#: The one over/under line football-data.co.uk publishes.
OVER_UNDER_LINE: Final[str] = "2.50"

#: Statistic columns, provider name -> canonical match_stats column.
#: Possession and xG are absent from this provider and stay NULL (§16.5).
STAT_COLUMNS: Final[dict[str, str]] = {
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_on_target",
    "AST": "away_shots_on_target",
    "HF": "home_fouls",
    "AF": "away_fouls",
    "HC": "home_corners",
    "AC": "away_corners",
    "HY": "home_yellow_cards",
    "AY": "away_yellow_cards",
    "HR": "home_red_cards",
    "AR": "away_red_cards",
}


def season_label(season_folder: str) -> tuple[str, int]:
    """'2324' -> ('2023/24', 2023).

    The provider's folder name is the only season identity it supplies; the
    canonical label shape is ours (§10.2).
    """
    if len(season_folder) != 4 or not season_folder.isdigit():
        raise ValueError(f"season folder must be four digits, got {season_folder!r}")
    start_two, end_two = season_folder[:2], season_folder[2:]
    # The archive begins in 1993/94, so a start below 93 is a 2000s season.
    century = 1900 if int(start_two) >= 93 else 2000
    start_year = century + int(start_two)
    return f"{start_year}/{end_two}", start_year
