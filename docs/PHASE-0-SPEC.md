# Phase 0 Specification — Provenance, Bitemporality, Providers, Reproducibility

Final pre-implementation document. Supersedes the schema in `ARCHITECTURE.md` §3 and incorporates the corrections from `DECISIONS-01.md` §6.
Written 2026-09-06. **No application code is written until this is signed off.**

---

# 1. Data provenance and lineage

## 1.1 The chain we must be able to walk

```
prediction
  └─ model_version ─── training_run ─── dataset_snapshot ─┐
  └─ feature_snapshot (data_cutoff, input_manifest_hash)  │
        └─ [as-of query over bitemporal facts] ───────────┴─→ fact revisions
              └─ raw_payload_bodies (exact bytes)
                    └─ raw_payloads (the fetch that observed them)
                          └─ job_runs
                                └─ data_sources
```

## 1.2 The design decision that makes this affordable

The naive approach enumerates every fact a prediction consumed in a join table. A single prediction reads the last 10 matches for both teams plus league-wide context — call it 200–400 fact rows. At 5,000 fixtures × several model versions that is hundreds of millions of lineage rows carrying almost no information, because the *same* facts are read over and over.

**Instead: lineage is recorded at the feature-snapshot level and proven by reconstruction, not enumeration.**

A feature snapshot stores its `data_cutoff` and an `input_manifest_hash` — the hash of the ordered set of `(table, fact_id, revision)` tuples the feature build actually read. Because the fact tables are bitemporal (§2), the as-of query is deterministic: re-running it at the same cutoff must return the same rows and therefore the same hash. Matching hashes *prove* lineage without storing it.

Enumeration remains available as an opt-in: `feature_snapshot_inputs` is populated only when the model version is flagged `audit_lineage = true`, or for a sampled percentage of predictions. Use it for debugging, not as the default.

This is the whole trick: **store a hash, not a graph. Reconstruct the graph on demand.**

## 1.3 Minimum structures required

**Three columns on every fact revision row** — this is the fact→source edge and there is no substitute for it:

| Column | Purpose |
|---|---|
| `source_id` | which provider asserted it |
| `raw_payload_body_id` | the exact bytes it was parsed from — a single-column FK to the unpartitioned `raw_payload_bodies` (§9.1) |
| `known_at` | when we learned it |

**Five tables** *(amended 2026-09-07 — the raw-ingestion tables are specified in full in §9; that section governs)*:

1. **`data_sources`** — provider registry. Root of all provenance.
2. **`job_runs`** — one row per invocation of one job against one source: job name, scope, source, **adapter version**, params, timing, status, counts. The adapter version matters: a parsing bug is a provenance question, and you must be able to find every row a broken adapter produced. *(Replaces the separately-planned `ingestion_runs`; see §9.2.)*
3. **`raw_payload_bodies`** + **`raw_payloads`** — the archive, split into content and observation. Bodies are content-addressed and globally deduplicated; observations are one row per fetch, partitioned monthly. The split is not stylistic: PostgreSQL cannot enforce a global unique constraint on a partitioned table, and a partitioned table cannot be the target of a single-column foreign key. See §9.1.
4. **`feature_snapshots`** — `data_cutoff`, `feature_set_version`, `features`, `input_manifest_hash`, `completeness`, `missing_fields`.
5. **`dataset_snapshots`** — the training-set identity: a `spec` (competitions, date range, filters) plus an `as_of` timestamp, a `row_count` and a `content_hash`. **It does not copy the data.** A training set is defined by a query and a point in time; the bitemporal tables make that definition reproducible, and the hash makes it verifiable.

Plus **`training_runs`** joining a model version to its dataset snapshot, hyperparameters and seed (§5).

`feature_snapshot_inputs` is a sixth, optional table — sampled, not universal.

## 1.4 The two questions provenance must answer

- *"Why did the model predict this?"* → `prediction → feature_snapshot → features jsonb`. Immediate, no reconstruction needed.
- *"Was that input correct, and where did it come from?"* → re-run the as-of query at `data_cutoff`, land on fact revisions, each carrying `raw_payload_body_id` → the original bytes, and through `raw_payloads` the fetch and run that observed them.

If the recomputed manifest hash does not match the stored one, something in the fact history was mutated outside the bitemporal discipline. **That mismatch is the single most important alarm in the system** — it means historical claims can no longer be trusted, and it should page someone.

---

# 2. Bitemporal facts

## 2.1 Three time axes, and which tables need them

| Axis | Column(s) | Meaning |
|---|---|---|
| **Valid time** | `occurred_at`, or `valid_from`/`valid_to` | When the fact was true in the football world |
| **Transaction time — start** | `known_at` | When our system first held this version |
| **Transaction time — end** | `superseded_at` (NULL = current belief) | When we learned a different version |

**Do not bitemporalise everything.** It costs query complexity on every read. Apply it only where providers actually revise:

| Table | Treatment | Why |
|---|---|---|
| `match_results` | **Full bitemporal** | Scores get corrected; matches get awarded or voided |
| `match_stats` | **Full bitemporal** | xG and detailed stats are routinely revised days later |
| `fixture_schedule` | **Full bitemporal** | Kickoff, venue and status change constantly |
| `external_ids` | **Full bitemporal** | Providers merge and reuse IDs |
| `team_names`, `competition_names` | Valid-time only | These change in the world; we rarely learn them *wrong* |
| `countries`, `venues`, `bookmakers` | Plain mutable | Reference data. Versioning it buys nothing |
| `predictions`, `team_ratings` | Append-only + `superseded_at` | Never corrected, only superseded by a newer computation |

## 2.2 Implementation pattern

One table, append-only, with `superseded_at`. Not a separate history table, not range types with exclusion constraints — those are more machinery for the same guarantee.

- Current row: `superseded_at IS NULL`, enforced by a **partial unique index** on the business key.
- The as-of predicate:
  ```
  known_at <= :cutoff AND (superseded_at IS NULL OR superseded_at > :cutoff)
  ```

**This predicate must never be hand-written.** Wrap it in a SQL function (`fn_as_of(cutoff)`) or a parameterised view per fact table, and make the feature builder physically unable to query the fact tables any other way. **The mechanism is owned and first implemented by P0-06** (§11.1), which creates the first bitemporal table; P0-08 consumes it. Every leakage bug in this system's future is a hand-written variant of that WHERE clause.

**Enforce append-only with column-level grants**, not convention:

```
GRANT INSERT, SELECT ON match_results TO engine_rw;
GRANT UPDATE (superseded_at) ON match_results TO engine_rw;
```

The engine can insert revisions and close old ones. It cannot rewrite a score. Postgres enforces this; no code review can.

## 2.3 Worked example — a result corrected after the match

**Everton vs Liverpool, fixture `F`, kickoff 2026-03-14 15:00Z, full time ≈16:50Z.**

| Time | Event |
|---|---|
| `T1` = 2026-03-14 **17:05Z** | Provider reports **2–1**. We ingest. |
| `T2` = 2026-03-15 **03:00Z** | Nightly `rebuild_ratings` + `generate_predictions` run with `data_cutoff = T2`. Predictions written for the following weekend. |
| `T3` = 2026-03-16 **11:20Z** | Provider corrects the score to **2–2** (a goal was wrongly disallowed in their feed). |
| `T4` = 2026-03-17 **03:00Z** | Next nightly run, `data_cutoff = T4`. |

**Rows in `match_results` after T3** — note that nothing was overwritten:

| revision | ft_home | ft_away | occurred_at | known_at | superseded_at | source | raw_payload |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 1 | 2026-03-14 16:50Z | **T1** | **T3** | S1 | P1 |
| 2 | 2 | 2 | 2026-03-14 16:50Z | **T3** | NULL | S1 | P2 |

Both revisions share the same `occurred_at` — the match happened once. They differ on transaction time, which is exactly the distinction that makes the backtest honest.

**Query 1 — "what did we know at T2?"** (reproducing the prediction)
`known_at <= T2 AND (superseded_at IS NULL OR superseded_at > T2)`
Revision 1 qualifies (`T1 ≤ T2` ✓, `T3 > T2` ✓). Revision 2 is excluded (`T3 > T2`).
→ **2–1.** The feature snapshot from T2 reproduces exactly, hash and all.

**Query 2 — "what is true now?"**
`superseded_at IS NULL` → **2–2.**

**Consequences, in order:**

1. **The prediction made at T2 is not recomputed.** It was an honest claim given what we knew. Rewriting it would be falsifying the record.
2. **Predictions on fixture `F` itself must be re-settled.** Every `prediction_market` settled against 2–1 is now wrong. Insert *new* `prediction_outcomes` rows with their own `known_at`; supersede the old ones. Never update in place.
3. **`model_performance` is recomputed** for affected periods — again as new rows.
4. **An alert fires:** *"Result correction on fixture F: 2–1 → 2–2. 47 prediction_markets re-settled. Model v1.2 accuracy for 2026-W11 revised from 54.1% to 53.8%."* Silent corrections are precisely how a published accuracy figure quietly becomes a lie.
5. **Ratings are not retro-patched.** The T2 rating rows used 2–1 and remain as historical fact. The T4 run recomputes forward with 2–2.

## 2.4 The asymmetry rule — features as-of cutoff, outcomes as-of now

This is subtle and gets it wrong in most hobby systems:

| Purpose | Which revision to read |
|---|---|
| Reproducing a past prediction | **As-of that prediction's `data_cutoff`** |
| Building features for a backtest of a *new* model | **As-of the historical cutoff** — a live model would only have had the wrong value, so using the corrected one is a look-ahead leak |
| Settling outcomes / evaluating accuracy | **Latest known truth** — that is what actually happened |

Backtesting a new model on corrected data inflates results in a way that never shows up as an obvious bug. The `input_manifest_hash` check in §1.2 is what catches it.

---

# 3. Provider bake-off

**Sportmonks is not selected.** The recommendation in `DECISIONS-01` was a starting hypothesis based on published terms and pricing. This section replaces it with measurement.

## 3.1 Test competition — EFL Championship, season 2023/24

Deliberately **not** the Premier League. Every provider covers the Premier League immaculately, so it discriminates nothing. The Championship is the right probe because:

- 24 teams, 552 league matches — a large enough sample for meaningful rates.
- Mid-tier coverage, where providers actually differ.
- **Play-offs include two-legged ties** — directly exercises the identity model from §6.
- Promotion and relegation churn the team set across seasons, exercising entity resolution.
- The season is complete, so ground truth is stable.

Add one **stretch competition** if a provider claims broad coverage — a second-tier league outside the big five — but score only the Championship. The stretch league is pass/fail on coverage existing at all.

## 3.2 Establishing ground truth

We have no oracle, so we build one:

1. Harvest the season from **every candidate provider plus at least one free reference** (football-data.co.uk results, football-data.org).
2. Build a consensus by majority vote per field.
3. **Manually adjudicate every disagreement.** There will be few — likely tens, not thousands — and they are the entire point of the exercise. A field where three providers disagree is a field none of them can be trusted on.
4. Freeze the adjudicated set as `bakeoff_ground_truth`, versioned and committed.

Manual adjudication of a few dozen cells is a couple of hours of work and produces the only trustworthy scoring baseline available.

## 3.3 Objective metrics

Every metric is a number computed by a script, not a judgement.

| Dimension | Metric | Definition |
|---|---|---|
| **Fixtures — recall** | % | ground-truth fixtures present in provider feed |
| **Fixtures — precision** | % | provider fixtures that correspond to a real fixture (catches phantoms and duplicates) |
| **Results — FT** | % | exact full-time score match |
| **Results — HT** | % | exact half-time score match |
| **Kickoff accuracy** | % / % | within ±0 min; within ±15 min |
| **Kickoff offset hygiene** | % | datetimes carrying an explicit UTC offset (see §6 rule 11) |
| **Team identity — completeness** | count | distinct teams reported vs 24 expected |
| **Team identity — resolvability** | % | auto-resolved to canonical without human input |
| **Team identity — stability** | count | provider team IDs that changed between pull 1 and pull 2 |
| **Competition identity** | pass/fail + count | correct competition and season labelling; ID stable across pulls |
| **Stats fill rate** | % per field | non-null shots, SOT, corners, cards, possession, fouls |
| **Stats plausibility** | % | rows passing bounds: `shots ≥ SOT ≥ goals`, possession pair sums to 100 ±1, cards ≤ 11 |
| **xG availability** | % | fixtures with non-null team xG |
| **xG sanity** | ratio | `Σ xG / Σ goals` across the season — should sit near 1.0; a value far off indicates a broken or differently-scoped model |
| **xG agreement** | correlation | Pearson r against another provider's xG on shared fixtures |
| **Lineups** | % / % | fixtures with 11+11 starters; with formation |
| **Events** | % / % | fixtures where goal events reconcile to the final score; events carrying a minute |
| **Historical depth** | count | seasons of this competition actually retrievable (**verified by fetching, not by the docs**) |
| **Update latency** | median, p95 | minutes from full time to result available — measured prospectively over ≥20 live matches |
| **Corrections / silent revision** | count | cells differing between pull 1 and pull 2, taken **14 days apart on the same completed season** |
| **Correction transparency** | pass/fail | does anything in the payload flag that a revision occurred? |
| **API reliability** | % / ms | success rate; p50 and p95 latency; 5xx rate across the whole harvest |
| **Backfill cost** | count | API calls consumed to harvest one full season — the real cost driver |

## 3.4 The test that decides it

**Pull the same completed season twice, fourteen days apart, and diff every cell.**

A completed season from 2023/24 must not change. Any cell that does is a silent revision of settled history. A provider that silently rewrites the past cannot support honest backtesting no matter how good its coverage is, because your point-in-time reconstruction will disagree with theirs and you will never know which is right.

