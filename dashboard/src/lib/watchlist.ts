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
 * ═══ CREATING A PAIR HAS TWO HALVES, AND THE SECOND ONE IS THE POINT ═══
 *
 * A tracking row does not only say "send me this company's jobs". It also decides
 * what counts as this person's history. The digest queue
 * (notify/store.CANDIDATES_SQL) is:
 *
 *     open, in a company you watch, not back catalogue, not yet sent to you
 *
 * so the instant the pair exists, every job that company already holds is in that
 * person's queue — hundreds of perfectly ordinary rows, open, never emailed to
 * anybody — and their first digest is an archive dump. `jobs.seeded` does not
 * cover this: it stops a company's FIRST-EVER crawl reaching anyone and says
 * nothing about the weeks of ordinary crawling after it, which is the door history
 * walks through.
 *
 * So watchCompany() writes the pair AND records that company's stored jobs as
 * already-notified for that user, IN ONE TRANSACTION. Not tidiness: between the
 * two there is a state in which the pair exists and the suppression does not, and
 * the hourly digest runs in exactly that state if the process dies there.
 *
 * ONLY ON A GENUINE INSERT — the sharp edge. Suppression records every job the
 * company has RIGHT NOW as already-notified. Running it over a pair that already
 * exists would swallow everything that arrived since, which is somebody's actual
 * queue, silently. So it is driven by what `RETURNING` gave back against
 * `ON CONFLICT DO NOTHING`, which the table's UNIQUE makes exact.
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
  /** FALSE means the pair was already there — a no-op, and no history suppressed. */
  inserted: boolean;
  /**
   * How many of that company's stored jobs were recorded as already-seen for this
   * user by this call. THE NUMBER THAT SAYS THE FIRST DIGEST IS NOT AN ARCHIVE
   * DUMP, so it is returned rather than logged: the picker prints it.
   */
  suppressed: number;
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
 * Start watching one company, and do not deliver its history.
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
 *    already watching: return, having written nothing and — critically — having
 *    suppressed nothing.
 * 4. the cap, checked AFTER the insert and on the resulting count. Checking
 *    before would refuse a re-add of a company the person already watches with a
 *    message about a limit, when the honest answer is "you already do". Over the
 *    cap the transaction is rolled back by the throw, so the refusal writes
 *    nothing at all.
 * 5. the history suppression, over EVERY job of that company — not only the open
 *    ones. A closed job that reopens keeps its identity and comes back
 *    `status = 'open'` from crawler/store.UPSERT_SEEN, so scoping this to open
 *    rows leaves a hole one flap wide.
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
      // Already watching it. Its history was suppressed when the pair was first
      // created, and re-running the suppression now would swallow every job that
      // has arrived since.
      return { inserted: false, suppressed: 0, watching };
    }

    const [account] = await tx<{ plan: string }[]>`
      SELECT plan FROM public.users WHERE id = ${userId}
    `;
    const cap = watchlistCapForPlan(account?.plan);
    if (cap !== null && watching > cap) {
      // The throw rolls the transaction back, so the pair inserted above is gone
      // and no suppression ran. `watching - 1` is what they actually have, since
      // the row being counted is the one about to disappear.
      throw new WatchlistFullError(cap, watching - 1);
    }

    // db/notifications.SUPPRESS_COMPANY_HISTORY_SQL, same modulo placeholders.
    // INSERT ... SELECT rather than a loop for the reason the Python states: the
    // set is decided by the database at the instant the statement runs, so there
    // is no window between "which jobs does this company have" and "record them"
    // for the two answers to disagree.
    const suppressed = await tx<{ canonical_id: string }[]>`
      INSERT INTO user_job_notifications (user_id, canonical_id)
      SELECT ${userId}, j.canonical_id
        FROM jobs j
       WHERE j.company_id = ${companyId}
      ON CONFLICT (user_id, canonical_id) DO NOTHING
      RETURNING canonical_id
    `;

    return { inserted: true, suppressed: suppressed.length, watching };
  });
}

/**
 * Stop watching one company. Returns false if it was not being watched.
 *
 * THE SUPPRESSION ROWS ARE DELIBERATELY LEFT BEHIND, and that is the whole of
 * what this function decides. Deleting them would mean that untracking and
 * re-tracking re-delivers everything the company held in between — an inbox full
 * of postings a person once said they did not want to hear about, arriving
 * because they toggled a checkbox twice. Leaving them costs some rows that no
 * query joins to while the pair is absent, and db/watchlist.track_companies
 * already documents the re-track case as the one that proves the design: the
 * suppression runs again over a catalogue that has moved on, and every job it
 * names is either already recorded (a no-op) or history this person never asked
 * to hear about.
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

/**
 * Does this account watch anything at all?
 *
 * Read by PUT /api/preferences, which refuses to complete onboarding for an
 * account watching nothing — the fourth member of the family that already holds
 * the city answer, the keyword list and the work arrangement. An empty watch list
 * matches no job by construction: the queue's first clause is
 * `EXISTS (user_companies …)`, so a person released from onboarding without one
 * gets an empty digest and an empty Home, for a reason nothing on the screen
 * explains. See the route for why that refusal is the same argument as an empty
 * city selection rather than a new kind of rule.
 *
 * `EXISTS` rather than a count, because the question is existence — the same
 * reason notify/store.CANDIDATES_SQL asks it that way one table over.
 */
export async function watchesAnything(userId: string): Promise<boolean> {
  const [row] = await sql<{ any_watched: boolean }[]>`
    SELECT EXISTS (SELECT 1 FROM user_companies WHERE user_id = ${userId}) AS any_watched
  `;
  return row?.any_watched ?? false;
}
