/**
 * Schema-input consistency check.
 *
 * WHAT THIS PROVES: every table under the drizzle.config.ts `schema` glob is
 * already represented in Drizzle's migration snapshot - i.e. nobody edited a
 * schema file without generating the migration for it.
 *
 * WHAT THIS DOES NOT PROVE: anything at all about the state of PostgreSQL.
 * `drizzle-kit generate` never opens a database connection (proven; see
 * PHASE-0-SPEC.md 8.3). A column added by hand, a dropped partition, or an
 * altered raw-SQL-owned object all pass this check silently. Database-side
 * drift needs explicit invariant checks - db:verify-partitions, from P0-04.
 *
 * Generation runs against a throwaway copy of the ledger, so a failing check
 * never leaves a stray migration behind.
 */
import { execSync } from "node:child_process";
import { cpSync, existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import config from "../drizzle.config.ts";

const LEDGER = "./drizzle";
// Relative on purpose: drizzle-kit joins --out onto the cwd, so an absolute
// path produces a mangled "packages/db/C:/..." and the run dies.
const SHADOW = "./.verify-generate";

// drizzle-kit refuses --config alongside any other flag, so redirecting output
// means passing dialect and schema explicitly. They are read from the config
// object rather than retyped, so the check can never test a stale glob.
const { dialect, schema } = config;
if (typeof schema !== "string") {
  throw new Error("verify-generate expects drizzle.config.ts `schema` to be a single glob string");
}

const sqlFiles = (dir: string): string[] =>
  readdirSync(dir)
    .filter((f) => f.endsWith(".sql"))
    .sort();

const fail = (message: string): void => {
  console.error(`FAIL: ${message}`);
  process.exitCode = 1;
};

rmSync(SHADOW, { recursive: true, force: true });
try {
  cpSync(LEDGER, SHADOW, { recursive: true });
  const before = sqlFiles(SHADOW);

  const output = execSync(
    `pnpm exec drizzle-kit generate --dialect=${dialect} --schema="${schema}" --out="${SHADOW}"`,
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  );

  const emitted = sqlFiles(SHADOW).filter((f) => !before.includes(f));

  if (emitted.length > 0) {
    fail("schema input is ahead of the migration snapshot.");
    console.error("Generate produced a migration that is not in the ledger:\n");
    for (const file of emitted) {
      console.error(`--- ${file} ---`);
      console.error(readFileSync(join(SHADOW, file), "utf8"));
    }
    console.error("Run: pnpm db:generate --name=<descriptive_slug>");
  } else if (!output.includes("No schema changes")) {
    // drizzle-kit exits 0 even when it crashes, so "no new file" alone is not
    // evidence of a clean run. Demand the positive confirmation too.
    fail("drizzle-kit did not report a clean comparison; treating as inconclusive.");
    console.error(output);
  } else {
    console.log(`schema input matches the snapshot (${before.length} migrations in ledger)`);
  }
} finally {
  if (existsSync(SHADOW)) rmSync(SHADOW, { recursive: true, force: true });
}
