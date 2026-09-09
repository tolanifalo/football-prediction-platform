"""football-data.co.uk: CSV downloads, one file per season and division."""

from engine.providers.football_data_couk.adapter import (
    ADAPTER_VERSION,
    BASE_URL,
    PROVIDER_SLUG,
    FootballDataCoUkAdapter,
)
from engine.providers.football_data_couk.parser import (
    CLOSING_CONVENTION,
    ParsedFile,
    parse_csv,
)

__all__ = [
    "ADAPTER_VERSION",
    "BASE_URL",
    "CLOSING_CONVENTION",
    "PROVIDER_SLUG",
    "FootballDataCoUkAdapter",
    "ParsedFile",
    "parse_csv",
]
