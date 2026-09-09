"""The production prediction job (PREDICTIONS.md §4).

    uv run python -m engine.jobs.generate_predictions
    uv run python -m engine.jobs.generate_predictions --data-cutoff 2024-01-15T00:00:00Z

Follows the established job shape: open a `job_runs` row, do the work, close
it with counters. One fit per distinct cutoff, one transaction for the batch.

TWO INVOCATIONS, ONE CODE PATH:

  PRODUCTION   no `--data-cutoff`. The cutoff is now, and eligible fixtures
               are the ones that have not kicked off. This is the mode the
               website will run.
  HISTORICAL   an explicit `--data-cutoff`. Eligible fixtures are those
               kicking off at or after it, and the model is fitted on what was
               known before it. This is how a past prediction is reproduced,
               and it is the mode the corpus can currently exercise.

"NOW" NEVER LEAKS INTO THE FOOTBALL FEATURES. The wall clock is used for
exactly two things - the default cutoff and `known_at` - and both are passed
in explicitly. The estimator is handed observations filtered by `data_cutoff`
and has no other route to the present.

ONE FIT PER CUTOFF, NOT ONE PER FIXTURE. Every fixture in a run shares the
same `data_cutoff`, so they share the same training set and therefore the same
model. Fitting once is not an approximation of fitting per fixture; it is the
same computation performed once.

NO ODDS ANYWHERE. The job reads fixtures, schedules and results, and writes
predictions.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from engine.db import database_url
from engine.ingestion.postgres import (
    PostgresJobRunStore,
    PostgresPredictionStore,
    PredictionOutcome,
)
from engine.ingestion.runs import RunIdentity, RunStatus
from engine.ingestion.stats import IngestionStats
from engine.model.artifact import artifact_from
from engine.model.fit import InsufficientHistory, fit_poisson, observations_before
from engine.model.predict import predict_fixture
from engine.model.profiles import DEFAULT_PROFILE, PROFILES, profile
from engine.model.repository import (
    EligibleFixture,
    load_corpus,
    load_eligible_fixtures,
)

JOB_NAME = "generate_predictions"
COMPETITION = "england-premier-league"
#: The corpus the model may train on. One league, deliberately (§7).
SEASONS: tuple[str, ...] = (
    "2019/20", "2020/21", "2021/22", "2022/23", "2023/24", "2024/25",
)
#: Below this many training matches the model declines rather than guessing.
MIN_TRAINING_MATCHES = 30


@dataclass
class PredictionReport:
    job_run_id: int
    status: RunStatus
    data_cutoff: datetime
    known_at: datetime
    profile_name: str
    eligible: int = 0
    created: int = 0
    unchanged: int = 0
    revised: int = 0
    cold_start: int = 0
    skipped: int = 0
    training_matches: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def persisted(self) -> int:
        return self.created + self.revised


def generate(
    conn: psycopg.Connection[Any],
    *,
    data_cutoff: datetime,
    known_at: datetime,
    profile_name: str = DEFAULT_PROFILE.name,
    competition_slug: str = COMPETITION,
    seasons: tuple[str, ...] = SEASONS,
) -> PredictionReport:
    """Fit once at the cutoff, predict every eligible fixture, persist."""
    if data_cutoff > known_at:
        raise ValueError(
            "data_cutoff is after known_at: a prediction recorded now cannot "
            "use football knowledge from the future"
        )
    chosen = profile(profile_name)
    scope_key = f"{competition_slug}:{data_cutoff.isoformat()}"

    runs = PostgresJobRunStore(conn)
    source_id = _model_source_id(conn)
    identity = RunIdentity(
        job_name=JOB_NAME,
        scope_key=scope_key,
        run_date=known_at.date(),
        attempt=runs.next_attempt(
            RunIdentity(JOB_NAME, scope_key, known_at.date())
        ),
    )
    job_run_id = runs.open(
        identity,
        source_id=source_id,
        adapter_version=chosen.fit.profile or chosen.name,
        params={
            "competition": competition_slug,
            "data_cutoff": data_cutoff.isoformat(),
            "profile": chosen.name,
        },
    )
    conn.commit()

    stats = IngestionStats()
    report = PredictionReport(
        job_run_id=job_run_id,
        status=RunStatus.RUNNING,
        data_cutoff=data_cutoff,
        known_at=known_at,
        profile_name=chosen.name,
    )

    # -- what the model is allowed to know ---------------------------------
    corpus = load_corpus(
        conn,
        competition_slug=competition_slug,
        season_labels=seasons,
        as_of=known_at,
    )
    training = observations_before(corpus, data_cutoff)
    report.training_matches = len(training)

    fixtures: list[EligibleFixture] = load_eligible_fixtures(
        conn,
        competition_slug=competition_slug,
        data_cutoff=data_cutoff,
        as_of=known_at,
        season_labels=seasons,
    )
    report.eligible = len(fixtures)

    if len(training) < MIN_TRAINING_MATCHES:
        report.status = RunStatus.FAILED
        report.problems.append(
            f"only {len(training)} training matches before the cutoff; "
            f"{MIN_TRAINING_MATCHES} required"
        )
        runs.close(job_run_id, report.status, stats, error=report.problems[0])
        conn.commit()
        return report

    if not fixtures:
        # Not a failure: it is the honest answer when nothing is unplayed.
        report.status = RunStatus.OK
        runs.close(job_run_id, report.status, stats)
        conn.commit()
        return report

    try:
        model = fit_poisson(
            training,
            data_cutoff=data_cutoff,
            as_of=known_at,
            scope=f"{competition_slug}:{seasons[0]}..{seasons[-1]}",
            config=chosen.fit,
        )
    except InsufficientHistory as exc:
        report.status = RunStatus.FAILED
        report.problems.append(str(exc))
        runs.close(job_run_id, report.status, stats, error=str(exc))
        conn.commit()
        return report

    stats.rows_parsed = len(fixtures)
    store = PostgresPredictionStore(conn, job_run_id=job_run_id)

    # One transaction for the batch: a partially written generation would be
    # indistinguishable from a complete one that predicted fewer fixtures.
    try:
        for fixture in fixtures:
            prediction = predict_fixture(
                model, fixture.home_team, fixture.away_team
            )
            artifact = artifact_from(
                prediction,
                model,
                fixture_id=fixture.fixture_id,
                known_at=known_at,
            )
            outcome = store.store(artifact)
            if outcome is PredictionOutcome.CREATED:
                report.created += 1
            elif outcome is PredictionOutcome.REVISED:
                report.revised += 1
            else:
                report.unchanged += 1
            if artifact.is_cold_start:
                report.cold_start += 1
            stats.rows_accepted += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - the batch is all or nothing
        conn.rollback()
        report.status = RunStatus.FAILED
        report.problems.append(f"{type(exc).__name__}: {exc}")
        runs.close(job_run_id, report.status, stats, error=report.problems[0])
        conn.commit()
        return report

    report.status = RunStatus.OK
    runs.close(job_run_id, report.status, stats)
    conn.commit()
    return report


def _model_source_id(conn: psycopg.Connection[Any]) -> Any:
    """`job_runs.source_id` is NOT NULL, and a prediction has no provider.

    The run is attributed to the source whose data it consumed. That is
    honest - the predictions are derived from that provider's results - and it
    avoids inventing a synthetic "the model" data source, which would be a
    row claiming to be a provider that never supplied anything.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM data_sources ORDER BY slug LIMIT 1")
        row = cur.fetchone()
    if row is None:
        raise SystemExit("no data_sources row; import the corpus first")
    return row[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate and persist production predictions."
    )
    parser.add_argument("--competition", default=COMPETITION)
    parser.add_argument(
        "--data-cutoff",
        default=None,
        help="ISO-8601 instant. Omit for a production run, which uses now. "
             "Supply one to reproduce predictions as they stood historically.",
    )
    parser.add_argument(
        "--profile", default=DEFAULT_PROFILE.name, choices=sorted(PROFILES)
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    known_at = datetime.now(UTC)
    if args.data_cutoff:
        cutoff = datetime.fromisoformat(args.data_cutoff)
        if cutoff.tzinfo is None:
            print("--data-cutoff needs an explicit UTC offset", file=sys.stderr)
            return 1
    else:
        cutoff = known_at

    with psycopg.connect(args.database_url or database_url()) as conn:
        report = generate(
            conn,
            data_cutoff=cutoff,
            known_at=known_at,
            profile_name=args.profile,
            competition_slug=args.competition,
        )

    print(f"job_run {report.job_run_id}: {report.status}")
    print(f"  profile            {report.profile_name}")
    print(f"  data_cutoff        {report.data_cutoff.isoformat()}")
    print(f"  known_at           {report.known_at.isoformat()}")
    print(f"  training matches   {report.training_matches}")
    print(f"  eligible fixtures  {report.eligible}")
    print(f"  created            {report.created}")
    print(f"  unchanged          {report.unchanged}")
    print(f"  revised            {report.revised}")
    print(f"  cold start         {report.cold_start}")
    for problem in report.problems[:10]:
        print(f"  ! {problem}")
    if report.eligible == 0 and report.status is RunStatus.OK:
        print(
            "  note: no fixture kicks off at or after the cutoff. "
            "football-data.co.uk publishes only completed matches "
            "(PHASE-0-SPEC.md §16.8), so a production run against this "
            "corpus has nothing to predict; use --data-cutoff to reproduce "
            "historical predictions.",
        )
    return 0 if report.status is RunStatus.OK else 1


if __name__ == "__main__":
    sys.exit(main())
