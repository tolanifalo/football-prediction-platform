# Decisions 01 — ORM, Data Providers, Ownership, Architecture Stress Test

Companion to `ARCHITECTURE.md`. Written 2026-09-06, before Phase 0.
**Verification status is marked throughout.** `[CONFIRMED]` = read from the vendor's own page during this research. `[REPORTED]` = came from search summaries or third-party pages, not verified at source. `[ASSUMPTION]` = my inference, explicitly not a fact. `[UNKNOWN]` = could not establish; must be answered during trials.

Two vendor sites (api-football.com, football-data.co.uk) blocked automated access during this research, so several claims about them are `[REPORTED]` rather than `[CONFIRMED]`. My training data ends May 2026 and today is September 2026 — **treat every price below as an indicative figure to re-verify in a trial, never as a quote.**

---

# 1. ORM — Decision: **Drizzle**

Locked. Prisma is the better product in some absolute sense; Drizzle is the better fit for *this* schema.

### The honest counter-case first

Prisma v7 (Nov 2025) made the rust-free TypeScript/WASM client the default, reportedly cutting bundle size ~90% (≈14MB → ≈1.6MB) and Vercel cold starts from ~800ms to under 100ms `[REPORTED]`. TypedSQL lets you keep raw analytical SQL in `.sql` files with generated result types. **The "Prisma is slow and fat on serverless" argument is largely obsolete, and I'm not using it.** Prisma also has the better migration tooling — `migrate dev`, drift detection and shadow-database verification are more polished than `drizzle-kit`.

So the decision rests on schema fit, not performance.

### Why Drizzle wins for this project

**1. This schema is mostly Postgres features Prisma does not model.** `ARCHITECTURE.md` requires monthly-partitioned tables (`odds_snapshots`, `raw_payloads`), materialised views (`standings`), partial unique indexes (`WHERE superseded_at IS NULL`), expression indexes, `jsonb` operators, RLS policies and generated columns. Prisma models none of partitioning or matviews — you would manage those in hand-written SQL *outside* Prisma's ledger, which fragments schema ownership into two systems. That is precisely the failure mode §2 of the architecture warns about. Drizzle keeps everything in one migration ledger because you can drop to SQL inside it.

**2. Two languages read this schema.** Python must query the same tables. Drizzle's migrations *are* SQL files, so the database is describable without a TypeScript toolchain. Prisma's source of truth is a `.prisma` DSL that Python cannot read.

**3. The analytical workload is the product.** Value boards, calibration bins, rolling model performance, CLV, standings — window functions, CTEs, lateral joins, `DISTINCT ON`. Prisma's query API cannot express these, so you would live in TypedSQL for the interesting half of the app and the Prisma client for the boring half: two paradigms. Drizzle's builder covers both, and `sql` template escape-hatches type cleanly.

**4. Claude Code DX — this is a real criterion, and it favours Drizzle.** Prisma requires `prisma generate` after every schema change before types are correct. In an agentic edit→typecheck loop that is an extra step whose omission produces confusing stale-type errors. Drizzle infers query types directly from `schema.ts` with no codegen between edit and typecheck. Drizzle's SQL-shaped queries are also easier to verify by reading. Prisma's `.prisma` schema file is genuinely more compact and readable for schema *comprehension* — that is Prisma's one DX win here, and it is outweighed.

**5. Simpler, which you asked me to prefer.** No engine, no client codegen, no second DSL.

### Mitigations for Drizzle's real weaknesses

| Weakness | Mitigation |
|---|---|
| Weaker migration ergonomics than `prisma migrate dev` | Use `drizzle-kit generate` + `migrate` exclusively; review every generated SQL file by hand before merge. Treat migrations as reviewed artefacts, not output. |
| `drizzle-kit push` does not apply RLS policies correctly `[REPORTED]` | **Never use `push` outside a local throwaway database.** CI runs `generate` + `migrate` only. |
| Cannot express `PARTITION BY`, matviews | Declare those tables in `schema.ts` for typing, exclude them from drizzle-kit's managed set via `tablesFilter`, and own their DDL in `--custom` SQL migrations in the same ledger. — **[SUPERSEDED 2026-09-06]** `tablesFilter` has no effect on `drizzle-kit generate`; a table declared in the schema glob is emitted regardless, as a plain *unpartitioned* `CREATE TABLE`. Authoritative rule: **`PHASE-0-SPEC.md` §8.1–§8.2** (exclusion is by directory, not by filter). Original text retained as a record of what was believed at the time. |
| No drift detection | A CI job diffs `drizzle-kit generate` output against an empty diff; non-empty = someone changed the DB out of band. — **[SUPERSEDED 2026-09-06]** `generate` never reads the database. It detects drift between the schema input and Drizzle's snapshot only, so a database changed out of band passes this check silently. Authoritative rule: **`PHASE-0-SPEC.md` §8.3**. |

