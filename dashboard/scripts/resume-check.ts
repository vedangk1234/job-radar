/**
 * Prove resume isolation and resume erasure against the LIVE bucket.
 *
 *     npm run resume-check
 *
 * WHAT THIS ANSWERS THAT NOTHING ELSE CAN
 * ---------------------------------------
 * db/schema.sql states four storage policies. tests/test_rls.py proves the
 * statements are in the file. scripts/check_rls.py proves they are on the
 * database and shaped correctly. None of those three proves the thing that
 * actually matters — that a REAL signed-in person is refused a REAL object
 * belonging to somebody else — because all three read catalogues rather than
 * exercising a policy.
 *
 * So this signs two throwaway accounts in for real, has one upload a file, and
 * then asks for that file three ways: with the published anon key, with the
 * other account's genuine JWT, and with the owner's own. The first two must be
 * refused and the third must succeed. That last clause is not a courtesy — a
 * policy that denies everybody would pass a test that only checked for refusals,
 * and would ship a feature where nobody can read their own resume.
 *
 * WHY IT CAN CREATE A SESSION AT ALL, GIVEN THE APP USES GOOGLE. Google's
 * consent screen needs a browser, which would make the entire authenticated half
 * of storage untestable from a terminal — the same problem `npm run mint-session`
 * solved for Phase 1 by minting a cookie. Here a cookie is not enough: the
 * storage API needs a JWT GoTrue itself issued, because the policies read
 * auth.uid() out of it. So the throwaways are created as EMAIL/PASSWORD users
 * directly in auth.users (this connection is `postgres`, the same role the
 * Supabase SQL editor uses) and signed in through the ordinary public endpoint
 * with the anon key. The JWTs that come back are indistinguishable from a real
 * person's, which is the entire point — a proof against a hand-made token would
 * be a proof about the token.
 *
 * SAFETY, on the same terms as scripts/delete-check.ts:
 *   - Fixed ids in a reserved range, @example.test addresses (RFC 6761).
 *   - §0 scrubs leftovers before starting; the `finally` scrubs after, and it
 *     removes storage objects as well as rows.
 *   - Nothing here ever names a real account, and the only DELETE aimed at
 *     public.users is aimed at an id this file just created.
 */

import { config as loadEnv } from "dotenv";

loadEnv({ path: ".env.local" });

import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import WebSocketImpl from "ws";

/*
 * A WEBSOCKET SHIM, FOR NODE 20 AND FOR THIS SCRIPT ONLY.
 *
 * supabase-js constructs a RealtimeClient inside createClient() whether or not
 * anything subscribes to a channel, and that constructor wants a global
 * WebSocket. Node 22 has one; Node 20 does not, and this machine is on 20 —
 * so the client threw before a single storage call was made.
 *
 * `ws` is a devDependency and is referenced nowhere under src/. Vercel runs Node
 * 22, where the global is native, so the deployed app neither needs nor sees
 * this. It is a local Node-version shim for a check script, not a runtime
 * dependency — and it is assigned only when the global is genuinely absent, so
 * on Node 22 this line does nothing at all.
 */
if (!(globalThis as { WebSocket?: unknown }).WebSocket) {
  (globalThis as { WebSocket?: unknown }).WebSocket = WebSocketImpl;
}

/** Deferred for the reason delete-check defers: lib/db builds its pool at import. */
type Lib = {
  sql: typeof import("../src/lib/db").sql;
  deleteAccount: typeof import("../src/lib/account").deleteAccount;
  resumeStorage: typeof import("../src/lib/resume").resumeStorage;
  BUCKET: typeof import("../src/lib/resume").BUCKET;
};

async function loadLib(): Promise<Lib> {
  const [db, account, resume] = await Promise.all([
    import("../src/lib/db"),
    import("../src/lib/account"),
    import("../src/lib/resume"),
  ]);
  return {
    sql: db.sql,
    deleteAccount: account.deleteAccount,
    resumeStorage: resume.resumeStorage,
    BUCKET: resume.BUCKET,
  };
}

