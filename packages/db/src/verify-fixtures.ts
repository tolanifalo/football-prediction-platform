/**
 * Live PostgreSQL invariant check for the P0-07 fixture identity layer
 * (PHASE-0-SPEC.md §12 and the §E P0-07 acceptance criterion).
 *
 * Inspects the database, never a TypeScript declaration. Mutating probes run
 * inside rolled-back transactions with SAVEPOINTs, so one expected failure
 * cannot poison the rest (25P02, the defect verify-canonical hit). No fixed
 * identifiers anywhere: repeated runs must be identical.
 *
 * Positive outcomes are asserted on VALUES, not on the absence of an error.
 * An as-of read that returns nothing is not evidence that it returned the
 * right revision.
 *
 * NOTE ON THE AS-OF PREDICATE: this file must never restate the bitemporal
 * visibility comparison. Every as-of read goes through fixture_schedule_as_of()
 * or fn_visible_at(). Check 42 asserts the database holds exactly one
 * definition; 43 that the new wrapper delegates; 44 that this file itself
 * restates it zero times - the probe string is assembled from fragments (§11.1).
 */
import { readFileSync } from "node:fs";

import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const ENGINE = "engine_rw";
/**
 * The bitemporal comparison, assembled from fragments so the literal never
 * appears whole in this file. That lets check 44 demand ZERO restatements.
 */
const PREDICATE = ["superseded_at IS NULL OR", "superseded_at >"].join(" ");

const sql = postgres(getDatabaseUrl(), { max: 1, onnotice: () => {} });

