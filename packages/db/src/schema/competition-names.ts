import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  date,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { competitions } from "./competitions.ts";

/**
 * Temporal competition names - DISPLAY TRUTH (PHASE-0-SPEC.md §10.3).
 *
 * The Championship was Coca-Cola, then npower, then Sky Bet, then EFL inside
 * fifteen years. The canonical name is sponsor-free; sponsored names are their
 * own name_type and coexist with it.
 *
 * Intervals are half-open [valid_from, valid_to); valid_to IS NULL means
 * current. Rendering uses the name valid at the fixture's local_date.
 *
 * No source_id: a competition's official name is not a provider's opinion
 * (§10.3). Provider spellings belong in aliases, provider keys in external_ids.
 *
 * Append-and-close: engine_rw is granted UPDATE (valid_to) only, so a row can
 * be closed but never edited (§10.6).
 */
export const competitionNames = pgTable(
  "competition_names",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    competitionId: uuid("competition_id")
      .notNull()
      .references(() => competitions.id),
    name: text("name").notNull(),
    nameType: text("name_type").notNull(),
    validFrom: date("valid_from").notNull(),
    validTo: date("valid_to"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // At most one CURRENT name per type. Historical overlap is permitted; an
    // exclusion constraint would need btree_gist for no practical gain (§10.4).
    uniqueIndex("competition_names_current_idx")
      .on(t.competitionId, t.nameType)
      .where(sql`${t.validTo} IS NULL`),
    check(
      "competition_names_name_type_check",
      sql`${t.nameType} IN ('official','sponsored','short','abbreviation')`,
    ),
    check(
      "competition_names_interval_check",
      sql`${t.validTo} IS NULL OR ${t.validTo} > ${t.validFrom}`,
    ),
  ],
);