const A = {
  accountId: "00000000-0000-4000-8000-0000000c0fa1",
  authId: "00000000-0000-4000-8000-0000000c0fa2",
  email: "resume-check-a@example.test",
  password: "resume-check-a-pw-8f2b1d",
};
const B = {
  accountId: "00000000-0000-4000-8000-0000000c0fb1",
  authId: "00000000-0000-4000-8000-0000000c0fb2",
  email: "resume-check-b@example.test",
  password: "resume-check-b-pw-4a7e3c",
};
const THROWAWAYS = [A, B];

let failures = 0;

function check(ok: boolean, label: string, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail && !ok ? `\n        ${detail}` : ""}`);
  if (!ok) failures += 1;
}

const SUPABASE_URL = (process.env.NEXT_PUBLIC_SUPABASE_URL ?? "").trim();
const ANON_KEY = (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "").trim();

/** A tiny but genuinely valid PDF, so the object stored is a real file. */
function samplePdf(): Uint8Array {
  const text = "BT /F1 12 Tf 72 720 Td (Resume check fixture) Tj ET";
  const objs = [
    "<</Type/Catalog/Pages 2 0 R>>",
    "<</Type/Pages/Kids[3 0 R]/Count 1>>",
    "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
    `<</Length ${text.length}>>\nstream\n${text}\nendstream`,
  ];
  let out = "%PDF-1.4\n";
  const offsets: number[] = [];
  objs.forEach((body, i) => {
    offsets.push(out.length);
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n`;
  for (const o of offsets) out += `${String(o).padStart(10, "0")} 00000 n \n`;
  out += `trailer\n<</Size ${objs.length + 1}/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF\n`;
  return new Uint8Array(Buffer.from(out, "latin1"));
}

/**
 * Create an auth.users row that GoTrue will accept a password sign-in for.
 *
 * email_confirmed_at is the one that is easy to miss: without it GoTrue refuses
 * the sign-in as unconfirmed and the failure looks like a wrong password.
 * pgcrypto lives in the `extensions` schema on Supabase, so crypt() is tried
 * qualified first and unqualified second rather than assuming a search_path.
 */
async function createAuthUser(sql: Lib["sql"], who: typeof A): Promise<void> {
  // WHICH COLUMNS EXIST IS ASKED, NOT ASSUMED. auth.users is Supabase's table and
  // GoTrue reshapes it on their schedule — confirmation_token and friends are NOT
  // NULL on some versions and absent on others. A hardcoded column list would
  // fail on a live database for a reason that has nothing to do with what this
  // script tests, so the four empty-string token columns are included only if
  // this project's auth schema actually has them.
  const columns = await sql<{ column_name: string }[]>`
    SELECT column_name FROM information_schema.columns
     WHERE table_schema = 'auth' AND table_name = 'users'
  `;
  const present = new Set(columns.map((c) => c.column_name));
  const tokenColumns = [
    "confirmation_token",
    "recovery_token",
    "email_change",
    "email_change_token_new",
  ].filter((c) => present.has(c));

  const names = [
    "instance_id", "id", "aud", "role", "email", "encrypted_password",
    "email_confirmed_at", "created_at", "updated_at",
    "raw_app_meta_data", "raw_user_meta_data",
    ...tokenColumns,
  ];

  // pgcrypto lives in `extensions` on Supabase, but a project whose search_path
  // includes it answers to the bare name. Qualified first, bare second.
  for (const schema of ["extensions.", ""]) {
    const values = [
      "'00000000-0000-0000-0000-000000000000'", "$1", "'authenticated'",
      "'authenticated'", "$2",
      `${schema}crypt($3, ${schema}gen_salt('bf'))`,
      "now()", "now()", "now()",
      `'{"provider":"email","providers":["email"]}'::jsonb`, "'{}'::jsonb",
      ...tokenColumns.map(() => "''"),
    ];
    try {
      await sql.unsafe(
        `INSERT INTO auth.users (${names.join(", ")}) VALUES (${values.join(", ")})`,
        [who.authId, who.email, who.password],
      );
      return;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (/function .*(crypt|gen_salt)/i.test(message) && schema !== "") continue;
      throw err;
    }
  }
}

