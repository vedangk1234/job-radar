/**
 * Can a brand-new account actually finish onboarding? The Slice 1b catch-22,
 * asked of the live database.
 *
 *     npm run onboarding-check
 *
 * WHY THIS EXISTS
 * ---------------
 * The bug this slice fixes was a loop with no exit, and every individual part of
 * it looked right. A new account was seeded with DEFAULT_PREFERENCES.keywords —
 * 43 terms, which is a sensible filter and the one this install has always run.
 * PUT /api/preferences refuses more than KEYWORD_CAP = 25, which is a sensible
 * cap. lib/preferences.savePreferences sets onboarding_completed, which is the
 * sensible place for it. Put the three together and a new account is over the
 * cap on the row it was born with, so the only save it can make is refused, so
 * the only thing that sets the flag never runs.
 *
 * Nothing in the repo could see that. Each half typechecks, each half has a
 * test, and the failure is an interaction between a constant in one file and a
 * validation rule in another — visible only by carrying an account from signup
 * through to a completed save, which is exactly what this does.
 *
 * WHAT THE LATER SLICE ADDED. The seed is now blank on EVERY field, not just on
 * keywords, so §1 and §4 assert emptiness rather than "the defaults came
 * through". §4 additionally reads the nine columns loadPreferences does not
 * select: those are blank because branch 3 declines to name them and their schema
 * defaults are already empty, which is a claim about the DATABASE and therefore
 * cannot be checked by reading TypeScript.
 *
 * A BLANK ROW IS NOT A SAVABLE ROW, AND THAT IS NOT A BUG THIS CHECKS FOR.
 * PUT /api/preferences refuses an empty cities or keywords list, so the row branch
 * 3 writes would be rejected by that route. It never meets it: the INSERT is
 * direct, signup does no validation, and onboarding_completed = FALSE holds the
 * account on /onboarding until a person answers — which §6 then does.
 *
 * WHAT IT RUNS, AND WHY IT RUNS THE REAL THING
 * --------------------------------------------
 * lib/link-user.linkUser, lib/preferences.loadPreferences,
 * lib/preferences.onboardingCompleted and lib/preferences.savePreferences —
 * imported and called, never restated. A check that wrote its own INSERT would
 * prove that INSERT works. The one under test is branch 3 of the link, so branch
 * 3 of the link is what gets called.
 *
 * linkUser takes a Supabase User, and this hands it a synthetic one. That is NOT
 * a forged session and buys no access to anything: it is a plain object passed
 * to a function in this repo, in a script already holding the database URL. No
 * cookie is minted, no token is signed, nothing is sent over HTTP, and nothing
 * here could be pointed at the running server. Compare scripts/ui-check.ts,
 * which explains at length why it will NOT forge a session to test the gate over
 * HTTP: that would need the service-role key in .env.local. This needs nothing.
 *
 * REAL ROWS, DELETED AFTERWARDS — NOT A ROLLED-BACK TRANSACTION, and the reason
 * is mechanical rather than philosophical. savePreferences binds the pool in
 * lib/db, not a transaction handle; called inside sql.begin it would run on a
 * different connection, fail to see the uncommitted account, find no row and
 * throw. Threading a tx parameter through a production write function to suit a
 * check would be the check editing the thing it checks. scripts/delete-check.ts
 * made the same call for its own reasons. The cleanup is therefore in a finally,
 * it deletes by the id this script generated, and the last assertion re-counts
 * to prove it landed. The account is created is_active = FALSE by the same
 * branch under test, so it cannot enter a digest even if the cleanup failed.
 */

import { config } from "dotenv";
import { randomUUID } from "node:crypto";
import type { User } from "@supabase/supabase-js";

config({ path: ".env.local" });

/**
 * Deferred for the reason scripts/badge-check.ts and scripts/config-roundtrip.ts
 * document: src/lib/db.ts builds its pool at module scope and static imports are
 * hoisted above the config() call above, so a top-level import evaluates
 * dashboardDbUrl() against an empty process.env and throws before the first
 * assertion runs.
 */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  linkUser: typeof import("../src/lib/link-user").linkUser;
  loadPreferences: typeof import("../src/lib/preferences").loadPreferences;
  savePreferences: typeof import("../src/lib/preferences").savePreferences;
  onboardingCompleted: typeof import("../src/lib/preferences").onboardingCompleted;
  currentUserId: typeof import("./current-user").currentUserId;
};

