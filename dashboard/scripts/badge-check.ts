/**
 * Prove the NEW badge is per-user, against the live database.
 *
 *     npm run badge-check
 *
 * WHAT THE BADGE READS, AND WHAT IT USED TO
 * ------------------------------------------
 * It used to read `jobs.notified`, one boolean on the job meaning "somebody was
 * emailed about this". Phase 1d gave that fact an owner — user_job_notifications,
 * one row per (user, job) — 1e moved the digest queue onto it, and 1f moved this
 * badge, which was the boolean's last reader. lib/jobs.notifiedToUser is the
 * definition now, and it is the same predicate the digest asks in
 * notify/store.CANDIDATES_SQL read from the other side: the queue asks NOT EXISTS
 * to decide what to send, the badge asks EXISTS to decide what to say.
 *
 * That was a change of MEANING, from "somebody was told" to "I was told". For a
 * single account the two are the same set of jobs, which is why 1f could not be
 * proved by looking at one screen — the interesting half only exists once there
 * is a second person.
 *
 * THE THREE SECTIONS
 * ------------------
 *   §1  The column is gone. `jobs` has no `notified`, and nothing in dashboard/
 *       names it.
 *   §2  The badge is genuinely per-user, proved in BOTH directions with a second
 *       account, inside a transaction that always rolls back.
 *   §3  The "for you" counts come from MY preferences row and nobody else's.
 *
 * §1 REPLACED THE MIGRATION PROOF, WHICH HAS EXPIRED. Through 1f this section
 * read both the retiring boolean and the per-user table for every open job and
 * failed if they disagreed about one — the evidence that Home was byte identical
 * across the change. Phase 1g dropped the column, so that comparison is not
 * possible and is not needed; what replaced it is the assertion that the drop
 * actually happened and did not silently get skipped on some environment. Both
 * halves are asked, because they can fail apart: the schema can still carry the
 * column while no code reads it (a database the migration never reached), and the
 * code can name a column the schema no longer has (a revert of 1g's diff).
 *
 * WHY §2 WRITES TO THE LIVE DATABASE, AND WHY THAT IS SAFE
 * --------------------------------------------------------
 * "A job notified to B reads NEW for me" cannot be proved by a user who owns no
 * rows — only the A-side of it can. Proving the other direction needs a second
 * account that has actually been notified about something, so §2 creates one,
 * asks the production predicate under both identities, and rolls back. The
 * rollback is in a `finally` and is unconditional: it does not depend on the
 * assertions passing, on them failing, or on the work reaching the end, so a red
 * run cannot leave an @example.test user or a stray notification row behind. The
 * rollback is then verified by re-counting outside the transaction rather than
 * being assumed. Precedent: scripts/config-roundtrip.ts writes real `companies`
 * rows and removes them.
 *
 * Read-mostly, and it never writes anything for the real user in any code path.
 */

import { config as loadEnv } from "dotenv";

loadEnv({ path: ".env.local" });

/**
 * src/lib/db.ts builds its pool at MODULE SCOPE and static imports are hoisted
 * above loadEnv() above, so importing at the top evaluates dashboardDbUrl()
 * against an empty process.env and throws before the first statement runs. Same
 * deferred import scripts/config-roundtrip.ts documents.
 *
 * Importing the app's own modules is the point of doing it this way: this check
 * runs lib/jobs.notifiedToUser, lib/preferences.loadPreferences,
 * lib/jobs.matchCountsByCompany and lib/badges.homeBadge THEMSELVES. A check that
 * restated the predicate would prove a copy of it, which is precisely the failure
 * `npm run match-parity` exists to catch one language over.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  notifiedToUser: typeof import("../src/lib/jobs").notifiedToUser;
  matchCountsByCompany: typeof import("../src/lib/jobs").matchCountsByCompany;
  loadPreferences: typeof import("../src/lib/preferences").loadPreferences;
  homeBadge: typeof import("../src/lib/badges").homeBadge;
  isMatch: typeof import("../src/lib/match").isMatch;
  currentUserId: typeof import("./current-user").currentUserId;
};

async function loadLib(): Promise<Lib> {
  const [db, jobs, preferences, badges, match, currentUser] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/jobs"),
    import("../src/lib/preferences"),
    import("../src/lib/badges"),
    import("../src/lib/match"),
    // Deferred with the rest, because it imports src/lib/db and would otherwise
    // build the pool before loadEnv() above has run. It lives in scripts/ since
    // 2b — src/ has no identity lookup left to import.
    import("./current-user"),
  ]);
  return {
    sql: db.sql,
    notifiedToUser: jobs.notifiedToUser,
    matchCountsByCompany: jobs.matchCountsByCompany,
    loadPreferences: preferences.loadPreferences,
    homeBadge: badges.homeBadge,
    isMatch: match.isMatch,
    currentUserId: currentUser.currentUserId,
  };
}

/** The second account §2 creates and rolls back. An RFC 6761 example domain. */
const OTHER_EMAIL = "badge-check-second-account@example.test";

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