async function seed(sql: Lib["sql"], who: typeof A): Promise<void> {
  await createAuthUser(sql, who);
  await sql`
    INSERT INTO public.users (id, email, auth_user_id, is_active)
    VALUES (${who.accountId}, ${who.email}, ${who.authId}, FALSE)
  `;
  await sql`
    INSERT INTO preferences (user_id, cities, keywords, exclusions, max_level)
    VALUES (${who.accountId}, ARRAY['blr']::text[], ARRAY['engineer']::text[],
            ARRAY['intern']::text[], 2)
  `;
}

/** Sign in for real and return a client carrying the issued JWT. */
async function signIn(who: typeof A): Promise<SupabaseClient> {
  const client = createClient(SUPABASE_URL, ANON_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data, error } = await client.auth.signInWithPassword({
    email: who.email,
    password: who.password,
  });
  if (error || !data.session) {
    throw new Error(
      `could not sign in as ${who.email}: ${error?.message ?? "no session returned"}. ` +
        `If this says the provider is disabled, enable Email in ` +
        `Supabase -> Authentication -> Providers for the length of this check.`,
    );
  }
  if (data.user?.id !== who.authId) {
    throw new Error(`signed in as ${data.user?.id}, expected ${who.authId}`);
  }
  return client;
}

/** Objects actually present in a folder, asked as postgres — the honest count. */
async function objectsInFolder(sql: Lib["sql"], authId: string): Promise<string[]> {
  const rows = await sql<{ name: string }[]>`
    SELECT name FROM storage.objects
     WHERE bucket_id = 'resumes' AND name LIKE ${`${authId}/%`}
     ORDER BY name
  `;
  return rows.map((r) => r.name);
}

/**
 * Sign in, or null if the account is not there. Used only by the scrubbers.
 *
 * A missing throwaway is the ordinary case on a clean run, so it is not an
 * error — but a throwaway that exists and cannot be signed in to IS worth
 * knowing about, because it means its objects cannot be cleaned up.
 */
async function signInIfExists(who: typeof A): Promise<SupabaseClient | null> {
  try {
    return await signIn(who);
  } catch {
    return null;
  }
}

/**
 * Remove a throwaway's storage objects THROUGH THE STORAGE API.
 *
 * NOT `DELETE FROM storage.objects`. Supabase guards that table with a trigger —
 * storage.protect_delete() raises 42501, "Direct deletion from storage tables is
 * not allowed", with the hint "This prevents accidental data loss from orphaned
 * objects". Which is exactly right: the row in storage.objects is a pointer, the
 * bytes live in object storage, and deleting the pointer by hand would strand
 * them where nothing can ever reach them again.
 *
 * That guard is worth noticing rather than working around, because it is the
 * same hazard lib/account.ts is built to avoid one level up. The API call is the
 * only honest way to remove an object, and the only credential that can make it
 * is the owner's own session — so this signs in FIRST and deletes the auth row
 * afterwards. Reversed, the object would outlive every credential able to
 * remove it.
 */
async function scrubObjects(sql: Lib["sql"], who: typeof A, bucket: string): Promise<void> {
  const objects = await objectsInFolder(sql, who.authId);
  if (objects.length === 0) return;

  const client = await signInIfExists(who);
  if (!client) {
    console.warn(
      `cleanup: ${who.email} has ${objects.length} object(s) but could not be signed ` +
        `in to remove them — the leftover check below will report them`,
    );
    return;
  }
  const { error } = await client.storage.from(bucket).remove(objects);
  if (error) console.warn(`cleanup: could not remove ${objects.join(", ")}: ${error.message}`);
}

/** The rows. Safe over SQL — only storage.objects is trigger-protected. */
async function scrubRows(sql: Lib["sql"]): Promise<void> {
  for (const who of THROWAWAYS) {
    await sql`DELETE FROM public.users WHERE id = ${who.accountId} OR lower(email) = ${who.email}`;
    await sql`DELETE FROM auth.users WHERE id = ${who.authId}`;
  }
}

