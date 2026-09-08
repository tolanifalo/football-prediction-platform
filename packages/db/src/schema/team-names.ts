import { sql } from "drizzle-orm";
import {
  bigint, check, date, pgTable, text, timestamp, uniqueIndex, uuid,
} from "drizzle-orm/pg-core";

import { teams } from "./teams.ts";

/**
 * Temporal team names - DISPLAY TRUTH (PHASE-0-SPEC.md §10.3).
 *
 * Half-open [valid_from, valid_to); valid_to IS NULL means current. Historical
 * pages render the name valid at the match date, so a rename cannot corrupt
 * history: the fixture keeps pointing at team_id and only the name lookup is
 * date-scoped.
 *
 * NO source_id (§10.3, superseding §6 rule 7). A club's official name is not a
 * provider's opinion - provider spellings are team_aliases, provider primary
 * keys are external_ids (P0-06).
 *
 * Append-and-close: engine_rw gets UPDATE (valid_to) only (§10.6).
 */
export const teamNames = pgTable(
  "team_names",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    teamId: uuid("team_id").notNull().references(() => teams.id),
    name: text("name").notNull(),
    nameType: text("name_type").notNull(),
    validFrom: date("valid_from").notNull(),
    validTo: date("valid_to"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // At most one CURRENT name per type; an official and a common name may be
    // current simultaneously (§10.4).
    uniqueIndex("team_names_current_idx")
      .on(t.teamId, t.nameType)
      .where(sql`${t.validTo} IS NULL`),
    check(
      "team_names_name_type_check",
      sql`${t.nameType} IN ('official','common','short','abbreviation')`,
    ),
    check("team_names_interval_check", sql`${t.validTo} IS NULL OR ${t.validTo} > ${t.validFrom}`),
  ],
);