**Rejected alternative worth one line:** Supabase CLI owning plain-SQL migrations with Drizzle as a query-builder only. Simpler in isolation, but it creates two sources of schema truth (SQL files + `schema.ts`) that drift silently. One ledger wins.

---

# 2. Football data provider — evaluation framework

### The framing that matters more than any vendor

**This product has two different data problems, and almost no vendor is good at both.**

- **(A) Football facts** — competitions, fixtures, results, stats, xG, lineups, injuries, live.
- **(B) Betting market history** — opening prices, price movement, and above all *closing* prices.

Requirement (B) is by far the harder one, and it is the one that determines whether the model can ever be honestly evaluated. Most football APIs sell you *current* odds. Almost none sell you a clean historical closing line. And closing odds cannot be backfilled — a season you did not capture is gone permanently.

**Therefore: the odds source must be selected and contracted separately from the football-data source.** They become two `data_sources` rows and probably two vendors. The adapter interface in `ARCHITECTURE.md` §4A already supports this; this section makes it an explicit requirement rather than an option.

### Tier 1 — Disqualifying gates (pass/fail, before scoring anything)

A vendor that fails any of these is architecturally incompatible, regardless of price or coverage:

| # | Gate | Why it is a hard gate |
|---|---|---|
| G1 | **May we store the data in our own database, indefinitely?** | The entire architecture (D2: the database is the engine↔app interface) collapses if we may only cache transiently. A "no" here is disqualifying, not negotiable. |
| G2 | **May we publicly display data and derivatives of it?** | We display results, stats and model outputs derived from their data. |
| G3 | **Is betting-adjacent use permitted?** | Many sports-data licences carve out betting explicitly. |
| G4 | **≥5 seasons of history for our launch competitions** | Below ~5 seasons the ratings model has no training spine. |
| G5 | **Bulk or paginated historical export, not just per-fixture calls** | Backfilling 5 seasons × 10 leagues one fixture at a time can cost more than the subscription. |

### Tier 2 — Weighted scoring (score 0–5, multiply by weight)

Weights reflect *this* product, not general-purpose sports apps. Note how low live data ranks — it is Phase 8.

| Dimension | Weight | Notes |
|---|---|---|
| Historical **closing** odds availability | 5 | The scarcest resource in the entire market |
| Historical odds depth & snapshot granularity | 5 | Determines backtest and CLV quality |
| Fixture & result coverage/correctness for launch leagues | 5 | Wrong results poison everything downstream |
| Entity identity stability (do team/comp IDs churn?) | 4 | Drives §5 pain permanently |
| Match stats depth (shots, SOT, corners, cards, possession) | 4 | Core model features |
| API reliability / uptime / status transparency | 4 | Jobs fail silently otherwise |
| Result update latency | 4 | Settlement and rating freshness |
| Schema stability & version policy | 3 | Breaking changes cost weeks |
| xG availability **and its history** | 3 | Valuable, not essential for v1 |
| Rate limits vs. our job cadence | 3 | Cheap plans often can't sustain odds polling |
| Price at our actual scale | 3 | Deliberately not weight 5 |
| Lineups & injuries | 2 | Phase 7 feature |
| Live / in-play | 2 | Phase 8 — **do not pay for this now** |
| Player-level data | 1 | Not in the model |

### Tier 3 — Operational qualifiers (tie-breakers)

Trial without a credit card · documentation quality · sandbox environment · support responsiveness · published status page · how they communicate breaking changes · whether historical backfill is billed differently from live calls.

### Tier 4 — The test that actually decides it

Scoring matrices are easy to fool. Before committing money, run this on a free trial for each shortlisted vendor:

1. Pull one full completed season for one launch league.
2. Reconcile every fixture and score against a second independent source.
3. Count: missing fixtures, wrong scores, missing stats rows, null xG, duplicated fixtures, teams that failed to resolve to a canonical entity.
4. Re-pull the same season two weeks later and diff. **Silent revisions are the finding that matters most** — they tell you whether the vendor rewrites history under you.

A vendor that scores well on the matrix and badly on step 4 is the wrong vendor.

---

