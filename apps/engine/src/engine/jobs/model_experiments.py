"""Run the controlled model-improvement experiments (MODEL-EXPERIMENTS.md).

    uv run python -m engine.jobs.model_experiments

READS ONLY. Same data, same repository boundary, same walk-forward
methodology, same eligible fixtures. The ONLY thing that varies between
candidates is the model configuration - which is what makes it a comparison
rather than seven separate results.

Candidate G is chosen from the walk-forward log loss of B..E, never from a
training likelihood. If a decay setting looks good only in training, it does
not get to be candidate G.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from engine.db import database_url
from engine.model.backtest import BacktestConfig, BacktestReport, walk_forward
from engine.model.decay import DecayConfig
from engine.model.dixon_coles import LOW_SCORES
from engine.model.fit import FitConfig, MatchObservation
from engine.model.repository import load_observations

BASELINE_LABEL = "A. frozen Poisson"
HALF_LIVES: tuple[float, ...] = (30.0, 90.0, 180.0, 365.0)

#: The frozen benchmark. A run that does not reproduce these exactly means the
#: baseline moved, and the experiment is void until that is explained.
FROZEN_PREDICTIONS = 350
FROZEN_FITS = 214
FROZEN_LOG_LOSS = 0.9802
FROZEN_BRIER = 0.5771


@dataclass(frozen=True)
class Candidate:
    label: str
    config: BacktestConfig

    @property
    def fit_config(self) -> FitConfig:
        return self.config.fit


@dataclass
class Outcome:
    label: str
    report: BacktestReport
    seconds: float

    @property
    def cold_start_predictions(self) -> int:
        return sum(1 for s in self.report.scored if s.cold_started)

    @property
    def total_goal_bias(self) -> float:
        r = self.report
        predicted = r.mean_predicted_home_goals + r.mean_predicted_away_goals
        actual = r.mean_actual_home_goals + r.mean_actual_away_goals
        return predicted - actual


def candidate(label: str, *, half_life: float | None, dixon: bool) -> Candidate:
    """Everything except the model configuration is held identical."""
    return Candidate(
        label=label,
        config=BacktestConfig(
            min_training_matches=30,
            fit=FitConfig(
                ridge=0.05,
                min_matches=4,
                decay=DecayConfig(half_life_days=half_life),
                dixon_coles=dixon,
            ),
        ),
    )


def run(
    observations: list[MatchObservation],
    candidates: list[Candidate],
    *,
    as_of: datetime,
    scope: str,
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for entry in candidates:
        started = time.perf_counter()
        report = walk_forward(
            observations, as_of=as_of, scope=scope, config=entry.config
        )
        outcomes.append(
            Outcome(entry.label, report, time.perf_counter() - started)
        )
    return outcomes


def low_score_report(
    observations: list[MatchObservation],
    outcomes: list[Outcome],
) -> list[str]:
    """Actual low-score frequency against what each candidate predicted."""
    lines: list[str] = []
    reference = outcomes[0].report
    eligible = {(s.kickoff, s.home_team, s.away_team) for s in reference.scored}
    actual = {cell: 0 for cell in LOW_SCORES}
    for o in observations:
        key = (o.kickoff, o.home_team, o.away_team)
        if key in eligible and (o.home_goals, o.away_goals) in actual:
            actual[(o.home_goals, o.away_goals)] += 1
    n = len(eligible)

    lines.append("")
    lines.append("LOW-SCORE DIAGNOSTICS (over the same 350 fixtures)")
    header = "  cell    actual n   actual %  " + "".join(
        f"{o.label.split('.')[0]:>10}" for o in outcomes
    )
    lines.append(header)
    for index, cell in enumerate(LOW_SCORES):
        share = actual[cell] / n
        row = f"  {cell[0]}-{cell[1]}      {actual[cell]:>6}    {share:>7.2%}  "
        for outcome in outcomes:
            scored = outcome.report.scored
            mean = sum(s.low_score[index] for s in scored) / len(scored)
            row += f"{mean:>10.4f}"
        lines.append(row)
    return lines


def comparison_table(outcomes: list[Outcome]) -> list[str]:
    base = outcomes[0].report
    lines = [
        "",
        "candidate                  log loss    delta     Brier    delta"
        "   preds  fits  cold   sec",
    ]
    for outcome in outcomes:
        r = outcome.report
        lines.append(
            f"{outcome.label:<26} {r.log_loss:.4f}  {r.log_loss - base.log_loss:+.4f}"
            f"   {r.brier:.4f}  {r.brier - base.brier:+.4f}"
            f"   {r.predictions:>5} {r.fits:>5} {outcome.cold_start_predictions:>5}"
            f"  {outcome.seconds:>4.1f}"
        )
    lines.append("")
    lines.append(
        "candidate                  pred home  pred away  act home  act away"
        "  total bias"
    )
    for outcome in outcomes:
        r = outcome.report
        lines.append(
            f"{outcome.label:<26} {r.mean_predicted_home_goals:>9.3f}"
            f"  {r.mean_predicted_away_goals:>9.3f}"
            f"  {r.mean_actual_home_goals:>8.3f}  {r.mean_actual_away_goals:>8.3f}"
            f"  {outcome.total_goal_bias:>+10.3f}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model-improvement experiments.")
    parser.add_argument("--competition", default="england-premier-league")
    parser.add_argument("--season", default="2023/24")
    parser.add_argument("--json", action="store_true")
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

    decay_candidates = [
        candidate(f"{chr(66 + i)}. decay {hl:g}d", half_life=hl, dixon=False)
        for i, hl in enumerate(HALF_LIVES)
    ]
    entries = [
        candidate(BASELINE_LABEL, half_life=None, dixon=False),
        *decay_candidates,
        candidate("F. Dixon-Coles", half_life=None, dixon=True),
    ]
    outcomes = run(observations, entries, as_of=as_of, scope=scope)

    # -- candidate G: the best decay BY WALK-FORWARD LOG LOSS, plus DC -----
    decay_outcomes = outcomes[1:5]
    best = min(decay_outcomes, key=lambda o: o.report.log_loss)
    best_half_life = HALF_LIVES[decay_outcomes.index(best)]
    outcomes += run(
        observations,
        [
            candidate(
                f"G. decay {best_half_life:g}d + DC",
                half_life=best_half_life,
                dixon=True,
            )
        ],
        as_of=as_of,
        scope=scope,
    )

    baseline = outcomes[0].report
    frozen_ok = (
        baseline.predictions == FROZEN_PREDICTIONS
        and baseline.fits == FROZEN_FITS
        and round(baseline.log_loss, 4) == FROZEN_LOG_LOSS
        and round(baseline.brier, 4) == FROZEN_BRIER
    )

    if args.json:
        print(json.dumps(
            {
                "frozen_baseline_reproduced": frozen_ok,
                "best_decay_half_life_days": best_half_life,
                "candidates": [
                    {"label": o.label, "seconds": o.seconds,
                     "cold_start_predictions": o.cold_start_predictions,
                     **o.report.as_metadata()}
                    for o in outcomes
                ],
            },
            indent=2, sort_keys=True,
        ))
        return 0

    print(f"scope                {scope}")
    print(f"observations         {len(observations)}")
    print(
        f"eligible fixtures    {baseline.predictions}"
        " (identical for every candidate)"
    )
    print(
        f"frozen baseline      {'REPRODUCED' if frozen_ok else 'CHANGED - STOP'}"
        f"  ({FROZEN_PREDICTIONS} preds, {FROZEN_FITS} fits,"
        f" {FROZEN_LOG_LOSS}, {FROZEN_BRIER})"
    )
    for line in comparison_table(outcomes):
        print(line)
    for line in low_score_report(observations, outcomes):
        print(line)
    print()
    print(f"best decay by walk-forward log loss: {best_half_life:g}d")
    return 0 if frozen_ok else 1


if __name__ == "__main__":
    sys.exit(main())
