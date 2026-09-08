import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  index,
  numeric,
  pgTable,
  text,
  timestamp,
  unique,
  uuid,
} from "drizzle-orm/pg-core";

import { bookmakers } from "./bookmakers.ts";
import { fixtures } from "./fixtures.ts";

/**
 * The identity of one price series - which question, at which line, from which
 * firm, on which side (PHASE-0-SPEC.md §14.3).
 *
 * Stored ONCE and referenced by every tick. That normalisation plus the
 * change-only ingestion rule is what turns the 15-20 GB/year projection of
 * DECISIONS-01 §6.1 into low single-digit GB, losslessly (§4.2).
 *
 * IMMUTABLE AFTER INSERT (G14). No UPDATE is granted at all: repointing
 * `fixture_id` would silently re-attribute an entire price history to a
 * different match - the same hazard §10.6 names for team_aliases.team_id. A
 * mis-mapping is corrected by superseding the wrong series' ticks and
 * inserting under the right series.
 *
 * NULLS NOT DISTINCT ON THE BUSINESS KEY IS LOAD-BEARING (G2). `line` is NULL
 * for 1x2 and btts, and PostgreSQL treats NULLs as distinct by default - so an
 * ordinary UNIQUE accepts unlimited duplicate 1x2 series. Proven: two
 * byte-identical 1x2 series were both ACCEPTED under the default semantics.
 * The line-presence CHECK below is what makes the key sound: a market that
 * takes a line must have one, and a market that does not must not.
 */
export const oddsSeries = pgTable(
  "odds_series",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    fixtureId: uuid("fixture_id")
      .notNull()
      .references(() => fixtures.id),
    bookmakerId: uuid("bookmaker_id")
      .notNull()
      .references(() => bookmakers.id),
    /** Half-time markets are different markets sharing a market_type label (§4.1). */
    period: text("period").notNull(),
    marketType: text("market_type").notNull(),
    /**
     * The total or handicap. For asian_handicap the line is ALWAYS expressed
     * from the HOME team's perspective (G8): "Home -0.5" and "Away +0.5" are
     * the two selections of the single market (asian_handicap, -0.5), never
     * two markets. Without that rule one market has two encodings and
     * overround grouping silently breaks.
     */
    line: numeric("line", { precision: 6, scale: 2 }),
    /**
     * Free text with a documented convention, not a CHECK: enumerating every
     * selection now would fix a vocabulary before its consumer exists (§11.2).
     * 1x2: home|draw|away · over_under: over|under · btts: yes|no ·
     * asian_handicap: home|away.
     */
    selection: text("selection").notNull(),
    /**
     * A lay price is a different offer with its own price path, not a variant
     * of the back price - which is why exchange back and lay are separate
     * series rather than two columns on a tick.
     *
     * NOT coupled to bookmakers.kind by a CHECK, and it must not be (G21): a
     * sportsbook lay price is invalid data, but expressing that needs a
     * subquery across tables, which PostgreSQL rejects outright (0A000) - and
     * the function-based workaround was proven UNSOUND in P0-08, passing at
     * write time and silently invalidating when the referenced row changes.
     * It is a db:verify-odds assertion and a P0-14 data-quality rule.
     */
    side: text("side").notNull().default("back"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    unique("odds_series_key")
      .on(t.fixtureId, t.bookmakerId, t.period, t.marketType, t.line, t.selection, t.side)
      .nullsNotDistinct(),
    index("odds_series_fixture_idx").on(t.fixtureId),
    // Cross-bookmaker comparison: every book's price for one canonical market.
    index("odds_series_market_idx").on(
      t.fixtureId,
      t.period,
      t.marketType,
      t.line,
      t.selection,
    ),
    check("odds_series_period_check", sql`${t.period} IN ('ft','ht','2h')`),
    check("odds_series_side_check", sql`${t.side} IN ('back','lay')`),
    // Four markets. HT variants use period='ht'; deferred markets stay
    // deferred until a real consumer requires them (§14.3).
    check(
      "odds_series_market_type_check",
      sql`${t.marketType} IN ('1x2','over_under','btts','asian_handicap')`,
    ),
    // Required exactly for the two markets that take a line, forbidden for the
    // two that do not. This is what makes NULLS NOT DISTINCT sound.
    check(
      "odds_series_line_presence_check",
      sql`(${t.marketType} IN ('over_under','asian_handicap')) = (${t.line} IS NOT NULL)`,
    ),
  ],
);