let failures = 0;
const check = (name: string, ok: boolean, detail = ""): void => {
  if (ok) console.log(`  PASS  ${name}`);
  else {
    failures += 1;
    console.error(`  FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
  }
};

const inRollback = async <T>(fn: (tx: postgres.TransactionSql) => Promise<T>): Promise<T> => {
  const sentinel = Symbol("rollback");
  let captured!: T;
  try {
    await sql.begin(async (tx) => {
      captured = await fn(tx);
      throw sentinel;
    });
  } catch (error: unknown) {
    if (error !== sentinel) throw error;
  }
  return captured;
};

let spSeq = 0;
/** SQLSTATE of `stmt`, or "none". Savepoint-isolated. */
const codeOf = async (tx: postgres.TransactionSql, stmt: string): Promise<string> => {
  const sp = `vf_sp_${(spSeq += 1)}`;
  await tx.unsafe(`SAVEPOINT ${sp}`);
  try {
    await tx.unsafe(stmt);
    await tx.unsafe(`RELEASE SAVEPOINT ${sp}`);
    return "none";
  } catch (error: unknown) {
    await tx.unsafe(`ROLLBACK TO SAVEPOINT ${sp}`);
    return (error as { code?: string }).code ?? "unknown";
  }
};

/** One row in every registry P0-07 references, plus provenance anchors. */
const seed = async (tx: postgres.TransactionSql, tag: string) => {
  const [src] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`s-${tag}`}, 'probe') RETURNING id`;
  const [body] = await tx<{ id: string }[]>`
    INSERT INTO raw_payload_bodies (body_hash, byte_size)
    VALUES (${"f".repeat(64)}, 0) RETURNING id`;
  const [c] = await tx<{ id: string }[]>`
    INSERT INTO countries (slug, name) VALUES (${`c-${tag}`}, 'Probeland') RETURNING id`;
  const [comp] = await tx<{ id: string }[]>`
    INSERT INTO competitions (slug, country_id, type, gender)
    VALUES (${`k-${tag}`}, ${c!.id}, 'league', 'men') RETURNING id`;
  const [se] = await tx<{ id: string }[]>`
    INSERT INTO seasons (slug, competition_id, label, start_year)
    VALUES (${`se-${tag}`}, ${comp!.id}, '2025/26', 2025) RETURNING id`;
  const [home] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`th-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [away] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`ta-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [v] = await tx<{ id: string }[]>`
    INSERT INTO venues (slug, name, country_id) VALUES (${`v-${tag}`}, 'Probe Park', ${c!.id}) RETURNING id`;
  return {
    source: src!.id, body: body!.id, country: c!.id, competition: comp!.id,
    season: se!.id, home: home!.id, away: away!.id, venue: v!.id,
  };
};

type S = Awaited<ReturnType<typeof seed>>;

/** A fixture insert. `home`/`away` select which way round the pairing sits. */
const fx = (s: S, stage: string, leg: number, replay: number, swap = false): string =>
  `INSERT INTO fixtures (season_id, stage, leg, replay_number, home_team_id, away_team_id)
   VALUES ('${s.season}', ${stage === "NULL" ? "NULL" : `'${stage}'`}, ${leg}, ${replay},
           '${swap ? s.away : s.home}', '${swap ? s.home : s.away}')`;

/** A schedule revision. `localDate` defaults to the value the CHECK demands. */
const sched = (
  s: S,
  fixtureId: string,
  kickoff: string,
  tz: string,
  status: string,
  opts: { localDate?: string; knownAt?: string; supersededAt?: string; neutral?: boolean } = {},
): string => {
  const localDate = opts.localDate ?? `(timestamptz '${kickoff}' AT TIME ZONE '${tz}')::date`;
  return `INSERT INTO fixture_schedule
      (fixture_id, kickoff_utc, local_date, local_tz, venue_id, status, is_neutral_venue,
       source_id, raw_payload_body_id${opts.knownAt ? ", known_at" : ""}${opts.supersededAt ? ", superseded_at" : ""})
    VALUES ('${fixtureId}', timestamptz '${kickoff}',
            ${opts.localDate ? `date '${opts.localDate}'` : localDate},
            '${tz}', '${s.venue}', '${status}', ${opts.neutral ? "true" : "false"},
            '${s.source}', ${s.body}${opts.knownAt ? `, timestamptz '${opts.knownAt}'` : ""}${
              opts.supersededAt ? `, timestamptz '${opts.supersededAt}'` : ""
            })`;
};

const planOf = async (tx: postgres.TransactionSql, query: string): Promise<string> => {
  const rows = await tx.unsafe(`EXPLAIN (COSTS OFF, FORMAT TEXT) ${query}`);
  return (rows as unknown as { "QUERY PLAN": string }[]).map((r) => r["QUERY PLAN"]).join("\n");
};

try {
  console.log("fixture identity invariants\n");

  // =====================================================================
  console.log("  identity key — the cases it exists to handle:");
  const ident = await inRollback(async (tx) => {
    const s = await seed(tx, "id");
    const r: Record<string, string> = {};
    // Two-legged tie: same pair, both legs, home side swapped.
    r.tieLeg1 = await codeOf(tx, fx(s, "semi_final", 1, 0));
    r.tieLeg2 = await codeOf(tx, fx(s, "semi_final", 2, 0, true));
    // Scottish three-round pre-split: the SAME side hosts twice in one season.
    r.round1 = await codeOf(tx, fx(s, "regular_r1", 1, 0));
    r.round2 = await codeOf(tx, fx(s, "regular_r2", 1, 0, true));
    r.round3SameHome = await codeOf(tx, fx(s, "regular_r3", 1, 0));
    // Without a distinguishing stage that third meeting collides.
    r.duplicate = await codeOf(tx, fx(s, "regular_r1", 1, 0));
    // Replay: a NEW fixture, linked back.
    const [orig] = await tx<{ id: string }[]>`
      SELECT id FROM fixtures WHERE stage = 'semi_final' AND leg = 1`;
    r.replay = await codeOf(
      tx,
      `INSERT INTO fixtures (season_id, stage, leg, replay_number, home_team_id, away_team_id, replaces_fixture_id)
       VALUES ('${s.season}', 'semi_final', 1, 1, '${s.home}', '${s.away}', '${orig!.id}')`,
    );
    r.replayDuplicate = await codeOf(
      tx,
      `INSERT INTO fixtures (season_id, stage, leg, replay_number, home_team_id, away_team_id, replaces_fixture_id)
       VALUES ('${s.season}', 'semi_final', 1, 1, '${s.home}', '${s.away}', '${orig!.id}')`,
    );
    r.selfReplace = await codeOf(
      tx,
      `UPDATE fixtures SET replaces_fixture_id = id WHERE stage = 'regular_r1'`,
    );
    r.homeEqualsAway = await codeOf(
      tx,
      `INSERT INTO fixtures (season_id, stage, leg, replay_number, home_team_id, away_team_id)
       VALUES ('${s.season}', 'x', 1, 0, '${s.home}', '${s.home}')`,
    );
    r.legThree = await codeOf(tx, fx(s, "y", 3, 0));
    r.negativeReplay = await codeOf(tx, fx(s, "z", 1, -1));
    r.nullStage = await codeOf(tx, fx(s, "NULL", 1, 0));
    // Defaults, asserted on the stored VALUES.
    await tx.unsafe(
      `INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
       VALUES ('${s.season}', 'defaults_probe', '${s.home}', '${s.away}')`,
    );
    const [def] = await tx<{ leg: number; replay_number: number; tie_id: string | null }[]>`
      SELECT leg, replay_number, tie_id FROM fixtures WHERE stage = 'defaults_probe'`;
    const [n] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM fixtures WHERE season_id = ${s.season}`;
    return { codes: r, def: def!, total: n!.n };
  });

  check("1. two-legged tie: leg 1 inserts", ident.codes.tieLeg1 === "none", ident.codes.tieLeg1);
  check("2. two-legged tie: leg 2 inserts (same pair, reversed)", ident.codes.tieLeg2 === "none", ident.codes.tieLeg2);
  check("3. round-robin round 1 inserts", ident.codes.round1 === "none", ident.codes.round1);
  check("4. round-robin round 2 inserts (home side swapped)", ident.codes.round2 === "none", ident.codes.round2);
  check(
    "5. round 3 with the SAME home side inserts — stage distinguishes it (§12.2)",
    ident.codes.round3SameHome === "none",
    ident.codes.round3SameHome,
  );
  check("6. an identical six-tuple is REJECTED", ident.codes.duplicate === "23505", ident.codes.duplicate);
  check("7. a replay inserts as a NEW fixture with replaces_fixture_id", ident.codes.replay === "none", ident.codes.replay);
  check("8. the same replay twice is REJECTED", ident.codes.replayDuplicate === "23505", ident.codes.replayDuplicate);
  check("9. a fixture may not replace itself", ident.codes.selfReplace === "23514", ident.codes.selfReplace);
  check("10. home = away is REJECTED", ident.codes.homeEqualsAway === "23514", ident.codes.homeEqualsAway);
  check("11. leg = 3 is REJECTED", ident.codes.legThree === "23514", ident.codes.legThree);
  check("12. replay_number = -1 is REJECTED", ident.codes.negativeReplay === "23514", ident.codes.negativeReplay);
  check("13. stage NULL is REJECTED (NOT NULL, not a silent key hole)", ident.codes.nullStage === "23502", ident.codes.nullStage);
  check("14. defaults: leg = 1", ident.def.leg === 1, `${ident.def.leg}`);
  check("15. defaults: replay_number = 0", ident.def.replay_number === 0, `${ident.def.replay_number}`);
  check("16. tie_id defaults to NULL (not a tie)", ident.def.tie_id === null, `${ident.def.tie_id}`);
  check("17. exactly the 7 intended fixtures exist after the probe", ident.total === 7, `${ident.total}`);

  // =====================================================================
  console.log("\n  identity vs fact — what fixtures must and must not carry (§12.3, §12.7):");
  const cols = async (table: string): Promise<Map<string, string>> => {
    const rows = await sql<{ column_name: string; is_nullable: string }[]>`
      SELECT column_name, is_nullable FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = ${table}`;
    return new Map(rows.map((r) => [r.column_name, r.is_nullable]));
  };
  const fixCols = await cols("fixtures");
  const schCols = await cols("fixture_schedule");

  const IDENTITY = ["season_id", "stage", "leg", "replay_number", "home_team_id", "away_team_id"];
  check(
    "18. all six identity columns are NOT NULL (a NULL would void the unique key)",
    IDENTITY.every((c) => fixCols.get(c) === "NO"),
    IDENTITY.filter((c) => fixCols.get(c) !== "NO").join(","),
  );
  const forbiddenOnFixtures = [
    "source_id", "raw_payload_body_id", "known_at", "superseded_at",
    "kickoff_utc", "local_date", "local_tz", "status", "venue_id",
    "competition_id", "matchweek", "round", "is_neutral_venue",
  ];
  const present = forbiddenOnFixtures.filter((c) => fixCols.has(c));
  check(
    "19. fixtures carries NO provenance, NO schedule and NO competition_id/matchweek",
    present.length === 0,
    `found ${present.join(",")}`,
  );
  const PROV = ["source_id", "raw_payload_body_id", "known_at"];
  check(
    "20. fixture_schedule carries all three provenance columns, NOT NULL",
    PROV.every((c) => schCols.get(c) === "NO"),
    PROV.filter((c) => schCols.get(c) !== "NO").join(","),
  );
  check("21. fixture_schedule.superseded_at is nullable (NULL = current belief)",
    schCols.get("superseded_at") === "YES", schCols.get("superseded_at") ?? "absent");
  check("22. fixture_schedule.is_neutral_venue exists and is NOT NULL (§12.4, D13)",
    schCols.get("is_neutral_venue") === "NO", schCols.get("is_neutral_venue") ?? "absent");

  const [key] = await sql<{ cols: string }[]>`
    SELECT string_agg(a.attname, ',' ORDER BY k.ord) AS cols
      FROM pg_constraint c
      JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.conname = 'fixtures_identity_key'`;
  check(
    "23. the identity key is exactly the six approved columns, in order",
    key?.cols === IDENTITY.join(","),
    key?.cols ?? "absent",
  );

  // =====================================================================
  console.log("\n  fixture_schedule constraints:");
  const STATUSES = ["scheduled", "live", "suspended", "ft", "postponed", "abandoned", "cancelled"];
  const schedule = await inRollback(async (tx) => {
    const s = await seed(tx, "sc");
    const r: Record<string, string> = {};
    const ids: string[] = [];
    for (let i = 0; i < STATUSES.length + 4; i += 1) {
      const [f] = await tx<{ id: string }[]>`
        INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
        VALUES (${s.season}, ${`st-${i}`}, ${s.home}, ${s.away}) RETURNING id`;
      ids.push(f!.id);
    }
    for (const [i, st] of STATUSES.entries()) {
      r[`status_${st}`] = await codeOf(tx, sched(s, ids[i]!, "2026-02-01 15:00Z", "Europe/London", st));
    }
    const spare = ids.slice(STATUSES.length);
    r.statusAwarded = await codeOf(tx, sched(s, spare[0]!, "2026-02-01 15:00Z", "UTC", "awarded"));
    // The Brazil case local_date exists for: 23:30Z is the PREVIOUS local day.
    r.localDateUtc = await codeOf(
      tx,
      sched(s, spare[0]!, "2026-06-01 23:30Z", "America/Sao_Paulo", "scheduled", { localDate: "2026-06-02" }),
    );
    r.localDateOk = await codeOf(
      tx,
      sched(s, spare[0]!, "2026-06-01 23:30Z", "America/Sao_Paulo", "scheduled", { localDate: "2026-06-01" }),
    );
    r.badZone = await codeOf(tx, sched(s, spare[1]!, "2026-02-01 15:00Z", "Europe/Narnia", "scheduled"));
    r.zeroLength = await codeOf(
      tx,
      sched(s, spare[1]!, "2026-02-01 15:00Z", "UTC", "scheduled", {
        knownAt: "2026-01-01 00:00Z", supersededAt: "2026-01-01 00:00Z",
      }),
    );
    r.secondCurrent = await codeOf(tx, sched(s, spare[0]!, "2026-02-08 15:00Z", "UTC", "scheduled"));
    // A historical revision alongside the current one is legal.
    r.historyPlusCurrent = await codeOf(
      tx,
      sched(s, spare[0]!, "2026-02-08 15:00Z", "UTC", "scheduled", {
        knownAt: "2025-01-01 00:00Z", supersededAt: "2025-06-01 00:00Z",
      }),
    );
    // Positive: the accepted BST/DST row really stored the local date we expect.
    const [ld] = await tx<{ local_date: Date }[]>`
      SELECT local_date FROM fixture_schedule
       WHERE local_tz = 'America/Sao_Paulo' AND superseded_at IS NULL`;
    return { codes: r, storedLocalDate: ld?.local_date };
  });

  for (const st of STATUSES) {
    const code = schedule.codes[`status_${st}`];
    check(`24. status '${st}' is accepted`, code === "none", code);
  }
  check(
    "25. status 'awarded' is REJECTED — result semantics belong to P0-08 (§6 rule 3)",
    schedule.codes.statusAwarded === "23514",
    schedule.codes.statusAwarded,
  );
  check("26. local_date set to the UTC date is REJECTED", schedule.codes.localDateUtc === "23514", schedule.codes.localDateUtc);
  check("27. the correct competition-local date is accepted", schedule.codes.localDateOk === "none", schedule.codes.localDateOk);
  check(
    "28. and it was stored as 2026-06-01, not the UTC 2026-06-02",
    schedule.storedLocalDate instanceof Date &&
      schedule.storedLocalDate.toISOString().slice(0, 10) === "2026-06-01",
    String(schedule.storedLocalDate),
  );
  check("29. an unknown IANA zone is REJECTED", schedule.codes.badZone === "22023", schedule.codes.badZone);
  check("30. superseded_at = known_at (zero-length validity) is REJECTED", schedule.codes.zeroLength === "23514", schedule.codes.zeroLength);
  check("31. a second CURRENT revision for one fixture is REJECTED", schedule.codes.secondCurrent === "23505", schedule.codes.secondCurrent);
  check("32. a historical revision alongside the current one is accepted", schedule.codes.historyPlusCurrent === "none", schedule.codes.historyPlusCurrent);

  // =====================================================================
  console.log("\n  reschedule and bitemporal as-of behaviour:");
  const T0 = "2026-01-01 00:00Z";
  const T1 = "2026-02-01 00:00Z";
  const asof = await inRollback(async (tx) => {
    const s = await seed(tx, "as");
    const [f] = await tx<{ id: string }[]>`
      INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
      VALUES (${s.season}, 'reschedule_probe', ${s.home}, ${s.away}) RETURNING id`;
    // Revision 1, learned T0: kicks off 2026-03-14 15:00Z, scheduled.
    await tx.unsafe(
      sched(s, f!.id, "2026-03-14 15:00Z", "Europe/London", "scheduled", {
        knownAt: T0, supersededAt: T1,
      }),
    );
    // Revision 2, learned T1: moved to 2026-03-21 20:00Z. Same fixture.
    await tx.unsafe(
      sched(s, f!.id, "2026-03-21 20:00Z", "Europe/London", "ft", { knownAt: T1 }),
    );
    const before = await tx<{ kickoff_utc: Date; status: string }[]>`
      SELECT kickoff_utc, status FROM fixture_schedule_as_of(${`${T0}`}::timestamptz)
       WHERE fixture_id = ${f!.id}`;
    const after = await tx<{ kickoff_utc: Date; status: string }[]>`
      SELECT kickoff_utc, status FROM fixture_schedule_as_of(now())
       WHERE fixture_id = ${f!.id}`;
    const [counts] = await tx<{ fixtures: number; revisions: number }[]>`
      SELECT (SELECT count(*)::int FROM fixtures WHERE stage = 'reschedule_probe') AS fixtures,
             (SELECT count(*)::int FROM fixture_schedule WHERE fixture_id = ${f!.id}) AS revisions`;
    // The wrapper and a direct fn_visible_at call must agree exactly.
    const agree = await tx<{ cutoff: Date; via_wrapper: number; via_fn: number }[]>`
      SELECT c.cutoff,
             (SELECT count(*)::int FROM fixture_schedule_as_of(c.cutoff)
               WHERE fixture_id = ${f!.id})                                  AS via_wrapper,
             (SELECT count(*)::int FROM fixture_schedule
               WHERE fixture_id = ${f!.id}
                 AND fn_visible_at(known_at, superseded_at, c.cutoff))       AS via_fn
        FROM (VALUES (${`2025-12-01 00:00Z`}::timestamptz), (${T0}::timestamptz),
                     (${`2026-01-15 00:00Z`}::timestamptz), (${T1}::timestamptz),
                     (now())) AS c(cutoff)
       ORDER BY 1`;
    return { before, after, counts: counts!, agree };
  });

  check("33. a reschedule leaves exactly ONE fixture row", asof.counts.fixtures === 1, `${asof.counts.fixtures}`);
  check("34. …and creates a SECOND schedule revision", asof.counts.revisions === 2, `${asof.counts.revisions}`);
  check("35. as-of returns exactly one revision at a past cutoff", asof.before.length === 1, `${asof.before.length}`);
  check(
    "36. as-of the earlier cutoff returns the OLD kickoff (2026-03-14 15:00Z)",
    asof.before[0]?.kickoff_utc?.toISOString() === "2026-03-14T15:00:00.000Z",
    String(asof.before[0]?.kickoff_utc),
  );
  check(
    "37. …and the OLD status 'scheduled' — post-kickoff state cannot leak (§12.7)",
    asof.before[0]?.status === "scheduled",
    String(asof.before[0]?.status),
  );
  check(
    "38. as-of now returns the NEW kickoff (2026-03-21 20:00Z) and status 'ft'",
    asof.after[0]?.kickoff_utc?.toISOString() === "2026-03-21T20:00:00.000Z" && asof.after[0]?.status === "ft",
    `${String(asof.after[0]?.kickoff_utc)} / ${String(asof.after[0]?.status)}`,
  );
  check(
    "39. wrapper and fn_visible_at agree at every one of 5 cutoffs",
    asof.agree.length === 5 && asof.agree.every((r) => r.via_wrapper === r.via_fn),
    asof.agree.map((r) => `${r.via_wrapper}/${r.via_fn}`).join(" "),
  );
  check(
    "40. the cutoff before either revision was known returns NOTHING",
    asof.agree[0]?.via_wrapper === 0,
    `${asof.agree[0]?.via_wrapper}`,
  );

  // =====================================================================
  console.log("\n  referential integrity (real FKs — no trigger needed here):");
  const fks = await inRollback(async (tx) => {
    const s = await seed(tx, "fk");
    const [f] = await tx<{ id: string }[]>`
      INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
      VALUES (${s.season}, 'fk_probe', ${s.home}, ${s.away}) RETURNING id`;
    await tx.unsafe(sched(s, f!.id, "2026-02-01 15:00Z", "Europe/London", "scheduled"));
    return {
      venue: await codeOf(tx, `DELETE FROM venues WHERE id = '${s.venue}'`),
      body: await codeOf(tx, `DELETE FROM raw_payload_bodies WHERE id = ${s.body}`),
      fixture: await codeOf(tx, `DELETE FROM fixtures WHERE id = '${f!.id}'`),
      season: await codeOf(tx, `DELETE FROM seasons WHERE id = '${s.season}'`),
      team: await codeOf(tx, `DELETE FROM teams WHERE id = '${s.home}'`),
      unrelatedVenue: await codeOf(
        tx,
        `INSERT INTO venues (slug, name) VALUES ('v-fk-free', 'Unreferenced') RETURNING id`,
      ),
    };
  });
  check("41. a venue referenced by a schedule cannot be deleted", fks.venue === "23503", fks.venue);
  check("42. a referenced raw_payload_body cannot be deleted", fks.body === "23503", fks.body);
  check("43. a fixture with a schedule revision cannot be deleted", fks.fixture === "23503", fks.fixture);
  check("44. a season with fixtures cannot be deleted", fks.season === "23503", fks.season);
  check("45. a team with fixtures cannot be deleted", fks.team === "23503", fks.team);
  check("46. an unreferenced venue is still insertable (the FKs are not blanket)", fks.unrelatedVenue === "none", fks.unrelatedVenue);

  // =====================================================================
  console.log("\n  every fixture has at least one schedule revision (§12.4, D10):");
  const [orphan] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM fixtures f
     WHERE NOT EXISTS (SELECT 1 FROM fixture_schedule s WHERE s.fixture_id = f.id)`;
  check(
    "47. no fixture in the database lacks a schedule revision",
    orphan?.n === 0,
    `${orphan?.n} orphaned fixture(s)`,
  );

  // =====================================================================
  console.log("\n  grants — engine_rw:");
  const engineWrite = await inRollback(async (tx) => {
    const s = await seed(tx, "gw");
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE}`);
    const insertFixture = await codeOf(
      tx,
      `INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
       VALUES ('${s.season}', 'grant_probe', '${s.home}', '${s.away}')`,
    );
    const [f] = await tx<{ id: string }[]>`SELECT id FROM fixtures WHERE stage = 'grant_probe'`;
    return {
      insertFixture,
      insertSchedule: await codeOf(tx, sched(s, f!.id, "2026-02-01 15:00Z", "Europe/London", "scheduled")),
      updStatsComplete: await codeOf(tx, `UPDATE fixtures SET stats_complete_at = now() WHERE id = '${f!.id}'`),
      updTieId: await codeOf(tx, `UPDATE fixtures SET tie_id = gen_random_uuid() WHERE id = '${f!.id}'`),
      updReplaces: await codeOf(tx, `UPDATE fixtures SET replaces_fixture_id = NULL WHERE id = '${f!.id}'`),
      updSuperseded: await codeOf(tx, `UPDATE fixture_schedule SET superseded_at = now() + interval '1 second'`),
      updHomeTeam: await codeOf(tx, `UPDATE fixtures SET home_team_id = '${s.away}' WHERE id = '${f!.id}'`),
      updSeason: await codeOf(tx, `UPDATE fixtures SET season_id = '${s.season}' WHERE id = '${f!.id}'`),
      updStage: await codeOf(tx, `UPDATE fixtures SET stage = 'rewritten' WHERE id = '${f!.id}'`),
      updLeg: await codeOf(tx, `UPDATE fixtures SET leg = 2 WHERE id = '${f!.id}'`),
      updReplayNumber: await codeOf(tx, `UPDATE fixtures SET replay_number = 9 WHERE id = '${f!.id}'`),
      updKickoff: await codeOf(tx, `UPDATE fixture_schedule SET kickoff_utc = now()`),
      updStatus: await codeOf(tx, `UPDATE fixture_schedule SET status = 'ft'`),
      updVenue: await codeOf(tx, `UPDATE fixture_schedule SET venue_id = NULL`),
      updNeutral: await codeOf(tx, `UPDATE fixture_schedule SET is_neutral_venue = true`),
      delFixture: await codeOf(tx, `DELETE FROM fixtures WHERE id = '${f!.id}'`),
      delSchedule: await codeOf(tx, `DELETE FROM fixture_schedule`),
      truncFixture: await codeOf(tx, `TRUNCATE fixtures CASCADE`),
      truncSchedule: await codeOf(tx, `TRUNCATE fixture_schedule`),
    };
  });

  for (const [n, k] of [
    ["48", "insertFixture"], ["49", "insertSchedule"], ["50", "updStatsComplete"],
    ["51", "updTieId"], ["52", "updReplaces"], ["53", "updSuperseded"],
  ] as const) {
    check(`${n}. engine_rw PERMITTED: ${k}`, engineWrite[k] === "none", engineWrite[k]);
  }
  for (const [n, k] of [
    ["54", "updHomeTeam"], ["55", "updSeason"], ["56", "updStage"], ["57", "updLeg"],
    ["58", "updReplayNumber"], ["59", "updKickoff"], ["60", "updStatus"], ["61", "updVenue"],
    ["62", "updNeutral"], ["63", "delFixture"], ["64", "delSchedule"],
    ["65", "truncFixture"], ["66", "truncSchedule"],
  ] as const) {
    check(`${n}. engine_rw REFUSED: ${k}`, engineWrite[k] === "42501", engineWrite[k]);
  }

  console.log("\n  grants — read-only roles:");
  for (const role of ["app_rw", "analytics_ro"]) {
    const res = await inRollback(async (tx) => {
      const s = await seed(tx, `ro-${role}`);
      const [f] = await tx<{ id: string }[]>`
        INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
        VALUES (${s.season}, 'ro_probe', ${s.home}, ${s.away}) RETURNING id`;
      await tx.unsafe(sched(s, f!.id, "2026-02-01 15:00Z", "Europe/London", "scheduled"));
      await tx.unsafe(`SET LOCAL ROLE ${role}`);
      const rows = await tx<{ n: number }[]>`
        SELECT (SELECT count(*)::int FROM fixtures WHERE id = ${f!.id})
             + (SELECT count(*)::int FROM fixture_schedule WHERE fixture_id = ${f!.id}) AS n`;
      return {
        rows: rows[0]!.n,
        asOf: await codeOf(tx, `SELECT 1 FROM fixture_schedule_as_of(now())`),
        insert: await codeOf(
          tx,
          `INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
           VALUES ('${s.season}', 'nope', '${s.home}', '${s.away}')`,
        ),
        update: await codeOf(tx, `UPDATE fixtures SET stats_complete_at = now()`),
        del: await codeOf(tx, `DELETE FROM fixture_schedule`),
        trunc: await codeOf(tx, `TRUNCATE fixtures CASCADE`),
      };
    });
    check(`67. ${role} may SELECT both tables (2 rows visible)`, res.rows === 2, `${res.rows}`);
    check(`68. ${role} may call the as-of wrapper`, res.asOf === "none", res.asOf);
    check(`69. ${role} may NOT INSERT`, res.insert === "42501", res.insert);
    check(`70. ${role} may NOT UPDATE`, res.update === "42501", res.update);
    check(`71. ${role} may NOT DELETE`, res.del === "42501", res.del);
    check(`72. ${role} may NOT TRUNCATE`, res.trunc === "42501", res.trunc);
  }

  // =====================================================================
  console.log("\n  the as-of mechanism stays in exactly one place (§11.1):");
  const [pred] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n
      FROM pg_proc p JOIN pg_namespace n2 ON n2.oid = p.pronamespace
     WHERE n2.nspname = 'public' AND p.prokind = 'f'
       AND pg_get_functiondef(p.oid) LIKE ${`%${PREDICATE}%`}`;
  check(
    "73. catalog: still exactly ONE object defines the predicate, after adding a second wrapper",
    pred?.n === 1,
    `found ${pred?.n}`,
  );
  const [deleg] = await sql<{ src: string }[]>`
    SELECT prosrc AS src FROM pg_proc WHERE proname = 'fixture_schedule_as_of'`;
  check(
    "74. fixture_schedule_as_of delegates to fn_visible_at rather than restating",
    (deleg?.src ?? "").includes("fn_visible_at") && !(deleg?.src ?? "").includes(PREDICATE),
    deleg?.src ? "present" : "function missing",
  );
  const [ret] = await sql<{ t: string }[]>`
    SELECT pg_get_function_result(oid) AS t FROM pg_proc WHERE proname = 'fixture_schedule_as_of'`;
  check(
    "75. it returns SETOF fixture_schedule, preserving full column typing",
    ret?.t === "SETOF fixture_schedule",
    ret?.t ?? "absent",
  );
  const selfSrc = readFileSync(new URL(import.meta.url), "utf8");
  const restatements = selfSrc.split(PREDICATE).length - 1;
  check("76. this verification file restates the predicate ZERO times", restatements === 0, `found ${restatements}`);

  // =====================================================================
  console.log("\n  planning — inlining and the approved index set:");
  const plans = await inRollback(async (tx) => {
    const s = await seed(tx, "pl");
    // Enough rows that the planner has a real choice to make.
    await tx.unsafe(`
      INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
      SELECT '${s.season}', 'bulk_' || g, '${s.home}', '${s.away}' FROM generate_series(1, 600) g`);
    await tx.unsafe(`
      INSERT INTO fixture_schedule
        (fixture_id, kickoff_utc, local_date, local_tz, venue_id, status, source_id, raw_payload_body_id,
         known_at, superseded_at)
      SELECT f.id, k.kickoff,
             (k.kickoff AT TIME ZONE 'UTC')::date,
             'UTC', '${s.venue}', 'scheduled', '${s.source}', ${s.body},
             timestamptz '2025-01-01 00:00Z' + (r * interval '30 days'),
             CASE WHEN r < 2 THEN timestamptz '2025-01-01 00:00Z' + ((r + 1) * interval '30 days') END
        FROM fixtures f
        CROSS JOIN generate_series(0, 2) r
        CROSS JOIN LATERAL (
          SELECT timestamptz '2026-01-01 15:00Z'
                 + (substring(f.stage from 6)::int * interval '1 day')
                 + (r * interval '7 days') AS kickoff
        ) k
       WHERE f.stage LIKE 'bulk|_%' ESCAPE '|'`);
    await tx.unsafe("ANALYZE fixtures");
    await tx.unsafe("ANALYZE fixture_schedule");
    const [f] = await tx<{ id: string }[]>`SELECT id FROM fixtures WHERE stage = 'bulk_1'`;
    const viaWrapper = await planOf(
      tx,
      `SELECT id FROM fixture_schedule_as_of(timestamptz '2026-01-01 00:00Z') WHERE fixture_id = '${f!.id}'`,
    );
    const viaFn = await planOf(
      tx,
      `SELECT id FROM fixture_schedule
        WHERE fixture_id = '${f!.id}'
          AND fn_visible_at(known_at, superseded_at, timestamptz '2026-01-01 00:00Z')`,
    );
    const currentLookup = await planOf(
      tx,
      `SELECT id FROM fixture_schedule WHERE fixture_id = '${f!.id}' AND superseded_at IS NULL`,
    );
    const calendar = await planOf(
      tx,
      `SELECT fixture_id FROM fixture_schedule
        WHERE superseded_at IS NULL
          AND kickoff_utc BETWEEN timestamptz '2026-03-01 00:00Z' AND timestamptz '2026-03-08 00:00Z'`,
    );
    const [n] = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM fixture_schedule`;
    return { viaWrapper, viaFn, currentLookup, calendar, rows: n!.n };
  });

  check("77. the plan probe ran against a non-trivial table", plans.rows >= 1800, `${plans.rows} rows`);
  check(
    "78. the wrapper produces NO Function Scan — LANGUAGE sql is inlined",
    !plans.viaWrapper.includes("Function Scan"),
    plans.viaWrapper.split("\n")[0],
  );
  check(
    "79. the wrapper's plan is IDENTICAL to calling fn_visible_at directly",
    plans.viaWrapper === plans.viaFn,
    `${plans.viaWrapper.split("\n")[0]} vs ${plans.viaFn.split("\n")[0]}`,
  );
  check(
    "80. the wrapper's filter shows the INLINED comparison, not a function call",
    !plans.viaWrapper.includes("fn_visible_at") &&
      !plans.viaWrapper.includes("fixture_schedule_as_of") &&
      plans.viaWrapper.includes("superseded_at"),
    plans.viaWrapper.replace(/\n/g, " | "),
  );
  // The specific index NAME is deliberately not asserted here. The partial
  // index covers exactly the rows where superseded_at IS NULL, so when
  // little or nothing has been superseded it is the same size as the full
  // fixture_id index and the planner may pick either - which is what
  // happened once the six-season corpus landed and every one of its 2,280
  // schedule rows was current. Index existence is asserted by check 84;
  // what matters here is that a current-state lookup is an INDEX scan
  // filtered on superseded_at and never a sequential scan.
  check(
    "81. a current-state lookup uses an index on fixture_id, never a seq scan",
    /Index (Only )?Scan using fixture_schedule_(current|fixture)_idx/.test(
      plans.currentLookup,
    ) &&
      !plans.currentLookup.includes("Seq Scan on fixture_schedule") &&
      plans.currentLookup.includes("superseded_at"),
    plans.currentLookup.replace(/\n/g, " | "),
  );
  check(
    "82. the partial kickoff index serves the calendar query",
    plans.calendar.includes("fixture_schedule_kickoff_idx"),
    plans.calendar.replace(/\n/g, " | "),
  );
  // The reason fixture_schedule_fixture_idx exists: the partial current index
  // cannot serve a historical read, because the as-of predicate admits rows
  // whose superseded_at is later than the cutoff rather than NULL.
  check(
    "82b. the full fixture_id index serves an AS-OF lookup — its whole purpose",
    plans.viaWrapper.includes("fixture_schedule_fixture_idx"),
    plans.viaWrapper.replace(/\n/g, " | "),
  );

  const idx = await sql<{ indexname: string }[]>`
    SELECT indexname FROM pg_indexes
     WHERE schemaname = 'public' AND tablename IN ('fixtures', 'fixture_schedule')
     ORDER BY indexname`;
  const names = idx.map((r) => r.indexname);
  check(
    "83. no known_at index was created (ruled out — no consumer, §12.8)",
    !names.includes("fixture_schedule_known_at_idx"),
    names.join(","),
  );
  const EXPECTED_INDEXES = [
    "fixture_schedule_current_idx", "fixture_schedule_fixture_idx",
    "fixture_schedule_kickoff_idx", "fixture_schedule_pkey",
    "fixtures_away_team_idx", "fixtures_home_team_idx", "fixtures_identity_key", "fixtures_pkey",
  ];
  check(
    "84. exactly the approved index set exists — no more, no fewer",
    names.length === EXPECTED_INDEXES.length && EXPECTED_INDEXES.every((n) => names.includes(n)),
    names.join(","),
  );

  // =====================================================================
  console.log("\n  P0-06 boundary — P0-07 changed nothing there (§12.5, D6):");
  const [etype] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conname = 'external_ids_entity_type_check'`;
  check(
    "85. external_ids.entity_type still permits only the five canonical registries",
    !!etype?.def && !etype.def.includes("fixture"),
    etype?.def ?? "absent",
  );
  const [trg] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_trigger
     WHERE NOT tgisinternal AND tgrelid IN ('fixtures'::regclass, 'fixture_schedule'::regclass)`;
  check("86. no triggers on either P0-07 table (no local_date trigger)", trg?.n === 0, `${trg?.n}`);

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}