# 3. Realistic providers — findings

### Football facts

**Sportmonks** — published, self-serve pricing `[CONFIRMED, from their pricing page]`: Starter €29/mo (5 leagues, 2,000 API calls per entity/hour), Growth €99/mo (30 leagues, 2,500/hr), Pro €249/mo (120 leagues, 3,000/hr), Enterprise custom (2,300+ leagues, 5,000/hr, historical included). Add-ons: Odds & Predictions €15/mo, xG & Pressure Index €24/mo, Premium Odds Feed €129/mo, extra API calls €29/mo, extra leagues €4/mo, historical data €29 one-time, News €99/mo, Transfer Rumours €99/mo. 20% off annually. 14-day trial on paid plans.
Storage rights `[REPORTED]`: storing, transferring and distributing data inside your own product — including caching in your own database — is permitted; reselling data as data is not; redistribution as a feed requires written approval.
xG `[REPORTED]`: available as an add-on; one source indicates xG coverage begins from the 2024 season with no earlier history. **If true this is significant** — it means xG cannot be a core model feature for a backtest spanning more than two seasons. Verify first.

**API-Football / API-Sports** — `[REPORTED, unverified: Cloudflare blocked direct access]` Pro ≈$19/mo (7,500 req/day), Ultra ≈$29/mo (75,000/day), Mega ≈$39/mo (150,000/day); per-minute limits ≈300/450/900. One search source claimed the *free* tier offers 7,500 requests/day, which conflicts with my prior understanding of a much smaller free allowance — **treat the free-tier figure as unresolved.**
Storage rights `[REPORTED]`: storage, distribution and transfer permitted; reselling prohibited without permission; notably, they state they do **not** grant a licence to publish the data, and that publication rights must be obtained from the relevant rights holders — with betting platforms called out as potentially requiring additional licences. See §4.
xG availability `[UNKNOWN]`.

**football-data.org** — `[REPORTED]` free tier: 10 req/min, 12 competitions, delayed scores, no lineups or deep player data. Fine for a Phase 0 seed, insufficient for production.

**Opta / Stats Perform** and **StatsBomb (Hudl)** — `[CONFIRMED in kind]` enterprise-only, quote-based, no self-serve tier. Public estimates ranged from ~€250/mo to >€50,000/yr `[REPORTED]`, which is a spread wide enough to be useless — treat as unknown. Best-in-class data quality and xG. StatsBomb publishes a free open-data set for research `[REPORTED]`. **Not viable at Phase 0; worth revisiting only if the product earns real revenue.**

### Odds

**football-data.co.uk** — `[REPORTED, notes.txt returned 503 during research]` free CSV downloads, 25+ leagues, deep history, odds from multiple bookmakers, and — critically — **explicit closing-odds columns** distinguished by a `C` in the column name (e.g. `B365CH` = Bet365 closing home). Covers 1X2, over/under and Asian handicap. Licence terms are not clearly published; the files are offered as free public downloads.
**This is the single most valuable Phase 0 asset in the entire evaluation** and it costs nothing. Verify the columns and the terms directly before depending on it.

**The Odds API** — `[CONFIRMED, from their historical-odds page]` historical snapshots from 6 June 2020 at 10-minute intervals; 5-minute intervals from September 2022; additional markets (props, periods) from 3 May 2023. Historical queries cost 10 credits per region per market. Paid plans only. `[REPORTED]` Starter free/500 credits, ≈$30/mo for 20k credits, ≈$59/mo for 100k, Business ≈$99/mo. `[REPORTED]` closing-line snapshots exist internally with public exposure planned in a higher tier — **verify whether closing lines are actually exposed to you today.**
Storage/redistribution terms `[UNKNOWN]` — their historical page does not address it. **Must read the ToS before this becomes a dependency.**

**Betfair Historical Data** — `[REPORTED]` `historicdata.betfair.com`, available to registered Betfair account holders. Three tiers: a free tier giving last-traded-price-per-minute with no volume and no full ladder, and two paid tiers with the full price ladder at different frequencies. Near-complete Exchange market coverage since 2016, including traded volume and Betfair Starting Price (BSP). A separate Historical Data Licence exists for storing and redistributing at scale.
**BSP is arguably the best closing-line reference that exists** — it is a real, transacted, commission-adjusted settlement price rather than a bookmaker's advertised number. Strong candidate for the CLV benchmark.

**Sportmonks Premium Odds Feed** — €129/mo `[CONFIRMED]`. Contents and historical depth `[UNKNOWN]`.

