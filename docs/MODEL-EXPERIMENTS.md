# Model experiments — time decay and Dixon-Coles

**Phase 1, milestone 2.** A controlled comparison against the frozen independent-Poisson baseline. `MODEL-BASELINE.md` describes the baseline and is unchanged by this document: **nothing here has been adopted**, and the production default is still the frozen model.

**Conclusion up front: neither modification earned adoption on this evidence.** Time decay makes 1X2 forecasting *worse* at every half-life tested. Dixon-Coles improves it by 0.0032 of log loss — real but tiny, on one season, with an unstable parameter. Details below.

## 1. The control

Frozen, and reproduced exactly at the start and end of this work:

```
350 predictions · 214 fits · 2023-09-02 to 2024-05-19
1X2 log loss 0.9802 · 1X2 Brier 0.5771
```

The experiment runner asserts all four figures and **exits non-zero if any has moved**. Everything else is held identical across candidates: 30-match minimum history, the same eligible fixtures, the same cutoff semantics, the same target, the same metrics, the same cold-start policy (`min_matches = 4`, 11 affected predictions in every candidate).

**Decay off is bit-identical to the frozen estimator, not approximately equal.** `DecayConfig()` yields a weight of exactly `1.0`, and multiplying by 1.0 is exact in IEEE 754 with the summation order unchanged. A test asserts the two paths agree to the bit; the runner asserts the published numbers.

## 2. Time-decay formulation

```
weight = 0.5 ** (age_days / half_life_days)
age_days = (data_cutoff - kickoff) / 1 day
```

Half-life rather than a bare rate because it is interpretable: at 90 days a match three months old counts half as much as yesterday's. `half_life_days = None` disables it entirely.

The weight enters the likelihood, not the data:

```
L = SUM_m w_m [ y_h log(lambda_h) - lambda_h + y_a log(lambda_a) - lambda_a ]
    - (ridge/2) SUM_i (attack_i^2 + defence_i^2)
```

Every coordinate update generalises cleanly — counts become weighted counts, lambda sums become weighted lambda sums — so `mu` and `home_advantage` keep their exact closed forms and the team coordinates keep their closed-form curvature. **The parameterisation, the identifiability constraints and the convergence behaviour are untouched.**

**Age is measured from the prediction cutoff**, so each fit has its own weight vector. A model predicting in September must weight an August match the way September saw it.

Tested half-lives: **30, 90, 180, 365 days**, plus no decay. Four values and a control — not a continuous sweep, which on 350 fixtures would be fitting the test set.

## 3. Dixon-Coles formulation

The standard multiplicative adjustment to exactly four cells:

```
tau(0,0) = 1 - lambda_h * lambda_a * rho
tau(0,1) = 1 + lambda_h * rho
tau(1,0) = 1 + lambda_a * rho
tau(1,1) = 1 - rho
tau(x,y) = 1                       everywhere else
```

**Constraints.** Every tau must stay positive, giving `max(-1/lh, -1/la) < rho < min(1/(lh*la), 1)`. The fit takes the intersection over all training fixtures; prediction clamps into the interval for that fixture's own rates, with a 1e-6 margin, rather than discarding the fixture.

**Total probability is preserved exactly** — worth stating because it is not obvious. With `K = exp(-lh)exp(-la)`, the four adjusted cells carry `K(1 - lh·la·rho + la + la·lh·rho + lh + lh·la·rho + lh·la - lh·la·rho)`, and every rho term cancels, leaving `K(1 + la + lh + lh·la)` — the uncorrected total. The correction *moves* mass between the four cells and never creates any. **The matrix is still renormalised**, because the grid is truncated at `MAX_GOALS` and the identity does not cover the tail; a test asserts the pre-normalisation total is within 1e-12 of the uncorrected total, so a broken identity fails loudly instead of being absorbed.

**How rho is estimated: per training fit, in two stages.** The Poisson parameters are fitted first, then rho maximises the profile likelihood holding them fixed. Because the adjustment preserves total probability, no normalising term enters, and the objective reduces to `max_rho SUM_m w_m log tau(y_m | lambda_m, rho)` over the feasible interval — a smooth 1-D problem solved by golden-section search, deterministic and derivative-free. Only low-score training matches contribute; tau is 1 elsewhere. Recency weights reach rho too.

**Deviation from the 1997 paper, stated plainly.** Dixon and Coles estimate rho *jointly* with attack, defence and home advantage. This is a two-stage conditional estimate. The simplification is deliberate: it leaves the frozen estimator untouched, which is what makes this a controlled comparison rather than two simultaneous changes. The joint optimum may differ slightly.

## 4. Results — Premier League 2023/24

Identical 350 fixtures, 214 fits and 11 cold-start predictions for every candidate.

