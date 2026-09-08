import { sql } from "drizzle-orm";
import { check, char, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

/**
 * Football associations and territories - NOT sovereign states
 * (PHASE-0-SPEC.md §10.1).
 *
 * England, Scotland, Wales and Northern Ireland have no ISO 3166-1 alpha-2
 * code (ISO gives them GB), and all four are launch competitions. Gibraltar,
 * the Faroe Islands and Curacao are FIFA members without sovereign status.
 * Both code columns are therefore nullable and neither is the identifier.
 *
 * Plain mutable reference data (§2.1): a country rename is cosmetic, is no
 * model input, and rewrites no fact.
 */
export const countries = pgTable(
  "countries",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    name: text("name").notNull(),
    /** Nullable: the UK home nations have none. */
    isoAlpha2: char("iso_alpha2", { length: 2 }),
    /** FIFA/IOC-style code (ENG, SCO, WAL, NIR). Nullable: not every territory is a member. */
    fifaCode: char("fifa_code", { length: 3 }),
    confederation: text("confederation"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    check(
      "countries_confederation_check",
      sql`${t.confederation} IS NULL OR ${t.confederation} IN ('uefa','conmebol','concacaf','caf','afc','ofc','fifa')`,
    ),
    check("countries_iso_alpha2_check", sql`${t.isoAlpha2} IS NULL OR ${t.isoAlpha2} ~ '^[A-Z]{2}$'`),
    check("countries_fifa_code_check", sql`${t.fifaCode} IS NULL OR ${t.fifaCode} ~ '^[A-Z]{3}$'`),
  ],
);
