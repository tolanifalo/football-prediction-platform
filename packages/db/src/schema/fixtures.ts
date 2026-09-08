import { sql } from "drizzle-orm";
import {
  check,
  index,
  integer,
  pgTable,
  text,
  timestamp,
  unique,
  uuid,
  type AnyPgColumn,
} from "drizzle-orm/pg-core";

import { seasons } from "./seasons.ts";
import { teams } from "./teams.ts";

/**
 * Canonical fixture identity (PHASE-0-SPEC.md §12.2, §6 rules 4-5).
 *
 * THE IDENTITY KEY NEVER CONTAINS KICKOFF TIME. That is the v1 defect
 * DECISIONS-01 §6.5 records: keyed on kickoff, a rescheduled match inserts a
 * duplicate instead of revising. Kickoff, venue and status all live on
 * fixture_schedule, and a reschedule is a new revision there.
 *
 * This is an IDENTITY REGISTRY, not a fact table, so it carries no source_id,
 * no raw_payload_body_id and no known_at (§12.3). Provenance attaches to
 * claims two providers could disagree about; a fixture row is our own
 * assertion that a contest exists, keyed on six values that are immutable by
 * definition. `teams`, `competitions` and `seasons` are treated the same way.
 *
 * All six identity columns are NOT NULL and that is load-bearing: NULLs in a
 * unique index are distinct from one another, so a single NULL would silently
 * disable duplicate-fixture prevention entirely - no error, no warning, and a
 * constraint that appears to exist.
 */
export const fixtures = pgTable(
  "fixtures",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    seasonId: uuid("season_id")
      .notNull()
      .references(() => seasons.id),
    /**
     * Provider-independent structural stage, free text by design (§12.2).
     *
     * There is deliberately NO CHECK enumerating values: a closed set would fix
     * the vocabulary before the competitions that need it are known, and every
     * format reform would arrive as a migration on a constraint.
     *
     * It MUST distinguish structurally repeated meetings, including repeated
     * round-robin rounds. The Scottish Premiership plays a three-round
     * pre-split phase, so Celtic host Rangers TWICE in a season: same season,
     * same teams, same home side, one leg, no replay. Without a stage that
     * separates the rounds, that second meeting is a duplicate-key violation
     * and three of the four launch competitions cannot be loaded.
     *
     * Examples of the convention, not an enumeration: 'regular', 'regular_r1',
     * 'championship_split', 'group_a', 'semi_final', 'final'.
     */
    stage: text("stage").notNull(),
    /** 1 = single-leg fixture or first leg; 2 = second leg of a tie. */
    leg: integer("leg").notNull().default(1),
    /** 0 = the original fixture; 1, 2, ... = successive replays. */
    replayNumber: integer("replay_number").notNull().default(0),
    homeTeamId: uuid("home_team_id")
      .notNull()
      .references(() => teams.id),
    awayTeamId: uuid("away_team_id")
      .notNull()
      .references(() => teams.id),
    /**
     * Groups the two legs of a tie. A bare UUID with no `ties` table (§12.5):
     * aggregate score is a computation over two fixtures' results, performed in
     * Phase 1, not a registry with an identity of its own. Its presence is also
     * what distinguishes "leg 1 of a tie" from "the only leg".
     */
    tieId: uuid("tie_id"),
    /** A replay points at the fixture it replaces. A reschedule does not. */
    replacesFixtureId: uuid("replaces_fixture_id").references((): AnyPgColumn => fixtures.id),
    /**
     * Watermark for late-arriving stats (§6 rule 13). NEVER a feature input:
     * it is a mutable current-state column on a non-temporal table, so no
     * as-of query can hide it, and reading it at a pre-kickoff cutoff leaks
     * the fact that the match finished (§12.7).
     */
    statsCompleteAt: timestamp("stats_complete_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    unique("fixtures_identity_key").on(
      t.seasonId,
      t.stage,
      t.leg,
      t.replayNumber,
      t.homeTeamId,
      t.awayTeamId,
    ),
    // Team form reads both sides; PostgreSQL combines these under BitmapOr, so
    // the OR in the query is not a problem and needs no UNION ALL rewrite.
    index("fixtures_home_team_idx").on(t.homeTeamId),
    index("fixtures_away_team_idx").on(t.awayTeamId),
    check("fixtures_teams_differ_check", sql`${t.homeTeamId} <> ${t.awayTeamId}`),
    check("fixtures_leg_check", sql`${t.leg} IN (1,2)`),
    check("fixtures_replay_number_check", sql`${t.replayNumber} >= 0`),
    check(
      "fixtures_replaces_not_self_check",
      sql`${t.replacesFixtureId} IS NULL OR ${t.replacesFixtureId} <> ${t.id}`,
    ),
    // tie_id is non-null only for two-legged ties (§6 rule 5).
    check("fixtures_tie_leg_check", sql`${t.tieId} IS NULL OR ${t.leg} IN (1,2)`),
  ],
);
