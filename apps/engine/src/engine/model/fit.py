"""The estimator: observations in, attack/defence ratings out.

See MODEL-BASELINE.md §2 for the formulation and the identifiability argument.

NO DATABASE AND NO ODDS. It is handed a list of `MatchObservation` and knows
nothing about where they came from, which is what makes the leakage rule
testable: a fixture that is not in the list cannot influence the fit, and the
caller is the only thing that decides what goes in the list.

    log(lambda_home) = mu + home_advantage + attack[home] - defence[away]
    log(lambda_away) = mu + attack[away] - defence[home]

`mu` is the league scoring baseline on the log scale. A positive `attack` means
a team scores more than the league average; a positive `defence` means it
concedes FEWER, because defence enters with a minus sign. Getting that sign
backwards is the classic reading error, so it is stated here and asserted in
the tests.

IDENTIFIABILITY. The parameterisation has exactly two invariances: adding a
constant to every attack and subtracting it from mu leaves every lambda
unchanged, and likewise for defence with the opposite sign. Two invariances
need two constraints, so the fit ends by centring both vectors on zero and
absorbing the shift into mu - which changes no lambda at all.

ESTIMATION is coordinate ascent on the ridge-penalised Poisson log-likelihood.
Each coordinate is concave with a closed-form second derivative, so a damped
Newton step per coordinate converges quickly and deterministically. There is no
optimiser library, no random restart and no random seed: the same observations
in the same order always produce bit-identical parameters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

MODEL_VERSION: Final[str] = "poisson-independent@1.0.0"

#: Sweeps stop when no parameter moves more than this. On real league data the
#: fit converges in well under 100 sweeps; the cap is a guard, not a budget.
DEFAULT_TOLERANCE: Final[float] = 1e-10
DEFAULT_MAX_SWEEPS: Final[int] = 500

#: Ridge strength on attack and defence. Shrinks a team with a freak result
#: toward the league average instead of letting one 7-0 define its season, and
#: keeps the fit finite when a team has scored or conceded zero goals so far -
#: the unpenalised maximum for that team is minus infinity.
DEFAULT_RIDGE: Final[float] = 0.05

#: Below this many appearances a team is COLD-STARTED to league average.
DEFAULT_MIN_MATCHES: Final[int] = 4


@dataclass(frozen=True)
class MatchObservation:
    """One completed match, as the estimator needs it.

    `kickoff` is VALID TIME - when the match happened. It is the only temporal
    field the estimator uses, and it exists so a caller can prove a training
    set contains nothing that had not yet been played.
    """

    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    kickoff: datetime

    def __post_init__(self) -> None:
        if self.home_goals < 0 or self.away_goals < 0:
            raise ValueError("goals cannot be negative")
        if self.home_team == self.away_team:
            raise ValueError("a team cannot play itself")


@dataclass(frozen=True)
class FitConfig:
    ridge: float = DEFAULT_RIDGE
    min_matches: int = DEFAULT_MIN_MATCHES
    tolerance: float = DEFAULT_TOLERANCE
    max_sweeps: int = DEFAULT_MAX_SWEEPS

    def __post_init__(self) -> None:
        if self.ridge < 0.0:
            raise ValueError("ridge must not be negative")
        if self.min_matches < 0:
            raise ValueError("min_matches must not be negative")
        if self.tolerance <= 0.0:
            raise ValueError("tolerance must be positive")
        if self.max_sweeps < 1:
            raise ValueError("max_sweeps must be at least 1")

    def as_metadata(self) -> dict[str, float | int]:
        return {
            "ridge": self.ridge,
            "min_matches": self.min_matches,
            "tolerance": self.tolerance,
            "max_sweeps": self.max_sweeps,
        }


@dataclass(frozen=True)
class TeamRating:
    attack: float
    defence: float
    matches: int
    #: True when the team had too little history and was set to league average.
    cold_start: bool


@dataclass(frozen=True)
class FittedModel:
    """A fit, and everything needed to reproduce it (MODEL-BASELINE.md §2)."""

    model_version: str
    mu: float
    home_advantage: float
    ratings: Mapping[str, TeamRating]
    #: The valid-time cutoff this fit was entitled to see. Training data is
    #: strictly before it.
    data_cutoff: datetime
    #: The transaction-time cutoff: which revision of each result was read.
    as_of: datetime
    scope: str
    training_matches: int
    sweeps: int
    converged: bool
    config: FitConfig
    log_likelihood: float
    cold_started: tuple[str, ...] = field(default=())

    def rating_for(self, team: str) -> TeamRating:
        """A team with no history at all is league average, and says so."""
        found = self.ratings.get(team)
        if found is None:
            return TeamRating(attack=0.0, defence=0.0, matches=0, cold_start=True)
        return found

    def as_metadata(self) -> dict[str, object]:
        """Deterministic, serialisable, and enough to reproduce the fit."""
        return {
            "model_version": self.model_version,
            "scope": self.scope,
            "data_cutoff": self.data_cutoff.isoformat(),
            "as_of": self.as_of.isoformat(),
            "training_matches": self.training_matches,
            "teams": len(self.ratings),
            "sweeps": self.sweeps,
            "converged": self.converged,
            "log_likelihood": self.log_likelihood,
            "mu": self.mu,
            "home_advantage": self.home_advantage,
            "cold_started": list(self.cold_started),
            "config": self.config.as_metadata(),
            "ratings": {
                team: {
                    "attack": r.attack,
                    "defence": r.defence,
                    "matches": r.matches,
                    "cold_start": r.cold_start,
                }
                for team, r in sorted(self.ratings.items())
            },
        }


class InsufficientHistory(ValueError):
    """Raised when there is nothing to fit. Never fall back to a guess."""


def _newton_step(gradient: float, curvature: float) -> float:
    """One damped Newton step on a concave 1-D problem.

    `curvature` is the second derivative and is strictly negative for every
    coordinate in this model, so the step always moves uphill. It is capped
    because a coordinate whose current lambda is tiny can otherwise propose an
    enormous jump on the first sweep and overshoot into an exp() overflow.
    """
    step = -gradient / curvature
    return max(-2.0, min(2.0, step))


def fit_poisson(
    observations: Sequence[MatchObservation],
    *,
    data_cutoff: datetime,
    as_of: datetime,
    scope: str = "",
    config: FitConfig | None = None,
) -> FittedModel:
    """Fit attack, defence, home advantage and the league baseline.

    THE CALLER OWNS THE LEAKAGE RULE, but this function enforces the half it
    can see: an observation at or after `data_cutoff` had not been played, and
    including one is a programming error, not a judgement call.
    """
    settings = config or FitConfig()
    for observation in observations:
        if observation.kickoff >= data_cutoff:
            raise ValueError(
                f"observation at {observation.kickoff.isoformat()} is not "
                f"strictly before the cutoff {data_cutoff.isoformat()}"
            )
    if not observations:
        raise InsufficientHistory("no training matches before the cutoff")

    teams = sorted(
        {o.home_team for o in observations}
        | {o.away_team for o in observations}
    )
    index = {team: i for i, team in enumerate(teams)}
    n = len(teams)

    home_idx = [index[o.home_team] for o in observations]
    away_idx = [index[o.away_team] for o in observations]
    home_goals = [o.home_goals for o in observations]
    away_goals = [o.away_goals for o in observations]

    # Matches each team appeared in, and which side it was on. Built once so a
    # coordinate update touches only that team's own matches.
    as_home: list[list[int]] = [[] for _ in range(n)]
    as_away: list[list[int]] = [[] for _ in range(n)]
    for m, (h, a) in enumerate(zip(home_idx, away_idx, strict=True)):
        as_home[h].append(m)
        as_away[a].append(m)
    appearances = [len(as_home[i]) + len(as_away[i]) for i in range(n)]

    total_goals = sum(home_goals) + sum(away_goals)
    if total_goals == 0:
        raise InsufficientHistory("no goals in the training window")

    # Start from the league mean and no home advantage: a fixed, data-derived
    # origin, so the fit is reproducible without a seed.
    mu = math.log(total_goals / (2.0 * len(observations)))
    home_advantage = 0.0
    attack = [0.0] * n
    defence = [0.0] * n

    # The two rates per match, held as state rather than recomputed.
    #
    # A coordinate update multiplies lambda by exp(step) on exactly the
    # matches that coordinate touches, so a sweep costs O(matches) exp() calls
    # instead of O(matches * teams). Profiling the naive version showed 10
    # million exp() calls for 60 fits; this is the same arithmetic, done once.
    #
    # Each sweep RECOMPUTES both arrays from the parameters before touching
    # them, so multiplicative drift can never accumulate across sweeps: the
    # incremental updates only have to be exact within one sweep.
    lam_h = [0.0] * len(observations)
    lam_a = [0.0] * len(observations)

    def refresh() -> None:
        for m in range(len(observations)):
            h, a = home_idx[m], away_idx[m]
            lam_h[m] = math.exp(mu + home_advantage + attack[h] - defence[a])
            lam_a[m] = math.exp(mu + attack[a] - defence[h])

    def centre() -> None:
        # Project onto the identified manifold: sum(attack) = sum(defence) = 0,
        # with the shift absorbed into mu. Every lambda is unchanged.
        nonlocal mu
        mean_attack = sum(attack) / n
        mean_defence = sum(defence) / n
        for i in range(n):
            attack[i] -= mean_attack
            defence[i] -= mean_defence
        mu += mean_attack - mean_defence

    sweeps = 0
    converged = False
    previous = [mu, home_advantage, *attack, *defence]
    for sweep in range(1, settings.max_sweeps + 1):
        sweeps = sweep
        refresh()

        # -- mu: scales every lambda, so its update is exact ---------------
        predicted = sum(lam_h) + sum(lam_a)
        move = math.log(total_goals / predicted)
        mu += move
        scale = math.exp(move)
        for m in range(len(observations)):
            lam_h[m] *= scale
            lam_a[m] *= scale

        # -- home advantage: exact, it scales only the home lambdas --------
        predicted_home = sum(lam_h)
        scored_home = sum(home_goals)
        if scored_home > 0 and predicted_home > 0:
            move = math.log(scored_home / predicted_home)
            home_advantage += move
            scale = math.exp(move)
            for m in range(len(observations)):
                lam_h[m] *= scale
    
        # -- attack: one damped Newton step per team -----------------------
        for i in range(n):
            scored = 0
            expected = 0.0
            for m in as_home[i]:
                scored += home_goals[m]
                expected += lam_h[m]
            for m in as_away[i]:
                scored += away_goals[m]
                expected += lam_a[m]
            gradient = scored - expected - settings.ridge * attack[i]
            curvature = -expected - settings.ridge
            if curvature == 0.0:
                continue
            step = _newton_step(gradient, curvature)
            attack[i] += step
            scale = math.exp(step)
            for m in as_home[i]:
                lam_h[m] *= scale
            for m in as_away[i]:
                lam_a[m] *= scale

        # -- defence: same, but it enters lambda with a minus sign ---------
        for i in range(n):
            conceded = 0
            expected = 0.0
            for m in as_home[i]:
                conceded += away_goals[m]
                expected += lam_a[m]
            for m in as_away[i]:
                conceded += home_goals[m]
                expected += lam_h[m]
            gradient = -conceded + expected - settings.ridge * defence[i]
            curvature = -expected - settings.ridge
            if curvature == 0.0:
                continue
            step = _newton_step(gradient, curvature)
            defence[i] += step
            scale = math.exp(-step)
            for m in as_home[i]:
                lam_a[m] *= scale
            for m in as_away[i]:
                lam_h[m] *= scale

        # CONVERGENCE IS MEASURED ON THE CENTRED PARAMETERS.
        #
        # Without this the fit does not converge at all. The model has two
        # exact invariances - shift every attack and offset mu, likewise for
        # defence - so the objective is very nearly flat along them and
        # coordinate ascent crawls along that flat direction forever, changing
        # nothing observable. Measured before this fix: 52 of 60 fits hit the
        # 500-sweep cap. Centring each sweep removes the flat directions from
        # the iteration, and the same fits then converge in a few dozen.
        centre()
        current = [mu, home_advantage, *attack, *defence]
        largest_move = max(abs(c - p) for c, p in zip(current, previous, strict=True))
        previous = current
        if largest_move < settings.tolerance:
            converged = True
            break

    # -- identifiability ---------------------------------------------------
    # Already true: every sweep ends centred. Repeated so the postcondition is
    # guaranteed even for a fit that exhausted its sweep budget.
    centre()

    refresh()
    log_likelihood = 0.0
    for m in range(len(observations)):
        log_likelihood += home_goals[m] * math.log(lam_h[m]) - lam_h[m]
        log_likelihood += away_goals[m] * math.log(lam_a[m]) - lam_a[m]
        log_likelihood -= math.lgamma(home_goals[m] + 1.0)
        log_likelihood -= math.lgamma(away_goals[m] + 1.0)

    # -- cold start: too little history is league average, and it is said ---
    ratings: dict[str, TeamRating] = {}
    cold: list[str] = []
    for team, i in index.items():
        thin = appearances[i] < settings.min_matches
        if thin:
            cold.append(team)
        ratings[team] = TeamRating(
            attack=0.0 if thin else attack[i],
            defence=0.0 if thin else defence[i],
            matches=appearances[i],
            cold_start=thin,
        )

    return FittedModel(
        model_version=MODEL_VERSION,
        mu=mu,
        home_advantage=home_advantage,
        ratings=ratings,
        data_cutoff=data_cutoff,
        as_of=as_of,
        scope=scope,
        training_matches=len(observations),
        sweeps=sweeps,
        converged=converged,
        config=settings,
        log_likelihood=log_likelihood,
        cold_started=tuple(sorted(cold)),
    )


def observations_before(
    observations: Iterable[MatchObservation], cutoff: datetime
) -> list[MatchObservation]:
    """The training set for `cutoff`: strictly earlier kickoffs, in order.

    THE ONLY PLACE THE TRAINING FILTER IS WRITTEN. Sorting is by kickoff and
    then by the team names, so simultaneous kickoffs land in a stable order and
    the fit is reproducible regardless of how the rows arrived.
    """
    kept = [o for o in observations if o.kickoff < cutoff]
    kept.sort(key=lambda o: (o.kickoff, o.home_team, o.away_team))
    return kept
