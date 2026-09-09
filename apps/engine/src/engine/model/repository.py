"""The only SQL in the model layer (MODEL-BASELINE.md §5).

NO MATHEMATICS HERE, AND NO MODEL LOGIC IN SQL. This module does one thing:
canonical rows -> `MatchObservation`. Everything downstream is pure Python that
never sees a connection, which is what lets the estimator, the predictor and
the backtester be tested without PostgreSQL.

It reads `fixtures`, `fixture_schedule`, `match_results` and `team_names`. It
writes nothing.

TWO TIME AXES, KEPT SEPARATE (§2.1). They are not interchangeable and
collapsing them is the leakage bug this whole design exists to prevent:

  TRANSACTION TIME (`as_of`) - which REVISION of a result we believe. Read
  through `match_results_as_of`, the P0-06 wrapper, so the visibility
  predicate is never restated here (§11.1).

  VALID TIME (`data_cutoff`) - what had actually HAPPENED. Applied by the
  caller against `kickoff`, which this module attaches to every observation.

The distinction is not academic on this data. Every 2023/24 result was
imported in 2026, so every `known_at` is later than every kickoff: a
transaction-time filter at a 2024 cutoff returns nothing at all. The cutoff a
backtest walks is valid time.

VALID TIME COMES FROM `fixture_schedule.kickoff_utc`. `match_results.
occurred_at` is the natural home for it and is NULL on all 380 imported rows -
P0-11 never populated it - so the schedule's kickoff is the authoritative
answer to "when did this happen", with `occurred_at` preferred when a future
provider does supply it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg

from engine.model.fit import MatchObservation

#: Trainable, current results joined to their current schedule and the current
#: display name of each side. `is_trainable` is the P0-08 flag for a result
#: that describes ninety minutes actually played (§13.2); an abandoned or
#: awarded match must never train a scoring model.
_OBSERVATIONS = """
SELECT hn.name AS home_team,
       an.name AS away_team,
       r.ft_home,
       r.ft_away,
       coalesce(r.occurred_at, s.kickoff_utc) AS occurred_at,
       se.label AS season
  FROM match_results_as_of(%(as_of)s) r
  JOIN fixtures f ON f.id = r.fixture_id
  JOIN fixture_schedule_as_of(%(as_of)s) s ON s.fixture_id = f.id
  JOIN seasons se ON se.id = f.season_id
  JOIN competitions c ON c.id = se.competition_id
  JOIN team_names hn ON hn.team_id = f.home_team_id AND hn.valid_to IS NULL
  JOIN team_names an ON an.team_id = f.away_team_id AND an.valid_to IS NULL
 WHERE r.is_trainable
   AND c.slug = %(competition)s
   AND se.label = ANY(%(seasons)s)
   AND coalesce(r.occurred_at, s.kickoff_utc) IS NOT NULL
 ORDER BY occurred_at, hn.name, an.name
"""


def load_corpus(
    conn: psycopg.Connection[Any],
    *,
    competition_slug: str,
    season_labels: Sequence[str],
    as_of: datetime,
) -> list[MatchObservation]:
    """Every trainable result across one or more seasons, as of one instant.

    Returned in kickoff order across the WHOLE corpus, not per season: a
    walk-forward evaluation crosses season boundaries, and a season boundary
    is a reporting artefact rather than a modelling one.

    `as_of` selects the REVISION, not the training window. Filtering to a
    training window is `observations_before`, and keeping the two apart is
    deliberate: a caller that wants a 2024 cutoff wants matches played before
    then, believed as we believe them today.
    """
    with conn.cursor() as cur:
        cur.execute(
            _OBSERVATIONS,
            {
                "as_of": as_of,
                "competition": competition_slug,
                "seasons": list(season_labels),
            },
        )
        rows: Sequence[tuple[Any, ...]] = cur.fetchall()

    return [
        MatchObservation(
            home_team=str(row[0]),
            away_team=str(row[1]),
            home_goals=int(row[2]),
            away_goals=int(row[3]),
            kickoff=row[4],
            season=str(row[5]),
        )
        for row in rows
    ]


def load_observations(
    conn: psycopg.Connection[Any],
    *,
    competition_slug: str,
    season_label: str,
    as_of: datetime,
) -> list[MatchObservation]:
    """One season. The single-season form the baseline backtest uses."""
    return load_corpus(
        conn,
        competition_slug=competition_slug,
        season_labels=[season_label],
        as_of=as_of,
    )