### Deliberately excluded

**Understat and FBref scraping.** Understat embeds JSON in the page and is technically trivial to scrape `[REPORTED]`; FBref sits behind Cloudflare `[REPORTED]`. Both are attractive and both are the wrong call: scraping a source you have no licence to, for a commercial product that displays derived data, converts a data problem into a legal one. FBref's underlying data is Opta-derived, which makes redistribution someone else's rights issue as well. Use StatsBomb's open data for research if you need free xG; do not build a product dependency on scraping.

---

# 4. Data ownership and redisplay

This is where the framework's G1/G2/G3 gates get decided, and it is genuinely under-appreciated: **owning a copy of the data and being allowed to show it are different permissions, and some vendors grant the first without the second.**

| Provider | Store in our DB | Publicly redisplay | Redistribute / resell | Notes |
|---|---|---|---|---|
| Sportmonks | Yes `[REPORTED]` — caching in your own database explicitly permitted | Yes, in your own presentation `[REPORTED]` | Reselling data-as-data prohibited; feed redistribution requires written approval `[REPORTED]` | Cleanest published position of the self-serve vendors |
| API-Football | Yes `[REPORTED]` — storage/distribution/transfer permitted | **Ambiguous** `[REPORTED]` — they state they grant no publication licence and that rights must be sought from rights holders | Reselling prohibited without permission | The publication carve-out is the concerning part; get it in writing |
| The Odds API | `[UNKNOWN]` | `[UNKNOWN]` | `[UNKNOWN]` | Blocking question before Phase 4 |
| football-data.co.uk | `[UNKNOWN]` — free public download, terms not clearly stated | `[UNKNOWN]` | `[UNKNOWN]` | Fine for internal model training; get clarity before displaying it |
| Betfair Historical | Yes, under the Historical Data Licence `[REPORTED]` | `[UNKNOWN]` | Licensed arrangement exists `[REPORTED]` | Licence is explicitly designed for storage at scale |
| Opta / StatsBomb | Per negotiated contract | Per contract | Per contract | Everything is bespoke |

### Three structural points

**1. Derived output is materially safer than raw redisplay.** A model probability computed from a provider's results is our work product. A table of their raw match statistics reproduced verbatim is their data. Where a licence is ambiguous, lean the product toward *derived* presentation — ratings, probabilities, edges, form indices — and be sparing with verbatim stat dumps. This is a product-design consequence of a legal constraint, and it happens to also be the more differentiated product.

**2. Fixture lists and results carry third-party rights independent of your API contract.** In some jurisdictions, football fixture lists are separately licensed by rights bodies. Your vendor's permission to *give* you data is not automatically permission to *publish* it. API-Football says this out loud; the others mostly don't. Budget for a lawyer's opinion before Phase 5 monetisation, not after.

**3. Never let odds licensing block model training.** Even under the most restrictive reading, using odds internally to fit and evaluate a model is a different act from displaying bookmaker prices to users. Design so that the *value engine* can run on data we may not be permitted to display, and the UI shows our derived edge rather than the vendor's price where terms are unclear.

---

# 5. Recommendation

### Primary: **Sportmonks** (football facts)

Growth €99/mo + xG & Pressure Index €24/mo + historical data €29 one-time ≈ **€123/mo** to start; drop to Starter €29 during Phase 0–2 while only one or two leagues matter.

Why: it is the only shortlisted vendor with (a) published self-serve pricing, (b) an explicit and permissive published position on storing data in your own database — which is the G1 gate the whole architecture depends on — and (c) xG available as a €24 add-on rather than an enterprise negotiation. Rate limits are expressed per entity per hour, which suits batch jobs better than a global daily cap.

Supplies: competitions, seasons, teams, fixtures, results, home/away and team statistics, standings, lineups, injuries, live scores, match events, xG (add-on), pre-match and in-play odds (add-on).

### Backup / cross-validation: **API-Football**

≈$19–29/mo. Broad coverage, cheap enough to run permanently as a *second* `data_sources` row rather than only as a failover. Its real value is the reconciliation job: two independent sources disagreeing on a scoreline is how you catch corruption before it reaches the model. Its publication-rights carve-out makes it a poor *primary*.

Supplies: the same fact categories, used for discrepancy detection and hot-swap capability.

### Odds: a three-part strategy, and the most important recommendation here

**Do not buy historical odds. Start manufacturing them in week one.**

