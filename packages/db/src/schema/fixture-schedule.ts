import { sql } from "drizzle-orm";
import {
  bigint,
  boolean,
  check,
  date,
  index,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";
import { fixtures } from "./fixtures.ts";
import { rawPayloadBodies } from "./raw-payload-bodies.ts";
import { venues } from "./venues.ts";

/**
 * When, where and in what administrative state - the bitemporal fact layer for
 * a fixture (PHASE-0-SPEC.md §12.4, §2.1, §6 rules 1-3 and 11).
 *
 * Read it ONLY through fixture_schedule_as_of(cutoff), never by hand-writing
 * the visibility predicate (§11.1). A reschedule, a venue change and a status
 * transition are all the same operation: append a revision, close the old one.
 *
 * WHY status LIVES HERE AND NOT ON fixtures. If status were a mutable column
 * on the identity row, every backtest reading a fixture would see its FINAL
 * status - and `ft` is post-kickoff knowledge by definition. There would be no
 * error and no NULL: a feature builder asking "is this match on?" three days
 * before kickoff would be answered with the fact that the match finished. The
 * same argument forbids denormalising kickoff_utc (§12.7).
 *
 * Carries the three provenance columns because this IS a fact - two providers
 * can disagree about a kickoff time (§12.3, CLAUDE.md #6).
 */
export const fixtureSchedule = pgTable(
  "fixture_schedule",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    fixtureId: uuid("fixture_id")
      .notNull()
      .references(() => fixtures.id),
    kickoffUtc: timestamp("kickoff_utc", { withTimezone: true }).notNull(),
    /**
     * The competition-local calendar date, computed and stored AT INGEST, not
     * generated - a competition's timezone can itself change (§6 rule 11).
     * It exists because the match date is not the UTC date: a 20:00 kickoff in
     * Brazil falls on the next UTC day, so "today's fixtures" computed from the
     * UTC date is wrong for a whole continent.
     */
    localDate: date("local_date").notNull(),
    /** IANA zone name. Validated by the local_date CHECK below, which raises 22023. */
    localTz: text("local_tz").notNull(),
    /**
     * No team owns a venue (§10.1). Shared grounds, temporary relocations and
     * neutral finals all resolve because the SCHEDULE carries the venue.
     * Nullable: NULL means "not provided", never a sentinel.
     */
    venueId: uuid("venue_id").references(() => venues.id),
    /** Administrative state only. Never a football result - see the CHECK. */
    status: text("status").notNull(),
    /**
     * Home advantage is a direct model input (ARCHITECTURE.md §5), and a
     * neutral-ground match scored as a home match is a silent modelling error:
     * no NULL, no exception, just a systematically wrong prior on every neutral
     * fixture. It belongs on the schedule because the designation moves with
     * the venue, and a relocation is a revision (§12.4).
     */
    isNeutralVenue: boolean("is_neutral_venue").notNull().default(false),
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
    // Exactly one current revision per fixture. Unique indexes are NOT
    // deferrable, so a supersession is two ordered statements - close the old
    // revision, then insert the new one - never a single statement (§10.4).
    uniqueIndex("fixture_schedule_current_idx")
      .on(t.fixtureId)
      .where(sql`${t.supersededAt} IS NULL`),
    // The parent-key lookup, and the primary access path for a historical
    // as-of read. It must be a FULL index: the partial current index above
    // cannot serve one, because the as-of predicate admits rows whose
    // superseded_at is later than the cutoff rather than NULL (§12.8).
    index("fixture_schedule_fixture_idx").on(t.fixtureId),
    // The calendar query. There is deliberately NO index on known_at: the
    // query probe found no consumer for one (§12.8).
    index("fixture_schedule_kickoff_idx")
      .on(t.kickoffUtc)
      .where(sql`${t.supersededAt} IS NULL`),
    // Seven administrative states, and only these. `awarded`, `walkover` and
    // `forfeit` are RESULT semantics and belong to match_results.result_source
    // in P0-08: an awarded score is an administrative outcome that must never
    // train the goals model, though it still settles bets (§6 rule 3).
    check(
      "fixture_schedule_status_check",
      sql`${t.status} IN ('scheduled','live','suspended','ft','postponed','abandoned','cancelled')`,
    ),
    check(
      "fixture_schedule_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
    // Declarative because timezone(text, timestamptz) is genuinely IMMUTABLE on
    // 17.6 - proven by an index on the bare expression being accepted, since
    // index expressions ARE strictly checked while CHECK constraints are not
    // (§12.4). Rejects a UTC-date local_date with 23514 and an unknown zone
    // with 22023. No trigger; the verifier is the secondary guard against a
    // future tzdata update silently invalidating stored rows.
    check(
      "fixture_schedule_local_date_check",
      sql`${t.localDate} = (${t.kickoffUtc} AT TIME ZONE ${t.localTz})::date`,
    ),
  ],
);