/** Objects first, then rows — see scrubObjects for why that order is forced. */
async function scrub(sql: Lib["sql"], bucket: string) {
  for (const who of THROWAWAYS) await scrubObjects(sql, who, bucket);
  await scrubRows(sql);
}

async function main() {
  if (!SUPABASE_URL || !ANON_KEY) {
    console.error(
      "NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY must be set in .env.local",
    );
    process.exit(1);
  }

  const { sql, deleteAccount, resumeStorage, BUCKET } = await loadLib();
  const aPath = `${A.authId}/resume.pdf`;

  try {
    console.log("\n§0  clearing any throwaway left by an interrupted run");
    await scrub(sql, BUCKET);
    for (const who of THROWAWAYS) {
      const left = await objectsInFolder(sql, who.authId);
      check(left.length === 0, `${who.email}: folder empty`, left.join(", "));
    }

    // The bucket must be private, or every refusal below is meaningless.
    const [bucket] = await sql<{ public: boolean }[]>`
      SELECT public FROM storage.buckets WHERE id = 'resumes'
    `;
    check(!!bucket, "the 'resumes' bucket exists");
    check(bucket?.public === false, "and it is PRIVATE — otherwise nothing below proves anything");

    // -----------------------------------------------------------------
    console.log("\n§1  two throwaway accounts, signed in for real");
    for (const who of THROWAWAYS) await seed(sql, who);
    const aClient = await signIn(A);
    const bClient = await signIn(B);
    check(true, `${A.email} and ${B.email} both hold a GoTrue-issued JWT`);

    // -----------------------------------------------------------------
    console.log("\n§2  A uploads a resume");
    const { error: uploadError } = await aClient.storage
      .from(BUCKET)
      .upload(aPath, samplePdf(), { contentType: "application/pdf", upsert: true });
    check(!uploadError, "A can write into A's own folder", uploadError?.message);

    const stored = await objectsInFolder(sql, A.authId);
    check(stored.length === 1 && stored[0] === aPath, "the object is really there", stored.join(", "));

    await sql`
      INSERT INTO resumes (user_id, bucket_path, filename, content_type, size_bytes)
      VALUES (${A.accountId}, ${aPath}, 'resume.pdf', 'application/pdf', ${samplePdf().length})
    `;

    // -----------------------------------------------------------------
    console.log("\n§3  THE ISOLATION PROOF — three ways of asking for A's file");

    // (a) the published anon key, with no session at all.
    const anonClient = createClient(SUPABASE_URL, ANON_KEY, {
      auth: { persistSession: false, autoRefreshToken: false },
    });
    const anonDownload = await anonClient.storage.from(BUCKET).download(aPath);
    check(!!anonDownload.error, "anon key CANNOT download A's resume", describe(anonDownload));

    const anonSign = await anonClient.storage.from(BUCKET).createSignedUrl(aPath, 60);
    check(!!anonSign.error, "anon key CANNOT sign a URL for A's resume", describe(anonSign));

    const anonList = await anonClient.storage.from(BUCKET).list(A.authId);
    check(
      !!anonList.error || (anonList.data ?? []).length === 0,
      "anon key CANNOT list A's folder",
      JSON.stringify(anonList.data),
    );

    // (b) user B, holding a genuine session of their own.
    const bDownload = await bClient.storage.from(BUCKET).download(aPath);
    check(!!bDownload.error, "user B's real JWT CANNOT download A's resume", describe(bDownload));

    const bSign = await bClient.storage.from(BUCKET).createSignedUrl(aPath, 60);
    check(!!bSign.error, "user B CANNOT sign a URL for A's resume", describe(bSign));

    const bList = await bClient.storage.from(BUCKET).list(A.authId);
    check(
      !!bList.error || (bList.data ?? []).length === 0,
      "user B CANNOT list A's folder",
      JSON.stringify(bList.data),
    );

    const bWrite = await bClient.storage
      .from(BUCKET)
      .upload(`${A.authId}/planted.pdf`, samplePdf(), { upsert: true });
    check(!!bWrite.error, "user B CANNOT write into A's folder", describe(bWrite));

    const bDelete = await bClient.storage.from(BUCKET).remove([aPath]);
    const stillThere = await objectsInFolder(sql, A.authId);
    check(
      stillThere.includes(aPath),
      "user B CANNOT delete A's resume",
      `${describe(bDelete)}; folder now ${stillThere.join(", ")}`,
    );

    // (c) A, who must actually be able to read their own.
    const aDownload = await aClient.storage.from(BUCKET).download(aPath);
    check(!aDownload.error && !!aDownload.data, "A CAN download A's own resume", describe(aDownload));

    const aSign = await aClient.storage.from(BUCKET).createSignedUrl(aPath, 60);
    check(!aSign.error && !!aSign.data?.signedUrl, "A CAN sign a URL for it", describe(aSign));

    // And the signed URL is a real, working, expiring link.
    if (aSign.data?.signedUrl) {
      const res = await fetch(aSign.data.signedUrl);
      check(res.ok, "the signed URL serves the file", `HTTP ${res.status}`);
    }

    // -----------------------------------------------------------------
    console.log("\n§4  THE ERASURE PROOF — delete A, and the file goes with it");
    const receipt = await deleteAccount(A.accountId, A.authId, resumeStorage(aClient));
    console.log(`        receipt: ${JSON.stringify(receipt)}`);
    check(receipt.storageObjects === 1, "the receipt says one object was removed");
    check(receipt.resumes === 1, "and one resumes row went with the account");

    // Asked as postgres, of the catalogue, rather than of the API that just
    // claimed success — the same rule delete-check follows for the row counts.
    const afterObjects = await objectsInFolder(sql, A.authId);
    check(afterObjects.length === 0, "A's storage folder is EMPTY", afterObjects.join(", "));

    const [rowsLeft] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM resumes WHERE user_id = ${A.accountId}
    `;
    check(rowsLeft.n === 0, "A's resumes row is gone");

    const [accountLeft] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM public.users WHERE id = ${A.accountId}
    `;
    check(accountLeft.n === 0, "A's account is gone — no orphan row, no orphan file");

    // B is untouched by any of it.
    const [bLeft] = await sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM public.users WHERE id = ${B.accountId}
    `;
    check(bLeft.n === 1, "B survived A's deletion entirely");
  } finally {
    const { sql, BUCKET: bucket } = await loadLib();
    await scrub(sql, bucket);
    const leftover: string[] = [];
    for (const who of THROWAWAYS) {
      const objects = await objectsInFolder(sql, who.authId);
      if (objects.length) leftover.push(`storage ${objects.join(", ")}`);
      const [row] = await sql<{ n: number }[]>`
        SELECT count(*)::int AS n FROM public.users WHERE id = ${who.accountId}
      `;
      if (row.n > 0) leftover.push(`public.users ${who.accountId}`);
    }
    console.log(
      leftover.length === 0
        ? "\ncleanup: no throwaway rows or objects remain"
        : `\ncleanup: FAILED, still present — ${leftover.join("; ")}`,
    );
    if (leftover.length > 0) failures += 1;
    await sql.end();
  }

  console.log(failures === 0 ? "\nAll checks passed.\n" : `\n${failures} check(s) FAILED.\n`);
  process.exit(failures === 0 ? 0 : 1);
}

/** The interesting half of a Supabase result, for a failure line. */
function describe(result: { error?: unknown; data?: unknown }): string {
  const error = result.error as { message?: string; status?: number } | null | undefined;
  if (error) return `error ${error.status ?? ""} ${error.message ?? JSON.stringify(error)}`.trim();
  return `no error; data = ${result.data ? "present" : "null"}`;
}

main().catch((err) => {
  console.error("\nresume-check failed to run:", err);
  process.exit(1);
});
