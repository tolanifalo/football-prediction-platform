# The prediction pipeline

**Phase 1, milestone 6.** The first production pipeline: canonical database state in, persisted predictions out, reproducibly. Authoritative for the prediction artifact, its temporal semantics, its identity and the job that produces it.

This is the **engine/persistence boundary**. It is not the web UI, the value engine, the Wizard, bookmaker comparison or automation, and none of those are started here. The model itself is unchanged — `MODEL-BASELINE.md` remains authoritative for the mathematics and for which profile is production.

## 1. The prediction artifact

One table, `predictions`. **Not one table per market**: the scoreline matrix is the artifact and 1X2, totals and BTTS are read off it, so two rows can never disagree about the same match.

**A prediction is not a provider fact.** `PHASE-0-SPEC.md` §6 rule 6 defines a fact as "a claim two providers could disagree about" and requires `source_id` and `raw_payload_body_id` on every one. A prediction is *our* derived claim about a match nobody has observed; no provider supplied it and no archived bytes contain it. Its provenance anchor is instead the pair that makes it reproducible: **which model, and what it was allowed to know**.

| stored | why |
|---|---|
| `fixture_id` | what is being predicted |
| `model_family`, `model_version`, `profile` | which model |
| `data_cutoff` | what football information it was permitted to use |
| `known_at`, `superseded_at` | when we produced it, and when it was replaced |
| `lambda_home`, `lambda_away` | expected goals |
| `max_goals`, `scoreline`, `truncated_mass` | the joint distribution and its approximation |
| `p_home`, `p_draw`, `p_away` | 1X2 |
| `p_over_0_5` … `p_over_3_5`, `p_btts_yes` | totals and BTTS |
| `is_cold_start`, `cold_started_teams` | cold start, never silent |
| `training_matches`, `fit_metadata` | enough to reproduce the fit |
| `job_run_id` | which run produced it |

**Not stored, deliberately: bookmaker odds, implied probabilities, value, recommendations.** Rule 1 is the model/odds wall; a price column here would put bookmaker data one join from the estimator. Those belong to the value engine, which does not exist yet.

**The web read path needs no refit.** One indexed row per fixture carries the expected goals, the full matrix and every derived market.

### Numeric representation

`double precision`, never `numeric`. The model computes in float64; `numeric` would force a scale, and choosing one is rounding — applied silently to every probability on the way in. float8 stores exactly what was computed and reads back bit-identical, which is what makes *"the persisted artifact equals the direct model output"* an exact, testable claim rather than an approximate one. **Nothing is rounded before persistence.**

### Scoreline representation

Flat and **row-major**: index `h * (max_goals + 1) + a` is P(home `h`, away `a`), in a `double precision[]` of length `(max_goals+1)²`. Flat rather than a 2-D array because the shape is then a single cardinality CHECK against `max_goals`, and no database driver has to agree with us about multidimensional array bounds. At the production `MAX_GOALS = 15` that is 256 cells.

### Why the derived markets are stored at all

They are redundant — every one is recomputable from the matrix. They are stored anyway because the alternative is the **web application re-implementing `derive_markets` in TypeScript**, which is a second implementation of "the matrix is the source of truth" and would drift. Storing them keeps the derivation in exactly one place, and `derived_markets_match()` is the test that keeps the two consistent.

Redundancy is minimised where it is free: two-way markets store **one side only**. `under = 1 − over` and `btts_no = 1 − btts_yes` are exact, so a second column would be a copy that can fall out of step. 1X2 stores all three, guarded by a sum CHECK, because it is displayed as three.

## 2. Temporal semantics

Two axes, and they are not interchangeable — the same distinction `PHASE-0-SPEC.md` §2.1 draws for facts:

| | column | question |
|---|---|---|
| **valid time** | `data_cutoff` | what football information was the model permitted to use? |
| **transaction time** | `known_at` | when did we generate and record it? |

`data_cutoff <= known_at` is a **CHECK**, because the converse is incoherent: a prediction recorded at T cannot have been entitled to knowledge from after T.

Reading historical predictions uses `predictions_as_of(cutoff)` — the sixth wrapper, delegating to `fn_visible_at` and never restating it. That wrapper bounds **transaction time only**; a reproducibility read must filter `data_cutoff` itself, because "which predictions had we produced by T" and "what were they allowed to know" are different questions.

**No prediction may use** results or statistics after its `data_cutoff`, identities invisible at the relevant knowledge time, or future information introduced by a repository query. The eligible-fixture query deliberately **does not join `match_results`**: a fixture is eligible because of when it is scheduled, never because of whether we happen to know its score.

## 3. Identity and idempotency

**Identity is `(fixture_id, model_version, profile, data_cutoff)`.** `known_at` is not part of it: regenerating the same prediction tomorrow is the same prediction, not a new one.

A rerun is **content-compared**, and the answer is one of three:

| outcome | when | effect |
|---|---|---|
| `unchanged` | identity and numbers both match | **nothing is written** |
| `created` | no current row for that identity | insert |
| `revised` | identity matches, numbers differ | supersede the old row, insert the new one |

`revised` happens when a result *underneath* the cutoff was corrected, which legitimately changes what the model should have said. There is no `latest` column and **no probability is ever overwritten in place**: a historical prediction stays readable at its own cutoff forever. This is the `fixture_schedule` pattern, unchanged.

A different cutoff, or a different profile, is a **different generation** — both persist side by side.

## 4. The production job

```
uv run python -m engine.jobs.generate_predictions
uv run python -m engine.jobs.generate_predictions --data-cutoff 2024-01-15T00:00:00Z
```

Follows the established job shape: open a `job_runs` row, work, close it with counters. Two invocations, one code path:

- **Production** — no `--data-cutoff`. The cutoff is now; eligible fixtures are those that have not kicked off.
- **Historical** — an explicit cutoff. Eligible fixtures are those kicking off at or after it, and the model is fitted on what was known before it.

**"Now" never leaks into the football features.** The wall clock is used for exactly two things — the default cutoff and `known_at` — and both are passed in explicitly. The estimator receives observations already filtered by `data_cutoff` and has no other route to the present.

**One fit per run, not one per fixture.** Every fixture in a run shares the cutoff, so they share the training set and therefore the model. Fitting once is not an approximation of fitting per fixture; it is the same computation performed once.

**One transaction for the batch.** A partially written generation would be indistinguishable from a complete one that predicted fewer fixtures.

The job **fails rather than guessing** when fewer than 30 training matches precede the cutoff.

## 5. Known limitations

**There are currently no unplayed fixtures, so a production invocation predicts nothing.** All 2,280 canonical fixtures are `ft` with a result, and the most recent kickoff is 2025-05-25. football-data.co.uk publishes **only completed matches** (`PHASE-0-SPEC.md` §16.8), so the corpus contains no future fixture to predict. The job reports this explicitly rather than exiting silently. The historical-cutoff mode is fully exercised, and the production mode becomes useful the moment a fixture-list provider is added — that is a provider question, not a pipeline one.

**Cold starts are frequent at early cutoffs.** A club with no prior appearance in the corpus is predicted at league average and flagged; at a January 2024 cutoff that is every Ipswich fixture in 2024/25.

**One league, one profile per run.** No multi-league orchestration, no scheduler, no automation — all deliberately out of scope.

**`fit_metadata` omits the per-team ratings.** They are recomputable from the cutoff and configuration, and storing 27 of them per fixture would write the same table 380 times per run.

---
