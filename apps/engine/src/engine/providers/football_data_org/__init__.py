"""football-data.org: upcoming fixtures for the prediction pipeline.

A DIFFERENT ORGANISATION FROM football-data.co.uk. `.co.uk` supplies the
historical CSV archive of completed matches; `.org` is a documented JSON API
carrying current and upcoming fixtures. Both are used, for different jobs.
"""

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
    MATCHES_ENDPOINT,
    STATUS_MAP,
    season_label,
)
from engine.providers.football_data_org.parser import ParsedFixtures, parse_matches

__all__ = [
    "ADAPTER_VERSION",
    "API_KEY_ENV",
    "API_KEY_HEADER",
    "BASE_URL",
    "COMPETITIONS",
    "MATCHES_ENDPOINT",
    "PROVIDER_SLUG",
    "STATUS_MAP",
    "FootballDataOrgAdapter",
    "ParsedFixtures",
    "parse_matches",
    "season_label",
]
