"""Provider team spellings for football-data.org (UPCOMING-FIXTURES.md §5).

DECLARED BY HAND, AND NOT VERIFIED AGAINST THE LIVE API. Every endpoint that
lists teams requires a token, so these strings are the provider's documented
naming convention rather than bytes I have seen. **That is safe only because
resolution fails closed**: a spelling that turns out to be wrong resolves to
nothing, the fixture is refused, and the name lands in `entity_review_queue`
for a human. A wrong guess here is loud, never a silent mis-mapping.

The `.org` API uses full club names with suffixes - "Arsenal FC",
"Wolverhampton Wanderers FC" - where the `.co.uk` archive uses short forms
like "Arsenal" and "Wolves". They are DIFFERENT STRINGS for the same clubs,
which is exactly why `team_aliases.source_id` scopes a spelling to the
provider that uses it (§10.3).

The canonical slugs are the ones the historical importer already created, so
both providers resolve to the same `teams` rows rather than forking the
registry.
"""

from __future__ import annotations

from typing import Final

#: canonical slug -> provider spellings that mean it.
#:
#: `name` and `shortName` are both accepted: the API publishes both, and each
#: is an exact string. Neither is a pattern and nothing here is fuzzy.
PL_TEAM_ALIASES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("arsenal", ("Arsenal FC", "Arsenal")),
    ("aston-villa", ("Aston Villa FC", "Aston Villa")),
    ("bournemouth", ("AFC Bournemouth", "Bournemouth")),
    ("brentford", ("Brentford FC", "Brentford")),
    ("brighton-hove-albion", ("Brighton & Hove Albion FC", "Brighton Hove")),
    ("burnley", ("Burnley FC", "Burnley")),
    ("chelsea", ("Chelsea FC", "Chelsea")),
    ("crystal-palace", ("Crystal Palace FC", "Crystal Palace")),
    ("everton", ("Everton FC", "Everton")),
    ("fulham", ("Fulham FC", "Fulham")),
    ("ipswich-town", ("Ipswich Town FC", "Ipswich Town")),
    ("leeds-united", ("Leeds United FC", "Leeds United")),
    ("leicester-city", ("Leicester City FC", "Leicester City")),
    ("liverpool", ("Liverpool FC", "Liverpool")),
    ("luton-town", ("Luton Town FC", "Luton Town")),
    ("manchester-city", ("Manchester City FC", "Man City")),
    ("manchester-united", ("Manchester United FC", "Man United")),
    ("newcastle-united", ("Newcastle United FC", "Newcastle")),
    ("norwich-city", ("Norwich City FC", "Norwich City")),
    ("nottingham-forest", ("Nottingham Forest FC", "Nottingham Forest")),
    ("sheffield-united", ("Sheffield United FC", "Sheffield Utd")),
    ("southampton", ("Southampton FC", "Southampton")),
    ("tottenham-hotspur", ("Tottenham Hotspur FC", "Tottenham")),
    ("watford", ("Watford FC", "Watford")),
    ("west-bromwich-albion", ("West Bromwich Albion FC", "West Bromwich Albion")),
    ("west-ham-united", ("West Ham United FC", "West Ham")),
    ("wolverhampton-wanderers", ("Wolverhampton Wanderers FC", "Wolverhampton")),
)
