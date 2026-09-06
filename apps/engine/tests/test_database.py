"""P0-02 acceptance: a Python process connects and round-trips a query.

Marked `db` and excluded from the default pytest run, so `pnpm verify` stays
green on a machine with no container running. CI starts a Postgres service and
runs these explicitly with `-m db`.
"""

import pytest

from engine.db import LOCAL_DEFAULT_URL, check_connection, database_url


@pytest.mark.db
def test_round_trips_a_query() -> None:
    info = check_connection()
    assert info.echo == "roundtrip-python"


@pytest.mark.db
def test_connects_to_the_configured_database() -> None:
    info = check_connection()
    assert info.database == "fpp"
    assert info.user == "fpp"


@pytest.mark.db
def test_server_is_postgres_17() -> None:
    """Local must match the Supabase platform default; see docker-compose.yml."""
    info = check_connection()
    assert info.server_version.startswith("17."), info.server_version


@pytest.mark.db
def test_server_timezone_is_utc() -> None:
    """CLAUDE.md: all timestamps are timestamptz stored UTC."""
    info = check_connection()
    assert info.timezone == "UTC", info.timezone


def test_database_url_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")
    assert database_url() == "postgresql://other/db"
    monkeypatch.delenv("DATABASE_URL")
    assert database_url() == LOCAL_DEFAULT_URL
