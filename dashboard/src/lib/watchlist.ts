import { sql } from "./db";
import { watchlistCapForPlan } from "./labels";

/**
 * The per-user watch list — and the first thing in the web app that has ever
 * written it.
 *
 * `user_companies` has existed since Phase 1c and the digest queue has read it
 * since 1e, but until this slice `scripts/track_companies.py` was its only
 * writer: a new account watched nothing, so its digest was empty forever while
 * Home showed it the whole 50-company shared catalog. This module is the missing
 * half.
 *
 * IT IS A PORT, NOT A NEW DESIGN. db/watchlist.track_companies and
 * db/notifications.SUPPRESS_COMPANY_HISTORY_SQL are the originals; the statements
 * below are those statements with asyncpg's `$1` swapped for postgres.js's tagged
 * placeholders and nothing else changed. tests/test_watchlist_write.py compares
 * them normalised, in both directions, because a port is a second implementation
 * of a rule and therefore a promise to drift — the same arrangement lib/match.ts
 * has with notify/match.py, one table over.
 *
 * WHY IT IS TYPESCRIPT AT ALL, given that the logic already exists in Python.
 * The two runtimes share no code and there is no API between them (CLAUDE.md,
 * "Two runtimes, one database"): a Vercel request cannot call into the crawler's
 * Python, and adding a way for it to would be a far larger change than fifteen
 * lines of SQL. So the statement is written twice and guarded by a test, exactly
 * as the matcher and the resolver already are.
 *
 * ═══ A PAIR USED TO HAVE TWO HALVES, AND THE FILTER-FIRST SLICE REMOVED ONE ═══
 *
 * The digest queue (notify/store.CANDIDATES_SQL) used to be:
 *
 *     open, IN A COMPANY YOU WATCH, not back catalogue, not yet sent to you
 *
 * so the instant a pair existed, every job that company already held was in that
 * person's queue — hundreds of perfectly ordinary rows, open, never emailed to
 * anybody — and their first digest was an archive dump. watchCompany() therefore
 * wrote the pair AND recorded that company's stored jobs as already-notified, in
 * one transaction, only on a genuine insert.
 *
 * The queue does not read this table any more. Company tracking is unlimited and
 * free, every account effectively sees the whole catalog, and what decides an
 * inbox is a person's FILTER — so a pair grants nothing, and suppressing history
 * on the strength of one takes away jobs the person is already owed:
 *
 *     you save your filter at T0, so everything matching then is suppressed. At
 *     T1 job J appears at company X, matches, and enters your queue. At T2, before
 *     the next digest, you pin X here — an act that grants you nothing you did not
 *     already have — and the suppression records every stored job of X, J
 *     included. J leaves your queue silently, behind a row saying it was meant.
 *
 * So the obligation moved rather than being kept as insurance: it hangs off the
 * FILTER changing now, in lib/preferences.savePreferences, which is the port of
 * notify/store.suppress_filter_backlog. This module writes `user_companies` and
 * nothing else — tests/test_watchlist_write.py asserts that it is the only writer
 * of that table, and that lib/preferences.ts is the only writer of the other.
 *
 * WHAT WOULD HAVE TO COME BACK, if the watch list ever becomes a filter dimension
 * again (an opt-in "only email me companies I watch" is the obvious shape): the
 * suppression, here, in one transaction, driven by `RETURNING` against
 * `ON CONFLICT DO NOTHING` so it runs only on a genuine insert. Running it over a
 * pair that already exists swallows everything that arrived since. See
 * db/watchlist.track_companies, which carries the same note on the Python side.
 */

/** How many companies this account watches, and the cap it is watching them under. */
export type WatchlistState = {
  companyIds: number[];
  /** null = no limit; see lib/labels.watchlistCapForPlan. */
  cap: number | null;
  plan: string;
};

