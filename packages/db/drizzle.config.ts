import { defineConfig } from "drizzle-kit";

import { getDatabaseUrl } from "./src/connection.ts";

// The `schema` glob is the whole exclusion mechanism (PHASE-0-SPEC.md 8.1):
// only Drizzle-owned tables live here. Declarations under src/raw-sql/ are
// typed and queryable but invisible to `generate`, because generate reads
// nothing but this glob and the snapshot in `out`.
//
// tablesFilter is deliberately absent - it does not filter the schema side of
// generate (proven; see PHASE-0-SPEC.md 8.2).
export default defineConfig({
  dialect: "postgresql",
  schema: "./src/schema/**/*.ts",
  out: "./drizzle",
  dbCredentials: { url: getDatabaseUrl() },
});
