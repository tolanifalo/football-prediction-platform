/**
 * Live PostgreSQL invariant check for the P0-09 odds layer
 * (PHASE-0-SPEC.md §14 and the amended §E P0-09 acceptance criterion).
 *
 * Inspects the database, never a TypeScript declaration. Mutating probes run
 * inside rolled-back transactions with SAVEPOINTs. No fixed identifiers.
 * Positive outcomes are asserted on VALUES, not on the absence of an error.
 *
 * TWO THINGS THIS FILE MUST NOT DO, both deliberate:
 *
 * 1. It must never restate the bitemporal visibility comparison. Every as-of
 *    read goes through odds_ticks_as_of() or fn_visible_at(); the probe string
 *    is assembled from fragments so the file can demand ZERO restatements.
 *
 * 2. It must assert NOTHING resembling odds coverage (G13). The absence of a
 *    tick is not proof that no market existed, that the fixture was uncovered,
 *    that the bookmaker did not offer the market, or that polling happened and
 *    the price held. Those are four different things and none is knowable here.
 */
import { readFileSync } from "node:fs";

import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const ENGINE = "engine_rw";
/** Assembled from fragments so the literal never appears whole in this file. */
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
const codeOf = async (tx: postgres.TransactionSql, stmt: string): Promise<string> => {
  const sp = `vo_sp_${(spSeq += 1)}`;
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

/** Two sources, two bookmakers (one exchange), a body, and a fixture. */
const seed = async (tx: postgres.TransactionSql, tag: string) => {
  const [a] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`sa-${tag}`}, 'Feed A') RETURNING id`;
  const [b] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`sb-${tag}`}, 'Feed B') RETURNING id`;
  const [body] = await tx<{ id: string }[]>`
    INSERT INTO raw_payload_bodies (body_hash, byte_size) VALUES (${"d".repeat(64)}, 0) RETURNING id`;
  const [book] = await tx<{ id: string }[]>`
    INSERT INTO bookmakers (slug, name, kind) VALUES (${`bk-${tag}`}, 'Probe Book', 'bookmaker') RETURNING id`;
  const [exch] = await tx<{ id: string }[]>`
    INSERT INTO bookmakers (slug, name, kind, commission_rate)
    VALUES (${`ex-${tag}`}, 'Probe Exchange', 'exchange', 0.0500) RETURNING id`;
  const [c] = await tx<{ id: string }[]>`
    INSERT INTO countries (slug, name) VALUES (${`c-${tag}`}, 'Probeland') RETURNING id`;
  const [comp] = await tx<{ id: string }[]>`
    INSERT INTO competitions (slug, country_id, type, gender)
    VALUES (${`k-${tag}`}, ${c!.id}, 'league', 'men') RETURNING id`;
  const [se] = await tx<{ id: string }[]>`
    INSERT INTO seasons (slug, competition_id, label, start_year)
    VALUES (${`se-${tag}`}, ${comp!.id}, '2025/26', 2025) RETURNING id`;
  const [h] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`th-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [aw] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`ta-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [f] = await tx<{ id: string }[]>`
    INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
    VALUES (${se!.id}, 'odds-probe', ${h!.id}, ${aw!.id}) RETURNING id`;
  await tx`
    INSERT INTO fixture_schedule
      (fixture_id, kickoff_utc, local_date, local_tz, status, source_id, raw_payload_body_id)
    VALUES (${f!.id}, ${"2026-05-01 15:00Z"}, ${"2026-05-01"}, 'UTC', 'scheduled', ${a!.id}, ${body!.id})`;
  return { srcA: a!.id, srcB: b!.id, body: body!.id, book: book!.id, exch: exch!.id, fixture: f!.id };
};

type S = Awaited<ReturnType<typeof seed>>;

const series = (
  s: S,
  market: string,
  selection: string,
  opts: { line?: string; period?: string; side?: string; bookmaker?: string } = {},
): string =>
  `INSERT INTO odds_series (fixture_id, bookmaker_id, period, market_type, line, selection, side)
   VALUES ('${s.fixture}', '${opts.bookmaker ?? s.book}', '${opts.period ?? "ft"}', '${market}',
           ${opts.line ?? "NULL"}, '${selection}', '${opts.side ?? "back"}')`;

const tick = (
  s: S,
  seriesId: string,
  opts: {
    price?: string; available?: boolean; kind?: string; observedAt?: string;
    knownAt?: string; supersededAt?: string; source?: string; providerAt?: string;
  } = {},
): string =>
  `INSERT INTO odds_ticks
     (series_id, source_id, raw_payload_body_id, price, is_available, price_kind,
      observed_at, provider_at, known_at${opts.supersededAt ? ", superseded_at" : ""})
   VALUES (${seriesId}, '${opts.source ?? s.srcA}', ${s.body}, ${opts.price ?? "2.0000"},
           ${opts.available === false ? "false" : "true"}, '${opts.kind ?? "observed"}',
           timestamptz '${opts.observedAt ?? "2026-04-01 12:00Z"}',
           ${opts.providerAt ? `timestamptz '${opts.providerAt}'` : "NULL"},
           timestamptz '${opts.knownAt ?? "2026-04-01 12:00Z"}'${
             opts.supersededAt ? `, timestamptz '${opts.supersededAt}'` : ""
           })`;