Score: 5 = zero changed cells; 3 = changes present but flagged in the payload; 0 = unflagged changes to scores or fixtures.

## 3.5 Scoring and gates

Apply the Tier-1 gates from `DECISIONS-01.md` §2 first — a vendor failing any of them is out regardless of score. Then normalise each metric to 0–5 and apply the Tier-2 weights.

Two override rules:

- **Any provider scoring 0 on silent revision is disqualified as *primary*.** It may still serve as a cross-validation second source.
- **Fixture precision below 99% is disqualifying.** Phantom and duplicate fixtures poison entity resolution permanently, and no amount of stats depth compensates.

## 3.6 The bake-off *is* Phase 0's integration test

Run it through the real ingest pipeline: real `ProviderAdapter` implementations, real `raw_payloads`, real entity resolution, real reconciliation. It simultaneously selects the provider and proves the architecture works end to end. Do not build it as a throwaway script.

**Output:** a committed `docs/BAKEOFF-RESULTS.md` with the scorecard, the adjudicated disagreements, and a dated decision with reasons.

---

# 4. Odds data model

Modelled separately from football data, sharing only `fixture_id`.

## 4.1 Structure

**`bookmakers`** — `id, slug, name, kind ('bookmaker' | 'exchange' | 'aggregator'), country_scope, commission_rate` (exchanges only), `sharpness_tier`.
Exchanges are not bookmakers: their prices are net of commission and their liquidity is a signal in itself. The `kind` column keeps every downstream calculation honest about that.

**`odds_series`** — the identity of a price series, stored **once**:
`id, fixture_id, bookmaker_id, period ('ft'|'ht'|'2h'), market_type, line numeric NULL, selection`
`UNIQUE (fixture_id, bookmaker_id, period, market_type, line, selection)`

`period` was missing from the v1 schema and is not optional — half-time markets are a different market with the same `market_type` label.

**`odds_ticks`** — append-only, **one row only when the price or availability changes**:
`series_id, observed_at, price numeric, is_available boolean, source_id, raw_payload_body_id`

**`odds_coverage`** — `fixture_id, source_id, first_polled_at, last_polled_at, poll_count`.
Without this, a gap in ticks is ambiguous between "price didn't move" and "we weren't looking". That distinction matters enormously when reconstructing a market.

## 4.2 Why this shape

The v1 flat `odds_snapshots` table repeated the whole `(fixture, bookmaker, market, line, selection)` tuple on every row. Normalising the series out and recording only changes attacks the storage problem from both directions:

- Series normalisation: a tick is `(bigint, timestamptz, numeric, bool, ...)` ≈ 30 bytes instead of ≈ 120.
- Change-only recording: prices are static between moves, so 80–95% of polls write nothing.

Together this turns the ~15–20 GB/year projection from `DECISIONS-01` §6.1 into something in the low single-digit GB, **losslessly** — every price at every instant is still recoverable by step-function interpolation between ticks, bounded by `odds_coverage`.

## 4.3 Closing prices — naming discipline

**A price is only `closing` if the source defines it as closing.** Three distinct things must never share a column:

| `price_kind` | Meaning | Sources |
|---|---|---|
| `provider_closing` | The source publishes an explicit closing price | football-data.co.uk `C`-suffixed columns |
| `exchange_sp` | Betfair Starting Price — a real transacted settlement price | Betfair |
| `last_observed_pre_kickoff` | **Our own last observation before kickoff. Not a closing price.** | Our polling |

Stored on `odds_series` as `reference_price`, `reference_price_kind`, `reference_captured_at`.

Two rules:

- The `last_observed_pre_kickoff` capture is triggered by the fixture status transitioning to `live`, **not by the scheduled kickoff time** — otherwise a rescheduled match captures a price hours early.
- Any CLV computed against `last_observed_pre_kickoff` must be **labelled an approximation everywhere it is displayed or stored**. Only the first two kinds are true CLV.

## 4.4 Calculations

**Implied probability.** Decimal price `d` → `p_raw = 1/d`.
For an exchange with commission `c`, the price is gross but the return is not: effective return on a winning unit stake is `1 + (d−1)(1−c)`. Use the gross price for probability and apply commission in EV, never both or neither.

**Overround.** For the set of mutually exclusive selections in one market: `overround = Σ (1/dᵢ)`. Margin = `overround − 1`. Two-way at 1.90/1.90 → `0.5263 × 2 = 1.0526`, a 5.26% margin.

**De-vigging.** Four methods, and the choice is empirical, not theoretical:

1. **Multiplicative** — `pᵢ = p_rawᵢ / Σp_raw`. The naive default; assumes margin scales with probability.
2. **Additive** — `pᵢ = p_rawᵢ − (Σp_raw − 1)/n`. Assumes margin is spread equally.
3. **Shin** — solves for an insider-trading parameter `z`; empirically better on favourite–longshot bias.
4. **Power** — find `k` such that `Σ p_rawᵢ^k = 1`. Often the best empirical fit.

**Store raw prices only. De-vig at analysis time and record the method used** in `market_consensus.method`. Which method works best depends on your specific book mix and is a Phase 4 experiment, not a Phase 0 decision. Baking one in now is a decision you cannot revisit.

**Market consensus.** Four rules, each of which is a real bug if broken:

- Group by exact `(period, market_type, line)`. **Never average across lines** — a 2.5 total and a 2.75 total are different questions.
- **De-vig each bookmaker individually first, then aggregate.** Averaging raw prices and de-vigging the average is wrong, because books carry different margins.
- **Aggregate in log-odds space**, then renormalise. Averaging probabilities compresses everything toward 0.5.
- **Exclude stale and suspended series** — no tick within N minutes, or `is_available = false`. A suspended market's last price is the least informative number in the dataset, and suspensions cluster at exactly the moments that matter.

Weighting: begin with the sharpest single available book as the consensus. It is simpler than a weighted mean and usually more accurate. Move to weights derived from observed CLV performance only once you have the data to fit them.

**Model edge.** `edge = p_model − p_market_fair`. Also report relative edge `p_model / p_market_fair − 1`, because a 2-point edge means something very different at 5% than at 50%.

**Expected value.** Against the **best available price** `d_best`, not the consensus — the consensus is the truth estimate, the best price is what you can actually take:
`EV = p_model × d_best − 1` per unit stake.
On an exchange: `EV = p_model × (d_best − 1)(1 − c) − (1 − p_model)`.

**Kelly.** `f* = (p_model × d − 1) / (d − 1)`, i.e. `edge / (d − 1)`.
Store the full-Kelly fraction; **display and recommend a quarter of it, capped.** Kelly is extraordinarily sensitive to error in `p_model` — full Kelly on a model that is 3 points overconfident is a ruin strategy. This is why calibration matters more than accuracy for this product.

**CLV.** Evaluate the price you took against the closing fair probability:
`CLV = (d_taken × p_close_fair) − 1`
Positive means you beat the close. Always store `reference_price_kind` alongside, so the number is interpretable — and never present CLV computed from `last_observed_pre_kickoff` as equivalent to CLV against a true closing line.

## 4.5 Known blind spot, recorded deliberately

We capture price but not **stake limits**. At the margin, limits determine whether an edge is real — a 6% edge available for £5 is noise. Record this as accepted and unresolved; do not let it be discovered later as a surprise.

---

# 5. Model reproducibility

## 5.1 What must be stored

**`model_versions`**

| Field | Why |
|---|---|
| `name`, `semver`, `algorithm` | Identity |
| `code_git_sha` | Exact commit |
| `code_tree_clean` boolean | **A dirty working tree may never reach `active`.** A SHA does not identify uncommitted code |
| `env_lock_hash` | Hash of `uv.lock` — numpy and scipy change results at the margin between versions |
| `container_image_digest` | The sha256 of the image, not a mutable tag |
| `feature_set_version` | Which feature builder produced its inputs |
| `hyperparams` jsonb | Complete, including defaults — never rely on a library's default staying constant |
| `random_seed` int | |
| `artifact_uri`, `artifact_sha256` | The fitted parameters, and proof they haven't changed |
| `training_run_id` | |
| `status`, `created_at` | |

**`training_runs`** — `model_version_id`, `dataset_snapshot_id`, `started_at`, `finished_at`, `thread_count`, `env_vars` jsonb, `metrics` jsonb, `log_uri`.

**`dataset_snapshots`** — `spec` jsonb (competitions, date range, filters, minimum-match thresholds), **`as_of`** (the bitemporal cutoff — the single most important field), `row_count`, `content_hash`.

**`feature_snapshots`** — `fixture_id`, `model_version_id`, `feature_set_version`, **`data_cutoff`**, `features` jsonb, `input_manifest_hash`, `completeness`, `missing_fields`, `computed_at`.

**`predictions`** — `feature_snapshot_id`, `model_version_id`, `computed_at` (the prediction timestamp), outputs, `output_hash`, `superseded_at`.

## 5.2 Determinism hazards to control

The obvious ones are seeds and versions. These are the ones that actually bite:

| Hazard | Control |
|---|---|
| BLAS/OpenMP thread count changes floating-point reduction order | `OMP_NUM_THREADS=1`, recorded in `training_runs.thread_count` |
| SQL without `ORDER BY` returns rows in arbitrary order, changing summation order | Mandatory `ORDER BY` in every feature and training query; enforced by review and a lint rule |
| Python hash randomisation affects dict/set iteration | `PYTHONHASHSEED=0` |
| Machine timezone leaking into date arithmetic | Container forced to UTC |
| Library minor-version drift | Lockfile hash pinned and recorded |
| Mutable image tags | Pin by digest |

## 5.3 The reproduction test — two levels

**L1 — Replay.** Re-run the stored `model_version` against the *stored* `feature_snapshot`. Output must match `output_hash` to within 1e-9.
Proves: the model artifact and code are intact.

**L2 — Rebuild.** Re-run the as-of feature query at `data_cutoff`, recompute `input_manifest_hash`, compare; then run the model on the rebuilt features and compare to stored outputs.
Proves: the entire chain — that the bitemporal history was not mutated, that no leakage was introduced, and that a six-month-old prediction can be regenerated from source.

**L1 must pass for every prediction, always. L2 runs nightly on a sample and in full before any model promotion.** An L2 failure with an L1 pass means the fact history changed underneath us — that is the §1.4 alarm.

`reproduce.py --prediction-id X` implements both and prints a verdict. **Build the harness in Phase 0 against a placeholder model**, while it is cheap. Proving reproducibility after you have a real model and six months of predictions is enormously harder.

---

# 6. Database stress test — concrete schema rules

| # | Case | Rule |
|---|---|---|
| **1** | **Rescheduled fixtures** | Identity key **excludes kickoff time**. Schedule lives in bitemporal `fixture_schedule(fixture_id, kickoff_utc, local_date, local_tz, venue_id, status, known_at, superseded_at)`. A kickoff move of **>24h supersedes all predictions** for that fixture with `reason = 'reschedule'`. |
| **2** | **Postponed fixtures** | Status `postponed`. The replayed match is the **same `fixture_id`** with a new `fixture_schedule` revision — never a new fixture. Postponement does not break identity. |
| **3** | **Abandoned fixtures** | Status `abandoned`. No trainable result: `match_results.is_trainable = false`. If a federation **awards** a score, `result_source = 'awarded'` and `is_trainable = false` — awarded scores are administrative outcomes and must never train the goals model. They still settle bets. |
| **4** | **Replayed fixtures** | A replay from 0–0 is a **new fixture row** with `replaces_fixture_id`. Identity: `UNIQUE (season_id, stage, leg, replay_number, home_team_id, away_team_id)`. |
| **5** | **Two-legged ties** | `stage` and `leg` are part of the identity key, so the same pair meeting twice in a season is legal. `tie_id` groups the legs and is where aggregate-score logic lives. `leg ∈ {1,2}`, `tie_id` non-null only for two-legged ties. |
| **6** | **Renamed competitions** | `competition_names(competition_id, name, name_type ∈ {official, sponsored, short}, valid_from, valid_to)`. Canonical name is **sponsor-free**. Render the name valid at the fixture's `local_date`. `seasons.format` jsonb describes structure — **no code may assume a group stage exists.** |
| **7** | **Renamed teams** | `team_names(team_id, name, name_type, valid_from, valid_to, source_id)`. — **[SUPERSEDED 2026-09-08]** `team_names` carries **no `source_id`**: a club's official name is display truth, not a provider's opinion. Provider spellings live in `team_aliases.source_id`; provider primary keys live in `external_ids` (P0-06). See **§10.3**. **`teams` has no name column at all** — removing it makes the correct behaviour the only possible behaviour. Historical pages render the name valid at match date. |
| **8** | **Dissolved / recreated clubs** | `teams.status ∈ {active, dissolved, merged}`, `succeeded_by_team_id`, `continuity ∈ {legal, sporting, none}`. A phoenix club is a **new `team_id` by default**; asserting statistical continuity is an explicit, recorded, reversible decision. **Never delete a team row** — tombstone it. |
| **9** | **Provider ID changes** | `external_ids` is bitemporal with `confidence` and `last_verified_at`. A nightly job re-checks the provider's current name for each mapped ID against our alias set; a mismatch drops confidence and files a review item. **Never auto-remap.** |
| **10** | **Duplicate provider records** | Many external IDs → one internal ID is legal and expected. One external ID → two internal IDs is blocked by `UNIQUE (source_id, entity_type, external_id)` where `superseded_at IS NULL`. Cross-provider duplicates go to `fixture_match_candidates`, scored on (teams, ±3 days, competition), and are **never auto-merged when ambiguous**. |
| **11** | **Timezones** | Store `kickoff_utc timestamptz` + `local_date date` + `local_tz` (IANA). **Reject any ingested datetime lacking an explicit UTC offset** — never infer one. `local_date` is computed and stored at ingest, not generated, because a competition's timezone can itself change. DST is handled automatically by storing instants; the hazard is providers sending wall-clock time, which the rejection rule catches. |
| **12** | **Missing data** | `competition_coverage(competition_id, season_id, field, availability ∈ {always, partial, never}, verified_at)`. — **[SUPERSEDED 2026-09-08]** the *rule* stands unchanged; the `competition_coverage` **table** is not built in P0-05 and has no replacement. It was never assigned to a task. Reassign to the provider/ingestion task that needs it. See **§10.9**. NULL means "not provided" and **no sentinel value is ever substituted**. Models declare `required_features`; a fixture whose coverage cannot satisfy them **receives no prediction rather than a silently degraded one**. |
| **13** | **Late-arriving data** | `fixtures.stats_complete_at` is set when every field the coverage profile marks `always` is present. Rating jobs are **watermark-driven** — they process fixtures where `stats_complete_at > last_watermark`, never `date = yesterday`. Late data moves ratings forward and never retro-edits a published prediction. |
| **14** | **Corrected data** | Bitemporal insert-and-supersede. `UPDATE` is forbidden on fact tables except to set `superseded_at`, enforced by **column-level GRANT**. Every correction re-settles affected outcomes as new rows and raises an alert naming the counts. |
| **15** | **Odds price changes** | A tick is written **iff** `(price, is_available)` differs from the series' latest tick. `odds_coverage` records polling windows so that an absence of ticks is interpretable rather than ambiguous. |

