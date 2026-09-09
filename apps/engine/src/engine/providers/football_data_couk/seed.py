"""The canonical seed for English top-flight clubs (PHASE-0-SPEC.md §16.4).

DECLARED BY HAND. This is not identity resolution: it is the operator stating,
explicitly and reviewably, which canonical club each provider string means.
P0-12 owns matching; the resolver only ever accepts an EXACT alias.

"Ath Madrid" and "Ath Bilbao" are why: a fuzzy matcher would merge them, and
the specification's rule is that ambiguous resolution must fail rather than
choose (§10.3). Nothing here is guessed.

TWENTY-SEVEN CLUBS, being every side that appeared in E0 across 2019/20 to
2024/25 - twenty per season, with promotion and relegation churning seven of
them. `teams` is a REGISTRY, NOT SEASON-SCOPED (§10.5), so seeding the whole
set once is correct: a club with no fixtures in a given season simply has
none, and nothing about its identity changes when it goes down and comes back.
"""

from __future__ import annotations

from typing import Final

#: canonical slug -> (display name, provider strings that mean it)
#: Provider strings are taken verbatim from the real E0 files, and each one is
#: exactly the spelling football-data.co.uk uses - never a guess at one.
E0_TEAMS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
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
    ("ipswich-town", "Ipswich Town", ("Ipswich",)),
    ("leeds-united", "Leeds United", ("Leeds",)),
    ("leicester-city", "Leicester City", ("Leicester",)),
    ("liverpool", "Liverpool", ("Liverpool",)),
    ("luton-town", "Luton Town", ("Luton",)),
    ("manchester-city", "Manchester City", ("Man City",)),
    ("manchester-united", "Manchester United", ("Man United",)),
    ("newcastle-united", "Newcastle United", ("Newcastle",)),
    ("norwich-city", "Norwich City", ("Norwich",)),
    ("nottingham-forest", "Nottingham Forest", ("Nott'm Forest",)),
    ("sheffield-united", "Sheffield United", ("Sheffield United",)),
    ("southampton", "Southampton", ("Southampton",)),
    ("tottenham-hotspur", "Tottenham Hotspur", ("Tottenham",)),
    ("watford", "Watford", ("Watford",)),
    ("west-bromwich-albion", "West Bromwich Albion", ("West Brom",)),
    ("west-ham-united", "West Ham United", ("West Ham",)),
    ("wolverhampton-wanderers", "Wolverhampton Wanderers", ("Wolves",)),
)
