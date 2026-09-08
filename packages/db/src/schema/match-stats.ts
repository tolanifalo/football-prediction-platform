import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  index,
  numeric,
  pgTable,
  smallint,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";
import { fixtures } from "./fixtures.ts";
import { rawPayloadBodies } from "./raw-payload-bodies.ts";

/**
 * Team statistics for a match - fully bitemporal (PHASE-0-SPEC.md §13, §2.1).
 *
 * ONE ROW PER (fixture, source, revision), with HOME/AWAY PAIRED COLUMNS. Not
 * one row per team. The reasons are concrete, not stylistic:
 *
 *   - The §3.3 plausibility rule "possession pair sums to 100 +/-1" is a
 *     single-row CHECK here and is IMPOSSIBLE per-team: a CHECK cannot contain
 *     a subquery (0A000).
 *   - There is NO team_id. Policing it would need a CHECK reading `fixtures`,
 *     and that was proven UNSOUND: the CHECK passes at write time, then the
 *     fixture can be repointed and the stored row is silently invalidated. The
 *     paired shape makes the invalid state unrepresentable instead.
 *   - There is NO is_home. It duplicates fixtures.home_team_id.
 *   - There is NO xga. One team's xga IS the other team's xg - two columns
 *     holding one fact, guaranteed to diverge.
 *
 * Revisions are INDEPENDENT of match_results: xG is routinely revised days
 * after a score is final, which is why §2.1 bitemporalises both separately.
 *
 * The metric set is exactly §3.3's fill-rate list plus xG - the only
 * authoritative field list in the specification, and what the bake-off scores.
 * Everything else (deep completions, PPDA, passes, lineups, per-period splits)
 * is added by the task that needs it, as an ordinary migration.
 *
 * NULL MEANS "NOT PROVIDED". ZERO MEANS ZERO (§6 rule 12). No column defaults
 * to 0, ever - a sentinel here is a silent modelling error.
 */
export const matchStats = pgTable(
  "match_stats",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    fixtureId: uuid("fixture_id")
      .notNull()
      .references(() => fixtures.id),
    homeShots: smallint("home_shots"),
    awayShots: smallint("away_shots"),
    homeShotsOnTarget: smallint("home_shots_on_target"),
    awayShotsOnTarget: smallint("away_shots_on_target"),
    homeCorners: smallint("home_corners"),
    awayCorners: smallint("away_corners"),
    homeFouls: smallint("home_fouls"),
    awayFouls: smallint("away_fouls"),
    homeYellowCards: smallint("home_yellow_cards"),
    awayYellowCards: smallint("away_yellow_cards"),
    homeRedCards: smallint("home_red_cards"),
    awayRedCards: smallint("away_red_cards"),
    /** Percentage. Exact numeric, never float. */
    homePossession: numeric("home_possession", { precision: 5, scale: 2 }),
    awayPossession: numeric("away_possession", { precision: 5, scale: 2 }),
    /**
     * Exact `numeric`, deliberately NOT double precision: §5.2 names
     * floating-point reduction order as a determinism hazard, and a
     * reproducibility harness that cannot reproduce its own inputs is worthless.
     */
    homeXg: numeric("home_xg", { precision: 6, scale: 3 }),
    awayXg: numeric("away_xg", { precision: 6, scale: 3 }),
    sourceId: uuid("source_id")
      .notNull()
      .references(() => dataSources.id),
    rawPayloadBodyId: bigint("raw_payload_body_id", { mode: "bigint" })
      .notNull()
      .references(() => rawPayloadBodies.id),
    knownAt: timestamp("known_at", { withTimezone: true }).notNull().defaultNow(),
    supersededAt: timestamp("superseded_at", { withTimezone: true }),
  },
  (t) => [
    uniqueIndex("match_stats_current_idx")
      .on(t.fixtureId, t.sourceId)
      .where(sql`${t.supersededAt} IS NULL`),
    // FULL, for the same reason as match_results (§13.7).
    index("match_stats_fixture_idx").on(t.fixtureId),
    // §3.3's plausibility rule, expressible only because the pair is one row.
    check(
      "match_stats_possession_pair_check",
      sql`(${t.homePossession} IS NULL AND ${t.awayPossession} IS NULL)
          OR (${t.homePossession} + ${t.awayPossession} BETWEEN 99 AND 101)`,
    ),
    check(
      "match_stats_home_sot_le_shots_check",
      sql`${t.homeShotsOnTarget} IS NULL OR ${t.homeShots} IS NULL
          OR ${t.homeShotsOnTarget} <= ${t.homeShots}`,
    ),
    check(
      "match_stats_away_sot_le_shots_check",
      sql`${t.awayShotsOnTarget} IS NULL OR ${t.awayShots} IS NULL
          OR ${t.awayShotsOnTarget} <= ${t.awayShots}`,
    ),
    check(
      "match_stats_counts_nonneg_check",
      sql`(${t.homeShots} IS NULL OR ${t.homeShots} >= 0)
          AND (${t.awayShots} IS NULL OR ${t.awayShots} >= 0)
          AND (${t.homeCorners} IS NULL OR ${t.homeCorners} >= 0)
          AND (${t.awayCorners} IS NULL OR ${t.awayCorners} >= 0)
          AND (${t.homeFouls} IS NULL OR ${t.homeFouls} >= 0)
          AND (${t.awayFouls} IS NULL OR ${t.awayFouls} >= 0)
          AND (${t.homeYellowCards} IS NULL OR ${t.homeYellowCards} >= 0)
          AND (${t.awayYellowCards} IS NULL OR ${t.awayYellowCards} >= 0)
          AND (${t.homeRedCards} IS NULL OR ${t.homeRedCards} >= 0)
          AND (${t.awayRedCards} IS NULL OR ${t.awayRedCards} >= 0)`,
    ),
    check(
      "match_stats_xg_nonneg_check",
      sql`(${t.homeXg} IS NULL OR ${t.homeXg} >= 0) AND (${t.awayXg} IS NULL OR ${t.awayXg} >= 0)`,
    ),
    check(
      "match_stats_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
  ],
);