---

# 7. Development environment

## 7.1 PostgreSQL version — pinned

**The local development database is PostgreSQL 17.6, pinned to the exact patch version.**

- Pin the patch, never a floating `17` or `latest` tag. An unpinned tag means two developers can silently run different servers, and "reproducible" stops meaning anything the first time behaviour differs between them.
- 17.x is chosen to match the Supabase platform default, so local and production do not diverge on server behaviour.
- **Production/Supabase compatibility must be verified against this exact version before any deployment.** Supabase's default moves over time; when it does, this pin is updated deliberately, as a recorded decision, and the local database is rebuilt — not left to drift.
- Raising the pin is a schema-affecting change. Treat it like a migration: verify, then update this section.

## 7.2 Connection and ports

| Setting | Default | Override |
|---|---|---|
| Port | **5433** | `POSTGRES_PORT` |
| Database | `fpp` | `POSTGRES_DB` |
| User | `fpp` | `POSTGRES_USER` |
| Password | `fpp_local_dev` (local only, not a secret) | `POSTGRES_PASSWORD` |
| Full URL | `postgresql://fpp:fpp_local_dev@localhost:5433/fpp` | `DATABASE_URL` |

**The local database listens on 5433, not the PostgreSQL default 5432.** This avoids colliding with a Postgres already installed on the host — a collision that otherwise presents as a confusing authentication failure against the wrong server. `POSTGRES_PORT` overrides it; `DATABASE_URL` overrides the whole connection string and is what CI and containers set.

Server timezone is forced to UTC and the database is initialised with `--locale=C`. Locale drift changes index ordering between machines, which would undermine the reproducibility guarantees in §5.

## 7.3 Which checks need a running database

**`pnpm verify` does not require Docker.** It runs typecheck, lint and the default test suites only. A developer working on TypeScript must not be blocked by a database they are not touching.

Database-dependent tests are marked `db` and **deselected by default** (`addopts = "-m 'not db'"`). They are run explicitly:

```bash
pnpm db:up          # docker compose up -d --wait
uv run pytest -m db # or: pnpm py:test:db
pnpm db:check       # TypeScript round-trip
pnpm db:reset       # down -v && up: rebuilds a clean database
```

**CI must run the DB test suite against a real PostgreSQL service** at the pinned version — both the Python `-m db` suite and the TypeScript round-trip. Opt-in tests that no automated job ever runs rot silently, and this suite is the only thing standing between us and a connection layer that quietly stops working.

---

# 8. Migration harness and schema ownership

Everything in this section marked **[PROVEN]** was established empirically against **drizzle-kit 0.31.10 / drizzle-orm 0.45.2 / PostgreSQL 17.6** on 2026-09-06, in an isolated experiment. Where this section contradicts `DECISIONS-01.md` §1, **this section is correct and `DECISIONS-01.md` is retained unchanged as a historical decision record.**

## 8.1 Schema ownership — two directories

Objects are split by *who owns their DDL*, and the split is physical:

| Path | Owns | Seen by `generate`? |
|---|---|---|
| `packages/db/src/schema/**` | Tables whose DDL Drizzle can express and generate | **Yes** — this is the config's `schema` glob |
| `packages/db/src/raw-sql/**` | Typed `pgTable` declarations for objects whose physical DDL is maintained by `--custom` SQL migrations | **No** |

**The `drizzle.config.ts` `schema` glob points at `packages/db/src/schema/**` and nothing else.** That single fact is what keeps raw-SQL-owned objects out of the generated diff.

Declarations under `raw-sql/` are ordinary `pgTable` definitions. They remain **fully typed and queryable** through Drizzle — select, insert, joins, inferred row types — because typing is a property of the declaration, not of the config glob. They are simply invisible to `generate`. **[PROVEN]** — a partitioned table declared this way accepted an insert through Drizzle, routed correctly to its partition, and left `generate` reporting *"No schema changes"*.

Objects that belong in `raw-sql/`: partitioned tables (`raw_payloads`, `odds_ticks`), materialised views (`standings`), and anything else whose DDL Drizzle cannot express.

**Withdrawn:** the earlier prescription — declare raw-SQL-owned tables in the generated schema and exclude them with `tablesFilter` — **does not work and must not be used.** **[PROVEN]** declaring a partitioned table in the `schema` glob makes `generate` emit a plain, *unpartitioned* `CREATE TABLE` for it, and adding `tablesFilter` produces byte-identical output.

## 8.2 `tablesFilter` — what it actually does

**`tablesFilter` does not filter the schema-input side of `drizzle-kit generate`. It applies to database introspection operations — `pull` and `push`.** **[PROVEN]** across three pattern forms (`["!excluded"]`, `["allowed"]`, `["glob*"]`); every one still emitted the excluded table.

Its real role, confirmed by control: with `tablesFilter` set, `push` left a database-only table alone; **without** it, `push` dropped that table. It protects *database-side* tables from tooling that reads the database.

Therefore:

- **It is not a mechanism for excluding tables from `generate`.** Use the directory split in §8.1.
- **Retain it only for pull-based database inspection** (§8.3), where it is genuinely needed to stop raw-SQL-owned objects registering as drift.
- `push` remains banned regardless (it skips RLS policies, and as shown above it drops unmanaged tables).

## 8.3 Drift — two different concerns, one of which we do not yet detect

These are not the same thing and must never be described as if they were.

**Schema-input consistency** — what `generate` checks:

> `drizzle-kit generate` detects drift between the Drizzle schema input and Drizzle's migration snapshot/journal state. **It does not inspect PostgreSQL and therefore cannot detect arbitrary database-side drift.**

**[PROVEN]** `generate` compares `schema.ts` against the latest `meta/*_snapshot.json` only. It ran successfully with the database container **stopped**; it reported no changes after the managed table was dropped from the database; and it re-emitted a `CREATE TABLE` when the table was stripped from the snapshot while the journal was left intact.

What the empty-diff check does and does not catch **[PROVEN]**:

| Change | Detected |
|---|---|
| Schema input changed without generating a migration | **Yes** |
| Migration journal edited | No — and `migrate` also reported success |
| Database column added by raw SQL | No |
| Raw-SQL-owned object altered, partition dropped | No |

**Database-side drift is a separate, currently-unaddressed concern.** Detecting it requires all three of:

1. `drizzle-kit pull` / introspection into a scratch location, diffed against expectation — this is where `tablesFilter` earns its place;
2. **explicit invariant checks for raw-SQL-owned objects** — nothing in the Drizzle toolchain knows a partitioned table should still be partitioned (`db:verify-partitions`, P0-04);
3. CI running both.

Only (2) is scheduled in Phase 0. (1) and (3) are deferred and must be recorded as a known gap, not assumed.

## 8.4 Migration conventions

| Concern | Convention |
|---|---|
| **Migration directory** | `packages/db/drizzle/` — the drizzle-kit default `out`, containing `NNNN_*.sql` and `meta/`. No override; the default is the simplest thing that works |
| **SQL migration naming** | Always pass `--name` with a descriptive slug: `pnpm db:generate --name=add_raw_payloads_partitions` → `0003_add_raw_payloads_partitions.sql`. Drizzle's auto-generated random names (`0000_faulty_vector`) are unreviewable and are not acceptable in this repo |
| **TS ↔ SQL casing** | Database is `snake_case`; TypeScript identifiers are `camelCase`; **the SQL name is given explicitly in every column definition** — `bodyHash: text("body_hash")`. **[PROVEN]** to work. A global `casing: "snake_case"` config option exists and may replace this if P0-03 verifies it, but explicit names are the default because they are greppable and immune to config changes |
| **Custom migrations** | `pnpm db:generate --custom --name=<slug>`, which scaffolds a file containing only a comment header. Separate statements with `--> statement-breakpoint` |
| **Custom SQL location** | **Inline, in the migration file itself.** Not maintained as a separate source file and copied in — that creates two copies and a synchronisation problem. Migrations are immutable once applied, so the migration file is the only correct home |
| **Review** | Every generated and custom migration is read by a human before merge. Migrations are reviewed artefacts, not build output |

## 8.5 Rollback policy

**Drizzle does not generate down migrations, and we are not building a rollback framework.** That is a deliberate decision, not an omission. Three distinct situations, three distinct answers:

**1. Forward corrective migration — the only production mechanism.**
A mistake is corrected by writing a new migration that moves forward. There is no reverse migration for a deployed schema change.

**2. Local development database reset — total and cheap.**
`pnpm db:reset` (`docker compose down -v && up`, then migrate). Local databases hold no data worth preserving in Phase 0, so reset is always available and always preferred over hand-repairing a local schema.

**3. Production rollback — restore, not reverse.**
Point-in-time restore from managed backups, never migration reversal. Reversing a migration that has already accepted writes loses data; restoring is honest about what is happening.

What makes forward-only safe is the rule already stated in `ARCHITECTURE.md` §8: **migrations are expand-then-contract and must remain compatible with the previously deployed application version.** An application rollback then never requires a schema rollback. Destructive changes (`DROP COLUMN`, `DROP TABLE`) ship as their own separately reviewed migration, never bundled with an additive one.

**Known limitation, recorded deliberately [PROVEN]:** Drizzle does not verify the integrity of already-applied migrations. Editing a migration file after it has been applied is silently ignored by `migrate` — no error, no re-application. Migration immutability is enforced by code review, not by tooling.

## 8.6 PostgreSQL version

**PostgreSQL 17.6, pinned to the exact patch** — see §7.1 for the full rule and the Supabase-compatibility requirement. The migration harness is developed and verified against this version.

---

# 9. Raw ingestion and provenance model (P0-04)

Amends §1.3. Approved 2026-09-07 after a read-only design review. Claims marked **[VERIFIED]** were proved against PostgreSQL 17.6 in a disposable database; the repository was not modified by those experiments.

## 9.1 Content and observation are separate tables

| Table | Row means | Partitioned | Dedup |
|---|---|---|---|
| `raw_payload_bodies` | one distinct response body | **No** | **`UNIQUE (hash_algo, body_hash)`** — global |
| `raw_payloads` | one observed fetch | **Yes** — monthly by `fetched_at` | **None** |

The split is forced by PostgreSQL, not preference. **[VERIFIED]** a unique constraint on a partitioned table must include every partitioning column (`ERROR: unique constraint on partitioned table must include all partitioning columns`), so a partitioned `raw_payloads` cannot carry a global dedup key. **[VERIFIED]** a foreign key cannot reference a partitioned table by `id` alone (`ERROR: there is no unique constraint matching given keys`), so a partitioned archive could never be the single-column FK target that §1.3 requires of every fact row.

Keeping bodies unpartitioned solves both, and preserves the property §1.3 depends on: **a future fact references one column, `raw_payload_bodies.id`, on an ordinary table.** Preserving that is the entire purpose of the split; the bitemporal fact model (§2) is otherwise unchanged.

**[VERIFIED]** a partitioned `raw_payloads` can carry ordinary foreign keys to `data_sources`, `job_runs` and `raw_payload_bodies`. All three are declared once on the parent and automatically inherited by every partition, including `DEFAULT`, and all three are enforced on insert. `ON DELETE RESTRICT` on `body_id` blocks deletion of a referenced body, from a monthly partition and from `DEFAULT` alike, while an unreferenced body deletes normally:

```
ERROR:  update or delete on table "raw_payload_bodies" violates foreign key
        constraint "raw_payloads_body_id_fkey" on table "raw_payloads"
DETAIL:  Key (id)=(1) is still referenced from table "raw_payloads".
```

Retention can therefore never silently orphan cited evidence.

## 9.2 `job_runs` absorbs `ingestion_runs`

**One `job_runs` row = one invocation of one job against one source.** A provider *request* is not a run — it is a `raw_payloads` observation, carrying its own `fetched_at` and `http_status`. Two levels, no third.

`ingestion_runs` is not built. It expressed the same concept under a different name, and Phase 0 has no scheduler to justify separating scheduling from ingestion. `source_id` and `adapter_version` are nullable columns on `job_runs`, set for ingest jobs and null for others. Split only if a single job genuinely fans out across several sources.

- **Request retry** → another `raw_payloads` row. A 429 or 503 is evidence about the provider and is archived like any other response.
- **Run retry** → a new row, `attempt` incremented, same `(job_name, scope_key, run_date)`.
- **Partial failure** → `status = 'partial'`, counts in `stats`. **A failing run never rolls back payloads already written.** Evidence survives the failure of the process that collected it.

