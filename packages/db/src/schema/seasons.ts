import { sql } from "drizzle-orm";
import {
  boolean,
  check,
  date,
  integer,
  jsonb,
  pgTable,
  text,
  timestamp,
  unique,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { competitions } from "./competitions.ts";

/**
 * One running of a competition (PHASE-0-SPEC.md §10.2).
 *
 * Season and edition are the same concept. Apertura and Clausura are two
 * seasons of one competition with distinct labels and non-overlapping dates -
 * which is why there is deliberately NO date-overlap constraint: playoff tails
 * and split-season boundaries would trip it for no benefit.
 *
 * Dates are `date`, not timestamptz: a season has no clock. Fixtures do.
 *
 * `format` describes the structure. No code may assume a group stage exists
 * (§6 rule 6).
 */
export const seasons = pgTable(
  "seasons",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    competitionId: uuid("competition_id")
      .notNull()
      .references(() => competitions.id),
    /** Canonical label: "2023/24" for split-year, "2026" for calendar-year. */
    label: text("label").notNull(),
    /** Sortable key that works for both label shapes. */
    startYear: integer("start_year").notNull(),
    startDate: date("start_date"),
    endDate: date("end_date"),
    isCurrent: boolean("is_current").notNull().default(false),
    format: jsonb("format").notNull().default({}),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    unique("seasons_competition_label_key").on(t.competitionId, t.label),
    // At most one current season per competition. Unique indexes are NOT
    // deferrable, so a handover must close the old season and open the new one
    // in two ordered statements, never a single UPDATE (§10.4).
    uniqueIndex("seasons_one_current_idx").on(t.competitionId).where(sql`${t.isCurrent}`),
    check(
      "seasons_dates_check",
      sql`${t.endDate} IS NULL OR ${t.startDate} IS NULL OR ${t.endDate} >= ${t.startDate}`,
    ),
    check("seasons_start_year_check", sql`${t.startYear} BETWEEN 1850 AND 2200`),
  ],
);
