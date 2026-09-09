"""Project-maintained metadata for football-data.org (UPCOMING-FIXTURES.md).

A DIFFERENT ORGANISATION FROM football-data.co.uk, despite the near-identical
name. `.co.uk` publishes historical CSV archives of completed matches; `.org`
is a documented JSON API at api.football-data.org that carries CURRENT and
UPCOMING fixtures. Both are used, for different jobs, and confusing them would
be easy - hence this paragraph.

EVERYTHING IN THIS FILE IS OURS, NOT THE PROVIDER'S. The API says the
competition is code `PL`, id 2021; mapping that to our canonical
`england-premier-league` and to Europe/London is a decision we make and record
in one auditable place.

Verified against the live `/v4/competitions` endpoint, which needs no
credentials: competition id 2021, code PL, name "Premier League", area England
(id 2072, code ENG), plan TIER_ONE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

BASE_URL: Final[str] = "https://api.football-data.org"
#: `{code}` is the competition code, never an interpolated secret (§9.1).
MATCHES_ENDPOINT: Final[str] = "/v4/competitions/{code}/matches"
#: The header the API authenticates with. Verified live: no token gives 403,
#: a bad token gives 400 "Your API token is invalid."
API_KEY_HEADER: Final[str] = "X-Auth-Token"
#: Secrets live in the environment and nowhere else (§9.1).
API_KEY_ENV: Final[str] = "FOOTBALL_DATA_ORG_TOKEN"


@dataclass(frozen=True)
class CompetitionMeta:
    """Our canonical reading of one provider competition code."""

    code: str
    provider_id: int
    competition_slug: str
    country_slug: str
    #: The provider reports kickoff in UTC; the local zone is OURS, and it is
    #: what `fixture_schedule.local_date` is computed in.
    local_tz: str


COMPETITIONS: Final[dict[str, CompetitionMeta]] = {
    "PL": CompetitionMeta(
        code="PL",
        provider_id=2021,
        competition_slug="england-premier-league",
        country_slug="england",
        local_tz="Europe/London",
    ),
}

#: Provider status -> canonical `fixture_schedule.status`.
#:
#: TIMED and SCHEDULED both mean "not started"; the provider distinguishes a
#: confirmed kick-off time from a provisional one, and our schema does not.
#: Collapsing them loses nothing we model - the kickoff instant itself carries
#: that information.
#:
#: AWARDED IS DELIBERATELY ABSENT. It means a result imposed administratively,
#: and there is no canonical status for it: calling it `ft` would claim ninety
#: minutes were played, and `cancelled` would claim there is no result. A
#: fixture in that state is refused with a problem rather than mapped to
#: something convenient (§13.2 records the same gap for results).
STATUS_MAP: Final[dict[str, str]] = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "IN_PLAY": "live",
    "PAUSED": "live",
    "SUSPENDED": "suspended",
    "FINISHED": "ft",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
}

#: The only stage this phase accepts. The provider's enum is large
#: (GROUP_STAGE, SEMI_FINALS, PLAYOFFS...) and every other value implies a
#: knockout structure whose leg and replay semantics we are not establishing
#: here (§6). One league, one stage, refuse the rest.
SUPPORTED_STAGES: Final[frozenset[str]] = frozenset({"REGULAR_SEASON"})


def season_label(start_date: date) -> tuple[str, int]:
    """A provider season's start date -> our canonical label and start year.

    2026-08-21 -> ("2026/27", 2026). The shape matches the one the historical
    importer produces, so both providers describe the same season the same
    way and their fixtures land under one `seasons` row.
    """
    year = start_date.year
    return f"{year}/{str(year + 1)[-2:]}", year