## 9.3 Append-only evidence, with no mutable counters

`seen_count` and `last_seen_at` are **removed from the schema**. They were mutable columns on immutable evidence, which contradicted append-only.

Seen-counts are **derived from observations**, through a documented view:

```
v_raw_payload_seen(body_id, seen_count, first_seen_at, last_seen_at)
  = aggregate over raw_payloads grouped by body_id
```

Consequently every column of `raw_payload_bodies` and `raw_payloads` is immutable once written. There is no `superseded_at` on either: **evidence is never corrected.** A provider issuing a correction sends new bytes, which are a new body and a new observation. Supersession belongs to derived facts, never to the archive — facts are beliefs and beliefs get revised; payloads are what the provider actually said.

Enforced by grant, not convention: `engine_rw` receives `SELECT, INSERT` on both tables and **no `UPDATE` and no `DELETE` at all**. `job_runs` is the sole exception, receiving `UPDATE (status, finished_at, stats, error)` so a run can be closed.

## 9.4 Body representation — exact semantics

Ambiguity here silently corrupts deduplication, so each term is defined:

| Term | Definition |
|---|---|
| **response content** | the response body **after transport decoding** — after `Content-Encoding: gzip` has been undone, before any parsing |
| **`body_hash`** | **SHA-256 of the response-content bytes.** Lowercase hex, 64 characters |
| **`hash_algo`** | names the algorithm (`sha256`) so it can be migrated without redefining the column |
| **`body`** | when retained, **exactly the response-content bytes** — the same bytes that were hashed |
| **`byte_size`** | the length in bytes of the response content, i.e. of what was hashed. Not the transfer size, not the stored size |

Four rules follow, and each exists because the opposite is a plausible mistake:

1. **JSON is never canonicalised before hashing.** Canonicalisation would make the hash depend on a canonicaliser whose output can change with a library upgrade, silently invalidating every historical hash. Determinism across time beats deduplication efficiency. A provider that reorders keys will defeat dedup; if that is ever observed, add a second hash column rather than redefining this one.
2. **`Content-Encoding` is metadata about the transport, not about our storage.** It is not recorded as a property of `body`.
3. **Never label `body` as gzip merely because the HTTP response was gzipped.** `body` holds decoded content. Any compression we apply for storage is our own choice, recorded separately if and when we apply it — it is not inherited from the response.
4. **One size, unambiguously.** `byte_size` measures the hashed content. No second size column is added.

## 9.5 The `DEFAULT` partition and its invariant

`DEFAULT` exists as a **data-safety fallback**. **[VERIFIED]** without one, a row outside every bound is rejected outright (`ERROR: no partition of relation found for row`) and the payload is lost. With one, the payload survives and only the partitioning degrades.

**`DEFAULT` must remain empty for any timestamp inside the pre-created range.** A row landing there means a month is missing — and it is not merely untidy: **[VERIFIED]** once a row for October sits in `DEFAULT`, attaching October's partition fails (`ERROR: updated partition constraint for default partition would be violated by some row`). Recovery requires detaching `DEFAULT`, relocating rows, and reattaching. **An unexpected row in `DEFAULT` is a failure, not a warning.**

`db:verify-partitions` must assert:

1. the parent is partitioned (`relkind = 'p'`) and its key is `fetched_at`;
2. **`DEFAULT` exists**;
3. **`DEFAULT` is empty**;
4. the expected monthly bounds are present and **contiguous**, compared by bound (`pg_get_expr(relpartbound, oid)`) rather than by name — a partition dropped and replaced with wrong bounds must not pass;
5. a **routing probe** for the current covered month reaches that month's partition — `BEGIN; INSERT; assert tableoid; ROLLBACK`, which works under append-only grants because nothing commits;
6. any unexpected row in `DEFAULT` **fails the check**.

This is a live PostgreSQL invariant check. It is not a variant of `db:verify-generate`, which never opens a connection (§8.3).

## 9.6 Partition creation policy

- **Pre-create 12 months** in the migration that creates the table.
- **`DEFAULT` remains** permanently, as the safety net above.
- **Phase 0 has no partition scheduler**, because Phase 0 has no scheduler at all (§G). Nothing creates month 13.
- **Phase 1 owns automatic future-partition creation**, alongside the rest of the job scheduling.
- Until then, **accumulation in `DEFAULT` must be observable**: `db:verify-partitions` is the alarm, and it must be run — not merely available.

## 9.7 Migration and key conventions

**Ownership** (§8.1): `data_sources`, `job_runs` and `raw_payload_bodies` are Drizzle-managed under `src/schema/**`. `raw_payloads`, its partitions, `DEFAULT`, the view and the grants are raw-SQL-owned, declared for typing under `src/raw-sql/**` and created by `--custom` migrations in the same ledger.

**Primary keys — a convention, not a technical requirement:**

- **UUID** for registry and reference entities (`data_sources`, and later `competitions`, `teams`, `seasons`) — stable, non-guessable, safe to mint outside the database.
- **`bigint` identity** for append-only, log-style, high-volume records (`job_runs`, `raw_payload_bodies`, `raw_payloads`) — smaller, ordered, index-friendly.

Either would work for either. The rule exists so the choice is not re-argued per table.

**Index caveat [VERIFIED]:** `CREATE INDEX CONCURRENTLY` cannot be used on a partitioned table (`ERROR: cannot create index on partitioned table concurrently`), and cannot run inside a transaction, which the migrator always uses. Plain `CREATE INDEX` on the parent inside a transaction works and cascades to partitions. Harmless while tables are empty; adding an index to a populated partitioned table later will need an out-of-band procedure, not a migration.

## 9.8 Retention — enabled, not implemented

**No retention mechanism is built in P0-04.** No policy, no reaper, no configuration.

Two schema decisions are made now because they are expensive to retrofit:

1. **`body` is nullable.** Future tiering must be able to drop the payload bytes while preserving `body_hash`, `byte_size` and all provenance metadata. `NOT NULL` would foreclose that.
2. **`ON DELETE RESTRICT`** on `raw_payloads.body_id`, so retention can never outrun referential integrity.

## 9.9 P0-03 harness objects

`harness_managed` and `harness_partitioned` **stay for now.** They are removed only after `db:verify-partitions` is green against the real P0-04 objects, and then through a forward migration (§8.5) — never by editing the existing ledger. Removing them before the real partitioning is verified would discard the only working proof of the pattern.

---

# 10. Canonical entity layer (P0-05)

Approved 2026-09-08 after a read-only design review. Claims marked **[VERIFIED]** were proved against drizzle-kit 0.31.10 / drizzle-orm 0.45.2 / PostgreSQL 17.6 in a disposable database; the repository was not modified by those experiments.

Eight tables: `countries`, `venues`, `competitions`, `competition_names`, `seasons`, `teams`, `team_names`, `team_aliases`.

## 10.1 Countries are football associations, not sovereign states

England, Scotland, Wales and Northern Ireland have **no ISO 3166-1 alpha-2 code** — ISO gives them `GB`. All four are launch competitions. Gibraltar, the Faroe Islands, Curaçao, Hong Kong and Macau are FIFA members without sovereign status.

Therefore `iso_alpha2` and `fifa_code` are both **nullable**, and neither is the identifier. Identity is a UUID key with a stable `slug`.

**`competitions.country_id` is nullable** — the Champions League and the World Cup have no country. `confederation` classifies those instead.

Countries remain **plain mutable** reference data (§2.1). Türkiye and North Macedonia renamed inside the data window, but a country name is cosmetic: it is no model input and it rewrites no fact.

## 10.2 Competition, season, edition, stage

| Concept | Lives on |
|---|---|
| Competition identity — the continuing entity | `competitions` |
| Season **and** edition — one running of it | `seasons` (**the same thing; do not model both**) |
| Stage, leg, replay | `fixtures` (P0-07), per §6 rule 5 |

Stable attributes on the competition: `country_id`, `type`, `tier`, `confederation`, `gender`, `age_group`, `is_reserve_competition`, `slug`. Per-edition attributes on the season: dates, `format` jsonb, `is_current`, `label`.

**`gender`, `age_group` and reserve classification appear on both `competitions` and `teams`, and are not decoration.** Without them the Women's Super League and the Premier League differ only by a name string, and a U21 or reserve side resolves onto its senior club — which §6 rule 8 forbids. `teams.parent_team_id` makes the reserve relationship explicit rather than inferred from a name suffix.

Seasons: `UNIQUE (competition_id, label)`, dates as `date` (a season has no clock; fixtures do). Apertura and Clausura are two seasons of one competition with distinct labels and non-overlapping dates. **No season date-overlap constraint** — playoff tails and split-season boundaries would trip it for no benefit.

## 10.3 Names, aliases, and the difference between them

**`team_names` and `competition_names` are display truth. `team_aliases` are matching strings.** Resolution searches both; they are separate tables because one carries temporal meaning and the other does not.

Valid intervals are **half-open `[valid_from, valid_to)`** — inclusive start, exclusive end, matching the `superseded_at > cutoff` convention in §2.2. `valid_to IS NULL` means current.

Overlap is legitimate **across** `name_type` (a competition has an official name and a sponsored name at once) and forbidden **within** one. That is enforced by partial unique index, not by an exclusion constraint (§10.4).

An alias must never silently merge two clubs. "Barcelona" matches FC Barcelona and Barcelona SC; "Arsenal" matches Arsenal FC, Arsenal Tula and Arsenal Sarandí. **Ambiguous resolution must fail rather than choose.** Alias strings therefore cannot be globally unique.

`team_aliases.source_id` records **which provider contributed a spelling** and is nullable. This is not an external-ID table: aliases hold human-readable strings, `external_ids` holds opaque provider primary keys. `team_names` and `competition_names` carry **no `source_id`** — a club's official name is not a provider's opinion.

`normalized_alias` is normalised **in application code**. `unaccent()` is not immutable and cannot back a generated column without wrapping it; avoid the extension entirely.

## 10.4 Partial unique indexes, not exclusion constraints

Forbidding overlapping history would need `EXCLUDE USING gist (... WITH &&)`, which requires the `btree_gist` extension and is not Drizzle-expressible. A partial unique index guarantees the case that actually matters — exactly one *current* row — with no extension and no raw SQL.

**[VERIFIED]** all three patterns generate from `uniqueIndex(...).where(...)` and enforce correctly:

```sql
CREATE UNIQUE INDEX "team_names_current_idx"
  ON "team_names" USING btree ("team_id","name_type") WHERE "valid_to" IS NULL;
CREATE UNIQUE INDEX "competition_names_current_idx"
  ON "competition_names" USING btree ("competition_id","name_type") WHERE "valid_to" IS NULL;
CREATE UNIQUE INDEX "seasons_one_current_idx"
  ON "seasons" USING btree ("competition_id") WHERE "is_current";
```

Proven against PostgreSQL 17.6: two historical rows for the same `(team, name_type)` are allowed; a historical row plus a current row is allowed; a **second current row is rejected**; and a current `official` name coexists with a current `common` name.

**[VERIFIED] operational gotcha:** unique *indexes* are not deferrable, and **a single-statement current-season flip is order-dependent**. *(Corrected 2026-09-08 during P0-05 implementation: an earlier revision of this paragraph claimed such a flip always fails. It does not.)*

It may raise —

```
ERROR: duplicate key value violates unique constraint "seasons_one_current_idx"
```

— if the replacement row is updated before the existing current row is cleared, or it may succeed, depending on update order. Both outcomes were reproduced on PostgreSQL 17.6 by varying only the physical row order; the index invariant (at most one current row) holds either way.

**Therefore current-season handovers MUST use two ordered statements: first clear the existing current row, then set the replacement row current.** The same applies to closing and opening a name.

That a one-shot flip can *succeed* is precisely why the rule is mandatory rather than advisory: it will pass in development and fail later on differently-ordered data.

Consequence: **P0-05 needs no exclusion constraints, no extensions and no custom SQL for structure.** All eight tables are Drizzle-managed. The only raw SQL is the grants in §10.6.

## 10.5 Identifier convention, with a recorded exception

- **UUID** — `countries`, `competitions`, `seasons`, `teams`, `venues`. Registry entities, per §9.7.
- **`bigint` identity** — `competition_names`, `team_names`, `team_aliases`. **A deliberate exception:** these are child detail rows, never foreign-key targets from other tables, and `team_aliases` is the hot lookup path during ingestion where a narrower key keeps the index tight.

The UUID choice on the five registries is load-bearing rather than stylistic: `external_ids.internal_id` (P0-06) is a single polymorphic column, so every entity it can map must share one key type.

## 10.6 Historical truth, and where an UPDATE rewrites it

Mutable: all of `countries` and `venues`; `competitions.{tier, is_active}`; `teams.{status, crest_url, succeeded_by_team_id, continuity}`; `seasons.{format, is_current, end_date}`.

Append-and-close: `team_names`, `competition_names`. Enforced by grant — the §9.3 pattern applied to valid-time:

```sql
GRANT SELECT, INSERT ON team_names, competition_names TO engine_rw;
GRANT UPDATE (valid_to) ON team_names, competition_names TO engine_rw;
```

A name row can be **closed but never edited**. Rewriting `name` in place would silently relabel every historical page, with no error raised.

Two hazards grants cannot cover: re-pointing `team_aliases.team_id` at a different club silently re-attributes all future ingestion, and deleting a team destroys history — **never delete a team; tombstone it** (§6 rule 8). The first belongs to the P0-06 review queue.

## 10.7 Slugs are stable public identifiers

Slugs on `countries`, `competitions`, `seasons`, `teams` and `venues` are the public URL identity.

