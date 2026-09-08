import { sql } from "drizzle-orm";
import {
  bigint,
  boolean,
  check,
  index,
  numeric,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";
import { oddsSeries } from "./odds-series.ts";
import { rawPayloadBodies } from "./raw-payload-bodies.ts";

/**
 * One market observation (PHASE-0-SPEC.md §14.4).
 *
 * ITS TEMPORAL CARDINALITY IS DELIBERATELY UNLIKE EVERY EARLIER FACT TABLE.
 * P0-06, P0-07 and P0-08 each have exactly ONE current row per business key.
 * odds_ticks has MANY simultaneously-current rows per series, on purpose:
 *
 *   - several sources may report the same bookmaker's price
 *   - several price kinds coexist (an observed tick and a provider_closing)
 *   - observations at different instants are the entire point of a price path
 *
 * Only the exact (series_id, source_id, observed_at, price_kind) current
 * duplicate is forbidden. A reviewer carrying the "one current row" pattern
 * forward from the earlier tables will misread this table (G10).
 *
 * FOUR TIMESTAMPS, NONE COLLAPSIBLE (G20):
 *
 *   observed_at   the substantiated observation-time convention. It asserts
 *                 "this price was on offer at this instant" and asserts
 *                 NOTHING about when it started being on offer. Polling tells
 *                 us when we LOOKED, never when the bookmaker CHANGED a price.
 *   provider_at   the provider's own timestamp. EXPLICITLY UNTRUSTED - kept
 *                 for forensics, never used for ordering or cutoffs.
 *   known_at      when the observation entered our database.
 *   superseded_at when our belief about the observation was superseded.
 *
 * PRICE_KIND IS ALSO THE INTERPRETATION KEY FOR observed_at:
 *   observed          -> a real poll instant; we were looking.
 *   provider_opening  -> a CONVENTION; the source gives a price with no
 *   provider_closing     substantiated instant, so the adapter must document
 *                        and apply one consistently.
 *   exchange_sp       -> a defined settlement instant (BSP is struck at the off).
 *
 * A reproducibility read must therefore filter price_kind = 'observed': a
 * provider_closing tick's observed_at is a convention we invented, and letting
 * it into a point-in-time snapshot would offer a price nobody could have taken.
 *
 * Append-only. A mis-parse is retracted with superseded_at, never deleted, so
 * a cutoff before the retraction still sees what we believed then.
 *
 * NOT PARTITIONED (G12). Measured: partitioning by observed_at made the
 * dominant query - latest price for one series - cost 42 buffers against 1,
 * with 2,020 planning buffers against 36, because series_id says nothing about
 * which month a tick lives in. Revisit at ~20-30M rows or when retention is
 * designed; §8.1's listing of this table as partitioned is superseded.
 */
export const oddsTicks = pgTable(
  "odds_ticks",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    seriesId: bigint("series_id", { mode: "bigint" })
      .notNull()
      .references(() => oddsSeries.id),
    /** Who SUPPLIED the observation. Distinct from whose price it is (§14.2). */
    sourceId: uuid("source_id")
      .notNull()
      .references(() => dataSources.id),
    /** Single-column FK to the UNPARTITIONED bodies table - why §9.1 split the archive. */
    rawPayloadBodyId: bigint("raw_payload_body_id", { mode: "bigint" })
      .notNull()
      .references(() => rawPayloadBodies.id),
    /**
     * Decimal odds, exact. Never float: §5.2 names floating-point reduction
     * order as a determinism hazard. NULL only when the market is suspended.
     */
    price: numeric("price", { precision: 9, scale: 4 }),
    /**
     * A suspended market returns no price, and absence is NOT "no change"
     * (§6.13b). Without this flag a reconstruction interpolates straight
     * through suspensions - which cluster at exactly the informative moments.
     */
    isAvailable: boolean("is_available").notNull(),
    priceKind: text("price_kind").notNull().default("observed"),
    observedAt: timestamp("observed_at", { withTimezone: true }).notNull(),
    providerAt: timestamp("provider_at", { withTimezone: true }),
    knownAt: timestamp("known_at", { withTimezone: true }).notNull().defaultNow(),
    supersededAt: timestamp("superseded_at", { withTimezone: true }),
  },
  (t) => [
    // NOT keyed on price. Two identical prices at different instants are two
    // legitimate observations and both must survive - keying on price would
    // destroy real history, which is the P0-04 lesson.
    uniqueIndex("odds_ticks_current_idx")
      .on(t.seriesId, t.sourceId, t.observedAt, t.priceKind)
      .where(sql`${t.supersededAt} IS NULL`),
    // Serves latest-price, price-path, closing-comparison and cutoff reads.
    index("odds_ticks_series_time_idx").on(t.seriesId, t.observedAt.desc()),
    check(
      "odds_ticks_price_kind_check",
      sql`${t.priceKind} IN ('observed','provider_opening','provider_closing','exchange_sp')`,
    ),
    // Structural bound. 1.01-1000 for sportsbooks is a P0-14 quality rule;
    // exchange lay prices legitimately exceed 1000.
    check(
      "odds_ticks_price_range_check",
      sql`${t.price} IS NULL OR (${t.price} >= 1.01 AND ${t.price} <= 100000)`,
    ),
    check("odds_ticks_unavailable_has_no_price_check", sql`${t.isAvailable} OR ${t.price} IS NULL`),
    check(
      "odds_ticks_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
  ],
);
