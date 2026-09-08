import { sql } from "drizzle-orm";
import {
  boolean,
  check,
  integer,
  pgTable,
  text,
  timestamp,
  uuid,
  type AnyPgColumn,
} from "drizzle-orm/pg-core";

import { countries } from "./countries.ts";

/**
 * Canonical team identity (PHASE-0-SPEC.md §10.2, §6 rules 7-8).
 *
 * THERE IS NO NAME COLUMN. Names are temporal and live in team_names. Removing
 * the column makes the correct behaviour the only possible behaviour: a rename
 * cannot silently relabel history because there is nothing to overwrite.
 *
 * gender / age_group / is_reserve / parent_team_id exist so a U21 or reserve
 * side can never resolve onto its senior club. parent_team_id makes the
 * relationship explicit rather than inferred from a name suffix.
 *
 * A phoenix club is a NEW team_id by default; asserting statistical continuity
 * is an explicit, recorded, reversible decision (`continuity`). Never delete a
 * team - tombstone it via `status`, which is why no DELETE is granted.
 */
export const teams = pgTable(
  "teams",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    countryId: uuid("country_id")
      .notNull()
      .references(() => countries.id),
    teamType: text("team_type").notNull().default("club"),
    gender: text("gender").notNull(),
    ageGroup: text("age_group").notNull().default("senior"),
    isReserve: boolean("is_reserve").notNull().default(false),
    /** Barcelona B -> Barcelona, Man Utd U21 -> Man Utd. */
    parentTeamId: uuid("parent_team_id").references((): AnyPgColumn => teams.id),
    status: text("status").notNull().default("active"),
    succeededByTeamId: uuid("succeeded_by_team_id").references((): AnyPgColumn => teams.id),
    continuity: text("continuity"),
    foundedYear: integer("founded_year"),
    crestUrl: text("crest_url"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    check("teams_team_type_check", sql`${t.teamType} IN ('club','national')`),
    check("teams_gender_check", sql`${t.gender} IN ('men','women')`),
    check(
      "teams_age_group_check",
      sql`${t.ageGroup} IN ('senior','u23','u21','u20','u19','u18','u17')`,
    ),
    check("teams_status_check", sql`${t.status} IN ('active','dissolved','merged')`),
    check(
      "teams_continuity_check",
      sql`${t.continuity} IS NULL OR ${t.continuity} IN ('legal','sporting','none')`,
    ),
    check("teams_parent_not_self_check", sql`${t.parentTeamId} IS NULL OR ${t.parentTeamId} <> ${t.id}`),
    check(
      "teams_successor_not_self_check",
      sql`${t.succeededByTeamId} IS NULL OR ${t.succeededByTeamId} <> ${t.id}`,
    ),
    check(
      "teams_founded_year_check",
      sql`${t.foundedYear} IS NULL OR ${t.foundedYear} BETWEEN 1800 AND 2200`,
    ),
  ],
);