- They **may be corrected freely before public exposure.**
- Once publicly exposed, changing a slug requires a redirect/alias mechanism, because external links and search indexes depend on it.
- **No redirect mechanism is built now**, and slugs are **not** made immutable at the database level now. This is a recorded policy, not an enforced constraint.

## 10.8 What P0-05 does not build

- **No team↔competition participation table.** The set of teams in a season is `SELECT DISTINCT home_team_id, away_team_id FROM fixtures WHERE season_id = ?`. A separate table would be a second source of truth that can diverge from the fixtures it summarises. Participation is derived from `fixtures` (**P0-07**) and materialised by `standings` in Phase 1. Accepted limitation: a team with no fixtures yet is invisible — and before fixtures exist there is nothing to model.
- **No `external_ids`, no `entity_review_queue`** — P0-06.
- **No resolution or fuzzy-matching logic** — P0-12. P0-05 provides only the tables it will read.
- **No provider IDs, columns or enum values** anywhere.
- **No `competition_coverage`** (§10.9).
- **No partitioning.** Every table is small: ~250 countries, ~1k teams and ~5k aliases at launch; ~50k teams and ~1M aliases at a global ceiling. All ordinary tables.
- **No localisation.** No i18n requirement exists; `locale` columns would be speculative.

## 10.9 `competition_coverage` is removed from scope

`competition_coverage` is **not built in P0-05, and has no replacement table.** It appeared in §6 rule 12 and §B without ever being assigned to a task.

The underlying rule stands and is unchanged: NULL means "not provided", no sentinel is substituted, and a fixture whose coverage cannot satisfy a model's required features **receives no prediction rather than a silently degraded one**. Only the mechanism is deferred. If coverage needs explicit modelling, it is assigned to the provider/ingestion task that needs it, at that time.

---

# 11. External identity mapping (P0-06)

Approved 2026-09-08 after a readiness review that identified three blocking gaps and one ownership gap. This section resolves them. It is design only — no schema, migrations or triggers exist yet.

P0-06 builds the join between a provider's opaque primary keys and our canonical UUIDs. P0-04 archives what a provider *said*; P0-05 defines what we *believe*; P0-06 records **which provider key means which canonical entity**. Without it, re-ingesting the same payload cannot find the team it already created.

## 11.1 The as-of mechanism belongs to P0-06

