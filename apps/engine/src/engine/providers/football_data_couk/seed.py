"""The minimum canonical seed for E0 2023/24 (PHASE-0-SPEC.md §16.4).

TWENTY TEAMS, DECLARED BY HAND. This is not identity resolution: it is the
operator stating, explicitly and reviewably, which canonical club each provider
string means. P0-12 owns matching; P0-11 only resolves an EXACT alias.

"Ath Madrid" and "Ath Bilbao" are why: a fuzzy matcher would merge them, and
the specification's rule is that ambiguous resolution must fail rather than
choose (§10.3). Nothing here is guessed.
"""

from __future__ import annotations

from typing import Final

#: canonical slug -> (display name, provider strings that mean it)
#: Provider strings are taken verbatim from the real E0 2023/24 file.
E0_2324_TEAMS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("arsenal", "Arsenal", ("Arsenal",)),
    ("aston-villa", "Aston Villa", ("Aston Villa",)),
    ("bournemouth", "AFC Bournemouth", ("Bournemouth",)),
    ("brentford", "Brentford", ("Brentford",)),
    ("brighton-hove-albion", "Brighton & Hove Albion", ("Brighton",)),
    ("burnley", "Burnley", ("Burnley",)),
    ("chelsea", "Chelsea", ("Chelsea",)),
    ("crystal-palace", "Crystal Palace", ("Crystal Palace",)),
    ("everton", "Everton", ("Everton",)),
    ("fulham", "Fulham", ("Fulham",)),
    ("liverpool", "Liverpool", ("Liverpool",)),
    ("luton-town", "Luton Town", ("Luton",)),
    ("manchester-city", "Manchester City", ("Man City",)),
    ("manchester-united", "Manchester United", ("Man United",)),
    ("newcastle-united", "Newcastle United", ("Newcastle",)),
    ("nottingham-forest", "Nottingham Forest", ("Nott'm Forest",)),
    ("sheffield-united", "Sheffield United", ("Sheffield United",)),
    ("tottenham-hotspur", "Tottenham Hotspur", ("Tottenham",)),
    ("west-ham-united", "West Ham United", ("West Ham",)),
    ("wolverhampton-wanderers", "Wolverhampton Wanderers", ("Wolves",)),
)

#: Season dates, taken from the dataset itself rather than invented: the
#: earliest and latest match dates in the file.
E0_2324_SEASON_SLUG: Final[str] = "england-premier-league-2023-24"