/** Thrown to end §2's transaction. Never escapes main(). */
class Rollback extends Error {}

/**
 * Every open job, with the one per-user fact a badge is made of.
 *
 * The columns are exactly what lib/match.isMatch reads, plus the company name for
 * the printout — so the comparison can be made where it matters, on the rows Home
 * actually shows. `user_notified` comes from the production predicate itself
 * rather than from a restatement of it.
 *
 * NAMES NO COLUMN PHASE 1g REMOVED. This is what every section runs on; it was
 * split out ahead of the drop so that deleting the old §1 could not silently take
 * the inputs of the sections below it.
 */
type OpenRow = {
  canonical_id: string;
  company: string;
  title: string;
  city: string | null;
  min_experience: number | null;
  is_pm_role: boolean;
  desc_checked: boolean;
  desc_fetch_failed: boolean;
  user_notified: boolean;
};

async function loadOpenRows(lib: Lib, userId: string): Promise<OpenRow[]> {
  const { sql, notifiedToUser } = lib;
  return sql<OpenRow[]>`
    SELECT j.canonical_id,
           c.name AS company,
           j.title,
           j.city,
           j.min_experience,
           j.is_pm_role,
           j.desc_checked,
           j.desc_fetch_failed,
           ${notifiedToUser(userId)}
      FROM jobs j
      JOIN companies c ON c.id = j.company_id
     WHERE j.status = 'open'
     ORDER BY c.name, j.title
  `;
}

