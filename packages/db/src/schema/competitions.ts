import { sql } from "drizzle-orm";
import { boolean, check, integer, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

import { countries } from "./countries.ts";

/**
 * The continuing competition entity (PHASE-0-SPEC.md §10.2).
 *
 * A competition is the thing that persists; a SEASON is one running of it.
 * Season and edition are the same concept - do not model both. Stage, leg and
 * replay live on fixtures (P0-07), never here.
 *
 * gender / age_group / is_reserve_competition are not decoration: without them
 * the Women's Super League and the Premier League differ only by a name
 * string, and entity resolution crosses them (§6 rule 8).
 *
 * There is no name column - names are temporal, in competition_names.
 */
export const competitions = pgTable(
  "competitions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    /** NULL for continental and international competitions (§10.1). */
    countryId: uuid("country_id").references(() => countries.id),
    type: text("type").notNull(),
    /** League tier. NULL for cups and international competitions. */
    tier: integer("tier"),
    confederation: text("confederation"),
    gender: text("gender").notNull(),
    ageGroup: text("age_group").notNull().default("senior"),
    isReserveCompetition: boolean("is_reserve_competition").notNull().default(false),
    isActive: boolean("is_active").notNull().default(true),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    check(
      "competitions_type_check",
      sql`${t.type} IN ('league','domestic_cup','super_cup','continental','international')`,
    ),
    check("competitions_gender_check", sql`${t.gender} IN ('men','women')`),
    check(
      "competitions_age_group_check",
      sql`${t.ageGroup} IN ('senior','u23','u21','u20','u19','u18','u17')`,
    ),
    check("competitions_tier_check", sql`${t.tier} IS NULL OR ${t.tier} >= 1`),
    check(
      "competitions_confederation_check",
      sql`${t.confederation} IS NULL OR ${t.confederation} IN ('uefa','conmebol','concacaf','caf','afc','ofc','fifa')`,
    ),
  ],
);