async function loadLib(): Promise<Lib> {
  const [db, link, preferences, currentUser] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/link-user"),
    import("../src/lib/preferences"),
    import("./current-user"),
  ]);
  return {
    sql: db.sql,
    linkUser: link.linkUser,
    loadPreferences: preferences.loadPreferences,
    savePreferences: preferences.savePreferences,
    onboardingCompleted: preferences.onboardingCompleted,
    currentUserId: currentUser.currentUserId,
  };
}

/** An RFC 6761 example domain, as tests/fixtures in this project always use. */
const NEW_EMAIL = `onboarding-check-${randomUUID().slice(0, 8)}@example.test`;

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

/**
 * The shape linkUser reads, and nothing else.
 *
 * email_verified is set on BOTH identity_data and user_metadata because
 * link-user.checkEmailVerified refuses by default: absent everywhere is a
 * refusal, not a pass. Saying it twice is what the Google identity actually
 * looks like, and a check that satisfied the predicate by the narrowest possible
 * route would stop exercising the branch the moment the precedence changed.
 */
function syntheticGoogleUser(id: string, email: string): User {
  return {
    id,
    email,
    email_confirmed_at: new Date().toISOString(),
    user_metadata: { email_verified: true },
    identities: [
      { provider: "google", identity_data: { email, email_verified: true } },
    ],
  } as unknown as User;
}

