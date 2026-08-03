/**
 * Prove the NEW / NOTIFIED badge is per-user, and that making it per-user changed
 * nothing on my screen.
 *
 *     npm run badge-check
 *
 * WHAT PHASE 1f DID, AND WHY IT NEEDS A CHECK OF ITS OWN
 * ------------------------------------------------------
 * The badge used to read `jobs.notified`, one boolean on the job meaning
 * "somebody was emailed about this". Phase 1d gave that fact an owner —
 * user_job_notifications, one row per (user, job) — and 1e moved the digest queue
 * onto it. The dashboard's badge was the last reader of the boolean left, and 1f
 * is what moved it. lib/jobs.notifiedToUser is the new definition, and it is the
 * same predicate the digest asks in notify/store.CANDIDATES_SQL, read from the
 * other side: the queue asks NOT EXISTS to decide what to send, the badge asks
 * EXISTS to decide what to say.
 *
 * That is a change of MEANING, from "somebody was told" to "I was told", and for
 * the single real account the two are the same set of jobs. "Are" rather than
 * "should be" is the thing worth checking, so §1 checks it job by job.
 *
 * THE THREE SECTIONS
 * ------------------
 *   §1  For me, the two readings agree — every open job, and the badge each one
 *       produces. This is the migration proof: if it is green, Home is byte
 *       identical to what it showed before 1f.
 *   §2  The badge is genuinely per-user, proved in BOTH directions with a second
 *       account, inside a transaction that always rolls back.
 *   §3  The "for you" counts come from MY preferences row and nobody else's.
 *
 * §1 IS THE ONE FILE IN dashboard/ THAT STILL READS jobs.notified, DELIBERATELY.
 * It is the before/after comparison, so it has to name both sides. Phase 1g drops
 * that column, at which point this section stops being possible and stops being
 * needed — 1g's diff should delete it, and until then the check failing with
 * `column "notified" does not exist` is the reminder. Everything a page render
 * touches is free of the column already, which is the precondition 1g depends on;
 * tests/test_dashboard_notified.py asserts that textually and names this file as
 * the single enumerated exemption.
 *
 * §1 IS ALSO SELF-CONTAINED, WHICH IT DID NOT USED TO BE. It runs its own query
 * and derives its own match set, so §2 and §3 borrow nothing from it. That is
 * deliberate preparation rather than tidiness: §1 is a block 1g deletes whole, and
 * a section that fed the two sections below it could not be deleted without
 * silently taking their inputs with it — the two per-user proofs would vanish in
 * the same diff that removes the column they exist to justify removing. Everything
 * §2 and §3 need comes from loadOpenRows(), which names only the per-user
 * predicate and never the boolean.
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
  currentUserId: typeof import("../src/lib/currentUser").currentUserId;
};

async function loadLib(): Promise<Lib> {
  const [db, jobs, preferences, badges, match, currentUser] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/jobs"),
    import("../src/lib/preferences"),
    import("../src/lib/badges"),
    import("../src/lib/match"),
    import("../src/lib/currentUser"),
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
 * NAMES NO COLUMN THAT PHASE 1g REMOVES. This is what every section below §1
 * runs on, which is what makes §1 a block that can be deleted on its own.
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

/** §1's extra column: the retiring boolean, alongside what replaced it. */
type MigrationRow = OpenRow & { boolean_says: boolean };

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
  console.log(`\ncurrent user, resolved from MY_EMAIL by lib/currentUser.ts: ${userId}`);

  const prefs = await lib.loadPreferences();

  // The rows every section from §2 onwards works from. Read once, before §1, so
  // that deleting §1 leaves them exactly where they are.
  const rows = await loadOpenRows(lib, userId);
  const matched = rows.filter((row) => isMatch(row, prefs));

  // -------------------------------------------------------------------------
  // §1  The migration proof: for me, the two readings are the same set.
  //     PHASE 1g DELETES THIS SECTION WHOLE. Nothing below reads anything it
  //     defines — see the header note.
  // -------------------------------------------------------------------------
  console.log("\n§1  the old boolean and the per-user table, compared for me");

  // Both readings on every open row: the column being retired, and the
  // production fragment that replaced it.
  const migrationRows = await sql<MigrationRow[]>`
    SELECT j.canonical_id,
           c.name AS company,
           j.title,
           j.city,
           j.min_experience,
           j.is_pm_role,
           j.desc_checked,
           j.desc_fetch_failed,
           j.notified AS boolean_says,
           ${notifiedToUser(userId)}
      FROM jobs j
      JOIN companies c ON c.id = j.company_id
     WHERE j.status = 'open'
     ORDER BY c.name, j.title
  `;

  const booleanTrue = migrationRows.filter((r) => r.boolean_says).length;
  const mineTrue = migrationRows.filter((r) => r.user_notified).length;
  console.log(
    `  open jobs ${migrationRows.length}  ·  jobs.notified = TRUE ${booleanTrue}  ·  ` +
      `a row in user_job_notifications for me ${mineTrue}`,
  );

  const disagreements = migrationRows.filter((r) => r.boolean_says !== r.user_notified);
  for (const row of disagreements.slice(0, 10)) {
    console.log(
      `  DIFF  ${row.company} — ${row.title}\n` +
        `        boolean says ${row.boolean_says ? "notified" : "not notified"}, ` +
        `the table says ${row.user_notified ? "notified" : "not notified"}`,
    );
  }
  check(
    disagreements.length === 0,
    "every open job reads the same either way (symmetric difference is 0)",
    `${disagreements.length} row(s) disagree`,
  );

  // The whole table, not just the open rows the screens can show. A disagreement
  // on a closed job is invisible on every screen and is still the same fault:
  // the boolean and the per-user table remembering different histories.
  const [wide] = await sql<{ boolean_but_no_row: number; row_but_no_boolean: number }[]>`
    SELECT
      (SELECT count(*) FROM jobs j
        WHERE j.notified = TRUE
          AND NOT EXISTS (SELECT 1 FROM user_job_notifications n
                           WHERE n.user_id = ${userId} AND n.canonical_id = j.canonical_id)
      )::int AS boolean_but_no_row,
      (SELECT count(*) FROM jobs j
        WHERE j.notified = FALSE
          AND EXISTS (SELECT 1 FROM user_job_notifications n
                       WHERE n.user_id = ${userId} AND n.canonical_id = j.canonical_id)
      )::int AS row_but_no_boolean
  `;
  check(
    wide.boolean_but_no_row === 0 && wide.row_but_no_boolean === 0,
    "and across every stored job, open or closed, in both directions",
    `notified with no row: ${wide.boolean_but_no_row}, row with no boolean: ${wide.row_but_no_boolean}`,
  );

  // The badge itself, from the shipped function, fed each reading in turn. This
  // is the assertion that actually says "Home looks identical": not that the two
  // columns agree, but that the thing rendered from them is the same word.
  const migrationMatched = migrationRows.filter((row) => isMatch(row, prefs));
  const before = { NEW: 0, NOTIFIED: 0 };
  const after = { NEW: 0, NOTIFIED: 0 };
  let badgeDiff = 0;
  for (const row of migrationMatched) {
    const was = homeBadge({ matches: true, user_notified: row.boolean_says })?.label;
    const now = homeBadge({ matches: true, user_notified: row.user_notified })?.label;
    if (was) before[was as "NEW" | "NOTIFIED"] += 1;
    if (now) after[now as "NEW" | "NOTIFIED"] += 1;
    if (was !== now) {
      badgeDiff += 1;
      if (badgeDiff <= 10) console.log(`  BADGE  ${row.company} — ${row.title}: ${was} -> ${now}`);
    }
  }
  console.log(
    `  matches ${migrationMatched.length}  ·  from the boolean: ${before.NOTIFIED} NOTIFIED / ` +
      `${before.NEW} NEW  ·  from the table: ${after.NOTIFIED} NOTIFIED / ${after.NEW} NEW`,
  );
  check(badgeDiff === 0, "every match on Home renders the same badge either way", `${badgeDiff} changed`);

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

  check(badge(flag(asMe, mine.canonical_id)) === "NOTIFIED", "a job I was emailed reads NOTIFIED for me");
  check(
    badge(flag(asThem, mine.canonical_id)) === "NEW",
    "and NEW for the second account, who was never told about it",
  );
  check(
    badge(flag(asThem, theirs.canonical_id)) === "NOTIFIED",
    "a job the second account was emailed reads NOTIFIED for them",
  );
  check(
    badge(flag(asMe, theirs.canonical_id)) === "NEW",
    "and NEW for me — their digest does not silence my badge",
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

  const counts = await lib.matchCountsByCompany(prefs);
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
      : `\nOK — the badge is per-user, and it says exactly what it said before Phase 1f for me.`,
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
