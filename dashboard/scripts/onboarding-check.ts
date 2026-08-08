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
  // The spread, asserted rather than read off the page: the three fields this
  // slice did NOT change must still be the install's defaults, which is what
  // stops a hand-copied literal drifting from the Python.
  check(
    [...NEW_ACCOUNT_SEED.cities].join() === [...DEFAULT_PREFERENCES.cities].join(),
    `cities is untouched (${NEW_ACCOUNT_SEED.cities.join(", ")})`,
  );
  check(
    [...NEW_ACCOUNT_SEED.exclusions].join() === [...DEFAULT_PREFERENCES.exclusions].join(),
    `exclusions is untouched (${NEW_ACCOUNT_SEED.exclusions.length} terms)`,
  );
  check(
    NEW_ACCOUNT_SEED.max_level === DEFAULT_PREFERENCES.max_level,
    `max_level is untouched (${NEW_ACCOUNT_SEED.max_level})`,
  );

  // §2 — the operator, read BEFORE the new account exists. Whatever this slice
  // does to a new signup, it must do nothing at all to the account that has been
  // running for months. Read here and re-read at the end, compared field by
  // field, so "unaffected" is a before/after rather than an assurance.
  console.log("\n§2 the existing account, before");
  const operatorId = await lib.currentUserId();
  type Snapshot = {
    onboarding_completed: boolean;
    keywords: string[];
    cities: string[];
    exclusions: string[];
    max_level: number;
    updated_at: string;
  };
  const snapshot = async (): Promise<Snapshot> => {
    const [row] = await sql<Snapshot[]>`
      SELECT onboarding_completed, keywords, cities, exclusions, max_level, updated_at
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
      [...seeded.cities].join() === [...DEFAULT_PREFERENCES.cities].join(),
      "cities came through as the install's defaults",
      `got [${seeded.cities.join(", ")}]`,
    );
    check(
      [...seeded.exclusions].join() === [...DEFAULT_PREFERENCES.exclusions].join(),
      `exclusions came through as the install's ${DEFAULT_PREFERENCES.exclusions.length} defaults`,
      `got ${seeded.exclusions.length}`,
    );
    check(seeded.max_level === DEFAULT_PREFERENCES.max_level, "max_level came through as the default");

    // §5 — the gate's own predicate, which is what lib/guard.requirePage reads.
    console.log("\n§5 the gate, before the save");
    const gatedBefore = await onboardingCompleted(newUserId);
    check(
      gatedBefore === false,
      `requirePage() would redirect this account to ${guard.ONBOARDING_PATH} instead of the dashboard`,
    );

    // §6 — the exit. One keyword is all a person has to supply, and this is the
    // save the old seed made impossible.
    console.log("\n§6 the first save — the act the old seed refused");
    const saved = await savePreferences(newUserId, {
      cities: [...DEFAULT_PREFERENCES.cities],
      keywords: ["backend engineer"],
      exclusions: [...DEFAULT_PREFERENCES.exclusions],
      max_level: DEFAULT_PREFERENCES.max_level,
    });

    check(saved.keywords.length === 1, "it stored the one keyword the person chose");
    check(
      saved.onboarding_completed === true,
      "and flipped onboarding_completed TRUE in the same statement",
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
    String(after.updated_at) === String(before.updated_at),
    "and updated_at has not moved — nothing claimed an edit that did not happen",
    `was ${before.updated_at}, now ${after.updated_at}`,
  );

  console.log(
    failures
      ? `\nFAILED — ${failures} assertion(s) did not hold.`
      : "\nOK — a new account starts with no keywords, is held at onboarding, " +
          "and its first save releases it. The existing account is untouched.",
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
