import { sql } from "drizzle-orm";
import {
  bigint,
  boolean,
  check,
  index,
  pgTable,
  smallint,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";
import { fixtures } from "./fixtures.ts";
import { rawPayloadBodies } from "./raw-payload-bodies.ts";

/**
 * The football result - fully bitemporal (PHASE-0-SPEC.md §13, §2.1, §2.3).
 *
 * Read it ONLY through match_results_as_of(cutoff), never by hand-writing the
 * visibility predicate (§11.1). A correction appends a revision and closes the
 * old one; nothing is ever overwritten, which is what makes a backtest honest
 * (§2.3).
 *
 * ONE CURRENT ROW PER (fixture, source) - NOT per fixture. Two providers may
 * assert the same match and disagree, and that disagreement must be visible
 * rather than destroyed (DECISIONS-01 §A). Proven: under a
 * (fixture_id)-only business key the second provider's result is rejected
 * 23505, which would make the §3.3 bake-off metrics - FT agreement, xG
 * agreement between providers on shared fixtures - impossible to compute.
 * P0-08 stores the disagreement and invents NO precedence rule; reconciliation
 * is P0-14 (§13.6).
 *
 * ADMINISTRATIVE STATUS IS NOT SPORTING TRUTH. Status lives on
 * fixture_schedule (P0-07). An abandoned fixture gets NO row here merely
 * because a partial score was observed - that observation stays in the raw
 * archive as evidence. Only an explicitly awarded result is recorded, as
 * `awarded` and never trainable (§6 rule 3, §13.4).
 */
export const matchResults = pgTable(
  "match_results",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    fixtureId: uuid("fixture_id")
      .notNull()
      .references(() => fixtures.id),
    /** 'played' = contested on the pitch. 'awarded' = a committee's decision. */
    resultSource: text("result_source").notNull(),
    /**
     * An awarded score is a settlement fact and a fictional football fact. It
     * settles bets and must NEVER train the goals model (§6 rule 3). The
     * awarded half of that rule is enforced below; the abandoned half spans
     * fixture_schedule and is a P0-14 assertion, not a constraint.
     */
    isTrainable: boolean("is_trainable").notNull(),
    htHome: smallint("ht_home"),
    htAway: smallint("ht_away"),
    /** After 90 minutes. NOT NULL: a result without a full-time score is not a result. */
    ftHome: smallint("ft_home").notNull(),
    ftAway: smallint("ft_away").notNull(),
    /**
     * CUMULATIVE score at the END of extra time, including the first 90
     * minutes - not the goals scored during extra time. The `aet >= ft` check
     * below is only sound under that reading, which is why the ambiguity is
     * settled here rather than left to each ingester.
     */
    aetHome: smallint("aet_home"),
    aetAway: smallint("aet_away"),
    /**
     * Shootout score. NEVER goals: a shootout decides a tie and must not reach
     * the goals model. Separate columns are how the schema keeps that true.
     */
    pensHome: smallint("pens_home"),
    pensAway: smallint("pens_away"),
    /** Valid time (§2.1): when the match concluded. Shared by every revision of it. */
    occurredAt: timestamp("occurred_at", { withTimezone: true }),
    sourceId: uuid("source_id")
      .notNull()
      .references(() => dataSources.id),
    /** Single-column FK to the UNPARTITIONED bodies table - why §9.1 split the archive. */
    rawPayloadBodyId: bigint("raw_payload_body_id", { mode: "bigint" })
      .notNull()
      .references(() => rawPayloadBodies.id),
    knownAt: timestamp("known_at", { withTimezone: true }).notNull().defaultNow(),
    supersededAt: timestamp("superseded_at", { withTimezone: true }),
  },
  (t) => [
    // The business key. Many sources per fixture; one CURRENT row per source.
    // Unique indexes are NOT deferrable, so supersession is two ordered
    // statements - close the old revision, then insert the new one (§10.4).
    uniqueIndex("match_results_current_idx")
      .on(t.fixtureId, t.sourceId)
      .where(sql`${t.supersededAt} IS NULL`),
    // FULL, not partial. The partial index above cannot serve an as-of read,
    // because the visibility predicate also admits rows whose superseded_at is
    // LATER than the cutoff. Without this index that lookup is a sequential
    // scan - measured, not assumed (§13.7).
    index("match_results_fixture_idx").on(t.fixtureId),
    check("match_results_source_check", sql`${t.resultSource} IN ('played','awarded')`),
    check(
      "match_results_awarded_untrainable_check",
      sql`${t.resultSource} <> 'awarded' OR ${t.isTrainable} = false`,
    ),
    check("match_results_ft_nonneg_check", sql`${t.ftHome} >= 0 AND ${t.ftAway} >= 0`),
    check("match_results_ht_pair_check", sql`(${t.htHome} IS NULL) = (${t.htAway} IS NULL)`),
    check(
      "match_results_ht_le_ft_check",
      sql`${t.htHome} IS NULL OR (${t.htHome} <= ${t.ftHome} AND ${t.htAway} <= ${t.ftAway})`,
    ),
    check("match_results_aet_pair_check", sql`(${t.aetHome} IS NULL) = (${t.aetAway} IS NULL)`),
    // Sound only because aet is cumulative.
    check(
      "match_results_aet_ge_ft_check",
      sql`${t.aetHome} IS NULL OR (${t.aetHome} >= ${t.ftHome} AND ${t.aetAway} >= ${t.ftAway})`,
    ),
    check("match_results_pens_pair_check", sql`(${t.pensHome} IS NULL) = (${t.pensAway} IS NULL)`),
    check(
      "match_results_pens_need_aet_check",
      sql`${t.pensHome} IS NULL OR ${t.aetHome} IS NOT NULL`,
    ),
    // A shootout that ends level did not happen.
    check(
      "match_results_pens_decisive_check",
      sql`${t.pensHome} IS NULL OR ${t.pensHome} <> ${t.pensAway}`,
    ),
    check(
      "match_results_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
  ],
);