**Ownership moves from P0-08 to P0-06.** *(Corrected 2026-09-08: §D previously assigned the `as_of` SQL function to P0-08, which conflicted with §E's P0-06 acceptance criterion and with §2.2.)*

The reasoning is forced rather than stylistic:

- **P0-06 creates the first bitemporal table.** `external_ids` is bitemporal per §2.1; nothing before it was.
- **§2.2 states the as-of predicate must never be hand-written.**
- **§E requires P0-06's acceptance test to perform an as-of query** — *"the as-of query returns the old mapping for a past cutoff."*

Those three together mean P0-06 cannot be complete without the reusable mechanism. Building `external_ids` first and the mechanism later would require hand-writing the predicate exactly once — which is how the practice starts.

**P0-06 owns and implements the mechanism. P0-08 consumes it** for `match_results` and `match_stats`, and owns neither its design nor its first implementation.

The mechanism encapsulates the approved predicate and nothing else:

```
known_at <= cutoff AND (superseded_at IS NULL OR superseded_at > cutoff)
```

**This predicate must appear in exactly one place.** It must not be duplicated across application code, feature builders or verification scripts — including P0-06's own verification script, which must call the mechanism rather than restate it. Every leakage bug in this system's future is a hand-written variant of that WHERE clause (§2.2).

### Settled shape

*(Determined 2026-09-08 by a disposable probe on PostgreSQL 17.6 against 20,000 seeded rows. Four candidate shapes were built and measured; all four returned identical results at every cutoff, so the choice rests on planning, typing and failure modes rather than correctness.)*

**One shared predicate function — the single definition:**

```sql
fn_visible_at(known_at timestamptz, superseded_at timestamptz, cutoff timestamptz)
  RETURNS boolean
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
```

**One thin wrapper per bitemporal table, which delegates and never restates:**

```sql
<table>_as_of(cutoff timestamptz)
  RETURNS SETOF <table>
  LANGUAGE sql STABLE
  -- body: SELECT * FROM <table> WHERE fn_visible_at(known_at, superseded_at, cutoff)
```

**`LANGUAGE sql` is load-bearing, not incidental.** SQL functions are inlined by the planner; PL/pgSQL functions are optimisation barriers.

### Two shapes were rejected, on evidence

**Rejected — a generic PL/pgSQL `anyelement` function.** It produced an opaque `Function Scan` with no predicate pushdown. With a selective outer filter it materialised **14,999 rows to return 1**, where the wrapper form used an index scan and discarded none. It also loses compile-time type safety: a table without the bitemporal columns fails at *runtime* (`ERROR: column "known_at" does not exist`).

**Rejected — a GUC-based cutoff (`current_setting`) read by parameterised views.** Its planning was acceptable, but the session variable persists beyond the query: **a caller that forgets to set the cutoff silently inherits the previous one**, and under connection pooling that value can come from an unrelated request. In a system whose first rule is leakage prevention, a mechanism whose failure mode is *wrong data rather than an error* is disqualifying.

### Proven properties

- **Plans are identical to hand-written SQL.** Both `fn_visible_at` and the wrapper inline to exactly the filter a hand-written predicate produces.
- **Index pushdown is preserved.** A selective outer filter through the wrapper yields `Index Scan … Index Cond: (external_id = …)`.
- **Column typing is preserved** in full by `RETURNS SETOF <table>`.
- **Both languages call it as an ordinary parameterised relation**, verified in the probe: TypeScript via postgres.js (`SELECT … FROM external_ids_as_of($1)`, returning typed columns with `known_at` as a JS `Date`) and Python via psycopg (`cur.execute('SELECT … FROM external_ids_as_of(%s)', (cutoff,))`). Neither needed driver-specific handling.

### The single-place rule is enforced, not merely stated

- `fn_visible_at` is **the only** object defining the visibility predicate.
- Per-table wrappers **delegate**; they must not restate it.
- Verification scripts **call the mechanism**; they must not reproduce the predicate.
- **CI must contain a catalog assertion** that exactly one object contains the predicate logic. The probe confirmed this is machine-checkable: scanning `pg_get_functiondef` across `pg_proc` reported `restates_predicate = true` for `fn_visible_at` alone, and `false` for every wrapper. Anything that restates it — a new table's wrapper, a verification script, a feature builder — pushes that count above one and fails the build.

Implementation details beyond the above were not proven by the probe and are deliberately left open.

## 11.2 `entity_review_queue` — minimal and generic

**Stays in P0-06.** P0-06 creates the table; **P0-12 populates and operates it.**

The queue records *that a human decision is required*. It performs no matching, holds no algorithm, and is not responsible for entity resolution.

Minimum required capability:

| Capability | Purpose |
|---|---|
| Stable queue-item identity | Reference an item across sessions |
| Entity type | Which canonical registry the item concerns (§11.4) |
| Source | Which provider raised it |
| External identifier | The provider key needing a decision |
| Candidate internal entity UUID, nullable | The proposed match, where one exists |
| Reason / category | Why review is needed (ambiguous, unverified, conflicting) |
| Review status | Open / resolved / rejected |
| Created timestamp | When it was raised |
| Resolved timestamp, nullable | When it was decided |
| Reviewer / resolution metadata | Who decided and what they chose |

**Deliberately excluded:** provider-specific columns, match scores, algorithm parameters, thresholds, and any column whose meaning cannot be determined without inventing a requirement. Where exact semantics are unclear, **the smallest defensible generic design wins** — P0-12 knows what it needs to record and can extend the table by ordinary migration.

The candidate UUID is nullable because the common case at scale is *"this provider key matches nothing we know"*, which has no candidate.

**`reason` is free text, not a closed set.** *(Recorded 2026-09-08 from the P0-06 implementation.)* The categories named above — ambiguous, unverified, conflicting — are **illustrative, not an enumeration.** Constraining `reason` with a `CHECK` would fix P0-12's vocabulary before P0-12 exists, on no authority in this specification. **P0-12 owns the semantics of `reason`** and may narrow it by ordinary migration once it knows what it needs to record. The column is constrained only to be non-empty.

## 11.3 Polymorphic `internal_id`, enforced by trigger

The approved design is retained without modification: **one polymorphic column, `external_ids.internal_id uuid`**, with `entity_type` selecting the canonical registry table.

Explicitly rejected: five nullable typed FK columns; a new entity supertype or registry table; abandoning the polymorphic design. §10.5 already made the UUID consistency of the five registries load-bearing for exactly this.

A single column cannot carry a conventional foreign key to five tables. Integrity is therefore **trigger-enforced polymorphic referential integrity**, and must be documented and read as such — it is not an FK, and `information_schema` will not report it as one.

The invariant, in full:

1. On INSERT, `internal_id` **must exist** in the canonical table named by `entity_type`.
2. `entity_type` must be one of the valid canonical types (§11.4).
3. `internal_id` and `entity_type` are **immutable after insert**.
4. A canonical entity **must not be deletable** while an `external_ids` row references it.
5. **Remapping happens by superseding the old mapping and inserting a new one — never by rewriting `internal_id` in place.**

Rule 5 is the one with teeth. An in-place rewrite silently re-attributes every historical fact that was ingested under the old mapping, and it is invisible to any test that checks only current state. Rules 3 and 5 together are the schema-level expression of §6 rule 9's **"Never auto-remap."**

Rule 4 deserves note because it inverts the usual direction: the canonical tables are protected *by* the mapping table, which a conventional FK would normally do for free. P0-05 already withholds `DELETE` on `teams`, `competitions` and `seasons` from `engine_rw` (§10.6), so this trigger is defence in depth rather than the only guard — but `countries` and `venues` do allow `DELETE`, and there the trigger is the guard.

**Which layer rejects, and with which SQLSTATE.** *(Recorded 2026-09-08 from the P0-06 implementation.)*

PostgreSQL evaluates a `BEFORE ROW` trigger **before** it evaluates `CHECK` constraints. An invalid `entity_type` therefore reaches the trigger first and is rejected as `23503` (`foreign_key_violation`) rather than by the column's `CHECK` as `23514`. **Both are valid rejection paths and both are present; the invariant — that the row cannot be written — is what matters.** Verification must accept either code rather than asserting one.

Attempts to change the immutable `internal_id` or `entity_type` raise **`23001` (`restrict_violation`)**, chosen deliberately so an immutability breach is distinguishable from a missing reference (`23503`) at a glance in logs.

**SQLSTATE values are an implementation detail, not an architectural contract.** They are recorded here so verification and operational tooling can rely on today's behaviour, not to freeze it. Only the invariants in the numbered list above are binding.

**Temporal behaviour of `now()`.** PostgreSQL's `now()` is the **transaction timestamp** and is stable for the life of a transaction: two calls inside one transaction return the identical instant. Consequently a mapping inserted and superseded **within the same transaction** must set `superseded_at` to an explicitly later instant, or it will violate the `superseded_at > known_at` invariant. This is a testing consideration rather than an operational one — **normal supersession happens in a later transaction**, where `now()` has advanced. The `superseded_at > known_at` invariant is preserved unchanged; zero-length validity remains disallowed.

**Triggers are not implemented in this task.** This section is their specification.

### Settled trigger design

*(Determined 2026-09-08 by a disposable probe on PostgreSQL 17.6. All fourteen invariant probes passed, including under P0-05's real grant model.)*

**Mapping-side — `BEFORE INSERT OR UPDATE ON external_ids`:**

- Resolve `entity_type` to its canonical registry and verify `internal_id` exists there.
- Perform that check with **`SELECT … FOR KEY SHARE`** on the referenced canonical row (see the race decision below).
- Reject any change to `internal_id` or `entity_type` after insert.
- Permit changes to `superseded_at`, `confidence` and `last_verified_at`.
- Raise `foreign_key_violation` (23503) so the failure reads like the FK it emulates.

**Canonical-side — `BEFORE DELETE` on each of `countries`, `competitions`, `seasons`, `teams`, `venues`:**

- Reject deletion while any `external_ids` row references the entity.

**Both sides are required.** This was proved, not assumed: with the canonical-side trigger dropped, deleting a mapped team succeeded and left **1 orphaned mapping** pointing at a row that no longer existed. The mapping-side trigger validates only at insert time and cannot prevent a later delete; the canonical-side trigger cannot validate a mapping being created. Neither alone is sufficient.

Rule 4's protection is not redundant with P0-05's grants. `engine_rw` genuinely holds `DELETE` on `countries` and `venues` (§10.6), and the probe confirmed that under `SET LOCAL ROLE engine_rw` the delete is stopped **only** by the trigger.

### This is not a foreign key

It is **trigger-enforced polymorphic referential integrity**. `information_schema.table_constraints` will not list it, and `drizzle-kit` will not see it. Nothing in the ordinary toolchain will tell you it is missing or broken.

**It therefore requires explicit verification.** The absence of a constraint the tooling never reports is invisible until data is already wrong.

### Race decision: `FOR KEY SHARE`, not weaker-than-FK semantics

The probe established that a plain `SELECT EXISTS` check takes **no lock on the canonical row**. Under READ COMMITTED, a session inserting a mapping and a session deleting the same entity can interleave, leaving an orphan — a window a real foreign key closes by taking a `KEY SHARE` lock.

**The production design closes that window with `SELECT … FOR KEY SHARE`.** Accepting the window was considered and rejected: the whole purpose of §11.3 is to obtain FK-equivalent integrity for a polymorphic column, and a mechanism that is FK-like except under concurrency is a mechanism that fails exactly when it is hardest to debug.

### Errors are transaction-safe

Trigger failures raise a clean SQLSTATE with a legible message and leave the transaction usable after `ROLLBACK TO SAVEPOINT` — verified in the probe across two consecutive failures. Verification must use the savepoint pattern established by `verify-canonical` (§10) for negative probes.

### TRUNCATE

`TRUNCATE` bypasses row-level triggers entirely, so a `TRUNCATE` on a canonical table would orphan mappings silently.

**No `BEFORE TRUNCATE` trigger is added unless a need is demonstrated.** Instead: **application roles must not hold `TRUNCATE` privilege on the canonical tables or on `external_ids`.** `TRUNCATE` is not granted by P0-05 or P0-06 and must not be added.

Administrative and retention roles that might legitimately hold it are **outside P0-06 scope**.

## 11.4 `entity_type` is TEXT with a CHECK constraint

Not a PostgreSQL enum. Consistent with every constrained column in P0-05, extensible by ordinary migration, and free of `ALTER TYPE` ceremony.

Permitted values — the five UUID-keyed canonical registries, and only those:

```
country · competition · season · team · venue
```

The child tables (`team_names`, `competition_names`, `team_aliases`) are `bigint`-keyed by the §10.5 exception and are deliberately **not** mappable: providers do not have primary keys for a name row.

## 11.5 `confidence` — storage here, meaning in P0-12

**P0-06 owns storage** of `confidence` and `last_verified_at`. It defines their type and nullability and nothing more.

**P0-12 owns interpretation** — what a value means, what threshold gates an auto-match, when a mismatch drops confidence, and what fires a review item.

**P0-06 must not invent a numerical threshold or a matching algorithm.** A threshold chosen before any matching exists would be a guess that later code would inherit as though it were a decision.

Per §6 rule 9, the nightly job that re-verifies mappings and drops confidence is **Phase 1** work — Phase 0 has no scheduler (§G). P0-06 provides the columns that job will write.

## 11.6 `fixture_match_candidates` belongs to P0-12

*(Ownership gap closed 2026-09-08. It was named in §B and §6 rule 10 without ever being assigned to a task.)*

It is resolution machinery — cross-provider duplicate scoring on teams, ±3 days and competition — not foundational identity mapping. **P0-12 owns entity resolution and review workflows, and owns this table.** P0-06 does not create it.

`competition_coverage` remains deferred as decided in §10.9.

## 11.7 Scope boundary

**P0-06 establishes the mechanism and the schema — nothing that interprets them.** It creates: `fn_visible_at` and the first `<table>_as_of` wrapper (§11.1), `external_ids`, `entity_review_queue` (§11.2), the mapping-side and canonical-side integrity triggers (§11.3), grants, a verification script that calls the as-of mechanism rather than restating its predicate, and the CI catalog assertion enforcing the single-place rule.

Ownership of everything adjacent, stated so no later task has to re-derive it:

| Concern | Owner |
|---|---|
| As-of mechanism — design and first implementation | **P0-06** |
| As-of mechanism — consumption for `match_results`, `match_stats` | **P0-08** (consumes; owns neither) |
| Resolution and matching semantics, `confidence` interpretation, thresholds | **P0-12** |
| `entity_review_queue` — population and operational use | **P0-12** (P0-06 creates the empty table) |
| `fixture_match_candidates` | **P0-12** (§11.6) |
| Nightly mapping re-verification job | **Phase 1** — Phase 0 has no scheduler (§G) |
| Administrative / retention roles, `TRUNCATE` privilege | Outside P0-06 (§11.3) |

**Read-only consumers.** *(Recorded 2026-09-08 from the P0-06 implementation.)* `app_rw` and `analytics_ro` hold `SELECT` on **both** `external_ids` and `entity_review_queue`. This is intentional: the admin data-status page (ARCHITECTURE.md §8) and analytics both need to see mapping state and the review backlog. **Neither role holds any mutation privilege** — no `INSERT`, `UPDATE`, `DELETE`, and no `TRUNCATE` on either table, nor on the canonical tables (§11.3).

P0-06 does **not** create: any matching, scoring or fuzzy-resolution logic, `fixture_match_candidates`, the nightly re-verification job, provider adapters (P0-10), or any change to P0-05 canonical schema.

---

# 12. Fixture identity (P0-07)

Approved 2026-09-08 after a read-only design review that surfaced fourteen decisions the specification had never fixed, two of which would have produced a schema unable to load three of the four launch competitions. All fourteen are ruled below.

**This section is design only.** No schema, migration or verification exists yet. Where a rule below is conditional on a disposable probe, it says so.

P0-07 builds the join between the canonical entities of P0-05 and every fact that will ever be attached to a match. `fixtures` says *which contest*; `fixture_schedule` says *when, where and in what administrative state* — and, being bitemporal, *when we came to believe it*.

## 12.1 Scope

**Owns:** `fixtures`, `fixture_schedule`, the `fixture_schedule_as_of(cutoff)` wrapper, grants on both, and `db:verify-fixtures`.

**Does not own:** any football result. Scores, half-time scores, extra time, penalties, `is_trainable` and `result_source` are `match_results` — **P0-08** (§B, §D). Odds are P0-09. Cross-provider fixture matching is P0-12 (§11.6). Provider adapters are P0-10/11. There is no scheduler in Phase 0 (§G).

**Dependencies are 04, 05 and 06** — `data_sources` and `raw_payload_bodies` for the provenance columns on `fixture_schedule`, the canonical registries for the identity foreign keys, and `fn_visible_at` for the schedule wrapper. *(§D's task row read `05` alone until corrected 2026-09-08.)*

## 12.2 Canonical identity — six columns, all NOT NULL

```
UNIQUE (season_id, stage, leg, replay_number, home_team_id, away_team_id)
```

Unchanged from §6 rule 4 and CLAUDE.md non-negotiable #4, and it **never contains kickoff time**: that is the defect `DECISIONS-01` §6.5 records, where a rescheduled match inserted a duplicate instead of revising.

`fixtures.id` is a **UUID** *(D5)*. Not stylistic. `fixtures` is the most-referenced foreign-key target in the finished schema — `match_results`, `match_stats`, `odds_series`, `odds_coverage`, `feature_snapshots`, `predictions` and `fixture_match_candidates` all point at it — and `external_ids.internal_id` is `uuid`, so a `bigint` key would permanently foreclose ever mapping a fixture through the external identity layer. §10.5 made exactly this argument for the five canonical registries; it applies here with more force, not less.

### `stage` *(D1)*

**`stage` is free `text`. There is no `CHECK` enumerating its values, and none may be added.**

It is a **provider-independent structural stage identifier**: it names where in a season's structure the meeting sits, in our vocabulary, never a provider's.

**`stage` MUST distinguish structurally repeated meetings within a season, including repeated round-robin rounds.** This is the load-bearing rule, and it is not hypothetical. The Scottish Premiership plays a **three-round** pre-split phase — twelve clubs, thirty-three matches — and over three rounds one club hosts the other **twice**. Celtic host Rangers twice in a single season: same season, same teams, same home side, one leg, no replay. The Welsh and Northern Irish leagues split the same way, and §10.1 names England, Scotland, Wales and Northern Ireland as launch competitions. Without a `stage` that separates the rounds, that second meeting is a duplicate-key violation and three of the four launch competitions cannot be loaded.

Values such as `regular`, `regular_r1`, `regular_r2`, `regular_r3`, `championship_split`, `group_a`, `quarter_final`, `semi_final`, `final` are **examples of the convention, not an enumeration.** They are written here to show the shape and for no other purpose.

The reason a `CHECK` is refused is the reason §11.2 refused one on `entity_review_queue.reason`: a closed set fixes the vocabulary before the competitions that need it are known, and every future format reform — the Champions League league phase broke every schema that assumed group stages (`DECISIONS-01` §6.8) — would arrive as a migration on a constraint rather than a row. §6 rule 6 already forbids any code path that assumes a league structure; enumerating `stage` would be that assumption in constraint form.

### `leg` and `replay_number` *(D2)*

All three of `stage`, `leg` and `replay_number` are **`NOT NULL`**.

This is not a style preference and it is not defensive habit. In PostgreSQL, NULLs in a unique index are distinct from one another by default, so **a single NULL in any of the six columns silently disables duplicate-fixture prevention altogether** — no error, no warning, and the constraint appears to exist. `NOT NULL` is what makes the identity key mean what it says.

| Column | Value | Meaning |
|---|---|---|
| `leg` | `1` | A single-leg fixture, **or** the first leg of a two-legged tie |
| `leg` | `2` | The second leg of a two-legged tie |
| `replay_number` | `0` | The original fixture |
| `replay_number` | `1`, `2`, … | Successive replays |

`leg = 1` covering both the single-leg case and the first leg **widens §6 rule 5**, which said `leg ∈ {1,2}` without providing any value for a fixture that is not part of a tie — that is, for the overwhelming majority of fixtures. The widening is deliberate and recorded here so it is not later read as drift.

`tie_id` remains the marker of a genuine two-legged tie (§12.5), so "leg 1 of a tie" and "the only leg" stay distinguishable: the first has a `tie_id`, the second does not.

### Data-quality constraints on identity

`home_team_id <> away_team_id`; `replay_number >= 0`; `leg IN (1,2)`; `replaces_fixture_id <> id`; `tie_id IS NOT NULL` implies `leg IN (1,2)`. All ordinary `CHECK`s.

**Not enforceable, and recorded as such rather than quietly skipped:** that both teams belong to the season's competition cannot be constrained, because §10.8 deliberately has no participation table — participation is *derived from fixtures*, so the constraint would be circular. That both teams match the competition's `gender` and `age_group` is a cross-row condition; it belongs to entity resolution (P0-12) and to the P0-14 assertion suite, not to a fixture constraint.

## 12.3 Identity versus fact — where provenance lives *(D3)*

**`fixtures` carries no `source_id`, no `raw_payload_body_id` and no `known_at`. `fixture_schedule` carries all three.**

This must be read as a **distinction, not an exemption**, and CLAUDE.md non-negotiable #6 is not narrowed by it.

The rule that governs is: **provenance attaches to claims we could have learned wrongly.** A schedule revision is such a claim — a provider said the match kicks off at 15:00 at Goodison, and may say otherwise tomorrow. A fixture *row* is not: it is our own assertion that a contest exists, keyed on six values that are immutable by definition, and it is the anchor other people's claims are hung from.

`fixtures` is therefore a **canonical identity registry**, and behaves exactly as the P0-05 registries do (§10). `teams`, `competitions` and `seasons` carry none of the three columns either, for the same reason, and no one has ever argued they violate #6. `fixtures` joins them.

The practical test, for any future table: *can two providers disagree about this row's contents?* If yes, it is a fact and carries provenance. If the row's whole content is its identity, it is a registry entry and does not. The line is drawn here rather than left to taste, precisely so that a later task cannot quietly move it.

One consequence follows and is addressed in §12.4: because `fixtures` has no `known_at`, **the existence of a fixture at a past cutoff is answered by its schedule, never by `fixtures` alone.**

## 12.4 `fixture_schedule` — the bitemporal fact layer

Columns, per §6 rule 1 and the rulings below:

```
fixture_id, kickoff_utc, local_date, local_tz, venue_id, status,
is_neutral_venue, source_id, raw_payload_body_id, known_at, superseded_at
```

`source_id` and `raw_payload_body_id` are additions to rule 1's list, which omitted them; CLAUDE.md #6 requires them of every fact row and the omission was an incompleteness, not an exemption. `raw_payload_body_id` is a single-column FK to the unpartitioned `raw_payload_bodies` — the reason §9.1 split the archive in the first place.

Primary key: `bigint generated always as identity`, matching `external_ids` and the §9.7 convention for append-only log-style rows.

Read only through **`fixture_schedule_as_of(cutoff)`**, which delegates to `fn_visible_at` and never restates the predicate (§11.1). `LANGUAGE sql` is load-bearing: a PL/pgSQL equivalent is an optimisation barrier.

Supersession follows §2.2 — append a revision, set `superseded_at` on the old row — and §10.4's operational rule applies unchanged: unique indexes are **not deferrable** and a one-shot flip is order-dependent, so closing the old revision and inserting the new one are **two ordered statements, never one**.

### Timezone columns

Per §6 rule 11, unchanged: `kickoff_utc timestamptz` stored UTC; `local_date date` computed and stored **at ingest, not generated**, because a competition's timezone can itself change; `local_tz` an IANA zone name. **Any ingested datetime lacking an explicit UTC offset is rejected, never inferred** — DST is handled for free by storing instants, and the real hazard is providers publishing wall-clock time, which the rejection rule catches.

`local_date` exists because the match date is not the UTC date: a 20:00 kickoff in Brazil falls on the next UTC day, so "today's fixtures" computed from the UTC date is wrong for a continent (`DECISIONS-01` §6.11).

**The agreement between `local_date`, `kickoff_utc` and `local_tz` IS enforceable declaratively** *(D11, confirmed 2026-09-08 by disposable probe on PostgreSQL 17.6)*:

```sql
CHECK (local_date = (kickoff_utc AT TIME ZONE local_tz)::date)
```

`timezone(text, timestamptz)` — what `AT TIME ZONE` with a text zone compiles to — is **`IMMUTABLE`** in PostgreSQL 17.6. An earlier revision of this paragraph claimed it was `STABLE` and therefore barred from a `CHECK`; that was wrong.

The evidence matters, because acceptance alone proves nothing here: **PostgreSQL 17.6 does not check volatility in `CHECK` constraints at all** — `now()`, `random()` and both STABLE and VOLATILE PL/pgSQL functions were all accepted. It *does* enforce it for generated columns and index expressions (`42P17` for both). The deciding test is therefore the strict one: **an index on the bare `(kickoff_utc AT TIME ZONE local_tz)::date` expression is accepted**, which only a genuinely immutable expression can be.

The same constraint rejects an invalid IANA zone at insert time with **`22023`** (`time zone "Europe/Narnia" not recognized`), so one constraint covers both invariants.

**No trigger is added** for `local_date`/`local_tz` consistency. **Verifier-level validation is retained as a secondary guard**: a `CHECK` is evaluated on write and never re-validated, so a future image shipping updated tzdata could leave stored rows disagreeing with the constraint they were written under. The `CHECK` cannot retroactively reject anything; the verifier is what would notice.

A `GENERATED ALWAYS AS … STORED` column is also accepted by PostgreSQL and is **not** used: §6 rule 11 stores `local_date` at ingest precisely because a competition's timezone can change, and a stored generated column is recomputed on a table rewrite.

### `status` — administrative state only *(D4)*

Exactly seven values, as a `CHECK`:

```
scheduled · live · suspended · ft · postponed · abandoned · cancelled
```

`suspended` and `cancelled` are additions: the corpus previously named only five, in a superseded section of `ARCHITECTURE.md`, and a match called off and never replayed is genuinely not the same thing as a postponed one. Forcing it into `postponed` would be a lie told by the schema.

**`awarded`, `walkover` and `forfeit` are NOT schedule statuses and must never be added as such.** They are result semantics and belong to P0-08's `match_results.result_source`. §6 rule 3 is the governing sentence: *awarded scores are administrative outcomes and must never train the goals model — they still settle bets.* A 3–0 walkover is a real settlement fact and a fictional football fact, and the schema keeps both apart by putting them in different tables. Adding `awarded` here would create a second, competing representation of the same event and guarantee they eventually disagree.

**`status` lives on `fixture_schedule` and nowhere else.** It is not denormalised onto `fixtures`. Neither is `kickoff_utc`. The reason is §12.7.

### Lifecycle rules, unchanged from §6

- **Reschedule** — a new schedule revision. Same `fixture_id`. Never a new fixture (rule 1).
- **Postponement** — `status = 'postponed'`; the replayed match is the **same `fixture_id`** with a new revision. Postponement does not break identity (rule 2).
- **Abandonment** — `status = 'abandoned'`; the untrainability of the result is `match_results.is_trainable`, in P0-08 (rule 3).
- **Replay from 0–0** — a **new fixture row**, `replay_number` incremented, `replaces_fixture_id` pointing at the original (rule 4). A replay is a different contest; a reschedule is the same contest at a different time.
- A kickoff move greater than 24 hours **supersedes every prediction for that fixture** (rule 1). P0-07 produces the schedule revision; the supersession belongs to whichever task owns `predictions`, and P0-07 builds no artifact for it.

### `is_neutral_venue` *(D13)*

A boolean on `fixture_schedule`, not on `fixtures`.

It is on the schedule because **the neutral designation moves with the venue**, and the venue is a schedule attribute: a tie relocated to a third country, a final at a neutral ground, a match moved behind closed doors to a shared stadium. A relocation is a schedule revision, and the neutrality of the new venue must revise with it.

It matters downstream out of proportion to its size. **Home advantage is a direct model input** (`ARCHITECTURE.md` §5), and a neutral-ground match scored as a home match is a silent modelling error — no NULL, no exception, no failing test, just a systematically wrong prior on every neutral fixture. `ARCHITECTURE.md` §10 risk 8 already names neutral venues among the things that "quietly corrupt results and settlement". This column is the correction.

It is an addition beyond the previous specification, recorded here as such.

### No `actual_kickoff_utc` *(D14)*

Not added. The revision whose `status` becomes `live` carries its own `known_at`, which already bounds the real kickoff, and §4.3's rule — that the `last_observed_pre_kickoff` capture is triggered by **the status transition to `live`, not by the scheduled kickoff time** — is satisfiable from that transition alone. A dedicated column would be a second derivation of something the bitemporal record already holds.

### Every fixture has at least one schedule revision *(D10)*

**Mandatory.** The initial `fixtures` row and its initial `fixture_schedule` revision are created **in the same transaction**.

It is enforced by the verifier and by the ingestion rule, **not by a database constraint**: a fixture requiring a schedule that requires a fixture is circular, and no deferrable constraint resolves it cleanly.

The rule is not tidiness. Because `fixtures` carries no `known_at` (§12.3), a fixture with no schedule revision is **invisible to every as-of query and present in every current-state query** — the worst of both, and exactly the shape a leakage bug takes.

## 12.5 Deliberately not built

- **No fixture provider IDs** *(D6)*. Deferred entirely. **P0-07 makes no change to P0-06's `external_ids`** — not to the `entity_type` `CHECK`, not to `trg_external_ids_integrity()`, not to the canonical-side delete triggers. §11.4's five permitted entity types stand unamended.

  The deferral is not merely conservative. Nothing before P0-11 ingests a fixture, and **P0-11's provider — football-data.co.uk CSV — has no fixture identifiers at all**; it identifies a match by date, home and away. There is nothing to map. Cross-provider fixture identity is already assigned: `fixture_match_candidates` is **P0-12** (§11.6), scored on teams, ±3 days and competition (§6 rule 10). Adding a sixth entity type now would mean writing and verifying a trigger branch that no code exercises for five tasks.

- **No `ties` table** *(D7)*. `tie_id` is a **nullable `uuid` with no foreign key and no table behind it.** §6 rule 5 says the tie is "where aggregate-score logic lives" — but aggregate score is a **computation over two fixtures' results**, performed in Phase 1, not a canonical registry with an identity of its own. A table would be state that can disagree with the fixtures it summarises, which is the argument §10.8 used to refuse a participation table.

- **No `fixtures.competition_id`** *(D8)*. The competition is reached through `season_id → seasons.competition_id`, one join away. A denormalised copy is a second source of truth that can diverge from the season it contradicts.

- **No `matchweek` or `round` column** *(D12)*. It appears in `ARCHITECTURE.md` §3 — a superseded section — and in no current specification. Where round-robin round is structurally significant it is already carried by `stage` (§12.2), which makes a separate column redundant as well as unspecified.

- **No partitioning, no extensions, no exclusion constraints.** Phase 0's ceiling is roughly 7,000 fixtures (P0-13); the global ceiling is low hundreds of thousands. §10.4 settled that partial unique indexes cover every temporal guarantee required.

## 12.6 Permissions *(D9)*

Column-level `GRANT`, following §2.2, §9.3 and §10.6. Not Drizzle-expressible, so it lives in a `--custom` migration as `0006` and `0008` do.

```
GRANT SELECT, INSERT ON fixtures TO engine_rw;
GRANT UPDATE (stats_complete_at, tie_id, replaces_fixture_id) ON fixtures TO engine_rw;

GRANT SELECT, INSERT ON fixture_schedule TO engine_rw;
GRANT UPDATE (superseded_at) ON fixture_schedule TO engine_rw;

GRANT SELECT ON fixtures, fixture_schedule TO app_rw;
GRANT SELECT ON fixtures, fixture_schedule TO analytics_ro;
```

**`engine_rw` may UPDATE exactly three columns on `fixtures`** — `stats_complete_at`, `tie_id`, `replaces_fixture_id` — because each is genuinely learned after the fixture is created: a stats-completeness watermark when late data lands (§6 rule 13), a tie relationship when the draw is understood, a replay link when the replay is scheduled. **Every identity column is immutable by privilege.**

**No `DELETE` to any role**, on either table. A fixture anchors results, odds, feature snapshots and predictions; the same reasoning withholds `DELETE` on `competitions` and `seasons` in §10.6. **No `TRUNCATE` to any role**, per §11.3.

What this makes impossible at the database level, rather than by convention: rewriting a kickoff in place, rewriting a status in place, re-pointing a fixture at a different team or season, and deleting a fixture. Every one of those is a silent history rewrite that no test of current state would catch.

Unlike `external_ids`, every reference here is a **real foreign key**, so §11.3's polymorphic-trigger machinery does not recur. The case worth verifying rather than assuming is `venues`: `engine_rw` holds `DELETE` on it (§10.6), and a real FK must refuse the deletion of a referenced venue with no trigger involved.

## 12.7 Leakage

CLAUDE.md non-negotiable #2 — features read only facts with `known_at <= data_cutoff`, through the shared as-of mechanism — resolves here into four concrete rules.

**1. `status` on the bitemporal table is the whole defence.** Were `status` a mutable column on `fixtures`, as the superseded `ARCHITECTURE.md` §3 had it, every backtest reading a fixture would see its **final** status. `ft` is post-kickoff knowledge by definition. There would be no error and no NULL: a feature builder asking "is this match on?" three days before kickoff would be answered with the fact that the match finished. On `fixture_schedule`, a pre-kickoff cutoff returns `scheduled`, because that is all we knew.

**2. The same forbids denormalising kickoff.** A cached `fixtures.kickoff_utc` shows the *final* kickoff, not the one known at the cutoff. Rest days, congestion and travel are all computed from kickoff intervals (`ARCHITECTURE.md` §5), so a leaked kickoff leaks straight into the feature vector.

**3. Fixture existence is itself point-in-time information.** `fixtures` has no temporal columns, so a fixture created after a cutoff is visible in `fixtures` forever. **Never read `fixtures` alone in a point-in-time context** — always join to `fixture_schedule_as_of(cutoff)`, where a fixture with no visible revision correctly disappears. D10 is what makes that reliable.

**4. `stats_complete_at` is a leak, and is named as one.** It is a mutable current-state column on a non-temporal table, so no as-of query can hide it: a backtest at a pre-kickoff cutoff reads a value set hours after full time, and its mere non-NULLness reveals that the match finished and its stats arrived. It is **watermark machinery for job scheduling** (§6 rule 13) and **must never be read by a feature builder.** It is a required target of P0-15's leakage tests, which `ARCHITECTURE.md` §7 calls the most important tests in the repository.

**What this does not do.** The mechanism makes the correct read available and the incorrect read visible. It does not make the incorrect read impossible — `SELECT * FROM fixture_schedule WHERE superseded_at IS NULL` still compiles and still leaks. The barriers in Phase 0 are the grant model, the single-place catalog assertion of §11.1, and P0-15's leakage tests. Not a lock.

## 12.8 Migration and verification shape

**Drizzle-managed:** both tables, in full. Neither belongs under `src/raw-sql/**` — that directory is only for objects whose DDL Drizzle cannot emit. §10.4 verified that `uniqueIndex(...).where(...)` generates and enforces correctly, and both tables are plain, unpartitioned and extension-free.

**`--custom` SQL, in the same ledger:** the `fixture_schedule_as_of` wrapper (Drizzle cannot express `CREATE FUNCTION`) and the grants (column-level `GRANT` is not Drizzle-expressible).

**Indexes, deliberately minimal** — at Phase 0 volumes, speculative indexing is cost without benefit. There is deliberately **no index on `known_at`** *(ruled 2026-09-08)*: the query probe found no consumer for one, and P0-06's `external_ids_known_at_idx` is not a reason to carry an unused index here.

**`fixture_schedule (fixture_id)` is a FULL index, and the partial current index does not replace it** *(added 2026-09-08 during P0-07 implementation)*. It is the parent-key lookup and the primary access path for a historical as-of read; **P0-08 is the immediate consumer**. The partial index cannot serve that read at all — `fixture_schedule_current_idx` is restricted to `superseded_at IS NULL`, while the as-of predicate also admits rows whose `superseded_at` is **later than the cutoff**. Without the full index a single-fixture as-of lookup is a sequential scan. This is a fundamental key index, not speculative tuning.

Bulk scans remain scans: the team-form join reads every visible revision, roughly 2 MB at the Phase 0 ceiling, which is correct and cheap.

```
fixtures          identity unique; (home_team_id); (away_team_id)
fixture_schedule  unique (fixture_id) WHERE superseded_at IS NULL
                  (fixture_id)                    -- full, not partial
                  (kickoff_utc) WHERE superseded_at IS NULL
```

**`db:verify-fixtures` must prove**, at minimum: the three §E acceptance criteria; that a duplicate identity key, a `home = away` fixture and a second current schedule revision are each rejected; that a reschedule read as-of the earlier cutoff returns the **old** kickoff while the current read returns the new one; that `engine_rw` is **refused** `UPDATE` on `home_team_id` and on `kickoff_utc`, **permitted** on `superseded_at`, and refused `DELETE` on both tables; that deleting a referenced venue is refused; that `fixture_schedule_as_of` delegates to `fn_visible_at` without restating the predicate, and that the verifier file itself restates it **zero** times; and that a selective filter through the wrapper still plans as an index scan.

The verifier must be **meta-tested** by deliberately breaking an invariant, must be **idempotent** (no fixed identifiers — the defect that required commit `812a180`), and must wrap every negative probe in a `SAVEPOINT`, or one expected failure aborts the transaction and every later probe returns `25P02`.

**Regression is part of the task, not a courtesy.** All five existing verifiers must pass unchanged against a freshly migrated database. Two specific exposures: `verify-external-ids` check 19 asserts that **exactly one** object in `public` contains the visibility predicate, so the new wrapper is the first live test of the single-place rule — if it restates instead of delegating, CI fails, by design. And the delete probes in `verify-canonical` and `verify-external-ids` expect an unreferenced `teams` or `venues` row to be deletable; the new foreign keys must not perturb them. Both are to be checked, not assumed.

---

# A. Final architecture

Unchanged from `ARCHITECTURE.md` in shape — three deployables, database as the engine↔app interface, one scoreline matrix deriving all markets. Four amendments:

1. **Provenance is a first-class layer**, not metadata. Every fact carries `source_id`, `raw_payload_body_id`, `known_at`; lineage is proven by hash reconstruction rather than stored as a graph.
2. **Bitemporality is scoped to four fact tables**, with the as-of predicate encapsulated in a function and append-only enforced by column grants.
3. **Odds are a separate normalised model** — `odds_series` + `odds_ticks` + `odds_coverage` — with strict naming discipline separating true closing prices from our own last observation.
4. **Reproducibility is a Phase 0 deliverable**, built against a placeholder model, not a Phase 7 aspiration.

# B. Final schema

**Provenance** (§9) — `data_sources`, `job_runs` (absorbs `ingestion_runs`), `raw_payload_bodies` (unpartitioned, globally deduped on `hash_algo + body_hash`), `raw_payloads` (partitioned monthly by `fetched_at`, one row per fetch, no global dedup constraint), `v_raw_payload_seen`, `external_ids` (bitemporal).

**Canonical** (§10) — `countries`, `venues`, `competitions`, `competition_names`, `seasons` (with `format`), `teams` (**no name column**), `team_names`, `team_aliases`. *`competition_coverage` removed from scope 2026-09-08 — see §10.9.*

**Fixtures** — `fixtures` (identity: `season_id, stage, leg, replay_number, home_team_id, away_team_id`; plus `tie_id`, `replaces_fixture_id`, `stats_complete_at`), `fixture_schedule` (bitemporal). *Fully specified in **§12** (P0-07), which fixes the six identity columns as `NOT NULL`, adds `is_neutral_venue` to `fixture_schedule`, and rules out `competition_id`, `matchweek` and a `ties` table.*

**Facts** — `match_results` (bitemporal, `is_trainable`, `result_source`), `match_stats` (bitemporal), `match_events` (deferred to Phase 8, schema reserved).

**Odds** — `bookmakers`, `odds_series` (with `reference_price`, `reference_price_kind`), `odds_ticks`, `odds_coverage`.

**Model** — `model_versions`, `training_runs`, `dataset_snapshots`, `feature_snapshots`, `feature_snapshot_inputs` (sampled), `predictions`, `prediction_markets`, `team_ratings`.

**Evaluation** — `prediction_outcomes` (bitemporal), `model_performance`.

**Review queues** — `entity_review_queue` (table created in **P0-06**, §11.2; populated by P0-12), `fixture_match_candidates` (**P0-12**, §11.6).

**Application** — deferred entirely to Phase 5. Not built in Phase 0.

# C. Provider bake-off plan

Championship 2023/24 · consensus ground truth with manual adjudication of disagreements · the metric table in §3.3 · **two pulls fourteen days apart** as the deciding test · Tier-1 gates then weighted scoring · disqualification on unflagged silent revision or sub-99% fixture precision · run through the real pipeline · results committed to `docs/BAKEOFF-RESULTS.md` with a dated decision.

# D. Phase 0 task list, in dependency order

| # | Task | Depends on |
|---|---|---|
| **P0-01** | Monorepo skeleton: pnpm workspaces, Turbo, `apps/web` (empty), `apps/engine`, `packages/db`; CI running lint + typecheck | — |
| **P0-02** | Local Postgres via Docker Compose; connection from both TS and Python | 01 |
| **P0-03** | Drizzle harness: config with the `schema` glob scoped per §8.1, `generate` + `migrate` scripts, **one `--custom` SQL migration and one raw-SQL-owned table proven outside the glob**, plus the `db:verify-generate` CI check | 02 |
| **P0-04** | Provenance core per §9: `data_sources`, `job_runs`, `raw_payload_bodies` (Drizzle), `raw_payloads` partitioned monthly + `DEFAULT` + `v_raw_payload_seen` (raw-SQL), grants, plus `db:verify-partitions` | 03 |
| **P0-05** | Canonical entities: countries, competitions, `competition_names`, seasons, teams (no name column), `team_names`, aliases, venues | 03 |
| **P0-06** | Per §11: the reusable **as-of mechanism** (§11.1), `external_ids` (bitemporal, polymorphic `internal_id` with trigger-enforced integrity), `entity_review_queue` (minimal, generic), grants | 04, 05 |
| **P0-07** | Fixture identity: `fixtures` + `fixture_schedule` (bitemporal), ties, legs, replays | 04, 05, 06 — *[CORRECTED 2026-09-08]* this row previously read `05` alone. `fixture_schedule` carries `source_id` and `raw_payload_body_id` (P0-04) and is read through `fn_visible_at` (P0-06). See §12.1 |
| **P0-08** | Bitemporal facts: `match_results`, `match_stats`, and **column-level grants**. **Consumes** the as-of mechanism built in P0-06 — *[CORRECTED 2026-09-08]* this row previously claimed ownership of the `as_of` SQL function; see §11.1 | 04, 07 |
| **P0-09** | Odds model: `bookmakers`, `odds_series`, `odds_ticks`, `odds_coverage` | 04, 07 |
| **P0-10** | `ProviderAdapter` interface + canonical DTOs + an adapter contract test suite any adapter must pass | 04 |
| **P0-11** | Adapter #1 — football-data.co.uk CSV (results **and** odds; free, no key, exercises the whole pipeline) | 10 |
| **P0-12** | Entity resolution: alias matching, fuzzy candidates, **`fixture_match_candidates`** (§11.6), review queue population, and the semantics of `confidence` (§11.5) | 06, 11 |
| **P0-13** | Historical import: 3 leagues × 5 seasons of results and odds, with full provenance | 08, 09, 12 |
| **P0-14** | Validation and reconciliation suite: data-quality assertions + cross-source reconciliation | 13 |
| **P0-15** | Reproducibility harness: `dataset_snapshots`, `model_versions`, `training_runs`, `feature_snapshots`, `reproduce.py` with **L1 and L2 against a placeholder model** | 08 |
| **P0-16** | DB roles (`app_rw`, `engine_rw`, `analytics_ro`) with explicit grants; RLS scaffolding on the (empty) user tables | 08, 09 |
| **P0-17** | Bake-off harness + first pull of Championship 2023/24 from every candidate | 11, 14 |
| **P0-18** | **Second bake-off pull, 14 days later**, diff, scorecard, decision | 17 + 14 days |

Phase 0 ends at P0-18, not P0-17. **The waiting period is part of the plan** — start the clock on P0-17 early and do P0-15/16 while it runs.

# E. Acceptance criteria

| Task | Accepted when |
|---|---|
| P0-01 | CI green on an empty repo; `pnpm -r typecheck` and `uv run pytest` both exit 0 |
| P0-02 | Both a TS and a Python process connect and round-trip a query; `docker compose down -v && up` reproduces a clean DB |
| P0-03 | Three proofs, none of which may use `tablesFilter` (§8.2): **(1)** a real Drizzle-managed table produces a migration, and repeated `drizzle-kit generate` runs on the unchanged schema produce an empty diff; **(2)** a `--custom` SQL migration applies successfully and is recorded in the same migration ledger; **(3)** a raw-SQL-owned table declared outside the Drizzle `schema` glob is fully typed and queryable but is **not emitted** by `generate`, and repeated generation stays clean. Plus `db:verify-generate` running in CI as a schema-input consistency check — **not** described or relied on as database drift detection (§8.3) |
| P0-04 | Fetching the same body twice yields **one `raw_payload_bodies` row and two `raw_payloads` rows**, with `v_raw_payload_seen.seen_count = 2` (§9.3); 12 monthly partitions plus `DEFAULT` exist; a partition drop leaves other data intact; `engine_rw` is **refused** `UPDATE` and `DELETE` on both archive tables; deleting a referenced body is refused by `ON DELETE RESTRICT`; and **`db:verify-partitions` asserts all six invariants in §9.5** — including that `DEFAULT` is empty and that a routing probe reaches the current month's partition — failing loudly on any violation |
| P0-05 | Eight tables per §10, all Drizzle-managed, plus one custom grants migration. A team can be renamed and both the historical and the current name resolve correctly at their respective dates; `teams` has no name column. A **second current** `(team_id, name_type)` or a second current season per competition is **rejected by a partial unique index**, while two historical rows and a historical+current pair are accepted (§10.4). `engine_rw` is refused `UPDATE` on `team_names.name` and permitted `UPDATE (valid_to)` (§10.6). No participation table, no `external_ids`, no `competition_coverage` |
| P0-06 | **As-of mechanism (§11.1):** every as-of read goes through `fn_visible_at` or a `<table>_as_of` wrapper — no caller, including the verification script, restates the predicate; a **CI catalog assertion proves exactly one object contains the predicate logic**; the wrapper is callable from **both TypeScript and Python**; and a selective filter through the wrapper still yields an **index scan**, not a function scan. **Bitemporal behaviour:** an external ID remapped to a different internal entity leaves the old mapping intact with `superseded_at` set, and the as-of query returns the old mapping for a past cutoff. **Uniqueness:** many external IDs may map to one internal ID; one external ID mapping to two current internal IDs is rejected by the partial unique. **Integrity (§11.3):** the mapping-side trigger validates `internal_id` against the registry named by `entity_type` **using `SELECT … FOR KEY SHARE`**, rejects mutation of `internal_id`/`entity_type`, and permits `superseded_at`/`confidence`/`last_verified_at`; the canonical-side trigger **blocks deletion of a referenced entity even where `engine_rw` holds `DELETE`**; trigger failures are **transaction-safe under `ROLLBACK TO SAVEPOINT`**; and **no application role holds `TRUNCATE`** on the canonical tables or `external_ids`. `entity_review_queue` exists and is empty — P0-06 writes no review items |
| P0-07 | Both legs of a two-legged tie insert without violating the unique constraint; a replay inserts as a new fixture linked by `replaces_fixture_id`; a reschedule creates a schedule revision, **not** a duplicate fixture |
| P0-08 | **The §2.3 worked example passes as an automated test** — insert 2–1, insert the 2–2 correction, and assert the as-of query at T2 returns 2–1 while the current query returns 2–2. An `UPDATE` on a score column is **rejected by the database** for `engine_rw` |
| P0-09 | Polling an unchanged price twice writes **one** tick; a suspension writes a tick with `is_available = false`; `odds_coverage` distinguishes "not polled" from "unchanged" |
| P0-10 | The contract test suite runs against a stub adapter and fails it for each of: provider shape leakage, missing `known_at`, absent raw payload persistence |
| P0-11 | The adapter passes the contract suite; every ingested row traces to a `raw_payload_body_id`; re-running the import is idempotent (row counts unchanged) |
| P0-12 | ≥95% of teams auto-resolve; every unresolved team appears in the review queue; **zero teams are silently auto-created** |
| P0-13 | Row counts match the source CSVs; every fact row has non-null `source_id`, `raw_payload_body_id`, `known_at`; spot-check of 20 fixtures against the source is exact |
| P0-14 | Every assertion from `ARCHITECTURE.md` §7 runs and passes; a deliberately corrupted row is caught and quarantined rather than published |
| P0-15 | **L1 passes bit-for-bit** on the placeholder model; **L2 passes**; mutating a historical fact makes L2 fail with a clear message |
| P0-16 | `app_rw` cannot write engine tables and `engine_rw` cannot write app tables — both proven by tests that expect a permission error |
| P0-17 | One full season harvested from each candidate through the real pipeline, all payloads archived |
| P0-18 | Scorecard produced; disagreements adjudicated; `docs/BAKEOFF-RESULTS.md` committed with a dated, reasoned decision |

# F. Verification commands

```bash
# Environment
docker compose up -d && docker compose ps
pnpm install && uv sync

# Schema-input consistency — NOT database drift detection (see section 8.3)
pnpm db:migrate
pnpm db:verify-generate     # generate must emit no new migration
pnpm db:verify-partitions   # database invariant check for raw-SQL-owned objects

# Static checks
pnpm -r typecheck && pnpm -r lint
uv run ruff check . && uv run mypy src

# Tests
pnpm -r test
uv run pytest -q
uv run pytest tests/bitemporal -v      # the §2.3 worked example
uv run pytest tests/adapters  -v       # adapter contract suite
uv run pytest tests/grants    -v       # permission boundaries

# Data integrity
uv run python -m engine.jobs.validate --all
uv run python -m engine.jobs.reconcile --competition championship --season 2023-24

# Reproducibility
uv run python -m engine.reproduce --prediction-id <id> --level L1
uv run python -m engine.reproduce --prediction-id <id> --level L2

# Bake-off
uv run python -m engine.bakeoff harvest  --competition championship --season 2023-24
uv run python -m engine.bakeoff diff     --pull-a 1 --pull-b 2
uv run python -m engine.bakeoff scorecard
```

A single `pnpm verify` should chain the static checks, both test suites and the validation run.

# G. Must NOT be implemented in Phase 0

- **Any frontend.** `apps/web` exists as an empty workspace with a typecheck script and nothing else. No pages, no components, no API routes.
- **Any prediction model.** The placeholder in P0-15 returns a constant. It exists to prove the reproducibility harness, and its constancy is the point.
- **The bet-slip wizard**, in any form.
- **User accounts, auth, Stripe, entitlements, RLS policies with actual rules.** P0-16 creates the roles and empty tables only.
- **Live or in-play anything.** No pollers, no Realtime, no `match_events` population.
- **A paid provider integration.** Free sources only until P0-18 decides.
- **The value engine** — no de-vigging, no consensus, no edge. Odds are stored, not interpreted.
- **Player-level data, lineups, injuries.** Schema space reserved; nothing built.
- **Deployment to Vercel, Fly or Supabase.** Phase 0 is local-only. Cloud comes with Phase 1.
- **Scheduling and cron.** Jobs are invoked manually by command in Phase 0.
- **`packages/ui` and `packages/contracts`.** No second consumer exists yet.

The temptation in Phase 0 is to build something visible. Resist it: **Phase 0's deliverable is a trustworthy database with proven provenance, and nothing else.** Everything visible is Phase 3.
