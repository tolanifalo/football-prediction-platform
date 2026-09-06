/**
 * Database connection for local development.
 *
 * P0-02 scope: prove a TypeScript process can reach Postgres and round-trip a
 * query. No schema, no ORM - Drizzle arrives in P0-03.
 *
 * Runnable directly (Node strips the types):  node src/connection.ts
 */
import { fileURLToPath } from "node:url";

import postgres from "postgres";

const LOCAL_DEFAULT_URL = "postgresql://fpp:fpp_local_dev@localhost:5433/fpp";

/**
 * Resolves the connection string. DATABASE_URL wins so CI and containers can
 * point elsewhere; the fallback matches docker-compose.yml so a fresh clone
 * works with no .env file.
 */
export function getDatabaseUrl(): string {
  const url = process.env["DATABASE_URL"];
  return url && url.length > 0 ? url : LOCAL_DEFAULT_URL;
}

export interface ConnectionInfo {
  database: string;
  user: string;
  serverVersion: string;
  timezone: string;
  echo: string;
}

/** Connects, round-trips a value, and reports what answered. */
export async function checkConnection(url: string = getDatabaseUrl()): Promise<ConnectionInfo> {
  const sql = postgres(url, { max: 1, onnotice: () => {} });
  try {
    const token = `roundtrip-${Date.now()}`;
    const rows = await sql<
      { database: string; user: string; server_version: string; timezone: string; echo: string }[]
    >`
      SELECT current_database()          AS database,
             current_user                AS user,
             current_setting('server_version') AS server_version,
             current_setting('TimeZone')       AS timezone,
             ${token}::text              AS echo
    `;

    const row = rows[0];
    if (row === undefined) {
      throw new Error("connection check returned no rows");
    }
    if (row.echo !== token) {
      throw new Error(`round-trip mismatch: sent ${token}, received ${row.echo}`);
    }

    return {
      database: row.database,
      user: row.user,
      serverVersion: row.server_version,
      timezone: row.timezone,
      echo: row.echo,
    };
  } finally {
    await sql.end({ timeout: 5 });
  }
}

async function main(): Promise<void> {
  const info = await checkConnection();
  console.log("postgres round-trip ok");
  console.log(`  server    ${info.serverVersion}`);
  console.log(`  database  ${info.database}`);
  console.log(`  user      ${info.user}`);
  console.log(`  timezone  ${info.timezone}`);
}

if (process.argv[1] !== undefined && process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error: unknown) => {
    console.error("postgres round-trip FAILED");
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
