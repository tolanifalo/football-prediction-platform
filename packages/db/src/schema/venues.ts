import { sql } from "drizzle-orm";
import { check, integer, numeric, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

import { countries } from "./countries.ts";

/**
 * Venue identity, independent of team identity (PHASE-0-SPEC.md §10.1, review §H).
 *
 * No team owns a venue. Shared grounds (San Siro), temporary relocations and
 * neutral finals all resolve because the FIXTURE carries venue_id (P0-07), not
 * the team. There is deliberately no team->venue column here.
 *
 * Plain mutable (§2.1): stadium names are not versioned. A sponsor rename is
 * cosmetic on a match page and is no model input.
 */
export const venues = pgTable(
  "venues",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    slug: text("slug").notNull().unique(),
    name: text("name").notNull(),
    city: text("city"),
    /** Nullable: NULL means "not provided", never a sentinel. */
    countryId: uuid("country_id").references(() => countries.id),
    capacity: integer("capacity"),
    latitude: numeric("latitude", { precision: 9, scale: 6 }),
    longitude: numeric("longitude", { precision: 9, scale: 6 }),
    openedYear: integer("opened_year"),
    closedYear: integer("closed_year"),
    status: text("status").notNull().default("active"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    check("venues_status_check", sql`${t.status} IN ('active','closed','demolished')`),
    check("venues_capacity_check", sql`${t.capacity} IS NULL OR ${t.capacity} >= 0`),
    check(
      "venues_closed_after_opened_check",
      sql`${t.closedYear} IS NULL OR ${t.openedYear} IS NULL OR ${t.closedYear} >= ${t.openedYear}`,
    ),
  ],
);
