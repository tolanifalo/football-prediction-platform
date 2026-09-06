"""Database connection for local development.

P0-02 scope: prove a Python process can reach Postgres and round-trip a query.
No schema, no SQLAlchemy - those arrive with P0-03 and the ingest work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg

LOCAL_DEFAULT_URL = "postgresql://fpp:fpp_local_dev@localhost:5433/fpp"


def database_url() -> str:
    """Resolve the connection string.

    DATABASE_URL wins so CI and containers can point elsewhere; the fallback
    matches docker-compose.yml so a fresh clone works with no .env file.
    """
    return os.environ.get("DATABASE_URL") or LOCAL_DEFAULT_URL


@dataclass(frozen=True)
class ConnectionInfo:
    database: str
    user: str
    server_version: str
    timezone: str
    echo: str


def check_connection(url: str | None = None) -> ConnectionInfo:
    """Connect, round-trip a value, and report what answered."""
    token = "roundtrip-python"
    with psycopg.connect(url or database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database(),
                   current_user,
                   current_setting('server_version'),
                   current_setting('TimeZone'),
                   %s::text
            """,
            (token,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError("connection check returned no rows")

    database, user, server_version, timezone, echo = row
    if echo != token:
        raise RuntimeError(f"round-trip mismatch: sent {token}, received {echo}")

    return ConnectionInfo(
        database=database,
        user=user,
        server_version=server_version,
        timezone=timezone,
        echo=echo,
    )


def main() -> None:
    info = check_connection()
    print("postgres round-trip ok")
    print(f"  server    {info.server_version}")
    print(f"  database  {info.database}")
    print(f"  user      {info.user}")
    print(f"  timezone  {info.timezone}")


if __name__ == "__main__":
    main()