const newSeries = async (tx: postgres.TransactionSql, s: S, stmt: string): Promise<string> => {
  const [row] = await tx.unsafe(`${stmt} RETURNING id`);
  return String((row as unknown as { id: string }).id);
};

const planOf = async (tx: postgres.TransactionSql, query: string): Promise<string> => {
  const rows = await tx.unsafe(`EXPLAIN (COSTS OFF, FORMAT TEXT) ${query}`);
  return (rows as unknown as { "QUERY PLAN": string }[]).map((r) => r["QUERY PLAN"]).join("\n");
};

const cols = async (table: string): Promise<Map<string, { nullable: string; type: string }>> => {
  const rows = await sql<{ column_name: string; is_nullable: string; data_type: string }[]>`
    SELECT column_name, is_nullable, data_type FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = ${table}`;
  return new Map(rows.map((r) => [r.column_name, { nullable: r.is_nullable, type: r.data_type }]));
};

try {
  console.log("odds layer invariants\n");

  // =====================================================================
  console.log("  schema shape:");
  const bmCols = await cols("bookmakers");
  const seCols = await cols("odds_series");
  const tkCols = await cols("odds_ticks");
  check("1. all three odds tables exist", bmCols.size > 0 && seCols.size > 0 && tkCols.size > 0);
  check(
    "2. odds_ticks provenance columns exist and are NOT NULL",
    ["source_id", "raw_payload_body_id", "known_at"].every((c) => tkCols.get(c)?.nullable === "NO"),
  );
  check(
    "3. the four timestamps are present with the right nullability",
    tkCols.get("observed_at")?.nullable === "NO" &&
      tkCols.get("provider_at")?.nullable === "YES" &&
      tkCols.get("known_at")?.nullable === "NO" &&
      tkCols.get("superseded_at")?.nullable === "YES",
  );
  check("4. price is numeric and nullable (suspension)", tkCols.get("price")?.type === "numeric" &&
    tkCols.get("price")?.nullable === "YES");
  check("5. is_available is NOT NULL", tkCols.get("is_available")?.nullable === "NO");
  check("6. odds_series.line is numeric and nullable", seCols.get("line")?.type === "numeric" &&
    seCols.get("line")?.nullable === "YES");
  check("7. bookmakers.commission_rate is numeric and nullable",
    bmCols.get("commission_rate")?.type === "numeric" &&
    bmCols.get("commission_rate")?.nullable === "YES");

  const FORBIDDEN_DERIVED = [
    "implied_probability", "implied_prob", "overround", "fair_probability", "fair_prob",
    "consensus_probability", "edge", "edge_pct", "ev", "kelly", "kelly_fraction", "clv",
  ];
  const CLOSING_FORBIDDEN = [
    "reference_price", "reference_price_kind", "reference_captured_at",
    "closing_odds", "closing_price", "last_observed_pre_kickoff", "kickoff_utc",
  ];
  for (const [table, map] of [["odds_series", seCols], ["odds_ticks", tkCols]] as const) {
    const derived = FORBIDDEN_DERIVED.filter((c) => map.has(c));
    check(`8. ${table} holds NO derived probability/value column`, derived.length === 0, derived.join(","));
    const closing = CLOSING_FORBIDDEN.filter((c) => map.has(c));
    check(`9. ${table} holds no stored closing/reference/kickoff column`, closing.length === 0, closing.join(","));
  }

  // =====================================================================
  console.log("\n  bookmakers — identity, not provenance (§14.2, G18/G19):");
  const bk = await inRollback(async (tx) => {
    const s = await seed(tx, "bk");
    return {
      aggregator: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind) VALUES ('agg-bk', 'Oddschecker', 'aggregator')`),
      sportsbook: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind) VALUES ('sb-bk', 'Book', 'bookmaker')`),
      exchangeNoComm: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind) VALUES ('ex2-bk', 'Smarkets', 'exchange')`),
      exchangeComm: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind, commission_rate) VALUES ('ex3-bk', 'Matchbook', 'exchange', 0.0150)`),
      sportsbookComm: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind, commission_rate) VALUES ('sb2-bk', 'Book2', 'bookmaker', 0.0500)`),
      commTooHigh: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind, commission_rate) VALUES ('ex4-bk', 'Bad', 'exchange', 1.5000)`),
      commNegative: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind, commission_rate) VALUES ('ex5-bk', 'Bad2', 'exchange', -0.0100)`),
      renameName: await codeOf(tx, `UPDATE bookmakers SET name = 'Renamed' WHERE id = '${s.book}'`),
    };
  });
  check("10. kind 'aggregator' is REJECTED — aggregators are data_sources", bk.aggregator === "23514", bk.aggregator);
  check("11. a sportsbook with no commission is accepted", bk.sportsbook === "none", bk.sportsbook);
  check("12. an exchange with NO commission is accepted (permitted, not required)", bk.exchangeNoComm === "none", bk.exchangeNoComm);
  check("13. an exchange WITH a commission is accepted", bk.exchangeComm === "none", bk.exchangeComm);
  check("14. a sportsbook WITH a commission is REJECTED", bk.sportsbookComm === "23514", bk.sportsbookComm);
  check("15. a commission of 1.5 is REJECTED", bk.commTooHigh === "23514", bk.commTooHigh);
  check("16. a negative commission is REJECTED", bk.commNegative === "23514", bk.commNegative);
  check("17. a bookmaker may be renamed (plain mutable reference data)", bk.renameName === "none", bk.renameName);

  const [srcKinds] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM information_schema.columns
     WHERE table_schema='public' AND table_name='data_sources' AND column_name='kinds'`;
  check("18. data_sources still carries `kinds` — an aggregator is a source there", srcKinds?.n === 1);

  // =====================================================================
  console.log("\n  odds_series — taxonomy and the NULLS NOT DISTINCT key (G2, G7, G8):");
  const se = await inRollback(async (tx) => {
    const s = await seed(tx, "se");
    const r: Record<string, string> = {};
    r.oneXTwo = await codeOf(tx, series(s, "1x2", "home"));
    r.duplicate1x2 = await codeOf(tx, series(s, "1x2", "home"));
    r.otherSelection = await codeOf(tx, series(s, "1x2", "draw"));
    r.lineOn1x2 = await codeOf(tx, series(s, "1x2", "away", { line: "2.50" }));
    r.btts = await codeOf(tx, series(s, "btts", "yes"));
    r.ouNoLine = await codeOf(tx, series(s, "over_under", "over"));
    r.ou250 = await codeOf(tx, series(s, "over_under", "over", { line: "2.50" }));
    r.ou275 = await codeOf(tx, series(s, "over_under", "over", { line: "2.75" }));
    r.ahHome = await codeOf(tx, series(s, "asian_handicap", "home", { line: "-0.50" }));
    r.ahAway = await codeOf(tx, series(s, "asian_handicap", "away", { line: "-0.50" }));
    r.htPeriod = await codeOf(tx, series(s, "1x2", "home", { period: "ht" }));
    r.badPeriod = await codeOf(tx, series(s, "1x2", "home", { period: "90min" }));
    r.badMarket = await codeOf(tx, series(s, "corners", "over", { line: "9.50" }));
    r.badSide = await codeOf(tx, series(s, "1x2", "home", { side: "middle" }));
    r.layExchange = await codeOf(tx, series(s, "1x2", "home", { side: "lay", bookmaker: s.exch }));
    return { codes: r };
  });
  const S = se.codes;
  check("19. a 1x2 series (line NULL) inserts", S.oneXTwo === "none", S.oneXTwo);
  check("20. an IDENTICAL 1x2 series is REJECTED — NULLS NOT DISTINCT", S.duplicate1x2 === "23505", S.duplicate1x2);
  check("21. a different selection on the same market inserts", S.otherSelection === "none", S.otherSelection);
  check("22. a line on 1x2 is REJECTED (line forbidden)", S.lineOn1x2 === "23514", S.lineOn1x2);
  check("23. a btts series (line NULL) inserts", S.btts === "none", S.btts);
  check("24. over_under WITHOUT a line is REJECTED (line required)", S.ouNoLine === "23514", S.ouNoLine);
  check("25. over_under 2.50 inserts", S.ou250 === "none", S.ou250);
  check("26. over_under 2.75 inserts alongside 2.50 — lines stay distinct", S.ou275 === "none", S.ou275);
  check("27. asian_handicap -0.50 home inserts", S.ahHome === "none", S.ahHome);
  check("28. …and -0.50 away is the OTHER SELECTION of the same market (G8)", S.ahAway === "none", S.ahAway);
  check("29. a half-time market uses period='ht', not a new market_type", S.htPeriod === "none", S.htPeriod);
  check("30. an unknown period is REJECTED", S.badPeriod === "23514", S.badPeriod);
  check("31. an unapproved market_type is REJECTED", S.badMarket === "23514", S.badMarket);
  check("32. an unknown side is REJECTED", S.badSide === "23514", S.badSide);
  check("33. an exchange lay series inserts alongside back", S.layExchange === "none", S.layExchange);
  const seriesUpd = await sql<{ column_name: string }[]>`
    SELECT column_name FROM information_schema.column_privileges
     WHERE table_name = 'odds_series' AND privilege_type = 'UPDATE' AND grantee <> 'fpp'`;
  check(
    "34. odds_series is IMMUTABLE — NO role holds UPDATE on any column (G14)",
    seriesUpd.length === 0,
    seriesUpd.map((r) => r.column_name).join(","),
  );

  const [keyDef] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = 'odds_series_key'`;
  check(
    "35. the business key really is declared NULLS NOT DISTINCT",
    !!keyDef?.def && /NULLS NOT DISTINCT/.test(keyDef.def),
    keyDef?.def ?? "absent",
  );

  // side-vs-kind is a DATA-QUALITY assertion, never a cross-table CHECK (G21).
  const [sideKindCheck] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_constraint
     WHERE conrelid = 'odds_series'::regclass AND contype = 'c'
       AND pg_get_constraintdef(oid) ILIKE '%bookmaker%kind%'`;
  check("36. no cross-table CHECK couples side to bookmakers.kind (G21)", sideKindCheck?.n === 0);
  const sportsbookLay = await inRollback(async (tx) => {
    const s = await seed(tx, "sl");
    await tx.unsafe(series(s, "1x2", "home", { side: "lay", bookmaker: s.book }));
    const [bad] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_series s JOIN bookmakers b ON b.id = s.bookmaker_id
       WHERE s.side = 'lay' AND b.kind <> 'exchange'`;
    return bad!.n;
  });
  check("37. …so a sportsbook LAY series is detectable by query, as designed", sportsbookLay === 1, `${sportsbookLay}`);
  const [liveBad] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM odds_series s JOIN bookmakers b ON b.id = s.bookmaker_id
     WHERE s.side = 'lay' AND b.kind <> 'exchange'`;
  check("38. and no sportsbook lay series exists in the live database", liveBad?.n === 0, `${liveBad?.n}`);

  // =====================================================================
  console.log("\n  odds_ticks — observations, prices and suspension:");
  const tk = await inRollback(async (tx) => {
    const s = await seed(tx, "tk");
    const sid = await newSeries(tx, s, series(s, "1x2", "home"));
    const r: Record<string, string> = {};
    r.normal = await codeOf(tx, tick(s, sid, { price: "2.1000" }));
    r.sameAgain = await codeOf(tx, tick(s, sid, { price: "2.1000", observedAt: "2026-04-01 13:00Z" }));
    r.exactDuplicate = await codeOf(tx, tick(s, sid, { price: "2.5000" }));
    r.otherSource = await codeOf(tx, tick(s, sid, { price: "2.2000", source: s.srcB }));
    r.otherKind = await codeOf(tx, tick(s, sid, { price: "2.0500", kind: "provider_closing" }));
    r.opening = await codeOf(tx, tick(s, sid, { price: "2.3000", kind: "provider_opening", observedAt: "2026-03-01 09:00Z" }));
    r.sp = await codeOf(tx, tick(s, sid, { price: "2.0200", kind: "exchange_sp", observedAt: "2026-05-01 15:00Z" }));
    r.suspended = await codeOf(tx, tick(s, sid, { price: "NULL", available: false, observedAt: "2026-04-01 14:00Z" }));
    r.unavailWithPrice = await codeOf(tx, tick(s, sid, { price: "2.4000", available: false, observedAt: "2026-04-01 15:00Z" }));
    r.tooLow = await codeOf(tx, tick(s, sid, { price: "0.9900", observedAt: "2026-04-01 16:00Z" }));
    r.tooHigh = await codeOf(tx, tick(s, sid, { price: "200000.0000", observedAt: "2026-04-01 17:00Z" }));
    r.longshot = await codeOf(tx, tick(s, sid, { price: "1500.0000", observedAt: "2026-04-01 18:00Z" }));
    r.badKind = await codeOf(tx, tick(s, sid, { price: "2.0000", kind: "closing", observedAt: "2026-04-01 19:00Z" }));
    r.zeroLength = await codeOf(tx, tick(s, sid, {
      price: "2.0000", observedAt: "2026-04-01 20:00Z",
      knownAt: "2026-04-01 20:00Z", supersededAt: "2026-04-01 20:00Z",
    }));
    const [live] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_ticks WHERE series_id = ${sid} AND superseded_at IS NULL`;
    const [prices] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_ticks
       WHERE series_id = ${sid} AND price = 2.1000`;
    return { codes: r, currentRows: live!.n, identicalPrices: prices!.n };
  });
  const T = tk.codes;
  check("39. an ordinary observed tick inserts", T.normal === "none", T.normal);
  check("40. the SAME price at a LATER instant also inserts", T.sameAgain === "none", T.sameAgain);
  check("41. …and BOTH identical prices survive — uniqueness is not keyed on price", tk.identicalPrices === 2, `${tk.identicalPrices}`);
  check("42. an exact (series, source, instant, kind) current duplicate is REJECTED", T.exactDuplicate === "23505", T.exactDuplicate);
  check("43. a SECOND SOURCE may report the same instant", T.otherSource === "none", T.otherSource);
  check("44. a provider_closing tick coexists at that same instant", T.otherKind === "none", T.otherKind);
  check("45. a provider_opening tick inserts", T.opening === "none", T.opening);
  check("46. an exchange_sp tick inserts", T.sp === "none", T.sp);
  check("47. a SUSPENDED tick (unavailable, price NULL) inserts", T.suspended === "none", T.suspended);
  check("48. unavailable WITH a price is REJECTED", T.unavailWithPrice === "23514", T.unavailWithPrice);
  check("49. a price below 1.01 is REJECTED", T.tooLow === "23514", T.tooLow);
  // numeric(9,4) tops out at 99999.9999, so an over-range price is refused by
  // the TYPE (22003) before the CHECK (23514) can see it. Both are valid
  // rejection paths; the invariant is that the row cannot be written (§11.3).
  check("50. a price above the numeric range is REJECTED",
    T.tooHigh === "22003" || T.tooHigh === "23514", T.tooHigh);
  check("51. a 1500.0 longshot is accepted (exchange lays exceed 1000)", T.longshot === "none", T.longshot);
  check("52. an unapproved price_kind is REJECTED", T.badKind === "23514", T.badKind);
  check("53. superseded_at equal to known_at is REJECTED", T.zeroLength === "23514", T.zeroLength);
  check(
    "54. MANY simultaneously-current rows per series is legal (G10)",
    tk.currentRows >= 7,
    `${tk.currentRows} current rows`,
  );

  // =====================================================================
  console.log("\n  temporal semantics and the cutoff (G20):");
  const T1 = "2026-04-10 09:00Z";
  const T2 = "2026-04-12 00:00Z";
  const T3 = "2026-04-15 11:00Z";
  const temporal = await inRollback(async (tx) => {
    const s = await seed(tx, "tm");
    const sid = await newSeries(tx, s, series(s, "1x2", "home"));
    // An observation made at T1, later found mis-parsed and retracted at T3.
    await tx.unsafe(tick(s, sid, { price: "3.5000", observedAt: T1, knownAt: T1, supersededAt: T3 }));
    // Its replacement, known only from T3 — but describing the SAME instant.
    await tx.unsafe(tick(s, sid, { price: "2.5000", observedAt: T1, knownAt: T3 }));
    // A later observation, and one whose provider_at disagrees wildly.
    await tx.unsafe(tick(s, sid, {
      price: "2.8000", observedAt: "2026-04-11 09:00Z", knownAt: "2026-04-11 09:00Z",
      providerAt: "2020-01-01 00:00Z",
    }));
    const atT2 = await tx<{ price: string }[]>`
      SELECT t.price FROM odds_ticks_as_of(${T2}::timestamptz) t
       WHERE t.series_id = ${sid} AND t.observed_at = ${T1}::timestamptz`;
    const now = await tx<{ price: string }[]>`
      SELECT t.price FROM odds_ticks_as_of(now()) t
       WHERE t.series_id = ${sid} AND t.observed_at = ${T1}::timestamptz`;
    // Reproducibility: known by T AND observed by T.
    const repro = await tx<{ price: string; observed_at: Date }[]>`
      SELECT DISTINCT ON (t.series_id) t.price, t.observed_at
        FROM odds_ticks_as_of(${T2}::timestamptz) t
       WHERE t.series_id = ${sid}
         AND t.observed_at <= ${T2}::timestamptz
         AND t.price_kind = 'observed'
       ORDER BY t.series_id, t.observed_at DESC`;
    // Dropping the observed_at bound would admit a later observation.
    const withoutObservedBound = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_ticks_as_of(${"2026-04-11 12:00Z"}::timestamptz) t
       WHERE t.series_id = ${sid} AND t.price_kind = 'observed'`;
    const withObservedBound = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_ticks_as_of(${"2026-04-11 12:00Z"}::timestamptz) t
       WHERE t.series_id = ${sid} AND t.price_kind = 'observed'
         AND t.observed_at <= ${"2026-04-10 12:00Z"}::timestamptz`;
    const [kept] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM odds_ticks WHERE series_id = ${sid}`;
    const provider = await tx<{ observed_at: Date; provider_at: Date }[]>`
      SELECT observed_at, provider_at FROM odds_ticks
       WHERE series_id = ${sid} AND provider_at IS NOT NULL`;
    return { atT2, now, repro, kept: kept!.n, provider,
             wo: withoutObservedBound[0]!.n, w: withObservedBound[0]!.n };
  });
  check("55. as-of T2 returns the belief we held then (3.5)", Number(temporal.atT2[0]?.price) === 3.5,
    String(temporal.atT2[0]?.price));
  check("56. as-of now returns the corrected belief (2.5)", Number(temporal.now[0]?.price) === 2.5,
    String(temporal.now[0]?.price));
  check("57. the retracted observation was NOT deleted — both rows survive", temporal.kept === 3, `${temporal.kept}`);
  // The LATEST observation both known and observed by T2 is the 04-11 tick at
  // 2.80, not the 04-10 one. Check 55 proves the supersession of the 04-10
  // instant; this proves the cutoff picks the right row of the price path.
  check("58. reproducibility read returns the latest price known AND observed by the cutoff",
    Number(temporal.repro[0]?.price) === 2.8, String(temporal.repro[0]?.price));
  check("59. observed_at is a SEPARATE bound from known_at — dropping it admits more rows",
    temporal.wo > temporal.w, `unbounded=${temporal.wo} bounded=${temporal.w}`);
  check("60. provider_at is stored but never substitutes for observed_at",
    temporal.provider[0] !== undefined &&
      temporal.provider[0].provider_at.getTime() < temporal.provider[0].observed_at.getTime(),
    "an absurd provider_at is retained without displacing observed_at");

  // =====================================================================
  console.log("\n  closing semantics (G4, G5):");
  const closing = await inRollback(async (tx) => {
    const s = await seed(tx, "cl");
    const sid = await newSeries(tx, s, series(s, "1x2", "home"));
    await tx.unsafe(tick(s, sid, { price: "2.6000", observedAt: "2026-04-30 20:00Z" }));
    await tx.unsafe(tick(s, sid, { price: "2.5500", observedAt: "2026-05-01 14:30Z" }));
    await tx.unsafe(tick(s, sid, { price: "2.5000", kind: "provider_closing", observedAt: "2026-05-01 15:00Z" }));
    await tx.unsafe(tick(s, sid, { price: "2.4800", kind: "exchange_sp", observedAt: "2026-05-01 15:00Z" }));
    const kinds = await tx<{ price_kind: string; price: string }[]>`
      SELECT price_kind, price FROM odds_ticks WHERE series_id = ${sid} ORDER BY price_kind`;
    // last_observed_pre_kickoff is DERIVED, bounded by the schedule's kickoff.
    const derived = await tx<{ price: string; observed_at: Date }[]>`
      SELECT t.price, t.observed_at FROM odds_ticks t
        JOIN odds_series os ON os.id = t.series_id
        JOIN fixture_schedule fs ON fs.fixture_id = os.fixture_id AND fs.superseded_at IS NULL
       WHERE t.series_id = ${sid} AND t.price_kind = 'observed'
         AND t.observed_at < fs.kickoff_utc
       ORDER BY t.observed_at DESC LIMIT 1`;
    return { kinds, derived };
  });
  check("61. all four price kinds coexist on one series", closing.kinds.length === 4,
    closing.kinds.map((k) => k.price_kind).join(","));
  check("62. provider_closing and exchange_sp are distinguishable from observed",
    closing.kinds.filter((k) => k.price_kind === "provider_closing").length === 1 &&
      closing.kinds.filter((k) => k.price_kind === "exchange_sp").length === 1);
  check("63. last_observed_pre_kickoff is DERIVABLE and returns 2.55, not the closing 2.50",
    Number(closing.derived[0]?.price) === 2.55, String(closing.derived[0]?.price));

  // =====================================================================
  console.log("\n  the as-of mechanism stays in exactly one place (§11.1):");
  const [pred] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_proc p JOIN pg_namespace n2 ON n2.oid = p.pronamespace
     WHERE n2.nspname = 'public' AND p.prokind = 'f'
       AND pg_get_functiondef(p.oid) LIKE ${`%${PREDICATE}%`}`;
  check("64. catalog: still exactly ONE object defines the predicate, across FIVE wrappers",
    pred?.n === 1, `found ${pred?.n}`);
  const [wrap] = await sql<{ prosrc: string; ret: string }[]>`
    SELECT prosrc, pg_get_function_result(oid) AS ret FROM pg_proc WHERE proname = 'odds_ticks_as_of'`;
  check("65. odds_ticks_as_of delegates to fn_visible_at and never restates",
    !!wrap && wrap.prosrc.includes("fn_visible_at") && !wrap.prosrc.includes(PREDICATE));
  check("66. …and returns SETOF odds_ticks", wrap?.ret === "SETOF odds_ticks", wrap?.ret ?? "absent");
  const [wrapCount] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_proc p JOIN pg_namespace n2 ON n2.oid = p.pronamespace
     WHERE n2.nspname = 'public' AND p.proname LIKE '%\\_as\\_of'`;
  check("67. five as-of wrappers now exist", wrapCount?.n === 5, `${wrapCount?.n}`);
  const selfSrc = readFileSync(new URL(import.meta.url), "utf8");
  const restatements = selfSrc.split(PREDICATE).length - 1;
  check("68. this verification file restates the predicate ZERO times", restatements === 0, `${restatements}`);

  // =====================================================================
  console.log("\n  planning and the approved index set:");
  const plans = await inRollback(async (tx) => {
    const s = await seed(tx, "pl");
    // 600 sibling fixtures, so odds_series is large enough for the planner to
    // face a real choice between an index and a sequential scan.
    await tx.unsafe(`
      INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
      SELECT f.season_id, 'plbulk_' || g, f.home_team_id, f.away_team_id
        FROM fixtures f CROSS JOIN generate_series(1, 600) g WHERE f.id = '${s.fixture}'`);
    await tx.unsafe(`
      INSERT INTO odds_series (fixture_id, bookmaker_id, period, market_type, line, selection)
      SELECT f.id, b.id, 'ft', '1x2', NULL, sel
        FROM fixtures f
        CROSS JOIN (SELECT id FROM bookmakers LIMIT 2) b
        CROSS JOIN (VALUES ('home'),('draw'),('away')) AS m(sel)
       WHERE f.stage LIKE 'plbulk|_%' ESCAPE '|' OR f.id = '${s.fixture}'`);
    // Ticks across EVERY probe series, not just one fixture's. A realistic
    // series is a tiny fraction of the table (~0.03% here); concentrating them
    // on one series would make a sequential scan the planner's correct choice
    // and the index assertion would flap.
    await tx.unsafe(`
      INSERT INTO odds_ticks (series_id, source_id, raw_payload_body_id, price, is_available, observed_at, known_at)
      SELECT os.id, '${s.srcA}', ${s.body}, (1.5 + g * 0.01)::numeric(9,4), true,
             timestamptz '2026-04-01 00:00Z' + (g * interval '30 minutes'),
             timestamptz '2026-04-01 00:00Z' + (g * interval '30 minutes')
        FROM odds_series os CROSS JOIN generate_series(1, 6) g`);
    await tx.unsafe("ANALYZE odds_series");
    await tx.unsafe("ANALYZE odds_ticks");
    const [one] = await tx<{ id: string }[]>`
      SELECT id FROM odds_series WHERE fixture_id = ${s.fixture} ORDER BY id LIMIT 1`;
    return {
      latest: await planOf(tx,
        `SELECT price FROM odds_ticks WHERE series_id = ${one!.id} AND superseded_at IS NULL
          ORDER BY observed_at DESC LIMIT 1`),
      path: await planOf(tx,
        `SELECT price, observed_at FROM odds_ticks WHERE series_id = ${one!.id} ORDER BY observed_at DESC`),
      byFixture: await planOf(tx,
        `SELECT id FROM odds_series WHERE fixture_id = '${s.fixture}'`),
      crossBook: await planOf(tx,
        `SELECT id FROM odds_series WHERE fixture_id = '${s.fixture}'
           AND period = 'ft' AND market_type = '1x2' AND line IS NULL AND selection = 'home'`),
      asOf: await planOf(tx,
        `SELECT id FROM odds_ticks_as_of(timestamptz '2026-04-02 00:00Z') WHERE series_id = ${one!.id}`),
      rows: (await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM odds_ticks`)[0]!.n,
    };
  });
  check("69. the plan probe ran against a non-trivial table", plans.rows >= 20000, `${plans.rows} rows`);
  check("70. latest-price uses odds_ticks_series_time_idx",
    plans.latest.includes("odds_ticks_series_time_idx"), plans.latest.replace(/\n/g, " | "));
  check("71. price-path uses odds_ticks_series_time_idx",
    plans.path.includes("odds_ticks_series_time_idx"), plans.path.replace(/\n/g, " | "));
  check("72. series-by-fixture uses an odds_series index",
    /odds_series_(fixture|market)_idx|odds_series_key/.test(plans.byFixture),
    plans.byFixture.replace(/\n/g, " | "));
  check("73. cross-bookmaker market lookup uses an index",
    /odds_series_market_idx|odds_series_key/.test(plans.crossBook), plans.crossBook.replace(/\n/g, " | "));
  check("74. the as-of wrapper produces NO Function Scan — LANGUAGE sql is inlined",
    !plans.asOf.includes("Function Scan"), plans.asOf.split("\n")[0]);

  const idx = await sql<{ indexname: string }[]>`
    SELECT indexname FROM pg_indexes WHERE schemaname='public'
       AND tablename IN ('bookmakers','odds_series','odds_ticks') ORDER BY indexname`;
  const names = idx.map((r) => r.indexname);
  const EXPECTED = [
    "bookmakers_pkey", "bookmakers_slug_unique",
    "odds_series_fixture_idx", "odds_series_key", "odds_series_market_idx", "odds_series_pkey",
    "odds_ticks_current_idx", "odds_ticks_pkey", "odds_ticks_series_time_idx",
  ];
  check("75. exactly the approved index set exists — no more, no fewer",
    names.length === EXPECTED.length && EXPECTED.every((n) => names.includes(n)), names.join(","));
  check("76. no known_at index on odds_ticks", !names.some((n) => n.includes("known_at")), names.join(","));
  const [parts] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_class WHERE relname = 'odds_ticks' AND relkind = 'p'`;
  check("77. odds_ticks is NOT partitioned (G12)", parts?.n === 0, `${parts?.n}`);

  // =====================================================================
  console.log("\n  grants — engine_rw:");
  const eng = await inRollback(async (tx) => {
    const s = await seed(tx, "ge");
    const sid = await newSeries(tx, s, series(s, "1x2", "home"));
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE}`);
    return {
      insertBookmaker: await codeOf(tx,
        `INSERT INTO bookmakers (slug, name, kind) VALUES ('eng-bk', 'Eng', 'bookmaker')`),
      updateBookmaker: await codeOf(tx, `UPDATE bookmakers SET name = 'X' WHERE id = '${s.book}'`),
      insertSeries: await codeOf(tx, series(s, "btts", "yes")),
      insertTick: await codeOf(tx, tick(s, sid, { price: "1.9000", observedAt: "2026-04-02 10:00Z" })),
      supersedeTick: await codeOf(tx,
        `UPDATE odds_ticks SET superseded_at = now() + interval '1 second' WHERE series_id = ${sid}`),
      updateSeries: await codeOf(tx, `UPDATE odds_series SET selection = 'away' WHERE id = ${sid}`),
      updatePrice: await codeOf(tx, `UPDATE odds_ticks SET price = 9.9 WHERE series_id = ${sid}`),
      updateAvailable: await codeOf(tx, `UPDATE odds_ticks SET is_available = false WHERE series_id = ${sid}`),
      updateKind: await codeOf(tx, `UPDATE odds_ticks SET price_kind = 'exchange_sp' WHERE series_id = ${sid}`),
      updateObserved: await codeOf(tx, `UPDATE odds_ticks SET observed_at = now() WHERE series_id = ${sid}`),
      updateKnown: await codeOf(tx, `UPDATE odds_ticks SET known_at = now() WHERE series_id = ${sid}`),
      updateSource: await codeOf(tx, `UPDATE odds_ticks SET source_id = '${s.srcB}' WHERE series_id = ${sid}`),
      deleteBookmaker: await codeOf(tx, `DELETE FROM bookmakers WHERE id = '${s.book}'`),
      deleteSeries: await codeOf(tx, `DELETE FROM odds_series WHERE id = ${sid}`),
      deleteTick: await codeOf(tx, `DELETE FROM odds_ticks WHERE series_id = ${sid}`),
      truncTicks: await codeOf(tx, `TRUNCATE odds_ticks`),
      truncSeries: await codeOf(tx, `TRUNCATE odds_series CASCADE`),
    };
  });
  for (const k of ["insertBookmaker", "updateBookmaker", "insertSeries", "insertTick", "supersedeTick"] as const) {
    check(`78. engine_rw PERMITTED: ${k}`, eng[k] === "none", eng[k]);
  }
  for (const k of [
    "updateSeries", "updatePrice", "updateAvailable", "updateKind", "updateObserved",
    "updateKnown", "updateSource", "deleteBookmaker", "deleteSeries", "deleteTick",
    "truncTicks", "truncSeries",
  ] as const) {
    check(`79. engine_rw REFUSED: ${k}`, eng[k] === "42501", eng[k]);
  }

  console.log("\n  grants — read-only roles:");
  for (const role of ["app_rw", "analytics_ro"]) {
    const ro = await inRollback(async (tx) => {
      const s = await seed(tx, `ro-${role}`);
      const sid = await newSeries(tx, s, series(s, "1x2", "home"));
      await tx.unsafe(tick(s, sid, { price: "2.0000" }));
      await tx.unsafe(`SET LOCAL ROLE ${role}`);
      const [n] = await tx<{ n: number }[]>`
        SELECT (SELECT count(*)::int FROM odds_series WHERE id = ${sid})
             + (SELECT count(*)::int FROM odds_ticks  WHERE series_id = ${sid}) AS n`;
      return {
        rows: n!.n,
        asOf: await codeOf(tx, `SELECT 1 FROM odds_ticks_as_of(now())`),
        insert: await codeOf(tx, series(s, "btts", "no")),
        update: await codeOf(tx, `UPDATE odds_ticks SET superseded_at = now()`),
        del: await codeOf(tx, `DELETE FROM odds_ticks`),
        trunc: await codeOf(tx, `TRUNCATE odds_ticks`),
      };
    });
    check(`80. ${role} may SELECT the odds layer`, ro.rows === 2, `${ro.rows}`);
    check(`81. ${role} may call odds_ticks_as_of`, ro.asOf === "none", ro.asOf);
    check(`82. ${role} may NOT INSERT`, ro.insert === "42501", ro.insert);
    check(`83. ${role} may NOT UPDATE`, ro.update === "42501", ro.update);
    check(`84. ${role} may NOT DELETE`, ro.del === "42501", ro.del);
    check(`85. ${role} may NOT TRUNCATE`, ro.trunc === "42501", ro.trunc);
  }

  // =====================================================================
  console.log("\n  boundaries — P0-10+ not started, coverage deferred (G13):");
  const [beyond] = await sql<{ t: string | null }[]>`
    SELECT string_agg(tablename, ',') AS t FROM pg_tables WHERE schemaname = 'public'
       AND tablename IN ('odds_coverage','odds_poll_windows','market_consensus','value_signals',
                         'match_events','predictions','prediction_markets','prediction_outcomes',
                         'team_ratings','standings','fixture_match_candidates','competition_coverage',
                         'model_versions','feature_snapshots','odds_snapshots')`;
  check("86. no coverage, value, prediction or P0-10+ table exists", beyond?.t === null, beyond?.t ?? "");
  const [trg] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_trigger WHERE NOT tgisinternal
       AND tgrelid IN ('bookmakers'::regclass,'odds_series'::regclass,'odds_ticks'::regclass)`;
  check("87. no triggers on any odds table", trg?.n === 0, `${trg?.n}`);
  const [ext] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conname = 'external_ids_entity_type_check'`;
  check("88. P0-06 external_ids is untouched — no bookmaker/market entity type",
    !!ext?.def && !/bookmaker|market|odds/i.test(ext.def), ext?.def ?? "absent");
  const [covCols] = await sql<{ c: string | null }[]>`
    SELECT string_agg(table_name || '.' || column_name, ',') AS c
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name IN ('bookmakers','odds_series','odds_ticks')
       AND (column_name ILIKE '%coverage%' OR column_name ILIKE '%poll%'
            OR column_name ILIKE '%complete%' OR column_name ILIKE '%freshness%')`;
  check(
    "89. no coverage-shaped column exists anywhere in the odds layer (G13)",
    covCols?.c === null,
    covCols?.c ?? "",
  );

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}
