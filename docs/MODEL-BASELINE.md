# The baseline prediction model — independent Poisson

**Phase 1, milestone 1.** Authoritative for the prediction engine's mathematics, cutoff policy and backtest methodology. Where it touches Phase 0 concepts — bitemporality, `is_trainable`, the as-of wrapper — `PHASE-0-SPEC.md` remains authoritative and this document consumes it.

The first thing in this repository that predicts anything. Deliberately the simplest defensible football model, because **its job is to be beaten**: every later improvement — Dixon-Coles, time decay, xG, ensembles — has to justify itself against a transparent number produced under the same leakage discipline, and a baseline that was already clever would hide whether the improvement was real.

**No migrations, no new tables.** The engine reads canonical facts and writes nothing.

## 1. The leakage rule, which is the whole point

A prediction for fixture F may use only what was knowable before F kicked off. The API makes the cutoff a required argument rather than a convention:

```python
fit_poisson(observations, data_cutoff=..., as_of=...)   # both mandatory
```

**Two time axes, kept apart** (§2.1). Collapsing them *is* the leakage bug:

| Axis | Argument | Question it answers | Applied by |
|---|---|---|---|
| Transaction time | `as_of` | which **revision** of a result do we believe? | `match_results_as_of`, the P0-06 wrapper |
| Valid time | `data_cutoff` | what had actually **happened**? | `kickoff < data_cutoff` |

The distinction is not academic on this data. Every 2023/24 result was imported in 2026, so **every `known_at` is later than every kickoff**: a transaction-time filter at a 2024 cutoff returns nothing at all. The cutoff a backtest walks is valid time.

**Valid time comes from `fixture_schedule.kickoff_utc`.** `match_results.occurred_at` is its natural home and is **NULL on all 380 imported rows** — P0-11 never populated it. The loader reads `coalesce(occurred_at, kickoff_utc)`, so it is already correct for a future provider that does supply it.

Enforcement is in three places, on purpose: the loader attaches a kickoff to every observation, `observations_before` is the single filter, and **`fit_poisson` raises if handed an observation at or after its own cutoff** — a training set containing the future is a programming error, not a judgement call.

## 2. Formulation

```
log(lambda_home) = mu + home_advantage + attack[home] - defence[away]
log(lambda_away) = mu +                  attack[away] - defence[home]
```

`mu` is the league scoring baseline on the log scale. **A positive `defence` means a team concedes FEWER**, because defence enters with a minus sign — the classic reading error, so it is asserted in the tests.

**Identifiability.** The parameterisation has exactly two invariances: shift every `attack` by *c* and offset `mu` by *−c*, and the same for `defence` with the opposite sign. Two invariances need two constraints, so both vectors are centred on zero with the shift absorbed into `mu` — which changes no lambda at all.

**Centring happens every sweep, not just at the end, and that is load-bearing.** Left uncentred the objective is very nearly flat along those two directions and coordinate ascent crawls along them forever, changing nothing observable. Measured: **52 of 60 fits hit the 500-sweep cap**. With per-sweep centring the same fits converge in **at most 31 sweeps, none capped**, and the backtest went from 83 s to 0.21 s.

**Estimation** is coordinate ascent on the ridge-penalised Poisson log-likelihood. `mu` and `home_advantage` have exact closed-form updates; each team coordinate is concave with a closed-form second derivative and takes one damped Newton step, capped at ±2 so a near-zero lambda cannot propose a jump that overflows `exp`. **No optimiser library, no random restart, no seed** — the same observations always produce bit-identical parameters. Rates are held as state and updated multiplicatively within a sweep, then recomputed exactly at the start of the next, so drift cannot accumulate.

**Ridge (default 0.05)** shrinks attack and defence toward league average. It is not decoration: without it a team that has not yet scored has an unpenalised maximum of minus infinity.

A fit serialises to deterministic metadata — model version, scope, both cutoffs, training count, config, log-likelihood, every rating. **There is no model registry**, because there is no second model to hold and a schema invented ahead of its requirement is a liability.

## 3. Scoreline matrix and derived markets

Goals are independent Poisson draws, so the joint distribution is the outer product of two marginals, built by the recurrence `p(0)=exp(-λ)`, `p(k)=p(k-1)·λ/k`. **No factorial is ever computed** — the textbook form overflows `k!` at k=171.

**Truncation is measured, not assumed.** Tail mass beyond the cap:

| cap | λ=1.4 | λ=3.0 | worst for λ≤4 |
|---|---|---|---|
| 10 | 2.8e-07 | 2.9e-04 | 2.8e-03 |
| **15** | **3.0e-11** | **1.2e-07** | **4.9e-06** |

