/**
 * Applies every migration in the ledger, generated and --custom alike.
 * Forward-only: there are no down migrations (PHASE-0-SPEC.md 8.5).
 */
import { drizzle } from "drizzle-orm/postgres-js";
import { migrate } from "drizzle-orm/postgres-js/migrator";
import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const sql = postgres(getDatabaseUrl(), { max: 1, onnotice: () => {} });
try {
  await migrate(drizzle(sql), { migrationsFolder: "./drizzle" });
  console.log("migrations applied");
} finally {
  await sql.end({ timeout: 5 });
}