| candidate | log loss | Δ | Brier | Δ | pred home | pred away | total goal bias | sec |
|---|---|---|---|---|---|---|---|---|
| **A. frozen Poisson** | **0.9802** | — | **0.5771** | — | 1.731 | 1.414 | −0.160 | 2.8 |
| B. decay 30d | 1.0061 | **+0.0259** | 0.5912 | +0.0141 | 1.779 | 1.465 | **−0.062** | 2.8 |
| C. decay 90d | 0.9836 | +0.0034 | 0.5782 | +0.0011 | 1.752 | 1.435 | −0.119 | 2.7 |
| D. decay 180d | 0.9810 | +0.0009 | 0.5770 | −0.0000 | 1.742 | 1.425 | −0.139 | 2.6 |
| E. decay 365d | 0.9804 | +0.0002 | 0.5769 | −0.0002 | 1.737 | 1.420 | −0.149 | 2.6 |
| **F. Dixon-Coles** | **0.9770** | **−0.0032** | **0.5759** | −0.0012 | 1.731 | 1.414 | −0.160 | 3.0 |
| G. decay 365d + DC | 0.9770 | −0.0032 | **0.5756** | **−0.0015** | 1.737 | 1.420 | −0.149 | 3.0 |

Actual: home 1.823, away 1.483. Lower is better for both metrics; negative Δ is an improvement.

Candidate G's decay setting was chosen as the **best of B–E by walk-forward log loss**, never by training likelihood. That happened to be 365 days — the weakest decay tested, which is itself the finding.

## 5. Goal-rate diagnostics

The baseline under-predicts goals because scoring rose through 2023/24 while an expanding window weights all history equally (`MODEL-BASELINE.md` §7). **Decay does fix that**, monotonically: total goal bias improves from −0.160 to −0.062 as the half-life shortens to 30 days.

**And it makes 1X2 forecasting worse, monotonically, over the same range.** The two effects move in opposite directions. Shortening the half-life throws away the sample size that makes the team ratings informative; the goal *level* gets closer while the *discrimination between teams* degrades, and 1X2 scoring depends on the latter. Correcting a level bias by discarding data is a bad trade when the level bias was not what the primary metric measured.

## 6. Dixon-Coles diagnostics

Realised frequencies over the 350 scored fixtures, against each candidate's mean assigned probability:

| cell | actual n | actual % | A (baseline) | F (Dixon-Coles) |
|---|---|---|---|---|
| 0-0 | 11 | 3.14% | 0.0533 | **0.0479** |
| 0-1 | 16 | 4.57% | 0.0687 | 0.0742 |
| 1-0 | 20 | 5.71% | 0.0767 | 0.0822 |
| 1-1 | 35 | 10.00% | 0.0925 | 0.0871 |

**The correction ran in the opposite direction to the 1997 paper**, and the data supports it: 2023/24 produced only 11 goalless draws in 380 matches (2.89%), where independent Poisson expected about 5.3%. A season with unusually few 0-0s implies a *positive* rho, which lowers 0-0 and 1-1 and raises the one-nils — exactly the column above. Dixon and Coles' −0.13 came from 1990s English football, a materially lower-scoring environment.

**Rho is unstable, which is the more important finding.** Across the 214 fits it ran from −0.2073 to +0.1947, median +0.0320, with 4 sign changes; it was negative in only 68 of 214 fits and finished at −0.0213. The first fit had 9 informative low-score matches to work with, the last 91. A parameter that changes sign four times over one season is not estimating a stable property of football on this sample.

## 7. Statistical caution

**350 predictions, one league, one season.** The Dixon-Coles gain is 0.0032 of log loss — about 0.3% — and the Brier gain 0.0012. Differences this small on a sample this size are directional evidence at best. No significance test is reported because a p-value here would imply an independence and stationarity that a single correlated season does not have, and would lend the number more authority than it deserves.

What *is* robust is the sign and ordering of the decay result: the penalty grows monotonically as the half-life shortens, across four settings, which is a pattern rather than a coincidence.

## 8. Conclusion

**Adopt nothing. The default remains the frozen independent-Poisson model.**

- **Time decay: rejected.** It hurts the primary metric at every tested half-life. It fixes the goal-level bias, but that bias was a documented diagnostic, not the objective, and buying it with worse 1X2 discrimination is the wrong trade. Worth revisiting with multiple seasons, where a longer half-life could down-weight *stale seasons* rather than early-season matches.
- **Dixon-Coles: not yet.** The direction is right and it costs nothing at prediction time, but a 0.3% log-loss gain from a parameter that flips sign four times in one season is not enough to change a production default. Revisit with several seasons, where rho can be estimated from thousands of low-score matches instead of tens.
- **The combination adds nothing** over Dixon-Coles alone: identical log loss, 0.0003 better Brier.

Both remain implemented, tested and off by default, so re-running this comparison on more data is a configuration change rather than new work.

## 9. Limitations

One season, one league, 350 predictions, and the two techniques were evaluated on the same fixtures that would select between them — so any "best" here is chosen on the evaluation set and would need fresh data to confirm. Rho is a two-stage conditional estimate, not the paper's joint one. Decay is applied only to the training likelihood, not to the cold-start threshold or the minimum-history rule. No significance testing, deliberately.

---
