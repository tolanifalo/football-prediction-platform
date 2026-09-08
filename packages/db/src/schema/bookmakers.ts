import { sql } from "drizzle-orm";
import { check, numeric, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

/**
 * The firms whose market prices we record (PHASE-0-SPEC.md §14.2).
 *
 * A BOOKMAKER IS NOT A DATA SOURCE. The two identities are deliberately
 * distinct and both are required on every observation:
 *
 *   source_id     (odds_ticks -> data_sources) = who SUPPLIED/TRANSMITTED it
 *   bookmaker_id  (odds_series -> bookmakers)  = whose MARKET PRICE it is
 *
 * An aggregator - Oddschecker, The Odds API, a vendor feed - transmits other
 * firms' prices, so it is a `data_sources` row and NEVER a bookmaker. That is
 * why `kind` has exactly two values (§14.2, G18): putting 'aggregator' here
 * would make the registry mean two different things at once.
 *
 * The separation is a feature rather than redundancy: Bet365's own API is a
 * data source, Bet365 the firm is a bookmaker, and recording both lets us
 * later compare "Bet365's price as Bet365 reported it" against "as an
 * aggregator reported it" - a real data-quality signal.
 *
 * Plain mutable reference data (§2.1), like countries and venues. A bookmaker
 * rename is cosmetic: it is no model input and rewrites no fact. This
 * deliberately differs from the P0-05 treatment of team names, on §2.1's
 * authority.
 *
 * KNOWN LIMITATION, recorded rather than absorbed: tote / pari-mutuel pools
 * are neither a bookmaker nor an exchange and are not representable here. No
 * Phase 0 or Phase 1 source supplies them; a third `kind` is not invented on
 * speculation (§14.2).
 */
export const bookmakers = pgTable(
  "bookmakers",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    /** Display truth, freely correctable. Not versioned (§2.1). */
    name: text("name").notNull(),
    kind: text("kind").notNull(),
    /** Where the firm accepts custom. NULL means "not provided", never a sentinel. */
    countryScope: text("country_scope"),
    /**
     * Exchange commission as a fraction, e.g. 0.0500 for 5%.
     *
     * PERMITTED FOR EXCHANGES, NEVER REQUIRED (G19). Betfair's rate varies by
     * market and by account discount, so mandating a value would invent one -
     * and §6 rule 12 forbids substituting a sentinel for "not provided". A
     * downstream EV calculation that finds NULL must refuse rather than assume
     * 5%, exactly as a model with unsatisfiable coverage returns no prediction
     * rather than a degraded one.
     */
    commissionRate: numeric("commission_rate", { precision: 5, scale: 4 }),
    /**
     * Consensus weighting input for Phase 4. Free text: enumerating tiers now
     * would fix a vocabulary before the code that uses it exists (§11.2).
     */
    sharpnessTier: text("sharpness_tier"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // Exactly two kinds. Aggregators are data_sources (§14.2, G18).
    check("bookmakers_kind_check", sql`${t.kind} IN ('bookmaker','exchange')`),
    // One-directional: only an exchange MAY carry a commission. Not mandated.
    check(
      "bookmakers_commission_only_exchange_check",
      sql`${t.commissionRate} IS NULL OR ${t.kind} = 'exchange'`,
    ),
    check(
      "bookmakers_commission_range_check",
      sql`${t.commissionRate} IS NULL OR (${t.commissionRate} >= 0 AND ${t.commissionRate} < 1)`,
    ),
  ],
);
