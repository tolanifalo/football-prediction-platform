"""Run the walk-forward backtest against imported canonical data (§18.6).

    uv run python -m engine.jobs.backtest_poisson \
        --competition england-premier-league --season 2023/24

READS ONLY. It opens no transaction, writes no row and creates no table: the
prediction engine consumes canonical facts and does not modify them.

There is no model registry and no predictions table. A fit is a Python object
with deterministic serialisable metadata, which is all this phase needs; a
registry with no second model to hold would be a schema invented ahead of its
requirement.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime

import psycopg

from engine.db import database_url
from engine.model.backtest import BacktestConfig, BacktestReport, walk_forward
from engine.model.profiles import DEFAULT_PROFILE, PROFILES, profile
from engine.model.repository import load_observations


def _print_report(report: BacktestReport, scope: str) -> None:
    print(f"scope                {scope}")
    print(f"predictions          {report.predictions}")
    print(f"skipped              {report.skipped}  (insufficient history)")
    print(f"fits                 {report.fits}")
    first = report.first_prediction
    last = report.last_prediction
    print(f"first prediction     {first.isoformat() if first else '-'}")
    print(f"last prediction      {last.isoformat() if last else '-'}")
    print()
    print("                     model    class-freq   league-avg   uniform")
    print(
        f"1X2 log loss         {report.log_loss:.4f}"
        f"      {report.baseline_frequency_log_loss:.4f}"
        f"       {report.baseline_league_log_loss:.4f}    1.0986"
    )
    print(
        f"1X2 Brier            {report.brier:.4f}"
        f"      {report.baseline_frequency_brier:.4f}"
        f"       {report.baseline_league_brier:.4f}    0.6667"
    )
    print()
    print(
        f"mean predicted goals home {report.mean_predicted_home_goals:.3f}"
        f"  away {report.mean_predicted_away_goals:.3f}"
    )
    print(
        f"mean actual    goals home {report.mean_actual_home_goals:.3f}"
        f"  away {report.mean_actual_away_goals:.3f}"
    )
    print()
    print("calibration          predicted   observed      bias")
    for row in report.calibration:
        print(
            f"  {row.outcome:<18} {row.mean_predicted:.4f}"
            f"      {row.observed_frequency:.4f}   {row.bias:+.4f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward Poisson backtest.")
    parser.add_argument("--competition", default="england-premier-league")
    parser.add_argument("--season", default="2023/24")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE.name,
        choices=sorted(PROFILES),
        help="Named model configuration. Defaults to production "
             "(180-day decay); pass 'control' for the frozen unweighted "
             "benchmark.",
    )
    parser.add_argument("--min-training-matches", type=int, default=30)
    parser.add_argument("--ridge", type=float, default=0.05)
    parser.add_argument("--min-matches", type=int, default=4)
    parser.add_argument("--skip-cold-start", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit metadata as JSON")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    as_of = datetime.now(UTC)
    scope = f"{args.competition}:{args.season}"
    with psycopg.connect(args.database_url or database_url()) as conn:
        observations = load_observations(
            conn,
            competition_slug=args.competition,
            season_label=args.season,
            as_of=as_of,
        )

    if not observations:
        print(f"no trainable results for {scope}", file=sys.stderr)
        return 1

    # The profile decides the MODEL; ridge and cold-start remain per-run knobs
    # shared by every profile, so overriding them does not silently produce a
    # different model under a profile's name.
    chosen = profile(args.profile)
    fit = replace(
        chosen.fit, ridge=args.ridge, min_matches=args.min_matches
    )
    report = walk_forward(
        observations,
        as_of=as_of,
        scope=scope,
        config=BacktestConfig(
            min_training_matches=args.min_training_matches,
            fit=fit,
            skip_cold_start=args.skip_cold_start,
        ),
    )

    if args.json:
        print(json.dumps(
            {**report.as_metadata(), "model": chosen.as_metadata()},
            indent=2, sort_keys=True,
        ))
    else:
        print(f"loaded               {len(observations)} trainable results")
        print(f"model                {chosen.name} - {chosen.description}")
        _print_report(report, scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