`MAX_GOALS = 15`. Ten leaves nearly three parts in a thousand unmodelled at the high end, which is larger than it looks beside a draw probability of 0.25; the matrix is `(cap+1)²` floats, so the accuracy is free. The truncated mass is redistributed proportionally and **reported per fixture** as `truncated_mass`, so the approximation is never silent.

**The matrix is the source of truth.** 1X2, over/under (0.5/1.5/2.5/3.5) and BTTS are all read off it. Nothing is fitted separately — two models of one match that disagree about P(over 2.5) and P(home win) are two models.

## 4. Cold start

A team with **fewer than 4 appearances** in the training window, or none at all, is set to league average (`attack = defence = 0`) and **flagged**. `Prediction.cold_started` names the teams and `is_cold_start` is true.

The policy is conservative on purpose: one 5-0 must not become a rating. The alternative — letting the fit speak for a team it has barely seen — produces a confident-looking number backed by nothing, which is worse than declining. **A prediction never looks ordinary while hiding that a team was a placeholder.** The backtester can also skip such fixtures entirely (`skip_cold_start`).

## 5. Boundaries

`database → domain observations → fit → prediction`. `repository.py` holds the only SQL and does one thing: canonical rows to `MatchObservation`. **No mathematics in SQL, no connection downstream of it** — which is what lets the estimator, the predictor and the backtester be tested without PostgreSQL. It reads `fixtures`, `fixture_schedule`, `match_results` and `team_names`, filters on P0-08's `is_trainable` so an abandoned or awarded match never trains a scoring model, and **writes nothing**.

**No bookmaker odds anywhere.** Non-negotiable rule 1: the engine never reads prices, or value detection is circular before it exists.

## 6. Backtest methodology

Walk-forward, and **there is no single fit over the whole season anywhere in the code**. Each fixture's cutoff is its own kickoff instant, so two matches on the same afternoon get different training sets and the later one is entitled to the earlier one's result. Fits are cached by cutoff — an exact saving, since an identical cutoff yields an identical training set — which is why 350 predictions need only 214 fits.

A fixture is skipped until the window holds **30 matches**. Two baselines are scored on the identical fixtures: the **empirical class frequency** in the training window (Laplace-smoothed), and the **league-average Poisson** — the same machinery with every team rating switched off, isolating what the ratings add over `mu` plus home advantage.

Both scoring rules are proper: **log loss** (unbounded, punishes confident misses) and the multiclass **Brier score** (0–2). Uniform thirds score 1.0986 and 0.6667.

## 7. Results — Premier League 2023/24

380 trainable results, 350 predictions, 214 fits, 4.2 seconds.

```
first prediction 2023-09-02   last 2024-05-19

                    model    class-freq   league-avg   uniform
1X2 log loss       0.9802      1.0624       1.0566     1.0986
1X2 Brier          0.5771      0.6419       0.6384     0.6667

mean predicted goals   home 1.731   away 1.414
mean actual   goals    home 1.823   away 1.483

calibration     predicted   observed     bias
  home            0.4488     0.4657    -0.0170
  draw            0.2078     0.2200    -0.0122
  away            0.3434     0.3143    +0.0291
```

**The model beats every baseline on both rules.** It is out of sample by construction; nothing here is a training-set score.

**It under-predicts goals by ~0.09 per side, and the reason is known.** Scoring rose through 2023/24 — mean total goals by quarter: 3.05, 3.15, 3.59, 3.33 — while the estimator trains on an expanding prefix and weights every past match equally. An unweighted expanding window *should* lag a rising rate. The remedy is time-decay weighting, which is out of scope here (§8).

**The calibration bias is within noise.** With 350 fixtures the standard error on a frequency near 0.31 is about 2.5 points, so the +2.9-point away bias is roughly one standard error. It is not evidence of anything yet.

**Cold start**: 11 of 350 predictions involved a cold-started team, all between 2 and 18 September. Excluding them changes the model's log loss from 0.9802 to 0.9767 over 339 predictions — slightly better, as expected.

## 8. Limitations, recorded

**Goals are assumed independent.** They are not — draws and low scores are under-predicted by exactly this model, which is what the Dixon-Coles low-score correction exists to fix. Deliberately absent.

**Every past match counts equally.** No time decay, so the model lags a drifting scoring rate, as §7 measures.

**One season, one league, 350 predictions.** Enough to show the model behaves sensibly out of sample; nowhere near enough to claim predictive quality, and far too few to support a reliability curve — which is why calibration is reported as three means rather than bucketed bins that would invite conclusions the sample cannot carry.

**No promotion or relegation handling.** A season is fitted in isolation, so a newly promoted side starts cold rather than inheriting anything.

**Not built, and not by omission**: Dixon-Coles, Elo, Glicko, xG, ML, ensembles, calibration layers, value detection, scheduling, a model registry, a predictions table.

---