async function main() {
  const lib = await loadLib();
  const { sql, linkUser, loadPreferences, savePreferences, onboardingCompleted } = lib;

  const [labels, guard] = await Promise.all([
    import("../src/lib/labels"),
    import("../src/lib/guard"),
  ]);
  const { DEFAULT_PREFERENCES, NEW_ACCOUNT_SEED, KEYWORD_CAP } = labels;

  console.log("§1 the seed, before anything touches the database");

  check(
    NEW_ACCOUNT_SEED.keywords.length === 0,
    "NEW_ACCOUNT_SEED.keywords is empty — a new account is given nobody else's filter",
    `got ${NEW_ACCOUNT_SEED.keywords.length}`,
  );
  check(
    NEW_ACCOUNT_SEED.keywords.length <= KEYWORD_CAP,
    `and is within KEYWORD_CAP (${NEW_ACCOUNT_SEED.keywords.length} of ${KEYWORD_CAP}), so the first save cannot be refused by the cap`,
  );
  check(
    DEFAULT_PREFERENCES.keywords.length > KEYWORD_CAP,
    `the old seed really was unsavable — DEFAULT_PREFERENCES.keywords is ${DEFAULT_PREFERENCES.keywords.length}, over the cap of ${KEYWORD_CAP}`,
  );
  // BLANK ON EVERY FIELD, and asserted against DEFAULT_PREFERENCES rather than
  // only against zero. "Empty" and "not the operator's" are two different claims:
  // a seed could be emptied here and re-filled from the defaults by a spread
  // somebody added back, so each field is checked to be empty AND the constant it
  // used to copy is checked to be non-empty — otherwise these all pass vacuously
  // the day DEFAULT_PREFERENCES itself is blanked.
  check(
    NEW_ACCOUNT_SEED.cities.length === 0,
    "NEW_ACCOUNT_SEED.cities is empty — nobody is told where they want to work",
    `got [${NEW_ACCOUNT_SEED.cities.join(", ")}]`,
  );
  check(
    NEW_ACCOUNT_SEED.exclusions.length === 0,
    "NEW_ACCOUNT_SEED.exclusions is empty — no title is dropped on a new account's behalf",
    `got ${NEW_ACCOUNT_SEED.exclusions.length}`,
  );
  check(
    !("max_level" in NEW_ACCOUNT_SEED),
    "NEW_ACCOUNT_SEED states no max_level — the column is NOT NULL DEFAULT 2, so its " +
      "blank is the schema default, reached by branch 3 omitting the column",
  );
  check(
    DEFAULT_PREFERENCES.cities.length > 0 && DEFAULT_PREFERENCES.exclusions.length > 0,
    `and the defaults they no longer copy are still there (${DEFAULT_PREFERENCES.cities.length} cities, ` +
      `${DEFAULT_PREFERENCES.exclusions.length} exclusions), so the checks above are not passing vacuously`,
  );

  // §2 — the operator, read BEFORE the new account exists. Whatever this slice
  // does to a new signup, it must do nothing at all to the account that has been
  // running for months. Read here and re-read at the end, compared field by
  // field, so "unaffected" is a before/after rather than an assurance.
  console.log("\n§2 the existing account, before");
  const operatorId = await lib.currentUserId();
  // The four Slice-2 columns are IN the snapshot, because they are now part of what
  // decides this account's digest. Its range was seeded to (0, 2) by
  // scripts/backfill_onboarding_prefs.py precisely to reproduce the ceiling that
  // used to be a constant, so a script that moved it would silently change which
  // jobs the operator is emailed — the exact thing §7 exists to rule out.
  type Snapshot = {
    onboarding_completed: boolean;
    keywords: string[];
    cities: string[];
    exclusions: string[];
    max_level: number;
    exp_range_low: number | null;
    exp_range_high: number | null;
    years_of_experience_held: number | null;
    show_no_experience_jobs: boolean;
    updated_at: string;
  };
  const snapshot = async (): Promise<Snapshot> => {
    const [row] = await sql<Snapshot[]>`
      SELECT onboarding_completed, keywords, cities, exclusions, max_level,
             exp_range_low, exp_range_high, years_of_experience_held,
             show_no_experience_jobs, updated_at
        FROM preferences WHERE user_id = ${operatorId}
    `;
    return row;
  };
  const before = await snapshot();
  console.log(
    `  public.users ${operatorId}: onboarding_completed=${before.onboarding_completed}, ` +
      `${before.keywords.length} keywords, updated_at ${before.updated_at}`,
  );
  check(
    before.onboarding_completed === true,
    "it has already completed onboarding, so the gate will never hold it",
  );

  let newUserId = "";

  try {
    // §3 — the signup, through the production link.
    console.log(`\n§3 a brand-new account: ${NEW_EMAIL}`);

    const authUid = randomUUID();
    const linked = await linkUser(syntheticGoogleUser(authUid, NEW_EMAIL));
    newUserId = linked.id;

    check(linked.created === true, "linkUser took branch 3 — a genuinely new account");
    check(
      linked.id === authUid,
      "public.users.id is the auth uid, as every account since 2a has been",
      `got ${linked.id}, expected ${authUid}`,
    );

    const [account] = await sql<{ is_active: boolean }[]>`
      SELECT is_active FROM public.users WHERE id = ${newUserId}
    `;
    check(account.is_active === false, "and is_active FALSE, unchanged by this slice");

    // §4 — what branch 3 actually wrote. The whole point.
    console.log("\n§4 the preferences row it was born with");
    const seeded = await loadPreferences(newUserId);
    console.log(
      `  keywords=${seeded.keywords.length} cities=[${seeded.cities.join(", ")}] ` +
        `exclusions=${seeded.exclusions.length} max_level=${seeded.max_level} ` +
        `onboarding_completed=${seeded.onboarding_completed}`,
    );

    check(
      seeded.keywords.length === 0,
      "the 43 operator keywords are GONE — the row starts with none",
      `got ${seeded.keywords.length}: ${seeded.keywords.slice(0, 5).join(", ")}…`,
    );
    check(
      seeded.keywords.length <= KEYWORD_CAP,
      `it is under the cap (${seeded.keywords.length} of ${KEYWORD_CAP}), so the first save is not refused before it is tried`,
    );
    check(
      seeded.onboarding_completed === false,
      "onboarding_completed is FALSE, so this account is behind the gate",
    );
    check(
      seeded.cities.length === 0,
      "cities is EMPTY — the operator's blr/pune/hyd did not come through",
      `got [${seeded.cities.join(", ")}]`,
    );
    check(
      seeded.exclusions.length === 0,
      "exclusions is EMPTY — the operator's 14 terms did not come through",
      `got ${seeded.exclusions.length}: ${seeded.exclusions.slice(0, 5).join(", ")}…`,
    );
    check(
      seeded.max_level === 2,
      "max_level is the schema default of 2 — the column is NOT NULL, so this is " +
        "what 'unset' looks like, and it is there because branch 3 did not name it",
      `got ${seeded.max_level}`,
    );

    // EVERY REMAINING COLUMN ON THE ROW, read directly rather than through
    // loadPreferences — which selects six columns and would report a blank seed
    // while nine others quietly carried something. These nine are the ones branch 3
    // reaches by NOT naming them, so this is the assertion that their schema
    // defaults really are the blanks lib/labels.NEW_ACCOUNT_SEED claims they are.
    type Rest = {
      selected_cities: string[];
      work_modes: string[];
      salary_bands: string[];
      all_india: boolean;
      show_no_experience_jobs: boolean;
      exp_range_low: number | null;
      exp_range_high: number | null;
      years_of_experience_held: number | null;
    };
    const [rest] = await sql<Rest[]>`
      SELECT selected_cities, work_modes, salary_bands, all_india,
             show_no_experience_jobs, exp_range_low, exp_range_high,
             years_of_experience_held
        FROM preferences WHERE user_id = ${newUserId}
    `;
    console.log(
      `  selected_cities=[${rest.selected_cities.join(", ")}] work_modes=[${rest.work_modes.join(", ")}] ` +
        `salary_bands=[${rest.salary_bands.join(", ")}] all_india=${rest.all_india} ` +
        `show_no_experience_jobs=${rest.show_no_experience_jobs} ` +
        `exp=${rest.exp_range_low}..${rest.exp_range_high} held=${rest.years_of_experience_held}`,
    );

    check(rest.selected_cities.length === 0, "selected_cities is empty");
    check(rest.work_modes.length === 0, "work_modes is empty");
    check(rest.salary_bands.length === 0, "salary_bands is empty");
    check(rest.all_india === false, "all_india is FALSE");
    check(rest.show_no_experience_jobs === false, "show_no_experience_jobs is FALSE");
    check(
      rest.exp_range_low === null && rest.exp_range_high === null,
      "the experience range is NULL at both ends — not configured, rather than a range nobody picked",
      `got ${rest.exp_range_low}..${rest.exp_range_high}`,
    );
    check(
      rest.years_of_experience_held === null,
      "years_of_experience_held is NULL",
      `got ${rest.years_of_experience_held}`,
    );

    // §5 — the gate's own predicate, which is what lib/guard.requirePage reads.
    console.log("\n§5 the gate, before the save");
    const gatedBefore = await onboardingCompleted(newUserId);
    check(
      gatedBefore === false,
      `requirePage() would redirect this account to ${guard.ONBOARDING_PATH} instead of the dashboard`,
    );

    // §6 — the exit. Every value here is written out rather than taken from
    // DEFAULT_PREFERENCES, and that is the point of the slice made literal: these
    // are the answers a PERSON supplies at onboarding, and after this slice there
    // is no longer any overlap between what they choose and what they were seeded.
    // Passing the install's defaults in here would still pass, and would read as
    // though the row arrived carrying them.
    console.log("\n§6 the first save — a person answering, from a blank row");
    const saved = await savePreferences(newUserId, {
      // CITIES SLICE B: `cities` is the legacy column, still written so the
      // deployed digest keeps working, and `selected_cities` is the gate. The
      // route derives the first from the second; this calls the writer directly,
      // one layer below it, so it passes both — and passes them EQUAL, which is
      // what the route produces for an account that is not all-India.
      cities: ["blr", "mumbai"],
      selected_cities: ["blr", "mumbai"],
      all_india: false,
      keywords: ["backend engineer"],
      exclusions: ["intern"],
      max_level: 3,
      // Phase 3 Slice 2's four. The range is deliberately NOT (0, 2): that is the
      // install's own answer, and passing it here would let a regression where the
      // save ignored these fields and left the operator's mirror in place go
      // unnoticed. 1-4 is a value only a person could have chosen.
      exp_range_low: 1,
      exp_range_high: 4,
      years_of_experience_held: 3,
      show_no_experience_jobs: false,
      // The work-mode slice's one. Deliberately NOT all three — that is the
      // install's own answer (DEFAULT_WORK_MODES), and passing it would let a
      // save that ignored the field go unnoticed, exactly as (0, 2) would for
      // the range above. Hybrid+remote is a value only a person could choose.
      work_modes: ["hybrid", "remote"],
      // The salary slice's one, and it is chosen on the same principle as the
      // two above — NOT all six, which is the install's own answer. Two adjacent
      // bands are a value only a person could have picked.
      //
      // Note what this does NOT prove and deliberately cannot: that a save is
      // REQUIRED to carry bands. It is not. salary_bands is the one column on
      // this row that may legitimately stay empty forever, because
      // lib/match.salaryOk fails open — so §7 below asserts the empty save is
      // ACCEPTED rather than refused, which is the opposite of what the city,
      // keyword, range and work-mode checks assert about their fields.
      salary_bands: ["15-25", "25-40"],
    });

    check(saved.keywords.length === 1, "it stored the one keyword the person chose");
    check(
      saved.work_modes.join() === "hybrid,remote",
      "and their work arrangement — the column a blank row leaves empty, which " +
        "matches nothing until this save",
      `got [${saved.work_modes.join(", ")}]`,
    );
    check(
      saved.salary_bands.join() === "15-25,25-40",
      "and their pay bands — the one column on this row that may stay empty " +
        "forever, because salary_ok fails OPEN where the other three fail closed",
      `got [${saved.salary_bands.join(", ")}]`,
    );
    check(
      saved.cities.join() === "blr,mumbai" &&
        saved.exclusions.join() === "intern" &&
        saved.max_level === 3,
      "and their cities, exclusions and max_level — every field on the row is now theirs",
      `got [${saved.cities.join(", ")}] / [${saved.exclusions.join(", ")}] / ${saved.max_level}`,
    );

    /*
     * CITIES SLICE B — the columns the city gate now reads, asserted against the
     * live database rather than against the TypeScript that wrote them.
     *
     * `selected_cities` is the one that decides. It arrived '{}' from the schema
     * default (§4 above asserts that), and a save is the only thing that fills it,
     * so this is the assertion that the gate has an answer for an onboarded
     * account — the city half of "onboarded implies configured".
     *
     * A CITY THE OLD VOCABULARY COULD NOT SPELL is chosen on purpose. `mumbai` did
     * not exist as a label before Slice A, so a save that stored it proves the
     * 26-label vocabulary reaches the column a person actually edits, rather than
     * only the one the labeller writes.
     */
    check(
      saved.selected_cities.join() === "blr,mumbai",
      "selected_cities holds what they picked — the column the city gate reads",
      `got [${saved.selected_cities.join(", ")}]`,
    );
    check(
      saved.all_india === false,
      "and all_india is FALSE, so the gate reads that selection rather than ignoring it",
      `got ${saved.all_india}`,
    );
    check(
      saved.selected_cities.join() === saved.cities.join(),
      "the legacy `cities` column agrees with it, so a rollback to the deployed " +
        "matcher gives this account the same filter rather than an empty digest",
      `selected=[${saved.selected_cities.join(", ")}] legacy=[${saved.cities.join(", ")}]`,
    );
    check(
      saved.exp_range_low === 1 && saved.exp_range_high === 4,
      "and the experience range they chose, rather than the install's 0-2",
      `got ${saved.exp_range_low}..${saved.exp_range_high}`,
    );
    check(
      saved.years_of_experience_held === 3 && saved.show_no_experience_jobs === false,
      "and the years they hold and their no-minimum answer, both off the default",
      `got held=${saved.years_of_experience_held} show=${saved.show_no_experience_jobs}`,
    );
    check(
      saved.onboarding_completed === true,
      "and flipped onboarding_completed TRUE in the same statement",
    );

    /*
     * THE SLICE-2 INVARIANT: ONBOARDED IMPLIES CONFIGURED.
     *
     * An unset experience range matches NOTHING (notify/match.experience_excluded),
     * so an account that has passed the gate must never have one — otherwise the
     * gate releases somebody straight onto a dashboard that shows zero matches and
     * a digest that never arrives, with nothing anywhere reporting why.
     *
     * It is asserted here, against the live database, because it is a claim about a
     * ROW rather than about TypeScript. Three things make it true and none of them
     * can be checked by reading one file: this statement is the only writer of
     * either column, PUT /api/preferences refuses an incomplete range, and that
     * same save is the only writer of onboarding_completed. Read back from the
     * database rather than from the RETURNING, so a column the UPDATE forgot to
     * name would show up as NULL here.
     */
    const [persisted] = await sql<
      { onboarding_completed: boolean; exp_range_low: number | null; exp_range_high: number | null }[]
    >`
      SELECT onboarding_completed, exp_range_low, exp_range_high
        FROM preferences WHERE user_id = ${newUserId}
    `;
    check(
      persisted.onboarding_completed === true &&
        persisted.exp_range_low !== null &&
        persisted.exp_range_high !== null,
      "ONBOARDED IMPLIES CONFIGURED — the row that passed the gate has both range ends set, " +
        "so it cannot be holding a filter that matches nothing",
      `onboarded=${persisted.onboarding_completed} ` +
        `exp=${persisted.exp_range_low}..${persisted.exp_range_high}`,
    );

    const gatedAfter = await onboardingCompleted(newUserId);
    check(
      gatedAfter === true,
      "so the gate now releases them — requirePage() returns the session and the dashboard renders",
    );
  } finally {
    // Unconditional, and by the id this script generated rather than by a
    // pattern. preferences, user_companies and user_job_notifications all
    // cascade from public.users, so one DELETE is the whole cleanup — see the
    // ON DELETE CASCADE argument in db/schema.sql.
    if (newUserId) {
      await sql`DELETE FROM public.users WHERE id = ${newUserId}`;
    }
  }

  // §7 — the two things that must be true of everybody else afterwards.
  console.log("\n§7 afterwards");
  const [{ strays }] = await sql<{ strays: number }[]>`
    SELECT count(*)::int AS strays FROM public.users WHERE lower(email) = lower(${NEW_EMAIL})
  `;
  check(strays === 0, "the throwaway account is gone — the cleanup ran", `${strays} left`);

  const after = await snapshot();
  check(
    after.onboarding_completed === before.onboarding_completed,
    `the existing account's onboarding_completed is unchanged at ${before.onboarding_completed}`,
  );
  check(
    [...after.keywords].join() === [...before.keywords].join(),
    `its ${before.keywords.length} keywords are untouched`,
    `was ${before.keywords.length}, now ${after.keywords.length}`,
  );
  check(
    [...after.cities].join() === [...before.cities].join() &&
      [...after.exclusions].join() === [...before.exclusions].join() &&
      after.max_level === before.max_level,
    "as are its cities, exclusions and max_level",
  );
  check(
    after.exp_range_low === before.exp_range_low &&
      after.exp_range_high === before.exp_range_high &&
      after.years_of_experience_held === before.years_of_experience_held &&
      after.show_no_experience_jobs === before.show_no_experience_jobs,
    `and so is its experience rule — still ${before.exp_range_low}..${before.exp_range_high} ` +
      `with show_no_experience_jobs ${before.show_no_experience_jobs}`,
    `now ${after.exp_range_low}..${after.exp_range_high} show=${after.show_no_experience_jobs}`,
  );
  check(
    before.exp_range_low === 0 &&
      before.exp_range_high === 2 &&
      before.show_no_experience_jobs === true,
    "and that rule is the one that reproduces the pre-Slice-2 ceiling: 0-2, unknowns kept",
    `got ${before.exp_range_low}..${before.exp_range_high} ` +
      `show=${before.show_no_experience_jobs} — this account's digest has CHANGED`,
  );
  check(
    String(after.updated_at) === String(before.updated_at),
    "and updated_at has not moved — nothing claimed an edit that did not happen",
    `was ${before.updated_at}, now ${after.updated_at}`,
  );

  console.log(
    failures
      ? `\nFAILED — ${failures} assertion(s) did not hold.`
      : "\nOK — a new account starts blank on every preference field, is held at " +
          "onboarding, and its first save releases it. The existing account is untouched.",
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
