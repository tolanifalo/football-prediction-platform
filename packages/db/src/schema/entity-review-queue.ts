import { sql } from "drizzle-orm";
import { bigint, check, index, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";

/**
 * Records THAT a human decision is required (PHASE-0-SPEC.md §11.2).
 *
 * Deliberately minimal and generic. It performs no matching, holds no
 * algorithm, no score and no threshold, and is not responsible for entity
 * resolution. P0-06 creates it empty; P0-12 populates and operates it.
 *
 * `reason` is free text rather than a CHECK-constrained set: §11.2 offers
 * "ambiguous, unverified, conflicting" as examples, not as a closed
 * enumeration, and fixing the set here would constrain P0-12 on no authority.
 */
export const entityReviewQueue = pgTable(
  "entity_review_queue",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    entityType: text("entity_type").notNull(),
    sourceId: uuid("source_id").notNull().references(() => dataSources.id),
    externalId: text("external_id").notNull(),
    /** Nullable: the common case is a provider key matching nothing we know. */
    candidateInternalId: uuid("candidate_internal_id"),
    reason: text("reason").notNull(),
    status: text("status").notNull().default("open"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    resolvedAt: timestamp("resolved_at", { withTimezone: true }),
    resolvedBy: text("resolved_by"),
    resolutionNote: text("resolution_note"),
  },
  (t) => [
    index("entity_review_queue_open_idx").on(t.status).where(sql`${t.status} = 'open'`),
    check(
      "entity_review_queue_entity_type_check",
      sql`${t.entityType} IN ('country','competition','season','team','venue')`,
    ),
    check("entity_review_queue_status_check", sql`${t.status} IN ('open','resolved','rejected')`),
    check("entity_review_queue_reason_check", sql`length(${t.reason}) > 0`),
    // An open item has no resolution; a closed one does.
    check(
      "entity_review_queue_resolution_check",
      sql`(${t.status} = 'open' AND ${t.resolvedAt} IS NULL)
          OR (${t.status} <> 'open' AND ${t.resolvedAt} IS NOT NULL)`,
    ),
  ],
);