async function main() {
  const lib = await loadLib();
  const { sql, notifiedToUser, homeBadge, isMatch } = lib;

  const userId = await lib.currentUserId();
  console.log(`\ncurrent user, resolved from MY_EMAIL by scripts/current-user.ts: ${userId}`);

  const prefs = await lib.loadPreferences(userId);

  // The rows every section works from.
  const rows = await loadOpenRows(lib, userId);
  const matched = rows.filter((row) => isMatch(row, prefs));

  // -------------------------------------------------------------------------
  // §1  The column is gone — from the schema, and from this app's source.
  // -------------------------------------------------------------------------
  console.log("\n§1  the retired notification column is gone");

  // Asked of information_schema rather than by SELECTing the column and catching
  // the error, because a query that throws cannot tell "the column is absent"
  // from "the database is unreachable" — and this check's whole job at this point
  // is to distinguish states that look alike from the outside.
  const [{ still_there: stillThere }] = await sql<{ still_there: number }[]>`
    SELECT count(*)::int AS still_there
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name   = 'jobs'
       AND column_name  = 'notified'
  `;
  // The label spells the column apart from its table on purpose: the dotted form
  // is exactly what tests/test_dashboard_notified.py bans across this directory,
  // and a check script that trips the ban in its own success message would be a
  // silly way to fail a suite.
  check(
    stillThere === 0,
    "the jobs table has no `notified` column",
    `information_schema still reports ${stillThere} — db/schema.sql has not been applied here`,
  );

  console.log(
    `  open jobs ${rows.length}  ·  a row in user_job_notifications for me ` +
      `${rows.filter((r) => r.user_notified).length}  ·  matches ${matched.length}`,
  );

  // The badge, from the shipped function, over the rows Home actually shows.
  //
  // THE ARITHMETIC CHANGED WITH THE FILTER-FIRST SLICE. This used to assert that
  // every match got exactly one of NEW or NOTIFIED, and that is now false by
  // construction: NOTIFIED is gone, so a match that has already been handled gets
  // NO badge. The reason is in lib/badges.ts — a user_job_notifications row means
  // "handled", which since the filter-first slice is far more often a filter save
  // recording the back catalogue than an email that was sent, so "already emailed
  // to you" was a claim the row could not support.
  //
  // What replaces the arithmetic is a PARTITION, which is a stronger statement
  // than the count it replaces: every match is badged NEW or badged nothing, the
  // NEW ones are exactly the un-notified ones, and nothing anywhere still produces
  // the string NOTIFIED.
  let newBadges = 0;
  let unbadged = 0;
  for (const row of matched) {
    const label = homeBadge({ matches: true, user_notified: row.user_notified })?.label;
    check(
      label === "NEW" || label === undefined,
      `a match renders NEW or nothing, never ${String(label)}`,
    );
    if (label === "NEW") newBadges += 1;
    else unbadged += 1;
  }
  console.log(`  Home renders: ${newBadges} NEW / ${unbadged} unbadged`);
  check(
    newBadges + unbadged === matched.length,
    "every match on Home is accounted for — badged NEW, or deliberately bare",
    `${matched.length} matches produced ${newBadges + unbadged} verdicts`,
  );
  check(
    newBadges === matched.filter((r) => !r.user_notified).length,
    "and NEW is exactly the set with no user_job_notifications row — the predicate " +
      "NEW is derived from did not move when NOTIFIED was removed",
    `${newBadges} NEW against ${matched.filter((r) => !r.user_notified).length} un-notified`,
  );
  // "AND NOTIFIED RENDERS NOWHERE" IS NOT ASSERTED HERE, because it cannot be:
  // HomeBadge no longer admits the string, so a comparison against it does not
  // typecheck. That is the stronger guarantee — the badge cannot come back without
  // someone widening the type, which is a deliberate edit rather than a slip — and
  // a runtime check that can never fail is one people learn to ignore. The
  // absence is pinned in the type, in lib/badges.HomeBadge.
  check(
    homeBadge({ matches: false, user_notified: false }) === null,
    "a non-match still gets no badge at all, which NEW's own definition depends on",
  );

  // -------------------------------------------------------------------------
  // §2  Per-user, in both directions, inside a transaction that always rolls back.
  // -------------------------------------------------------------------------
  console.log("\n§2  the badge is per-user — a second account, rolled back");

  // MINE is a job I have been notified about; THEIRS is one I have not, which
  // the second account will be notified about instead. Both are preferred from
  // the match set so the case is literally the one that matters: two people who
  // both match a job, one of whom has been emailed it.
  const mine = matched.find((r) => r.user_notified) ?? rows.find((r) => r.user_notified);
  const theirs = matched.find((r) => !r.user_notified) ?? rows.find((r) => !r.user_notified);
  if (!mine || !theirs) {
    throw new Error(
      "the live database has no pair of open jobs where one is notified to me and one is not, " +
        "so the two directions cannot both be exercised.",
    );
  }
  console.log(`  mine   (I have been told): ${mine.company} — ${mine.title}`);
  console.log(`  theirs (I have not)      : ${theirs.company} — ${theirs.title}`);

  const [{ total: notificationsBefore }] = await sql<{ total: number }[]>`
    SELECT count(*)::int AS total FROM user_job_notifications
  `;

  type Seen = { canonical_id: string; user_notified: boolean };
  let asMe: Seen[] = [];
  let asThem: Seen[] = [];
  let otherId = "";
  let inner: unknown = null;

  try {
    await sql.begin(async (tx) => {
      try {
        const [other] = await tx<{ id: string }[]>`
          INSERT INTO public.users (email) VALUES (${OTHER_EMAIL}) RETURNING id
        `;
        otherId = other.id;

        // The second account has been told about exactly the job I have not.
        await tx`
          INSERT INTO user_job_notifications (user_id, canonical_id)
          VALUES (${other.id}, ${theirs.canonical_id})
        `;

        // The production predicate, run twice over the same two jobs with only
        // the identity changed. Nothing else differs between these two queries.
        const ids = [mine.canonical_id, theirs.canonical_id];
        asMe = await tx<Seen[]>`
          SELECT j.canonical_id, ${notifiedToUser(userId)}
            FROM jobs j WHERE j.canonical_id = ANY(${ids}::text[])
        `;
        asThem = await tx<Seen[]>`
          SELECT j.canonical_id, ${notifiedToUser(other.id)}
            FROM jobs j WHERE j.canonical_id = ANY(${ids}::text[])
        `;
      } catch (err) {
        // Held rather than rethrown, so the finally below decides how this
        // transaction ends and the diagnosis still survives.
        inner = err;
      } finally {
        // UNCONDITIONAL. Not "if the assertions failed", not "if something threw"
        // — the only way out of this callback is a throw, so the transaction can
        // only ever end in ROLLBACK. Nothing above may add a return path.
        throw new Rollback();
      }
    });
  } catch (err) {
    if (!(err instanceof Rollback)) throw err;
  }
  if (inner) throw inner;

  const flag = (rowsSeen: Seen[], id: string) => rowsSeen.find((r) => r.canonical_id === id)?.user_notified;
  const badge = (v: boolean | undefined) => homeBadge({ matches: true, user_notified: v })?.label;

  // THE PROPERTY IS UNCHANGED AND THE SPELLING OF IT IS NOT. This asserted the
  // per-user half in both directions through the NOTIFIED label — "reads NOTIFIED
  // for me, NEW for them". With NOTIFIED removed, the same four claims are made
  // against the ABSENCE of a badge, which is what "handled" looks like now.
  //
  // Both halves still matter, and the second is the one with no visible symptom:
  // a job the OTHER account was emailed must not lose my NEW badge, or the screen
  // would be reporting somebody else's inbox as mine. That is the flattening 1d
  // removed from the queue and 1f removed from the badge, and it is exactly as
  // possible to reintroduce today as it was then.
  check(
    badge(flag(asMe, mine.canonical_id)) === undefined,
    "a job I was emailed carries NO badge for me — it is handled",
  );
  check(
    badge(flag(asThem, mine.canonical_id)) === "NEW",
    "and NEW for the second account, who was never told about it",
  );
  check(
    badge(flag(asThem, theirs.canonical_id)) === undefined,
    "a job the second account was emailed carries no badge for them",
  );
  check(
    badge(flag(asMe, theirs.canonical_id)) === "NEW",
    "and NEW for me — their digest does not silence my badge",
  );
  // The per-user READ is what all four rest on, asserted directly so a failure
  // above can be told apart from a badge bug.
  check(
    flag(asMe, mine.canonical_id) === true && flag(asThem, mine.canonical_id) === false,
    "because notifiedToUser answers differently for the two accounts on one job",
  );

  // The rollback, verified rather than assumed.
  const [{ strays }] = await sql<{ strays: number }[]>`
    SELECT count(*)::int AS strays FROM public.users WHERE lower(email) = lower(${OTHER_EMAIL})
  `;
  const [{ total: notificationsAfter }] = await sql<{ total: number }[]>`
    SELECT count(*)::int AS total FROM user_job_notifications
  `;
  check(strays === 0, "the second account is gone — the transaction rolled back", `${strays} left`);
  check(
    notificationsAfter === notificationsBefore,
    `user_job_notifications is unchanged at ${notificationsBefore} rows`,
    `was ${notificationsBefore}, now ${notificationsAfter}`,
  );
  check(otherId !== "", "…and it did exist inside the transaction, so the test was real");

  // -------------------------------------------------------------------------
  // §3  The "for you" counts are answered with MY filter.
  // -------------------------------------------------------------------------
  console.log('\n§3  "for you" is scoped to me');

  const counts = await lib.matchCountsByCompany(prefs, userId);
  const total = [...counts.values()].reduce((n, c) => n + c.match, 0);
  check(
    total === matched.length,
    `the per-company counts total ${total}, the same number the matcher finds over every open row`,
    `counts ${total}, matcher ${matched.length}`,
  );

  // loadPreferences() resolved a row by user_id; this is the same row asked for
  // by id directly. If the loader ever fell back to "the preferences row" the
  // way it did before Phase 1b, this is where a second account's filter would
  // show up as somebody else's numbers on my screen.
  const [ownRow] = await sql<{ cities: string[]; max_level: number }[]>`
    SELECT cities, max_level FROM preferences WHERE user_id = ${userId}
  `;
  check(Boolean(ownRow), "a preferences row exists for the resolved user");
  check(
    Boolean(ownRow) &&
      ownRow.max_level === prefs.max_level &&
      [...ownRow.cities].sort().join() === [...prefs.cities].sort().join(),
    "and it is the row the counts above were computed from",
  );

  const [{ others }] = await sql<{ others: number }[]>`
    SELECT count(*)::int AS others FROM preferences WHERE user_id IS DISTINCT FROM ${userId}
  `;
  console.log(`  preferences rows owned by anybody else: ${others}`);

  console.log("\nmatches per company (what Home shows):");
  const byId = new Map<number, { name: string }>(
    (await sql<{ id: number; name: string }[]>`SELECT id, name FROM companies`).map((c) => [
      c.id,
      { name: c.name },
    ]),
  );
  for (const [companyId, c] of [...counts].sort((a, b) => b[1].match - a[1].match)) {
    console.log(`  ${String(c.match).padStart(3)}  ${byId.get(companyId)?.name ?? companyId}`);
  }

  console.log(
    failures
      ? `\nFAILED — ${failures} assertion(s) did not hold.`
      : `\nOK — the column is gone, the badge is per-user in both directions, and "for you" is mine.`,
  );
  if (failures) process.exitCode = 1;
}

main()
  .catch((err) => {
    console.error("\nFAILED:", err instanceof Error ? err.message : err);
    process.exitCode = 1;
  })
  .finally(async () => {
    const { sql } = await import("../src/lib/db");
    await sql.end({ timeout: 5 });
  });
