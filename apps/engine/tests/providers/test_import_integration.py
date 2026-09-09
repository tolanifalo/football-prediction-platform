"""The P0-11 acceptance test: provider CSV -> canonical Postgres, twice.

Marked `db`: it needs the real 16-migration schema. Still no live provider
call - the bytes come from the checked-in fixture through `httpx.MockTransport`.

The second import is the point. It must leave canonical row counts unchanged
while `raw_payloads` grows and `raw_payload_bodies` does not.
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import psycopg
import pytest

from engine.db import database_url
from engine.ingestion.runs import RunStatus
from engine.ingestion.transport import HttpxTransport
from engine.jobs.import_football_data import run_import
from engine.providers.football_data_couk.seed import E0_TEAMS

SAMPLE = pathlib.Path(__file__).parent / "fixtures" / "E0_2324_sample.csv"

COUNT_SQL = {
    "countries": "SELECT count(*) FROM countries",
    "competitions": "SELECT count(*) FROM competitions",
    "seasons": "SELECT count(*) FROM seasons",
    "teams": "SELECT count(*) FROM teams",
    "team_aliases": "SELECT count(*) FROM team_aliases",
    "bookmakers": "SELECT count(*) FROM bookmakers",
    "fixtures": "SELECT count(*) FROM fixtures",
    "fixture_schedule": "SELECT count(*) FROM fixture_schedule",
    "match_results": "SELECT count(*) FROM match_results",
    "match_stats": "SELECT count(*) FROM match_stats",
    "odds_series": "SELECT count(*) FROM odds_series",
    "odds_ticks": "SELECT count(*) FROM odds_ticks",
    "raw_payloads": "SELECT count(*) FROM raw_payloads",
    "raw_payload_bodies": "SELECT count(*) FROM raw_payload_bodies",
    "job_runs": "SELECT count(*) FROM job_runs",
}


def counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for name, sql in COUNT_SQL.items():
            cur.execute(sql)
            row = cur.fetchone()
            out[name] = int(row[0]) if row else 0
    return out


def transport_for(body: bytes) -> HttpxTransport:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body)),
        base_url="https://football-data.co.uk",
    )
    return HttpxTransport("https://football-data.co.uk", client=client)


@pytest.fixture
def clean_db() -> Any:
    """A connection whose canonical tables are emptied, then restored."""
    conn = psycopg.connect(database_url())
    wipe = [
        # external_ids and the review queue come FIRST. The P0-06 canonical-side
        # trigger refuses to delete a team while a mapping references it, so a
        # database carrying P0-12 identity rows cannot be cleaned in any other
        # order - and omitting them entirely makes teardown fail outright.
        "external_ids", "entity_review_queue",
        "odds_ticks", "odds_series", "bookmakers", "match_stats", "match_results",
        "fixture_schedule", "fixtures", "team_aliases", "team_names", "teams",
        "seasons", "competition_names", "competitions", "venues", "countries",
        "raw_payloads", "raw_payload_bodies", "job_runs", "data_sources",
    ]
    with conn.cursor() as cur:
        for table in wipe:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        for table in wipe:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


@pytest.mark.db
class TestVerticalSlice:
    def test_imports_and_is_idempotent(self, clean_db: psycopg.Connection[Any]) -> None:
        body = SAMPLE.read_bytes()

        first = run_import(
            clean_db, division="E0", season="2324", transport=transport_for(body)
        )
        after_first = counts(clean_db)

        assert first.status is RunStatus.OK, "a clean import must report ok"
        assert first.fixtures == 12
        assert after_first["fixtures"] == 12
        assert after_first["fixture_schedule"] == 12
        assert after_first["match_results"] == 12
        assert after_first["match_stats"] == 12
        assert after_first["countries"] == 1
        assert after_first["competitions"] == 1
        assert after_first["seasons"] == 1
        # The whole seeded squad is created regardless of how many of them
        # appear in this sample: `teams` is a registry, not season-scoped.
        assert after_first["teams"] == len(E0_TEAMS)
        assert after_first["bookmakers"] == 6
        assert after_first["odds_series"] > 0
        assert after_first["odds_ticks"] == after_first["odds_series"]
        assert after_first["raw_payloads"] == 1
        assert after_first["raw_payload_bodies"] == 1

        # ---- the same file again --------------------------------------
        second = run_import(
            clean_db, division="E0", season="2324", transport=transport_for(body)
        )
        after_second = counts(clean_db)

        assert second.status is RunStatus.OK
        canonical = [
            "countries", "competitions", "seasons", "teams", "team_aliases",
            "bookmakers", "fixtures", "fixture_schedule", "match_results",
            "match_stats", "odds_series", "odds_ticks",
        ]
        for table in canonical:
            assert after_second[table] == after_first[table], (
                f"{table} changed on rerun: "
                f"{after_first[table]} -> {after_second[table]}"
            )
        # Evidence, by contrast, MUST grow: a re-fetch is a new observation.
        assert after_second["raw_payloads"] == after_first["raw_payloads"] + 1
        # ...but identical bytes are stored once.
        assert after_second["raw_payload_bodies"] == after_first["raw_payload_bodies"]
        assert after_second["job_runs"] == after_first["job_runs"] + 1

        # The run record must show the HTTP work it actually did. These
        # counters read zero for a run that fetched 5,928 bytes until the
        # job started calling `record_response` (§16.6).
        with clean_db.cursor() as cur:
            cur.execute("SELECT stats FROM job_runs ORDER BY id")
            run_stats = [r[0] for r in cur.fetchall()]
        for st in run_stats:
            assert st["requests"] == 1
            assert st["bytes"] == len(body)
            assert st["http_status_counts"] == {"200": 1}
            assert st["timeouts"] == 0

        # Nothing was superseded, because nothing actually differed.
        assert second.schedules_written == 0
        assert second.results_written == 0
        assert second.stats_written == 0
        assert second.ticks_written == 0

    def test_no_duplicate_current_rows_after_two_imports(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        body = SAMPLE.read_bytes()
        for _ in range(2):
            run_import(
                clean_db,
                division="E0",
                season="2324",
                transport=transport_for(body),
            )

        with clean_db.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM (
                  SELECT fixture_id FROM fixture_schedule WHERE superseded_at IS NULL
                   GROUP BY fixture_id HAVING count(*) > 1) d
                """
            )
            assert cur.fetchone()[0] == 0  # type: ignore[index]
            cur.execute(
                """
                SELECT count(*) FROM (
                  SELECT fixture_id, source_id FROM match_results
                   WHERE superseded_at IS NULL
                   GROUP BY fixture_id, source_id HAVING count(*) > 1) d
                """
            )
            assert cur.fetchone()[0] == 0  # type: ignore[index]
            cur.execute(
                """
                SELECT count(*) FROM (
                  SELECT series_id, source_id, observed_at, price_kind FROM odds_ticks
                   WHERE superseded_at IS NULL
                   GROUP BY series_id, source_id, observed_at, price_kind
                  HAVING count(*) > 1) d
                """
            )
            assert cur.fetchone()[0] == 0  # type: ignore[index]

    def test_persisted_facts_match_the_source(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        run_import(
            clean_db, division="E0", season="2324",
            transport=transport_for(SAMPLE.read_bytes()),
        )
        with clean_db.cursor() as cur:
            # Burnley 0-3 Man City, 11/08/2023 20:00 UK == 19:00Z.
            cur.execute(
                """
                SELECT r.ft_home, r.ft_away, r.ht_home, r.ht_away,
                       s.kickoff_utc, s.local_date, s.local_tz, s.status,
                       st.home_shots, st.away_shots, st.home_red_cards,
                       st.home_possession, st.home_xg
                  FROM fixtures f
                  JOIN team_names hn ON hn.team_id = f.home_team_id
                   AND hn.valid_to IS NULL
                  JOIN match_results r ON r.fixture_id = f.id
                   AND r.superseded_at IS NULL
                  JOIN fixture_schedule s ON s.fixture_id = f.id
                   AND s.superseded_at IS NULL
                  JOIN match_stats st ON st.fixture_id = f.id
                   AND st.superseded_at IS NULL
                 WHERE hn.name = 'Burnley'
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert (row[0], row[1], row[2], row[3]) == (0, 3, 0, 2)
            assert row[4].isoformat().startswith("2023-08-11T19:00")
            assert str(row[5]) == "2023-08-11"
            assert row[6] == "Europe/London" and row[7] == "ft"
            assert (row[8], row[9], row[10]) == (6, 17, 1)
            # The provider supplies neither, so both stay NULL (§16.5).
            assert row[11] is None and row[12] is None

    def test_closing_odds_are_stored_as_provider_closing(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        run_import(
            clean_db, division="E0", season="2324",
            transport=transport_for(SAMPLE.read_bytes()),
        )
        with clean_db.cursor() as cur:
            cur.execute("SELECT DISTINCT price_kind FROM odds_ticks")
            assert [r[0] for r in cur.fetchall()] == ["provider_closing"]
            cur.execute("SELECT DISTINCT side FROM odds_series")
            assert [r[0] for r in cur.fetchall()] == ["back"]
            cur.execute("SELECT DISTINCT kind FROM bookmakers")
            assert [r[0] for r in cur.fetchall()] == ["bookmaker"]
            cur.execute(
                "SELECT count(*) FROM bookmakers WHERE commission_rate IS NOT NULL"
            )
            assert cur.fetchone()[0] == 0  # type: ignore[index]
            # Bet365 closing home price for Burnley v Man City is 9, not the
            # pre-closing 8.
            cur.execute(
                """
                SELECT t.price FROM odds_ticks t
                  JOIN odds_series os ON os.id = t.series_id
                  JOIN bookmakers b ON b.id = os.bookmaker_id
                  JOIN fixtures f ON f.id = os.fixture_id
                  JOIN team_names hn ON hn.team_id = f.home_team_id
                   AND hn.valid_to IS NULL
                 WHERE hn.name = 'Burnley' AND b.slug = 'bet365'
                   AND os.market_type = '1x2' AND os.selection = 'home'
                """
            )
            assert float(cur.fetchone()[0]) == 9.0  # type: ignore[index]

    def test_provenance_links_every_fact_to_the_archived_body(
        self, clean_db: psycopg.Connection[Any]
    ) -> None:
        run_import(
            clean_db, division="E0", season="2324",
            transport=transport_for(SAMPLE.read_bytes()),
        )
        with clean_db.cursor() as cur:
            for table in (
                "fixture_schedule",
                "match_results",
                "match_stats",
                "odds_ticks",
            ):
                cur.execute(
                    f"""
                    SELECT count(*) FROM {table} t
                     WHERE NOT EXISTS (
                       SELECT 1 FROM raw_payload_bodies b
                        WHERE b.id = t.raw_payload_body_id)
                    """
                )
                assert cur.fetchone()[0] == 0, f"{table} cites a missing body"  # type: ignore[index]
            # The archived bytes are byte-identical to what the provider sent.
            cur.execute("SELECT body FROM raw_payload_bodies")
            stored = cur.fetchone()
            assert stored is not None
            assert bytes(stored[0]) == SAMPLE.read_bytes()
