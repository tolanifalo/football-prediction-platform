"""Multi-season walk-forward evaluation (MULTI-SEASON-EVALUATION.md).

    uv run python -m engine.jobs.multi_season_evaluation

READS ONLY. No migration, no prediction table, no canonical write, and no
bookmaker odds anywhere near the model - the 55,640 closing ticks in the
database are deliberately outside this, because the question is whether the
results themselves carry signal.

THE SPLIT IS PREDECLARED AND ENFORCED IN CODE. Candidates are compared on
2019/20-2022/23 only; 2023/24-2024/25 are held out and are scored for exactly
two models, the frozen baseline and whatever development selected. The
selection function cannot see held-out numbers because it is not given them.

ONE CHRONOLOGICAL PASS, NOT TWO CORPORA. The walk-forward runs over all six
seasons in order and the split is applied to the SCORED PREDICTIONS
afterwards. That is deliberate: a held-out fixture in 2023/24 is legitimately
entitled to train on 2019/20-2022/23, because those matches were played
before it. Splitting the corpus instead of the results would have thrown that
history away and measured a different, worse model.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from engine.db import database_url
from engine.model.backtest import (
    BacktestConfig,
    ScoredPrediction,
    walk_forward,
)
from engine.model.decay import DecayConfig
from engine.model.dixon_coles import LOW_SCORES
from engine.model.fit import FitConfig, MatchObservation
from engine.model.metrics import brier_score, calibration, log_loss
from engine.model.repository import load_corpus

COMPETITION = "england-premier-league"
DEVELOPMENT: tuple[str, ...] = ("2019/20", "2020/21", "2021/22", "2022/23")
HELD_OUT: tuple[str, ...] = ("2023/24", "2024/25")
ALL_SEASONS: tuple[str, ...] = DEVELOPMENT + HELD_OUT

#: Adoption thresholds, fixed BEFORE the numbers were seen (§11).
#:
#: A log-loss gain below this is not a reason to change a production default:
#: the previous single-season phase produced 0.0032 from a parameter that
#: flipped sign four times, and calling that an improvement would have been
#: reading noise.
MIN_LOG_LOSS_GAIN = 0.0050
#: Brier may not get materially worse in exchange for the log-loss gain.
MAX_BRIER_REGRESSION = 0.0010


@dataclass(frozen=True)
class Candidate:
    label: str
    half_life: float | None
    dixon_coles: bool
    #: Fewer moving parts wins a tie (§11, "prefer the simpler model").
    complexity: int

    def config(self) -> BacktestConfig:
        return BacktestConfig(
            min_training_matches=30,
            fit=FitConfig(
                ridge=0.05,
                min_matches=4,
                decay=DecayConfig(half_life_days=self.half_life),
                dixon_coles=self.dixon_coles,
            ),
        )


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("A. frozen Poisson", None, False, 0),
    Candidate("B. decay 30d", 30.0, False, 1),
    Candidate("C. decay 90d", 90.0, False, 1),
    Candidate("D. decay 180d", 180.0, False, 1),
    Candidate("E. decay 365d", 365.0, False, 1),
    Candidate("F. Dixon-Coles", None, True, 1),
    Candidate("G. decay 365d + DC", 365.0, True, 2),
)


@dataclass(frozen=True)
class Slice:
    """Metrics over one subset of the scored predictions."""

    label: str
    predictions: int
    log_loss: float
    brier: float
    predicted_home: float
    predicted_away: float
    actual_home: float
    actual_away: float
    cold_start: int

    @property
    def goal_bias(self) -> float:
        return (self.predicted_home + self.predicted_away) - (
            self.actual_home + self.actual_away
        )


def summarise(label: str, scored: Sequence[ScoredPrediction]) -> Slice:
    n = len(scored)
    if n == 0:
        raise ValueError(f"{label}: nothing to summarise")
    probabilities = [s.probabilities for s in scored]
    outcomes = [s.outcome for s in scored]
    return Slice(
        label=label,
        predictions=n,
        log_loss=log_loss(probabilities, outcomes),
        brier=brier_score(probabilities, outcomes),
        predicted_home=sum(s.lambda_home for s in scored) / n,
        predicted_away=sum(s.lambda_away for s in scored) / n,
        actual_home=sum(s.actual_home for s in scored) / n,
        actual_away=sum(s.actual_away for s in scored) / n,
        cold_start=sum(1 for s in scored if s.cold_started),
    )


@dataclass
class Run:
    candidate: Candidate
    scored: tuple[ScoredPrediction, ...]
    seconds: float
    fits: int

    def within(self, seasons: Sequence[str]) -> tuple[ScoredPrediction, ...]:
        wanted = set(seasons)
        return tuple(s for s in self.scored if s.season in wanted)


def evaluate(
    observations: list[MatchObservation],
    candidate: Candidate,
    *,
    as_of: datetime,
) -> Run:
    started = time.perf_counter()
    report = walk_forward(
        observations, as_of=as_of, scope=COMPETITION, config=candidate.config()
    )
    return Run(
        candidate=candidate,
        scored=report.scored,
        seconds=time.perf_counter() - started,
        fits=report.fits,
    )


def select(development: dict[str, Slice], baseline_label: str) -> Candidate | None:
    """Apply the predeclared rule to DEVELOPMENT metrics only.

    Held-out results are not a parameter of this function, so they cannot
    influence it even by accident.
    """
    base = development[baseline_label]
    qualifying: list[Candidate] = []
    for candidate in CANDIDATES:
        if candidate.label == baseline_label:
            continue
        slice_ = development[candidate.label]
        gain = base.log_loss - slice_.log_loss
        brier_cost = slice_.brier - base.brier
        if gain >= MIN_LOG_LOSS_GAIN and brier_cost <= MAX_BRIER_REGRESSION:
            qualifying.append(candidate)
    if not qualifying:
        return None
    # Simpler first, then the larger log-loss gain.
    return min(
        qualifying,
        key=lambda c: (c.complexity, development[c.label].log_loss),
    )


def rho_diagnostics(
    observations: list[MatchObservation], as_of: datetime
) -> dict[str, object]:
    """Every rho the Dixon-Coles candidate estimated, across the corpus."""
    from statistics import median

    from engine.model.fit import fit_poisson, observations_before

    config = FitConfig(ridge=0.05, min_matches=4, dixon_coles=True)
    values: list[tuple[datetime, float]] = []
    for cutoff in sorted({o.kickoff for o in observations}):
        training = observations_before(observations, cutoff)
        if len(training) < 30:
            continue
        model = fit_poisson(
            training, data_cutoff=cutoff, as_of=as_of, config=config
        )
        if model.rho is not None:
            values.append((cutoff, model.rho))
    rhos = [r for _, r in values]
    flips = sum(1 for a, b in zip(rhos, rhos[1:], strict=False) if (a > 0) != (b > 0))
    return {
        "fits": len(rhos),
        "mean": sum(rhos) / len(rhos),
        "median": median(rhos),
        "min": min(rhos),
        "max": max(rhos),
        "sign_changes": flips,
        "negative_share": sum(1 for r in rhos if r < 0) / len(rhos),
        "first": rhos[0],
        "last": rhos[-1],
        "by_year": {
            str(year): round(
                median([r for c, r in values if c.year == year]), 4
            )
            for year in sorted({c.year for c, _ in values})
        },
    }


def low_score_table(
    observations: list[MatchObservation], runs: Sequence[Run]
) -> list[str]:
    reference = runs[0].scored
    eligible = {(s.kickoff, s.home_team, s.away_team) for s in reference}
    actual = dict.fromkeys(LOW_SCORES, 0)
    for o in observations:
        if (o.kickoff, o.home_team, o.away_team) in eligible:
            cell = (o.home_goals, o.away_goals)
            if cell in actual:
                actual[cell] += 1
    n = len(eligible)
    lines = ["", f"LOW-SCORE CELLS over the {n} scored fixtures", ""]
    lines.append(
        "  cell   actual n   actual %"
        + "".join(f"{r.candidate.label.split('.')[0]:>9}" for r in runs)
    )
    for index, cell in enumerate(LOW_SCORES):
        row = f"  {cell[0]}-{cell[1]}     {actual[cell]:>6}    {actual[cell]/n:>7.2%}"
        for run in runs:
            mean = sum(s.low_score[index] for s in run.scored) / len(run.scored)
            row += f"{mean:>9.4f}"
        lines.append(row)
    return lines


def slice_row(name: str, s: Slice, base: Slice | None) -> str:
    if base is None:
        deltas = "      -        -"
    else:
        deltas = f"{s.log_loss - base.log_loss:>+8.4f} {s.brier - base.brier:>+8.4f}"
    return (
        f"{name:<22} {s.predictions:>5} {s.log_loss:.4f} {s.brier:.4f}"
        f" {deltas} {s.predicted_home:>6.3f} {s.predicted_away:>6.3f}"
        f" {s.actual_home:>6.3f} {s.actual_away:>6.3f}"
        f" {s.goal_bias:>+7.3f} {s.cold_start:>5}"
    )


HEADER = (
    f"{'candidate':<22} {'preds':>5} {'logloss':>6} {'brier':>6}"
    f" {'dLL':>8} {'dBrier':>8} {'predH':>6} {'predA':>6}"
    f" {'actH':>6} {'actA':>6} {'bias':>7} {'cold':>5}"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-season model evaluation.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-rho", action="store_true")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    as_of = datetime.now(UTC)
    with psycopg.connect(args.database_url or database_url()) as conn:
        observations = load_corpus(
            conn,
            competition_slug=COMPETITION,
            season_labels=ALL_SEASONS,
            as_of=as_of,
        )
    if not observations:
        print("no corpus loaded", file=sys.stderr)
        return 1

    started = time.perf_counter()
    runs = [evaluate(observations, c, as_of=as_of) for c in CANDIDATES]
    total_seconds = time.perf_counter() - started

    baseline = CANDIDATES[0].label
    development = {
        r.candidate.label: summarise(r.candidate.label, r.within(DEVELOPMENT))
        for r in runs
    }
    chosen = select(development, baseline)

    if args.json:
        print(json.dumps(
            {
                "development": {k: vars(v) for k, v in development.items()},
                "selected": chosen.label if chosen else None,
                "seconds": total_seconds,
            },
            indent=2, sort_keys=True, default=str,
        ))
        return 0

    print(f"corpus               {len(observations)} results, "
          f"{len({o.season for o in observations})} seasons")
    print(f"development          {', '.join(DEVELOPMENT)}")
    print(f"held out             {', '.join(HELD_OUT)}")
    print(f"total runtime        {total_seconds:.1f}s "
          f"({runs[0].fits} fits per candidate)")
    print()
    print("DEVELOPMENT PERIOD — the only evidence used for selection")
    print(HEADER)
    base_dev = development[baseline]
    for run in runs:
        s = development[run.candidate.label]
        print(slice_row(
            run.candidate.label,
            s,
            None if run.candidate.label == baseline else base_dev,
        ))

    print()
    print(f"selection rule       log-loss gain >= {MIN_LOG_LOSS_GAIN}, "
          f"Brier regression <= {MAX_BRIER_REGRESSION}")
    print(f"SELECTED             {chosen.label if chosen else 'none — retain A'}")

    # -- held out, scored for the baseline and the selection only ----------
    print()
    print("HELD-OUT SEASONS — scored after selection, never used to choose")
    print(HEADER)
    final = [runs[0]]
    if chosen is not None:
        final += [r for r in runs if r.candidate is chosen]
    base_held = summarise(baseline, runs[0].within(HELD_OUT))
    for run in final:
        s = summarise(run.candidate.label, run.within(HELD_OUT))
        print(slice_row(
            run.candidate.label,
            s,
            None if run.candidate.label == baseline else base_held,
        ))

    print()
    print("SEASON BY SEASON")
    print(f"{'candidate':<22} {'season':<9} {'preds':>5} {'logloss':>7} {'brier':>7}"
          f" {'predH':>6} {'actH':>6} {'predA':>6} {'actA':>6} {'cold':>5}")
    for run in final:
        for season in ALL_SEASONS:
            rows = run.within([season])
            if not rows:
                continue
            s = summarise(season, rows)
            print(f"{run.candidate.label:<22} {season:<9} {s.predictions:>5}"
                  f" {s.log_loss:>7.4f} {s.brier:>7.4f} {s.predicted_home:>6.3f}"
                  f" {s.actual_home:>6.3f} {s.predicted_away:>6.3f}"
                  f" {s.actual_away:>6.3f} {s.cold_start:>5}")

    print()
    print("COLD START BY SEASON (baseline)")
    for season in ALL_SEASONS:
        rows = runs[0].within([season])
        cold = [s for s in rows if s.cold_started]
        teams = sorted({t for s in cold for t in s.cold_started})
        names = ", ".join(teams) or "-"
        print(f"  {season}  {len(cold):>3} predictions  {names}")

    print()
    print("1X2 CALIBRATION — baseline, held-out seasons")
    for row in calibration(
        [s.probabilities for s in runs[0].within(HELD_OUT)],
        [s.outcome for s in runs[0].within(HELD_OUT)],
    ):
        print(f"  {row.outcome:<6} predicted {row.mean_predicted:.4f}"
              f"  observed {row.observed_frequency:.4f}  bias {row.bias:+.4f}")

    for line in low_score_table(observations, runs):
        print(line)

    if not args.skip_rho:
        print()
        print("DIXON-COLES RHO across the corpus")
        for key, value in rho_diagnostics(observations, as_of).items():
            print(f"  {key:<16} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