/** What one add actually did, so the screen can say it rather than guess. */
export type WatchResult = {
  /** FALSE means the pair was already there — a no-op. */
  inserted: boolean;
  /** The watch list size after the call. */
  watching: number;
};

/** Refused because the account is at its plan's limit. Nothing was written. */
export class WatchlistFullError extends Error {
  readonly cap: number;
  readonly watching: number;

  constructor(cap: number, watching: number) {
    super(
      `you are watching ${watching} companies, which is the limit on your plan ` +
        `(${cap}). Remove one before adding another — nothing has been changed, ` +
        `and the companies you already watch are unaffected.`,
    );
    this.name = "WatchlistFullError";
    this.cap = cap;
    this.watching = watching;
  }
}

/** Refused because no such company exists in the shared catalog. */
export class UnknownCompanyError extends Error {
  constructor(companyId: number) {
    super(
      `no company with id ${companyId} is in the catalog, so it cannot be ` +
        `watched. Add the board on the Add tab first — that puts it in the shared ` +
        `catalog, which is a different act from subscribing to it.`,
    );
    this.name = "UnknownCompanyError";
  }
}

/**
 * The ids this user watches, ascending — the port of
 * db/watchlist.TRACKED_COMPANY_IDS_SQL.
 */
export async function watchedCompanyIds(userId: string): Promise<number[]> {
  const rows = await sql<{ company_id: number }[]>`
    SELECT company_id
      FROM user_companies
     WHERE user_id = ${userId}
     ORDER BY company_id
  `;
  return rows.map((row) => row.company_id);
}

/**
 * The watch list plus the plan it is limited by, in one round trip.
 *
 * The plan is read from `public.users` rather than carried on the session,
 * deliberately: a session is minted once and a plan is a fact about an account
 * that can change under it, and the whole value of enforcing the cap on the
 * server is that it reads the current answer.
 */
export async function watchlistState(userId: string): Promise<WatchlistState> {
  const [ids, [account]] = await Promise.all([
    watchedCompanyIds(userId),
    sql<{ plan: string }[]>`SELECT plan FROM public.users WHERE id = ${userId}`,
  ]);
  const plan = account?.plan ?? "free";
  return { companyIds: ids, cap: watchlistCapForPlan(plan), plan };
}

/**
 * Start watching one company.
 *
 * The port of db/watchlist.track_companies for a single id, with one thing the
 * Python has no reason to carry: the per-plan cap, enforced here rather than in
 * the route, because this is the only place a tracking row can be created and a
 * rule enforced anywhere else is a rule the next caller can skip. That is the
 * argument db/watchlist.py makes about deleting its own bulk statement, applied
 * to a limit instead of to an obligation.
 *
 * ═══ EVERYTHING BELOW HAPPENS IN ONE TRANSACTION ═══
 *
 * 1. an advisory lock on this user, so two clicks racing cannot both see 4 and
 *    both insert. `pg_advisory_xact_lock` is released by the commit or the
 *    rollback and needs no unlock path — a lock this code has to remember to
 *    release is a lock a thrown error leaks. It is keyed by the user, so two
 *    different people never wait on each other.
 * 2. the company has to exist. Without this the FK would refuse the insert with a
 *    23503 that reaches the screen as "server error"; with it the person is told
 *    which id was not found.
 * 3. the pair, `ON CONFLICT DO NOTHING RETURNING company_id`. No row back means
 *    already watching: return, having written nothing.
 * 4. the cap, checked AFTER the insert and on the resulting count. Checking
 *    before would refuse a re-add of a company the person already watches with a
 *    message about a limit, when the honest answer is "you already do". Over the
 *    cap the transaction is rolled back by the throw, so the refusal writes
 *    nothing at all.
 *
 * There was a fifth step until the filter-first slice — the history suppression —
 * and the block comment at the top of this file is why it is not step 5 any more.
 * The transaction survives it: the cap check still has to roll its own insert
 * back, which is a guarantee about two statements rather than about three.
 */