1. **Backtest spine — football-data.co.uk CSV (free).** Deep history with explicit closing-odds columns across the major European leagues. This gives the model a training and evaluation baseline on day one at zero cost. Verify terms before displaying any of it; internal model training is the intended use.
2. **CLV benchmark — Betfair Historical Data.** BSP is a transacted settlement price, not an advertised one, and is the most honest closing line available. Start with the free tier to validate the pipeline; buy the paid tier only once CLV measurement is actually driving decisions.
3. **Forward capture — our own `odds_snapshots`, from week one.** Poll a live odds source (The Odds API, or Sportmonks' odds add-on) on our own cadence and store our own time series. After one season we own a proprietary closing-odds dataset for exactly our fixture universe, at exactly our granularity, that no vendor can revoke or reprice.

That third point reframes the hardest requirement in the brief from an expensive permanent dependency into a cheap operational habit. It is the highest-leverage decision in this document.

### Canonical internal model — the shape it must take

The stress test below forces four changes to `ARCHITECTURE.md` v1. The canonical model should be:

- **Provider-neutral.** Internal UUIDs only. `external_ids(source, entity_type, external_id, internal_id, valid_from, valid_to, confidence)` — with validity, because provider IDs get merged and reused.
- **Bitemporal on every correctable fact.** Not just `known_at`, but full revision history: `match_results` and `match_stats` become append-only revision tables with `(known_at, revision, superseded_at)` and a "latest" view. Providers silently revise scores and xG; without this, point-in-time backtests are quietly wrong.
- **Names are time-scoped, entities are not.** `team_names(team_id, name, valid_from, valid_to)` and the same for competitions. `teams.canonical_name` becomes a view of the currently-valid name.
- **Fixture identity survives rescheduling.** Identity key is `(season_id, stage, leg, home_team_id, away_team_id)` — *not* one containing `kickoff_utc`. — **[SUPERSEDED 2026-09-08]** the identity key is **six** columns, not five: `replay_number` is part of it, so a replay is a new fixture rather than a duplicate-key violation (**`PHASE-0-SPEC.md` §6 rule 4**, **§12.2**, CLAUDE.md non-negotiable #4). The principle stated here — that identity never contains kickoff time — is unchanged and in force.
- **Coverage is declared, not discovered.** `competition_coverage(competition_id, season_id, has_xg, has_shots, has_lineups, ...)` so the model knows what it may rely on instead of silently imputing nulls.
- **Multi-source by construction.** Every fact row carries `source_id`. Two providers may assert the same fixture; reconciliation picks a winner and records the disagreement rather than overwriting.

---

# 6. Architecture stress test

Fourteen probes against `ARCHITECTURE.md` v1. **Six found real defects**, marked **DEFECT**. The rest found risks needing explicit design.

### 6.1 Database growth — **DEFECT**

Sizing the v1 odds design: ~5,000 fixtures/season across 10 leagues × ~20 bookmakers × ~29 selections (1X2 + 6 O/U lines + BTTS + 6 AH lines) ≈ 580 rows per snapshot × ~40 snapshots per fixture ≈ **23,000 rows per fixture, ~116M rows/year, roughly 15–20GB/year with indexes.** That exceeds a Supabase Pro disk allowance inside year one, and `raw_payloads` storing every response body verbatim is larger still.

Fix: **store price *changes*, not polls.** Insert only when `(fixture, book, market, line, selection)` price differs from the last stored value — typically an 80–95% reduction, and lossless for every purpose we have. Same for `raw_payloads`: store the body only when its hash differs from the previous response for that request signature. Then monthly partitions with export-to-Parquet-and-drop after 90 days for interval snapshots, retaining opening and closing rows at full fidelity forever. — **[SUPERSEDED 2026-09-07]** the deduplication instinct was right, the mechanism changed: distinct bodies are stored once in unpartitioned `raw_payload_bodies` under a global `UNIQUE (hash_algo, body_hash)`, and every fetch is recorded as a `raw_payloads` observation. Dedup is therefore global rather than per-request-signature, and no mutable `seen_count` exists. Authoritative: **`PHASE-0-SPEC.md` §9.1** and **§9.3**.

### 6.2 Prediction versioning — **DEFECT**

`predictions.is_current` is ambiguous once a shadow model runs alongside the active one: current *for which version*? Fix: drop the boolean, add `superseded_at timestamptz NULL` with a partial unique index on `(fixture_id, model_version_id) WHERE superseded_at IS NULL`.

Second, unspecified in v1: what happens on model promotion mid-gameweek? Rule: **on promotion, re-predict all not-yet-started fixtures; never alter predictions for fixtures that have kicked off.** A published prediction is a historical claim and is immutable.

### 6.3 Historical backtesting — **DEFECT**

`feature_snapshots` records what the model saw at prediction time. But backtesting a *new* model on old fixtures rebuilds features as-of a past cutoff from *current* tables — and those tables have been corrected since. `known_at` alone does not solve this: a revised xG value overwrites the original and silently leaks a future correction into a "point-in-time" backtest. Requires the bitemporal fact tables from §5. This is the most subtle defect in the document and the one most likely to produce a model that backtests beautifully and underperforms live.

### 6.4 Data corrections — **DEFECT**

v1's `match_results` is one row per fixture; an UPDATE destroys the record the model actually trained on. Beyond the bitemporal fix: a corrected result must **re-settle** affected `prediction_outcomes` and re-run `model_performance` — as *new* rows, never by rewriting. And it must alert: "N predictions re-settled after a result correction." Silent corrections are precisely how published accuracy figures quietly become false.

### 6.5 Fixture rescheduling — **DEFECT**

v1's `UNIQUE(season_id, home_team_id, away_team_id, kickoff_utc)` contains the kickoff time, so a rescheduled match inserts a **duplicate** instead of updating. Fix: key on `(season_id, stage, leg, home_team_id, away_team_id)`. — **[SUPERSEDED 2026-09-08]** the identity key is **six** columns, not five: `replay_number` is part of it, so a replay is a new fixture rather than a duplicate-key violation (**`PHASE-0-SPEC.md` §6 rule 4**, **§12.2**, CLAUDE.md non-negotiable #4). The principle stated here — that identity never contains kickoff time — is unchanged and in force.
Additional rule: **a kickoff change greater than 24 hours invalidates every prediction for that fixture** — the teams' form, injuries and rest have changed. Abandoned matches replayed from 0-0 are a *new* fixture; awarded results are a settlement event, not a football result, and must be flagged so they never train the goals model.

### 6.6 Team renames — **DEFECT (shared with 6.7)**

Updating `teams.canonical_name` in place loses history, and a 2015 match page then displays a 2026 name. Fix: `team_names(team_id, name, valid_from, valid_to)`; render the name valid at the match date. Also design for: mergers (two `team_id`s collapse to one — needs a `merged_into_team_id` tombstone, never a delete), phoenix clubs (legally new entities whose statistical continuity is a judgement call — make it an explicit, recorded decision), and reserve/U21 sides, which must never resolve to the senior team.

### 6.7 Competitions changing names — risk

Same validity-scoped-names fix. The larger hazard is **format** change, not name change: the Champions League league-phase reform broke every schema that assumed group stages. Fix: `seasons.format` descriptor, a `stage` column on fixtures, and no code path that assumes a league structure. Strip sponsor names from canonical competition names.

### 6.8 Provider ID changes — risk

v1's `external_ids` maps 1:1 forever. Providers **merge and reuse** IDs; when they do, a stale mapping points at the wrong team and nothing errors. Fix: `valid_from`/`valid_to` plus a nightly reconciliation job that re-checks the provider's current name for each mapped ID against our alias set and raises a review item on mismatch. **Never trust a mapping indefinitely just because it was correct once.**

### 6.9 Duplicate fixtures — risk

Four distinct sources, and v1 handles none cleanly: rescheduling (6.5); two providers each creating a fixture; cup replays; and **two-legged ties, where the same pair legitimately meets twice in one season** — a constraint keyed on `(season, home, away)` would wrongly reject the second leg, which is why `stage` and `leg` belong in the key. Cross-provider deduplication is not a constraint problem: it needs a scoring function (same teams, kickoff within ±3 days, same competition) feeding a human review queue.

### 6.10 Missing statistics — risk

Lower leagues frequently lack xG and sometimes shots. v1's feature builder would emit nulls and the model would impute silently — and silent imputation is a correctness disaster, because the model cannot distinguish "zero shots" from "unknown". Fix: the `competition_coverage` table from §5; models declare required features; **a fixture gets no prediction from a model whose required features are unavailable** rather than a quietly degraded one. Store a `feature_completeness` score and surface confidence in the UI.

### 6.11 Late-arriving data — risk

xG and detailed stats often land hours or days after full time. v1's `rebuild_ratings` is clock-driven and nightly, so a match whose stats arrive two days late is silently excluded from the rating computed the night after. Fix: make rating jobs **watermark-driven** — track `stats_complete_at` per fixture and recompute any rating window containing newly-completed fixtures. Constraint: late data may update ratings going forward but must never retroactively alter a published prediction.

### 6.12 Timezone handling — risk

"Store UTC" is necessary and not sufficient. The **match date is not the UTC date**: a 20:00 kickoff in Brazil falls on the next UTC day, so "today's fixtures" is wrong for a whole continent. Fix: store `kickoff_utc` plus a stored `local_date` (competition-local calendar date) and index it; compute "today" in the *user's* timezone for personal views and the *competition's* for league pages. Also: DST transitions move kickoffs, and some providers publish local times without an offset — reject any ingested time lacking an explicit offset rather than guessing.

### 6.13 Odds snapshots — risk

Four hidden problems. (a) Books offer **different lines** — averaging a 2.5 and a 2.75 total is meaningless; group strictly by line, or normalise before comparing. (b) A suspended market returns no price, and **absence is not "no change"** — without an explicit `is_available` flag you will interpolate straight through suspensions, which is when prices are most informative. (c) "Closing" is ambiguous when kickoff moves: define it as the last available price before *actual* kickoff, captured by a job triggered on `status → live`, not on the scheduled time. (d) We capture price but not **stake limits**, and at the margin limits matter more than price for whether an edge is real. Record this as a known, accepted blind spot rather than discovering it later.

### 6.14 Model reproducibility — risk

`model_versions.git_sha` is insufficient. Results drift from library versions (numpy/scipy change results at the margin), unordered SQL feeding a fit, floating-point non-associativity under parallel aggregation, and unseeded randomness. Fix: pin the entire Python environment with a lockfile and store its hash in `model_versions.params`; pin the container image by digest; mandate `ORDER BY` in every feature query; fix the reduction order in fitting. **Test:** re-running a stored `model_version` against its stored `feature_snapshot` must reproduce the stored predictions to within 1e-9. If that test cannot pass, the public performance page is not defensible.

---

# A. Decisions to lock now

1. **Drizzle** as ORM; `generate` + `migrate` only, never `push`; partitioned tables and matviews owned by `--custom` SQL migrations inside the same ledger and excluded via `tablesFilter`. — **[SUPERSEDED 2026-09-06, in part]** The ORM choice, the `generate`/`migrate`-only rule, the `push` ban and the single-ledger requirement all stand. Only the exclusion mechanism is wrong: `tablesFilter` does not exclude anything from `generate`. Raw-SQL-owned objects are excluded by directory — see **`PHASE-0-SPEC.md` §8.1**.
2. **The database is the engine↔app interface** (D2). Unchanged.
3. **One scoreline matrix derives every market** (D3). Unchanged.
4. **Provider-neutral core**: internal UUIDs, `external_ids` with validity ranges, `raw_payloads` archive, adapter interface. No provider ID is ever a primary key.
5. **The odds source is decoupled from the football-data source** — separate vendors, separate contracts, separate adapters.
6. **Bitemporal facts**: `match_results` and `match_stats` are append-only revision tables. Non-negotiable, and near-impossible to retrofit.
7. **Fixture identity key** = `(season_id, stage, leg, home_team_id, away_team_id)`. Never includes kickoff time. — **[SUPERSEDED 2026-09-08]** the identity key is **six** columns, not five: `replay_number` is part of it, so a replay is a new fixture rather than a duplicate-key violation (**`PHASE-0-SPEC.md` §6 rule 4**, **§12.2**, CLAUDE.md non-negotiable #4). The principle stated here — that identity never contains kickoff time — is unchanged and in force.
8. **Validity-scoped names** for teams and competitions.
9. **Append-only predictions, ratings, odds**; `superseded_at` + partial unique index, not `is_current`.
10. **Odds capture begins in week one**, storing changes rather than polls.
11. **The model/odds wall** — the prediction engine never reads bookmaker odds.
12. **UTC plus competition-local date**, both stored.
13. **Full environment pinning** for model reproducibility.

Items 6, 7 and 11 are the ones that are effectively impossible to add later. Get them right in Phase 0.

# B. Decisions to defer

- **Which football provider** — lock the *interface* now, trial two in parallel with the §2 Tier-4 test, decide at the end of Phase 1. The architecture makes this cheap to defer, which is the entire point.
- **Which live-odds vendor** — not needed until forward capture starts in earnest; the free spine covers Phase 2.
- **Paid Betfair tier** — start free, upgrade only when CLV drives decisions.
- **Live/in-play** — Phase 8. Do not pay for a live feed now.
- **Player-level data, lineups, injuries** — reserve schema space, build in Phase 7.
- **Premium/free feature line and pricing** — Phase 5.
- **Correct-score and exotic markets in the UI** — the matrix already produces them; expose later.
- **Model ensembling, per-competition models** — Phase 7.
- **Opta/StatsBomb** — revisit only with real revenue.
- **Mobile app or public API product** — not before `packages/contracts` has a second consumer.

# C. Risks to explicitly design around

| Risk | Design response |
|---|---|
| Silent provider revisions rewriting history | Bitemporal facts; two-week re-pull diff as an acceptance test; correction alerts |
| Look-ahead leakage via corrected data (6.3) | Bitemporal reconstruction, not just `known_at`; leakage tests in CI |
| Odds volume outrunning the DB budget (6.1) | Store changes not polls; partition; tier to cold storage at 90 days |
| Publication rights ambiguity (§4) | Favour derived presentation over verbatim redisplay; legal opinion before Phase 5 |
| Closing odds being unbuyable | Manufacture our own from week one — the asset compounds and cannot be revoked |
| Silent null imputation in low-coverage leagues (6.10) | Declared coverage profiles; refuse to predict rather than degrade quietly |
| Entity drift and ID reuse (6.8) | Validity-ranged mappings; nightly reconciliation with a human review queue |
| Irreproducible models (6.14) | Environment pinning; a reproduction test that must pass before any promotion |
| Rescheduling corrupting predictions (6.5) | Stable identity key; >24h move invalidates predictions |
| Vendor lock-in | Two live sources from Phase 1, not one — the second pays for itself in error detection |

# D. Recommended Phase 0 implementation order

Strictly ordered; each step is verifiable before the next begins.

1. **Monorepo skeleton** — pnpm workspaces, Turborepo, `apps/web`, `apps/engine`, `packages/db`, CI running lint + typecheck. No features.
2. **Local Postgres via Docker** with a seed script. Never point development at a provider.
3. **Schema v1 in Drizzle** — reference and canonical entities only (countries, competitions, seasons, teams, team_names, aliases, venues, fixtures), with the corrected identity keys from A7 and A8 baked in from the first migration.
4. **The bitemporal fact tables** — `match_results`, `match_stats` as revision tables with their "latest" views. Do this before any data exists, because retrofitting is brutal.
5. **Provider abstraction tables** — `data_sources`, `external_ids` with validity, `raw_payloads` (partitioned, via a custom SQL migration), `ingestion_runs`, `job_runs`. Prove the partition + `tablesFilter` pattern works end to end here, on the least important table, before it matters. — **[SUPERSEDED 2026-09-06]** There is no "`tablesFilter` pattern": see **`PHASE-0-SPEC.md` §8.1–§8.2**. The instinct to prove the pattern early was right and survives as P0-03; the mechanism named here was wrong. Note that §D of this document is superseded in its entirety by **`PHASE-0-SPEC.md` §D**. **[SUPERSEDED 2026-09-07]** `ingestion_runs` is also not built — `job_runs` absorbs it (**`PHASE-0-SPEC.md` §9.2**) — and `raw_payloads` is now split from `raw_payload_bodies` (**§9.1**).
6. **`ProviderAdapter` interface + a throwaway adapter** against a free source (football-data.org, or football-data.co.uk CSVs). Goal is to exercise the seam, not to pick a vendor.
7. **Load the football-data.co.uk historical spine** — results plus opening and closing odds for two or three leagues. This becomes the backtest baseline and validates the odds schema against real, messy data.
8. **Entity resolution + the data-quality assertion suite** — aliases, the review queue, and the checks from `ARCHITECTURE.md` §7 running as part of ingest.
9. **The two DB roles** (`app_rw`, `engine_rw`) with explicit grants, plus RLS on user tables — even though no user tables have data yet. Grants are the enforcement mechanism for the §4B boundary; add them before there is anything to protect.
10. **The reproducibility harness** — environment pinning, `reproduce.py`, and the bit-for-bit test, with a trivial placeholder model. Proving the harness works on a toy model is cheap; proving it on a real one after the fact is not.
11. **Provider trials in parallel** — Sportmonks and API-Football free trials, running the §2 Tier-4 test including the two-week re-pull diff. Phase 0 ends when this produces a decision.

Steps 1–5 are roughly a week. Step 7 is where the project first becomes real — a queryable database of historical results and closing odds, with no vendor commitment and no money spent.
