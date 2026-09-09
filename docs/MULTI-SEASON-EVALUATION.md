# Multi-season model evaluation

**Phase 1, milestone 4.** Six Premier League seasons, 2,280 results, seven candidate configurations, one predeclared selection rule. `MODEL-BASELINE.md` and `MODEL-EXPERIMENTS.md` are unchanged; this document records what happened when the same candidates met a materially larger sample.

**Headline: the single-season conclusion was wrong, and more data overturned it.** `MODEL-EXPERIMENTS.md` rejected time decay because it hurt 1X2 log loss at every half-life on 350 predictions. On 1,490 development predictions, a **180-day half-life improves it**, and on the held-out seasons the improvement is four times larger again. That is exactly the "revisit with multiple seasons" the earlier phase called for, and it is why that phase kept the implementation rather than deleting it.

## 1. Evaluation design

| | seasons | predictions | purpose |
|---|---|---|---|
| **Development** | 2019/20 – 2022/23 | 1,490 | the **only** evidence used to choose a candidate |
| **Held out** | 2023/24 – 2024/25 | 760 | scored **after** selection, for the baseline and the choice only |

**One chronological pass, split on the results — not two corpora.** A held-out fixture in 2023/24 is legitimately entitled to train on 2019/20–2022/23, because those matches were played before it. Splitting the *corpus* would have thrown that history away and measured a different, worse model. The split is applied to the scored predictions.

**The split is enforced by the code's shape, not by discipline.** `select(development, baseline_label)` is not given held-out metrics, so it cannot use them; a test asserts its signature.

## 2. Walk-forward procedure

Unchanged from the baseline: each fixture's cutoff is its own kickoff instant, training is every observation strictly earlier, and fits are cached per distinct cutoff. **1,463 fits per candidate** over 2,280 fixtures. Minimum history stays at **30 matches** — no policy was changed.

The one implementation change is a performance fix that is exact rather than approximate: because the observation list is sorted by kickoff, the training set for a cutoff is a **prefix**, found by bisection instead of re-filtering and re-sorting 2,280 observations 2,280 times. A test compares the bisected slice against `observations_before` object-for-object at every cutoff in the corpus. The frozen one-season benchmark reproduces unchanged, which is the other half of the proof.

## 3. Cross-season learning

Training crosses season boundaries freely, subject only to the cutoff. A season label is attached to each observation **for reporting only** — a test asserts the estimator produces bit-identical parameters with and without it.

| season | first kickoff | training available | of which prior seasons |
|---|---|---|---|
| 2019/20 | 2019-08-09 | 0 | 0 |
| 2020/21 | 2020-09-12 | 380 | 380 |
| 2021/22 | 2021-08-13 | 760 | 760 |
| 2022/23 | 2022-08-05 | 1,140 | 1,140 |
| 2023/24 | 2023-08-11 | 1,520 | 1,520 |
| 2024/25 | 2024-08-16 | 1,900 | 1,900 |

Only 2019/20 is ever starved: its first 30 matches are skipped, and every later season opens with a full complement of history.

## 4. Development results — the only selection evidence

1,490 predictions, 29 cold-start, identical fixtures for every candidate.

| candidate | log loss | Δ | Brier | Δ | pred H/A | goal bias |
|---|---|---|---|---|---|---|
| **A. frozen Poisson** | 1.0043 | — | 0.5973 | — | 1.507 / 1.288 | +0.023 |
| B. decay 30d | 1.0504 | **+0.0461** | 0.6223 | +0.0250 | 1.509 / 1.270 | +0.007 |
| C. decay 90d | 1.0053 | +0.0010 | 0.5978 | +0.0006 | 1.501 / 1.274 | +0.002 |
| **D. decay 180d** | **0.9984** | **−0.0059** | **0.5933** | **−0.0039** | 1.501 / 1.280 | +0.009 |
| E. decay 365d | 0.9989 | −0.0053 | 0.5936 | −0.0037 | 1.502 / 1.285 | +0.015 |
| F. Dixon-Coles | 1.0058 | +0.0016 | 0.5978 | +0.0006 | 1.507 / 1.288 | +0.023 |
| G. decay 365d + DC | 1.0006 | −0.0037 | 0.5942 | −0.0031 | 1.502 / 1.285 | +0.015 |