export async function watchCompany(userId: string, companyId: number): Promise<WatchResult> {
  return sql.begin(async (tx) => {
    // hashtextextended gives the bigint the lock function wants from a uuid
    // string. Collisions are harmless: the worst case is two unrelated accounts
    // briefly serialising one insert each.
    await tx`SELECT pg_advisory_xact_lock(hashtextextended(${userId}, 0))`;

    const [company] = await tx<{ id: number }[]>`
      SELECT id FROM companies WHERE id = ${companyId}
    `;
    if (!company) throw new UnknownCompanyError(companyId);

    // db/watchlist.TRACK_ONE_COMPANY_SQL, character for character modulo the
    // placeholder syntax. The ON CONFLICT is the table's UNIQUE (user_id,
    // company_id) doing the work rather than a check in TypeScript, and the
    // RETURNING is what tells "I have just started watching this" from "I already
    // was" — the distinction the suppression hangs off.
    const inserted = await tx<{ company_id: number }[]>`
      INSERT INTO user_companies (user_id, company_id)
      VALUES (${userId}, ${companyId})
      ON CONFLICT (user_id, company_id) DO NOTHING
      RETURNING company_id
    `;

    const [{ watching }] = await tx<{ watching: number }[]>`
      SELECT count(*)::int AS watching FROM user_companies WHERE user_id = ${userId}
    `;

    if (inserted.length === 0) {
      // Already watching it — nothing written, nothing to say.
      return { inserted: false, watching };
    }

    const [account] = await tx<{ plan: string }[]>`
      SELECT plan FROM public.users WHERE id = ${userId}
    `;
    const cap = watchlistCapForPlan(account?.plan);
    if (cap !== null && watching > cap) {
      // The throw rolls the transaction back, so the pair inserted above is gone.
      // `watching - 1` is what they actually have, since the row being counted is
      // the one about to disappear.
      throw new WatchlistFullError(cap, watching - 1);
    }

    return { inserted: true, watching };
  });
}

/**
 * Stop watching one company. Returns false if it was not being watched.
 *
 * It writes nothing but this row, which is a smaller claim than it used to be:
 * while watching was a subscription, this function had to argue at length for
 * LEAVING the suppression rows behind, because deleting them would have made an
 * untrack/re-track cycle re-deliver everything the company held in between. There
 * is no suppression tied to a pair any more, so there is nothing to leave behind
 * and no cycle to worry about.
 *
 * Scoped by user_id AND company_id, so it can only ever remove the caller's own
 * row — the same shape as every other per-user statement in this app.
 */
export async function unwatchCompany(userId: string, companyId: number): Promise<boolean> {
  const rows = await sql<{ company_id: number }[]>`
    DELETE FROM user_companies
     WHERE user_id = ${userId}
       AND company_id = ${companyId}
    RETURNING company_id
  `;
  return rows.length > 0;
}

/*
 * watchesAnything() STOOD HERE AND WENT WITH THE ONBOARDING GATE.
 *
 * PUT /api/preferences was its only caller: it refused to complete onboarding for
 * an account watching nothing, as the fifth member of the family holding the city
 * answer, the keyword list, the experience range and the work arrangement. Every
 * other member of that family guards a row that matches NO JOB AT ALL, and this
 * one earned its place the same way — the queue's first clause was
 * `EXISTS (user_companies …)`, so an account watching nothing matched nothing
 * however good its keywords were.
 *
 * That is simply not true any more. An account watching nothing matches the whole
 * catalog, so the refusal guarded nothing and required an answer that decided
 * nothing — and on the screen, a required question whose answer changes no
 * outcome is worse than no question. The picker stays, the cap stays, /watchlist
 * stays; only the requirement went. See the route for the removal and
 * tests/test_watchlist_write.py for the test that went with it.
 *
 * Deleted rather than left exported-and-unread, on this codebase's standing
 * argument about statements nobody calls.
 */