**Selection rule, fixed before the numbers were seen:** log-loss gain ≥ 0.0050, Brier regression ≤ 0.0010, ties broken toward the simpler model.

D (0.0059) and E (0.0053) both qualify; both improve Brier. **Selected: D, Poisson + 180-day decay** — equal complexity to E, larger gain. B, C and F fail on log loss. G qualifies on Brier but misses the log-loss threshold and is the most complex candidate.

## 5. Held-out results

Scored after selection. 760 predictions, 8 cold-start.

| candidate | log loss | Δ | Brier | Δ | pred H/A | act H/A | goal bias |
|---|---|---|---|---|---|---|---|
| A. frozen Poisson | 0.9840 | — | 0.5865 | — | 1.529 / 1.290 | 1.657 / 1.450 | **−0.287** |
| **D. decay 180d** | **0.9560** | **−0.0280** | **0.5669** | **−0.0196** | 1.643 / 1.372 | 1.657 / 1.450 | **−0.091** |

The held-out gain is **nearly five times the development gain**. Decay also cuts the goal-rate bias from −0.287 to −0.091 — the diagnostic `MODEL-BASELINE.md` §7 recorded and could not fix.

Baseline 1X2 calibration on the held-out seasons is close: home predicted 0.4318 vs observed 0.4342, draw 0.2324 vs 0.2303, away 0.3357 vs 0.3355.

## 6. Season-by-season stability

| season | A log loss | D log loss | A Brier | D Brier | A pred H | D pred H | actual H |
|---|---|---|---|---|---|---|---|
| 2019/20 | 1.0143 | **1.0114** | 0.6008 | 0.5995 | 1.534 | 1.532 | 1.529 |
| 2020/21 | 1.0324 | **1.0304** | 0.6148 | 0.6132 | 1.503 | 1.450 | 1.353 |
| 2021/22 | **0.9515** | 0.9594 | 0.5633 | 0.5685 | 1.485 | 1.470 | 1.513 |
| 2022/23 | 1.0198 | **0.9936** | 0.6104 | 0.5926 | 1.510 | 1.556 | 1.634 |
| 2023/24 | 0.9394 | **0.9359** | 0.5543 | 0.5511 | 1.493 | 1.664 | 1.800 |
| 2024/25 | 1.0286 | **0.9760** | 0.6188 | 0.5827 | 1.566 | 1.622 | 1.513 |

**D improves in five of six seasons.** Best season for both is 2023/24 (0.9394 / 0.9359); worst for the baseline is 2020/21 (1.0324), worst for D is 2020/21 (1.0304). The single regression is **2021/22**, where D is 0.0079 worse.

The aggregate gain is **not** carried by one season: D's biggest win is 2024/25 (−0.0526) and its second 2022/23 (−0.0262), and it is at least level in three others. That is the pattern the stability check exists to look for, and it passes.

**COVID 2020/21 behaves differently and both models struggle with it.** It is the worst season for both, and the reason is visible in the data rather than the metric: home goals 1.353 against away 1.342, with away wins (40.3%) exceeding home wins (37.9%). Behind closed doors, home advantage nearly vanished. A model whose home-advantage term is estimated from surrounding seasons cannot know that, and neither candidate does better than fail gracefully.

## 7. Time-decay diagnostics

The single-season finding was that decay traded 1X2 discrimination for goal-rate accuracy. **Across six seasons the trade disappears in the middle of the range**:

| half-life | dev log loss Δ | goal bias | reading |
|---|---|---|---|
| 30d | +0.0461 | +0.007 | still ruinous — too little data survives the weighting |
| 90d | +0.0010 | +0.002 | neutral |
| **180d** | **−0.0059** | +0.009 | **best discrimination** |
| 365d | −0.0053 | +0.015 | nearly as good |
| none | — | +0.023 | baseline |

The explanation is sample size. On one season, any decay starves a fit that only had 380 matches to begin with. With up to 1,900 prior matches, a 180-day half-life still leaves several hundred effective observations while discarding genuinely stale ones — including the COVID season, whose home-advantage regime does not describe 2022 onward. **Decay was not chosen because it predicts total goals better** — B has the second-best goal bias and the worst log loss by a wide margin.

## 8. Dixon-Coles rho diagnostics

1,463 fits across the corpus:

| | one season (previous phase) | six seasons |
|---|---|---|
| median | +0.0320 | **−0.0314** |
| mean | — | −0.0371 |
| min / max | −0.2073 / +0.1947 | −0.2406 / +0.0093 |
| negative share | 68 / 214 = **32%** | **93.5%** |
| sign changes | 4 (of 214) | 8 (of 1,463) |

Median rho by calendar year: **−0.131, −0.058, −0.037, −0.030, −0.006, −0.008, −0.012** (2019 → 2025).

**Rho does stabilise with more data, and it settles in the classic negative direction** — the earlier phase's instability was a small-sample artefact, exactly as suspected. But it converges to roughly **−0.01**, an order of magnitude smaller than the −0.13 Dixon and Coles reported on 1990s English football. The low-score dependence is real, consistent, and nearly negligible in the modern game.

Low-score cells over the 2,250 scored fixtures:

| cell | actual | A (baseline) | F (Dixon-Coles) |
|---|---|---|---|
| 0-0 | 5.38% | 0.0682 | 0.0720 |
| 0-1 | 6.93% | 0.0825 | 0.0787 |
| 1-0 | 8.80% | 0.0930 | 0.0892 |
| 1-1 | 10.98% | 0.1066 | 0.1104 |

The correction moves 1-1 toward the observed rate and 0-0 away from it, which is consistent with a rho small enough that the adjustment is dominated by other error. **F does not qualify for adoption**: +0.0016 development log loss.

## 9. Cold start

| season | cold-start predictions | clubs |
|---|---|---|
| 2019/20 | 10 | all 20 (no prior history exists at all) |
| 2020/21 | 11 | Fulham, Leeds United, West Bromwich Albion |
| 2021/22 | 4 | Brentford |
| 2022/23 | 4 | Nottingham Forest |
| 2023/24 | 4 | Luton Town |
| 2024/25 | 4 | Ipswich Town |

**This table is the clearest evidence that cross-season history works and does not leak.** From 2020/21 onward the only cold-started clubs are those with no prior Premier League appearance *in this corpus*. Norwich and Watford, promoted for 2021/22, are not cold because they played in 2019/20; Burnley and Sheffield United, promoted for 2023/24, are not cold for the same reason; Leicester and Southampton, promoted for 2024/25, are not cold. Only Brentford, Forest, Luton and Ipswich — genuinely new — start cold, and each stops being cold once it has four appearances. No Championship prior is invented for any of them.

## 10. Decision

**Adopt nothing yet. The production default remains the frozen independent Poisson.** — **[SUPERSEDED 2026-09-09]** this recommendation was accepted and acted on: 180-day decay is now the production profile (`MODEL-BASELINE.md` §0). The reasoning below is kept as written, because it is the evidence the adoption rests on.

D (180-day decay) **passed the predeclared development rule** and was confirmed by a larger held-out gain, so the evidence for it is genuinely better than anything this project has produced. Recording it as *selected by the rule* is the honest result of the procedure. But this phase's mandate is evaluation, not promotion: adopting a production default is a change to `MODEL-BASELINE.md` and to what the engine does, and that is a decision to take deliberately rather than as a side effect of an experiment. **The recommendation is to adopt D in a dedicated phase**, with the baseline re-frozen against it.

The case for D, stated plainly so it can be argued with: it improves development log loss by 0.0059 and Brier by 0.0039, improves held-out log loss by 0.0280 and Brier by 0.0196, improves five of six seasons individually, cuts the long-standing goal-rate bias by two thirds, and adds exactly one interpretable parameter. The case against: 2,250 predictions is still one league, the 2021/22 regression is unexplained, and the held-out gain being five times the development gain is itself a little suspicious — it may reflect that the most recent seasons benefit most from forgetting COVID.

**Dixon-Coles is not adopted.** Rho stabilised, which answers the question the previous phase asked, but the effect is too small to earn a place: +0.0016 development log loss.

## 11. Limitations

One league. 2,250 scored predictions. Season labels come from the provider's file naming, not from a competition calendar. No significance tests: the seasons are not independent draws and a p-value would imply otherwise. The held-out seasons were scored once, but this document now exists — any future tuning against 2023/24–2024/25 would no longer be held out. Runtime is **597 s for seven candidates** (1,463 fits each) plus 88 s for the rho sweep; the estimator is pure Python and this is the point at which that starts to matter.

---
